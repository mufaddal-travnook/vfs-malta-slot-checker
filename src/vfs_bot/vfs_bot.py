import logging
import os
from abc import ABC
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.utils.config_reader import get_config_value
from src.utils.route_schema import get_route_schema


def _browser_activity_enabled() -> bool:
    """
    Whether to attach verbose Playwright page hooks (navigations, network
    requests/responses, console messages, page errors). Controlled by the
    BROWSER_ACTIVITY_LOG env var or [logging] browser_activity in config.
    """
    raw = (
        os.environ.get("BROWSER_ACTIVITY_LOG")
        or get_config_value("logging", "browser_activity", "False")
    )
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

SCREENSHOT_DIR = "screenshots"

# When False, the per-step `_take_screenshot` calls are no-ops; only the single
# final screenshot (taken at the end of run()) is written. Flip to True for
# step-by-step debugging.
SCREENSHOTS_ENABLED = False

# How many times to RELOAD the login page to retry a stuck Cloudflare Turnstile
# before giving up (a reload re-runs the challenge and often unsticks it). This
# is a cheap inner retry within one browser; the supervisor's browser relaunch
# is the outer retry.
TURNSTILE_REFRESH_ATTEMPTS = 2

USERNAME_SELECTOR = (
    "input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']"
)
PASSWORD_SELECTOR = (
    "input[formcontrolname='password'], #mat-input-1, input[type='password']"
)


class LoginError(Exception):
    """Exception raised when login fails."""


class RetryableError(Exception):
    """
    Base for failures the supervisor should retry by relaunching a fresh browser.

    The hourly EC2 run treats every failure mode below as "tear down Chrome and
    try the whole flow again", since a fresh browser is the most reliable way to
    get a clean Cloudflare pass / recover from a dead page.
    """


class CdpConnectError(RetryableError):
    """Could not connect to Chrome over CDP (Chrome not up / port not ready)."""


class LoginFormNotReadyError(RetryableError):
    """The login form never appeared (Cloudflare spinner / 403 / slow load)."""


class SignInDisabledError(RetryableError):
    """The Sign In button stayed disabled — Cloudflare 'Verify you are human'
    was not passed, so login can't proceed."""


class DashboardNotReachedError(RetryableError):
    """Sign In was clicked but the dashboard never loaded (bad creds, captcha,
    or a slow/blocked redirect)."""


class SlotCheckError(RetryableError):
    """Reached the appointment step but couldn't complete the slot check."""


