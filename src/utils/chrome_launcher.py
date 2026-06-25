"""Launch and own a real Chrome process with remote debugging (CDP).

The slot-check flow attaches to a real, headed Chrome over CDP (Cloudflare blocks
headless/automation browsers). On EC2 the bot must OWN that Chrome's lifecycle so
that every hourly run launches a fresh browser and — crucially — KILLS it on the
way out, success or failure. Leaking Chrome processes on an hourly cron quickly
exhausts memory on a small instance.

`ChromeProcess` is a context manager:

    with ChromeProcess(port=9222, url=VFS_URL) as chrome:
        ...  # attach to chrome.cdp_url via Playwright
    # Chrome (and its whole process tree) is guaranteed dead here.

It is cross-platform (Windows for local dev, Linux for EC2) so the same code runs
in both places; on EC2 it runs under Xvfb (a virtual display) so "headed" Chrome
has somewhere to render.
"""

import logging
import os
import platform
import shutil
import subprocess
import time
import urllib.request


def _find_chrome() -> str:
    """
    Locates a Google Chrome / Chromium executable for the current OS.

    Returns the path, or raises FileNotFoundError if none is found. Honors the
    CHROME_PATH env var first so deployments can pin an exact binary.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = []
    system = platform.system()
    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux (EC2) / macOS
        # Prefer names on PATH, then common absolute locations.
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]

    for path in candidates:
        if path and os.path.exists(path):
            return path

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Google Chrome, or set CHROME_PATH to "
        "its executable."
    )


class ChromeProcess:
    """
    Launches a real Chrome with `--remote-debugging-port` and owns its lifecycle.

    Args:
        port: CDP port to expose (default 9222).
        url: initial URL to open (the VFS login page).
        profile_dir: dedicated user-data-dir; isolated from your normal Chrome and
            required for the debugging port to be honored. Defaults to a temp dir.
        startup_timeout_s: how long to wait for the CDP endpoint to come up.
    """

    def __init__(self, port=9222, url=None, profile_dir=None, startup_timeout_s=30,
                 proxy=None):
        self.port = port
        self.url = url
        self.startup_timeout_s = startup_timeout_s
        self.proxy = proxy  # e.g. "socks5://127.0.0.1:1080" — routes all traffic
        self._proc = None

        # Use a STABLE, persistent profile dir by default. This is deliberate:
        # Cloudflare's clearance cookie (cf_clearance) lives in the profile, and
        # keeping it across runs is what lets us avoid the 403 challenge wall on a
        # datacenter IP. We do NOT want a throwaway profile here (that triggers a
        # fresh 403 every run). The stale VFS *login* session that a persistent
        # profile would otherwise carry is cleared separately, at run start, by
        # the bot (see VfsBot._clear_site_session) — so we keep Cloudflare's
        # cookies but drop VFS's. Pass profile_dir=... to override.
        if profile_dir:
            self.profile_dir = profile_dir
        else:
            base = os.environ.get("TEMP") or "/tmp"
            self.profile_dir = os.path.join(base, f"vfs-chrome-profile-{port}")
        self._owns_profile = False  # persistent — never delete it on close

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "ChromeProcess":
        chrome = _find_chrome()
        args = [
            chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            # Needed when running as root / in many EC2 setups.
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        if self.proxy:
            # Route ALL of Chrome's traffic through this proxy (e.g. an SSH
            # reverse tunnel back to your home PC, so VFS sees your residential
            # IP instead of the EC2 datacenter IP). With socks5:// Chrome also
            # resolves DNS through the proxy, so the EC2 IP isn't leaked via DNS.
            args.append(f"--proxy-server={self.proxy}")
            logging.info(f"Chrome routing through proxy: {self.proxy}")
        if self.url:
            args.append(self.url)

        logging.info(f"Launching Chrome (CDP :{self.port}) — {chrome}")
        # Own a process group so we can kill the whole tree (renderers, GPU proc).
        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True  # setsid → own process group

        self._proc = subprocess.Popen(args, **popen_kwargs)
        self._wait_for_cdp()
        return self

    def _wait_for_cdp(self) -> None:
        """Polls the CDP /json/version endpoint until it answers or we time out."""
        deadline = time.time() + self.startup_timeout_s
        last_err = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited early (code {self._proc.returncode}) before CDP came up."
                )
            try:
                with urllib.request.urlopen(
                    f"{self.cdp_url}/json/version", timeout=2
                ) as resp:
                    if resp.status == 200:
                        logging.info(f"Chrome CDP ready on {self.cdp_url}")
                        return
            except Exception as e:
                last_err = e
            time.sleep(0.5)
        raise RuntimeError(
            f"Chrome CDP endpoint never came up on {self.cdp_url} "
            f"within {self.startup_timeout_s}s (last error: {last_err})"
        )

    def close(self) -> None:
        """
        Kills Chrome and its entire process tree. Safe to call more than once.
        """
        if not self._proc:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return

        logging.info("Closing Chrome (killing process tree)...")
        try:
            if platform.system() == "Windows":
                # /T kills the whole tree, /F forces it.
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                import signal

                # Kill the whole process group (we created one via setsid).
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                # Give it a moment, then SIGKILL anything left.
                try:
                    self._proc.wait(timeout=8)
                except Exception:
                    try:
                        os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except Exception as e:
            logging.warning(f"Error while killing Chrome: {e}")
        finally:
            self._proc = None
            # Remove the throwaway profile so no state survives to the next run
            # (and /tmp doesn't fill up over many hourly runs).
            if getattr(self, "_owns_profile", False):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            logging.info("Chrome closed.")

    def __enter__(self) -> "ChromeProcess":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