class VfsBot(ABC):
    """
    Slot-check bot for the VFS Malta portal.

    This is the trimmed, production slice of the original schema-driven VFS bot:
    it only reaches Step 1 (Appointment Details), reads the 'Earliest available
    slot' banner for each configured combination, and reports the results via
    Telegram. The booking/OTP/payment machinery is intentionally absent — this
    project never books anything.
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None
        self.schema = {}

    def run(self) -> bool:
        """
        Connects to a browser over CDP, navigates to the VFS login URL, logs in,
        starts a new booking and runs the slot-check flow.

        On the EC2/supervisor path, Chrome is launched and killed by the
        supervisor (see src/supervisor.py); this method only attaches to it.

        Returns:
            bool: True if the slot check completed and a report was produced.

        Raises:
            RetryableError (and subclasses): on any failure the supervisor should
            retry with a fresh browser — CDP connect failure, login form never
            ready, Sign In disabled (Cloudflare), dashboard not reached, or a
            slot-check failure.
        """
        logging.info(
            f"Starting VFS Slot Checker for "
            f"{self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        # Load the per-route flow schema (the combinations to check).
        self.schema = get_route_schema(
            self.source_country_code, self.destination_country_code
        )

        browser_type = get_config_value("browser", "type", "chromium")
        headless_mode = get_config_value("browser", "headless", "True")
        url_key = self.source_country_code + "-" + self.destination_country_code
        vfs_url = get_config_value("vfs-url", url_key)
        if not vfs_url:
            logging.error(
                f"No VFS URL configured for '{url_key}'. Add it to config/vfs_urls.ini"
            )
            return False

        email_id = get_config_value("vfs-credential", "email")
        password = get_config_value("vfs-credential", "password")

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        with sync_playwright() as p:
            cdp_url = get_config_value("browser", "cdp_url")
            if cdp_url:
                # Attach to an existing Chrome launched with --remote-debugging-port.
                # On EC2 the supervisor launches/kills that Chrome; locally you run
                # run.ps1. Either way we only attach here.
                logging.info(f"Connecting to Chrome via CDP: {cdp_url}")
                try:
                    browser = p.chromium.connect_over_cdp(cdp_url)
                except Exception as e:
                    raise CdpConnectError(
                        f"Could not connect to Chrome at {cdp_url}: {e}"
                    ) from e
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
                # Drop only VFS's stale login/session cookies while KEEPING
                # Cloudflare's clearance (cf_clearance / __cf*). The persistent
                # profile keeps cf_clearance so we don't get a fresh 403, but its
                # old VFS session would otherwise land us on "Session Expired" —
                # clearing it makes each run log in fresh.
                VfsBot._clear_site_session(context)
                # Reuse Chrome's startup tab if present, else open one. Either way
                # we (re)navigate below so the page loads with clearance kept but
                # the VFS session gone.
                page = context.pages[0] if context.pages else context.new_page()
            else:
                # Launch our own browser (the prod path — headless on a server).
                is_headless = headless_mode in ("True", "true")
                launch_args = {}
                if browser_type == "chromium":
                    launch_args["args"] = [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ]
                browser = getattr(p, browser_type).launch(
                    headless=is_headless, **launch_args
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()
                stealth_sync(page)

            VfsBot._attach_activity_logging(page)

            # Always (re)load the login URL fresh — we just cleared the VFS
            # session, so this loads the clean login page with Cloudflare
            # clearance still intact.
            logging.info(f"Navigating to {vfs_url}")
            page.goto(vfs_url, timeout=60000, wait_until="domcontentloaded")

            self.pre_login_steps(page)

            try:
                self.login(page, email_id, password)
            except RetryableError:
                # A classified, expected failure — screenshot the end state and
                # let it bubble up so the supervisor retries with a fresh browser.
                self._take_final_screenshot(page, "final")
                raise
            except Exception as e:
                # Anything unclassified (incl. Playwright TargetClosedError when
                # the page/browser died mid-flow) is also retryable.
                self._take_final_screenshot(page, "final")
                raise RetryableError(f"Unexpected flow error: {e}") from e

            # Single final screenshot capturing the successful end state.
            self._take_final_screenshot(page, "final")
            logging.info("Slot check complete. Run finished.")
            return True

    # ------------------------------------------------------------------ #
    # Flow steps                                                          #
    # ------------------------------------------------------------------ #

    def pre_login_steps(self, page) -> None:
        """
        Accept ALL cookies on the OneTrust consent banner if present.

        We deliberately ACCEPT (never reject) — both because that's the desired
        behaviour and because the banner overlays the bottom of the page and
        intercepts pointer/focus events, so it must be cleared before we touch
        the login fields (otherwise filling the email field hangs).
        """
        # OneTrust's accept-all button has a stable id — try it first.
        try:
            ot = page.locator("#onetrust-accept-btn-handler").first
            if ot.count() > 0 and ot.is_visible():
                ot.click(timeout=4000)
                logging.info("Accepted all cookies (OneTrust accept-all).")
                page.wait_for_timeout(800)
                return
        except Exception:
            pass
        # Fallback: accept-only button labels (NO reject/close, so we never
        # accidentally reject cookies).
        for label in ["Accept Cookies", "Accept All Cookies", "Accept All", "Accept"]:
            try:
                btn = page.get_by_role("button", name=label).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=4000)
                    logging.info(f"Accepted all cookies via '{label}'.")
                    page.wait_for_timeout(800)
                    return
            except Exception:
                continue
        logging.debug("No cookie banner found, skipping")

    # Cookie domains to PRESERVE across runs — Cloudflare's clearance lives here.
    # Everything else on the VFS domains (the login/auth session) is cleared so
    # each run starts logged-out but still trusted by Cloudflare.
    _KEEP_COOKIE_NAMES = ("cf_clearance",)
    _KEEP_COOKIE_PREFIXES = ("__cf", "__cflb", "cf_")

    @staticmethod
    def _clear_site_session(context) -> None:
        """
        Clears VFS's stale login/session cookies while KEEPING Cloudflare's
        clearance cookies (cf_clearance / __cf*).

        Why: we run on a persistent Chrome profile so Cloudflare's clearance
        survives between runs (avoiding a fresh 403 on a datacenter IP). But that
        same profile would carry an expired VFS *login* session, landing us on
        "Session Expired or Invalid". So we surgically drop the VFS cookies and
        re-keep the Cloudflare ones by re-adding them after a full clear.
        """
        try:
            all_cookies = context.cookies()
        except Exception as e:
            logging.warning(f"Could not read cookies to clear session: {e}")
            return

        def _is_cloudflare(c) -> bool:
            name = c.get("name", "")
            return name in VfsBot._KEEP_COOKIE_NAMES or any(
                name.startswith(p) for p in VfsBot._KEEP_COOKIE_PREFIXES
            )

        keep = [c for c in all_cookies if _is_cloudflare(c)]
        dropped = len(all_cookies) - len(keep)

        try:
            context.clear_cookies()  # nukes everything...
            if keep:
                context.add_cookies(keep)  # ...then restore Cloudflare's only
            logging.info(
                f"Cleared {dropped} VFS session cookie(s); kept {len(keep)} "
                f"Cloudflare cookie(s)."
            )
        except Exception as e:
            logging.warning(f"Failed to selectively clear cookies: {e}")

    @staticmethod
    def _wait_for_signin_enabled(page, sign_in, timeout_ms: int = 60000) -> bool:
        """
        Polls until the Sign In button becomes enabled (Cloudflare Turnstile
        auto-solved) or the timeout elapses.

        Logs progress periodically and reports when the Turnstile token appears,
        so a stuck challenge is visible in the logs rather than a silent wait.
        Returns True if Sign In became enabled.
        """
        step_ms = 2000
        waited = 0
        token_seen = False
        while waited < timeout_ms:
            try:
                if sign_in.is_enabled():
                    logging.info(f"Sign In enabled after {waited/1000:.0f}s.")
                    return True
            except Exception:
                pass

            # Surface when the Turnstile token populates (challenge solved) even
            # if the button takes another moment to flip enabled.
            if not token_seen:
                try:
                    val = page.evaluate(
                        "() => { const i = document.querySelector(\"input[name='cf-turnstile-response']\");"
                        " return i ? i.value : ''; }"
                    )
                    if val:
                        token_seen = True
                        logging.info("Turnstile token populated (challenge passed).")
                except Exception:
                    pass

            page.wait_for_timeout(step_ms)
            waited += step_ms
            if waited % 10000 == 0:
                logging.info(f"Waiting for Cloudflare to enable Sign In... ({waited/1000:.0f}s)")
        return False

    @staticmethod
    def _fill_field(page, locator, value: str) -> None:
        """
        Sets an input's value robustly, immune to overlays and Xvfb hangs.

        Tries Playwright fill() first (bounded timeout). If that's blocked (an
        overlay intercepting actionability), falls back to setting the value via
        JS and dispatching the 'input'/'change' events Angular listens for, so
        the form model updates even without a real focus/click.
        """
        try:
            locator.fill(value, timeout=8000)
            return
        except Exception as e:
            logging.info(f"fill() blocked ({e}); using JS value-set fallback.")
        try:
            locator.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""",
                value,
            )
        except Exception as e:
            raise RetryableError(f"Could not fill a login field: {e}") from e

    def login(self, page, email_id: str, password: str) -> None:
        """
        Fills the login form, signs in, and — once on the dashboard — clicks
        Start New Booking and runs the slot check.
        """
        # Wait for login form to be ready (VFS can take a while behind Cloudflare).
        try:
            page.wait_for_selector(USERNAME_SELECTOR, timeout=120000)
        except Exception as e:
            raise LoginFormNotReadyError(
                "Login form never appeared within 120s (Cloudflare spinner / 403 / "
                f"slow load): {e}"
            ) from e
        logging.info("Login form loaded")

        # Dismiss the cookie banner (it overlays the form and blocks fields).
        self.pre_login_steps(page)

        # --- Turnstile FIRST -------------------------------------------------
        # Check the Cloudflare 'Verify you are human' challenge BEFORE touching
        # the credentials — no point filling a form we can't submit. The Sign In
        # button is enabled only once Turnstile passes, so that's our signal.
        #
        # Inner retry: a stuck Turnstile is often unstuck by a page reload (it
        # re-runs the challenge with more signals), which is far cheaper than
        # relaunching the whole browser. Try the wait, and on failure reload and
        # try again a couple of times before giving up to the supervisor.
        sign_in = page.get_by_role("button", name="Sign In").first
        passed = False
        for turn_attempt in range(1, TURNSTILE_REFRESH_ATTEMPTS + 2):  # 1 + N reloads
            try:
                sign_in.wait_for(state="visible", timeout=10000)
            except Exception as e:
                raise SignInDisabledError(f"Sign In button not visible: {e}") from e

            logging.info(
                f"Waiting for Cloudflare Turnstile to pass "
                f"(try {turn_attempt}/{TURNSTILE_REFRESH_ATTEMPTS + 1})..."
            )
            if VfsBot._wait_for_signin_enabled(page, sign_in, timeout_ms=45000):
                passed = True
                break

            # Not passed — reload and re-run the challenge, unless out of tries.
            if turn_attempt <= TURNSTILE_REFRESH_ATTEMPTS:
                logging.info("Turnstile not passed — refreshing the page to retry.")
                VfsBot._take_final_screenshot(page, f"turnstile_fail_{turn_attempt}")
                try:
                    page.reload(timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    logging.warning(f"Reload failed: {e}")
                try:
                    page.wait_for_selector(USERNAME_SELECTOR, timeout=120000)
                except Exception as e:
                    raise LoginFormNotReadyError(
                        f"Login form did not reappear after reload: {e}"
                    ) from e
                self.pre_login_steps(page)
                sign_in = page.get_by_role("button", name="Sign In").first

        if not passed:
            VfsBot._take_final_screenshot(page, "turnstile_failed_final")
            raise SignInDisabledError(
                "Cloudflare 'Verify you are human' did not pass after "
                f"{TURNSTILE_REFRESH_ATTEMPTS} refresh(es) — Sign In stayed disabled."
            )

        # --- Turnstile passed: now fill credentials --------------------------
        email_input = page.locator(USERNAME_SELECTOR).first
        password_input = page.locator(PASSWORD_SELECTOR).first

        logging.info("Turnstile passed. Filling email field...")
        VfsBot._fill_field(page, email_input, email_id)
        page.wait_for_timeout(500)
        logging.info("Email entered; filling password field...")

        VfsBot._fill_field(page, password_input, password)
        page.wait_for_timeout(800)
        logging.info("Password entered; about to click Sign In...")
        VfsBot._take_final_screenshot(page, "before_signin")

        # Re-confirm Sign In is still enabled after filling (Angular re-validates).
        if not sign_in.is_enabled():
            if not VfsBot._wait_for_signin_enabled(page, sign_in, timeout_ms=15000):
                raise SignInDisabledError(
                    "Sign In became disabled again after filling credentials."
                )

        try:
            sign_in.click(timeout=10000)
        except Exception:
            # An overlay may be intercepting the pointer event under Xvfb — retry
            # with force (skips the actionability hit-test).
            logging.info("Sign In normal click intercepted; retrying with force.")
            sign_in.click(force=True, timeout=10000)
        logging.info("Clicked Sign In")
        VfsBot._take_final_screenshot(page, "after_signin")
        # A Cloudflare captcha dialog often appears right after Sign In and blocks
        # the redirect to the dashboard, so watch for it during this wait.
        VfsBot._wait_with_captcha_check(page, 6000)

        try:
            page.wait_for_url("**/dashboard", timeout=60000)
        except Exception as e:
            raise DashboardNotReachedError(
                f"Did not reach /dashboard after Sign In (current URL: {page.url}): {e}"
            ) from e

        logging.info(f"Reached dashboard: {page.url}")
        page.wait_for_timeout(2000)
        self._start_new_booking(page)
        self._check_slots(page)

    @staticmethod
    def _start_new_booking(page) -> None:
        """Clicks the 'Start New Booking' button on the VFS dashboard."""
        try:
            page.wait_for_timeout(2000)
            # VFS renders two copies of this button (responsive: one for mobile,
            # one for desktop) — one is CSS-hidden at any given viewport. Target
            # the <button> (not its inner <span>) and keep only the visible copy,
            # otherwise the click lands on the hidden element and times out.
            booking_button = (
                page.locator("button:has-text('Start New Booking')")
                .filter(visible=True)
                .first
            )
            booking_button.scroll_into_view_if_needed(timeout=10000)
            booking_button.click(timeout=15000)
            logging.info("Clicked Start New Booking")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "06_start_new_booking")
            logging.info(f"Start New Booking opened. URL: {page.url}")
        except Exception as e:
            logging.warning(f"Start New Booking failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_start_new_booking")

    # ------------------------------------------------------------------ #
    # Slot check                                                         #
    # ------------------------------------------------------------------ #

    def _check_slots(self, page) -> None:
        """
        Slot-check flow (no booking): on the Appointment Details step, run through
        each configured combination (centre / category / sub-category), read the
        earliest-slot banner for each, then send all results in one Telegram
        message. Combinations come from the route schema's `slot_check.combinations`.
        """
        combos = self.schema.get("slot_check", {}).get("combinations", [])
        if not combos:
            logging.warning("Slot-check mode but no combinations configured.")
            return

        try:
            page.wait_for_url("**/application-detail", timeout=30000)
        except Exception as e:
            raise SlotCheckError(
                f"Did not reach the Appointment Details page; cannot check slots: {e}"
            ) from e

        VfsBot._wait_for_loader(page)
        page.wait_for_timeout(1000)

        results = []
        prev = {}  # the centre/category/sub-category selected for the previous combo
        for combo in combos:
            label = combo.get("label") or " / ".join(
                filter(None, [combo.get("centre"), combo.get("category"), combo.get("sub_category")])
            )
            logging.info(f"Checking slot for: {label}")

            # The dropdowns cascade (centre -> category -> sub-category), so they
            # are selected in order. Skip any level whose value is unchanged from
            # the previous combination — but once a parent level changes it resets
            # its children, so every level below a change must be re-selected too.
            selections = [
                ("centerCode", "centre", combo.get("centre")),
                ("selectedSubvisaCategory", "category", combo.get("category")),
                ("visaCategoryCode", "sub_category", combo.get("sub_category")),
            ]
            ok = True
            cascade_changed = False
            for control, key, value in selections:
                if not value:
                    continue
                # Re-select if this level's value differs OR a parent changed
                # (which reset this dependent dropdown).
                if not cascade_changed and prev.get(key) == value:
                    logging.info(f"  (unchanged) {key} = '{value}' — skipping re-select")
                    continue
                cascade_changed = True
                if not VfsBot._select_mat_dropdown(page, control, value):
                    ok = False
                    break

            prev = {
                "centre": combo.get("centre"),
                "category": combo.get("category"),
                "sub_category": combo.get("sub_category"),
            }

            if not ok:
                message = "Could not select this combination (option not found)."
            else:
                message = VfsBot._read_slot_message(page) or "No slot message shown (no availability?)."

            logging.info(f"  -> {message}")
            results.append((label, message))
            VfsBot._take_screenshot(page, f"slot_{len(results)}")
            page.wait_for_timeout(1000)

        VfsBot._send_slot_report(results)

    @staticmethod
    def _read_slot_message(page, timeout: int = 12000) -> str:
        """
        Returns the 'Earliest available slot ...' banner text on the Appointment
        Details step, or "" if none is shown (e.g. no availability for the chosen
        combination).
        """
        try:
            VfsBot._wait_for_loader(page)
            slot = page.get_by_text("Earliest available slot", exact=False).first
            slot.wait_for(timeout=timeout)
            return slot.inner_text().strip()
        except Exception:
            return ""

    @staticmethod
    def _send_slot_report(results: list) -> None:
        """Formats the collected (label, message) slot results and sends them to Telegram."""
        from src.utils import telegram

        lines = ["VFS Malta — Earliest appointment slots", ""]
        for label, message in results:
            lines.append(f"• {label}")
            lines.append(f"  {message}")
            lines.append("")
        report = "\n".join(lines).strip()

        logging.info("Slot report:\n" + report)
        if telegram.is_configured():
            telegram.send_message(report)
        else:
            logging.warning(
                "Telegram not configured — slot report logged only. "
                "Set [telegram] bot_token and chat_id in config.ini to receive it."
            )

    # ------------------------------------------------------------------ #
    # Shared UI helpers (loader / captcha / wait dialogs / dropdowns)    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _wait_for_loader(page, timeout: int = 30000) -> None:
        """
        Waits for VFS's ngx-ui-loader overlay to clear. This full-screen spinner
        intercepts pointer events, so clicking while it is up times out. Returns
        immediately if no loader is present.

        Also clears any Cloudflare 'Verify Captcha' dialog and the VFS 'please
        wait before continuing' reminder first — either can pop up at any step
        and blocks the form until dismissed.
        """
        VfsBot._dismiss_captcha(page)
        VfsBot._dismiss_wait_dialog(page)
        try:
            page.locator("ngx-ui-loader .ngx-overlay.loading-foreground").wait_for(
                state="hidden", timeout=timeout
            )
        except Exception:
            pass  # loader absent or already cleared

    @staticmethod
    def _dismiss_wait_dialog(page) -> None:
        """
        Dismisses VFS's intermittent reminder dialogs that block a step — e.g.
        'Please wait for some time before saving and continuing'. These are
        mat-dialogs whose only action is a 'Continue' (or 'OK') button; clicking
        it lets the flow proceed. Silent no-op when none is present.
        """
        try:
            dialog = page.locator("mat-dialog-container, .mat-mdc-dialog-container")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
            text = (dialog.first.inner_text() or "").lower()
        except Exception:
            return

        # Only handle the informational 'wait/reminder' dialogs here — leave the
        # Cloudflare captcha dialog to its dedicated handler.
        if "captcha" in text:
            return
        if not any(k in text for k in ("wait", "reminder", "received", "please")):
            return

        for label in ("Continue", "OK", "Ok", "Close"):
            try:
                btn = dialog.first.get_by_role("button", name=label).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=5000)
                    logging.info(f"Dismissed VFS reminder dialog via '{label}'.")
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    @staticmethod
    def _dismiss_captcha(page) -> None:
        """
        Dismisses the Cloudflare 'Verify Captcha' dialog (`app-cloudflare-dialog`)
        if it is showing. The Turnstile widget auto-solves, so we just need to
        click its 'Submit' button. Returns immediately (and silently) when no
        dialog is present.
        """
        try:
            dialog = page.locator("app-cloudflare-dialog")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
        except Exception:
            return

        VfsBot._do_dismiss_captcha(page)

    @staticmethod
    def _wait_with_captcha_check(page, total_ms: int, step_ms: int = 3000) -> None:
        """
        Sleeps for `total_ms`, checking for (and dismissing) the Cloudflare captcha
        dialog every `step_ms`. Use for long idle waits where the dialog could pop
        up while the bot is otherwise doing nothing.
        """
        elapsed = 0
        while elapsed < total_ms:
            page.wait_for_timeout(min(step_ms, total_ms - elapsed))
            elapsed += step_ms
            VfsBot._dismiss_captcha(page)

    @staticmethod
    def _do_dismiss_captcha(page) -> None:
        """
        Clears a confirmed-visible Cloudflare captcha dialog.

        The Turnstile widget shows 'Verifying...' for a few seconds and only
        populates its hidden `cf-turnstile-response` token once solved — clicking
        Submit before then does nothing. So we wait for that token to appear,
        then click Submit, and retry the whole cycle a few times if the dialog
        is still up (it can require more than one round).
        """
        dialog = page.locator("app-cloudflare-dialog")
        logging.info("Cloudflare 'Verify Captcha' dialog detected — handling it.")

        for attempt in range(1, 4):  # up to 3 Submit cycles
            VfsBot._wait_for_turnstile_token(page)
            try:
                submit = dialog.get_by_role("button", name="Submit").first
                submit.click(timeout=10000)
                logging.info(f"Clicked captcha 'Submit' (attempt {attempt})")
            except Exception as e:
                logging.warning(f"Could not click captcha 'Submit': {e}")
                VfsBot._take_screenshot(page, "ERROR_captcha")
                return

            # Did the dialog go away?
            try:
                dialog.first.wait_for(state="hidden", timeout=12000)
                logging.info("Captcha dialog cleared.")
                VfsBot._take_screenshot(page, "captcha_handled")
                return
            except Exception:
                # Still visible (e.g. token wasn't ready yet) — loop and retry.
                if not VfsBot._captcha_visible(page):
                    return  # raced away on its own
                logging.info(
                    f"Captcha still visible after Submit (attempt {attempt}); retrying..."
                )

        logging.warning(
            "Captcha dialog still visible after retries — it may need a manual solve."
        )
        VfsBot._take_screenshot(page, "ERROR_captcha_persist")

    @staticmethod
    def _captcha_visible(page) -> bool:
        """True if the Cloudflare captcha dialog is currently showing."""
        try:
            dialog = page.locator("app-cloudflare-dialog")
            return dialog.count() > 0 and dialog.first.is_visible()
        except Exception:
            return False

    @staticmethod
    def _wait_for_turnstile_token(page, timeout_ms: int = 15000) -> None:
        """
        Waits until the Turnstile hidden input (`cf-turnstile-response`) holds a
        non-empty token, meaning the challenge auto-solved. Falls back to a short
        fixed wait if the input can't be read (it lives in a closed shadow root on
        some pages, so its value isn't always queryable).
        """
        try:
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el && el.value && el.value.length > 0;
                }""",
                timeout=timeout_ms,
            )
            logging.debug("Turnstile token populated.")
        except Exception:
            # Token not observable (closed shadow DOM) — give it a moment anyway.
            page.wait_for_timeout(3000)

    @staticmethod
    def _select_mat_dropdown(page, control_name: str, value: str) -> bool:
        """
        Selects an option in an Angular Material dropdown (`mat-select`).

        Opens the dropdown identified by its `formcontrolname`, then clicks the
        option whose visible text contains `value` (case-insensitive substring).

        Returns:
            bool: True if the option was selected, False otherwise.
        """
        try:
            VfsBot._wait_for_loader(page)  # centre/category lists load behind a spinner
            trigger = page.locator(f"mat-select[formcontrolname='{control_name}']").first
            trigger.scroll_into_view_if_needed(timeout=10000)
            trigger.click(timeout=10000)
            page.wait_for_timeout(1000)  # wait for the options overlay to render
            page.get_by_role("option", name=value, exact=False).first.click(
                timeout=10000
            )
            logging.info(f"Selected '{value}' (dropdown: '{control_name}')")
            page.wait_for_timeout(1000)
            VfsBot._wait_for_loader(page)  # let the dependent dropdown reload
            return True
        except Exception as e:
            logging.warning(f"Could not select '{value}' for '{control_name}': {e}")
            VfsBot._take_screenshot(page, "ERROR_dropdown")
            return False

    # ------------------------------------------------------------------ #
    # Browser activity logging                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _attach_activity_logging(page) -> None:
        """
        Attaches verbose Playwright event listeners to `page` so that browser
        activity — frame navigations, network requests/responses, console
        messages and page/request errors — is logged at DEBUG level.

        No-op unless browser-activity logging is enabled (BROWSER_ACTIVITY_LOG
        env var or [logging] browser_activity in config). The network listeners
        are intentionally chatty, so they log at DEBUG: set the log level to
        DEBUG to actually see them.
        """
        if not _browser_activity_enabled():
            return

        log = logging.getLogger("browser")
        log.info("Browser-activity logging enabled (navigations, network, console).")

        def on_request(request):
            log.debug(f">> {request.method} {request.url}")

        def on_response(response):
            log.debug(f"<< {response.status} {response.url}")

        def on_request_failed(request):
            failure = getattr(request, "failure", None)
            log.warning(f"XX request failed: {request.method} {request.url} ({failure})")

        def on_console(msg):
            log.debug(f"[console:{msg.type}] {msg.text}")

        def on_page_error(error):
            log.warning(f"[page error] {error}")

        def on_frame_navigated(frame):
            # Only the main frame's navigations are interesting; iframes are noisy.
            if frame == page.main_frame:
                log.info(f"Navigated: {frame.url}")

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("framenavigated", on_frame_navigated)

    # ------------------------------------------------------------------ #
    # Screenshots                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _take_screenshot(page, name: str):
        """Per-step screenshot — a no-op unless SCREENSHOTS_ENABLED is True."""
        if not SCREENSHOTS_ENABLED:
            return
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _take_final_screenshot(page, name: str = "final"):
        """Always writes one screenshot (used at the end of the run)."""
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _write_screenshot(page, name: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{name}.png")
        # Playwright's screenshot() waits for fonts to load and the page to be
        # stable, which hangs on VFS's heavy page under Xvfb ('waiting for fonts
        # to load...'). Disable animations and DON'T let it block: try the normal
        # call briefly, then fall back to a CDP capture that skips all waits.
        try:
            page.screenshot(
                path=path, full_page=False, timeout=4000, animations="disabled"
            )
            logging.info(f"Screenshot saved: {path}")
            return
        except Exception as e:
            logging.debug(f"Normal screenshot blocked ({e}); trying CDP capture.")
        # Fallback: capture via the CDP protocol directly — this does not wait
        # for fonts/stability, so it always returns something we can look at.
        try:
            session = page.context.new_cdp_session(page)
            data = session.send(
                "Page.captureScreenshot", {"format": "png", "fromSurface": True}
            )
            import base64

            with open(path, "wb") as f:
                f.write(base64.b64decode(data["data"]))
            session.detach()
            logging.info(f"Screenshot saved (CDP): {path}")
        except Exception as e:
            logging.warning(f"Failed to take screenshot '{name}': {e}")
