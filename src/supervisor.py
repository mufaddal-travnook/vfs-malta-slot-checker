"""Self-healing supervisor for the VFS slot checker (EC2 / hourly cron).

One invocation runs the slot check for EVERY route listed in [vfs-url] (one
cron => all URLs). For each route, in order, the supervisor:

  1. Launches a fresh, real Chrome (CDP) that IT owns — a NEW Chrome per route.
  2. Points the bot at that Chrome and runs the full slot-check flow.
  3. KILLS Chrome on the way out — success or failure — so no zombie processes
     accumulate (and the next route always starts clean).
  4. On a retryable failure (Cloudflare not passed / Sign In disabled / page
     closed / dashboard not reached / CDP connect failure / any unexpected
     error), it tears everything down and tries again with a brand-new browser,
     up to MAX_ATTEMPTS times with a short backoff.
  5. Sends that route's slot report to Telegram (done by the bot), and a Telegram
     alert if all its attempts fail. One route failing does not stop the others.

So a single cron tick produces: url1 -> msg, url2 -> msg, url3 -> msg ...

Run directly:   python -m src.supervisor          # all routes
                python -m src.supervisor -sc AE -dc MT   # one route only
On EC2 it's invoked under xvfb-run by run_ec2.sh (see that script).
"""

import argparse
import logging
import sys
import time

from src.main import initialize_logger
from src.utils import telegram, telegram_message
from src.utils.chrome_launcher import ChromeProcess
from src.utils.config_reader import (
    get_config_section,
    get_config_value,
    initialize_config,
    set_config_value,
)
from src.vfs_bot.vfs_bot import GeoBlockedError, RetryableError
from src.vfs_bot.vfs_bot_factory import UnsupportedCountryError, get_vfs_bot

# Retry policy. The EC2 box is slow, so individual UI actions occasionally time
# out; relaunching a fresh browser usually succeeds. 3 attempts balances
# resilience against total run time (each attempt can take a few minutes).
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 15
CDP_PORT = 9222


def _vfs_url(source: str, dest: str) -> str:
    return get_config_value("vfs-url", f"{source.upper()}-{dest.upper()}")


def run_once_with_fresh_browser(source: str, dest: str) -> bool:
    """
    One attempt: launch a fresh Chrome, run the flow, always kill Chrome after.

    Returns True if the slot check completed. Raises RetryableError (or other
    exceptions) on failure — the caller decides whether to retry.
    """
    url = _vfs_url(source, dest)
    # Optional proxy (e.g. an SSH reverse tunnel to your home PC) so VFS sees a
    # residential IP instead of the EC2 datacenter IP. Off unless configured.
    proxy = get_config_value("browser", "proxy", "") or None
    chrome = ChromeProcess(port=CDP_PORT, url=url, proxy=proxy)
    try:
        chrome.start()
        # Point the bot at the Chrome we just launched.
        set_config_value("browser", "cdp_url", chrome.cdp_url)
        bot = get_vfs_bot(source, dest)
        return bot.run()
    finally:
        # Guaranteed cleanup — this is the anti-zombie guarantee.
        chrome.close()


def run(source: str = "AE", dest: str = "MT") -> bool:
    """
    Runs up to MAX_ATTEMPTS attempts with a fresh browser each time.

    Returns True on success, False if all attempts were exhausted.
    """
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logging.info(f"=== Attempt {attempt}/{MAX_ATTEMPTS} ===")
        try:
            if run_once_with_fresh_browser(source, dest):
                logging.info(f"Success on attempt {attempt}.")
                return True
            # run() returning False shouldn't normally happen (it raises on
            # failure), but treat it as a failed attempt to be safe.
            last_error = "Flow returned without completing."
        except GeoBlockedError as e:
            # Non-retryable: VFS geo-blocked the IP (403203). Retrying uses the
            # same IP and fails identically — stop now, no further attempts.
            logging.error(f"Geo-blocked for {source}-{dest}: {e}")
            _alert_failure(source, dest, f"Geo-blocked (403203): {e}", attempts=attempt)
            return False
        except UnsupportedCountryError as e:
            # Not retryable — a config problem, not a transient failure.
            logging.error(f"Unsupported route {source}-{dest}: {e}")
            _alert_failure(source, dest, str(e), attempts=attempt)
            return False
        except RetryableError as e:
            last_error = f"{type(e).__name__}: {e}"
            logging.warning(f"Attempt {attempt} failed (retryable): {last_error}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logging.exception(f"Attempt {attempt} failed (unexpected): {last_error}")

        if attempt < MAX_ATTEMPTS:
            logging.info(f"Backing off {BACKOFF_SECONDS}s before next attempt...")
            time.sleep(BACKOFF_SECONDS)

    logging.error(f"All {MAX_ATTEMPTS} attempts failed. Last error: {last_error}")
    _alert_failure(source, dest, last_error, attempts=MAX_ATTEMPTS)
    return False


def _all_routes() -> list:
    """
    Returns every route configured in [vfs-url] as (source, dest) tuples.

    Each key in the section is a '<SOURCE>-<DEST>' route (e.g. 'AE-MT'), so we
    just split the keys. Order follows the config file.
    """
    section = get_config_section("vfs-url")
    routes = []
    for key in section:
        parts = key.upper().split("-")
        if len(parts) == 2 and parts[0] and parts[1]:
            routes.append((parts[0], parts[1]))
        else:
            logging.warning(f"Skipping malformed [vfs-url] key '{key}' (want SRC-DEST).")
    return routes


def run_all_routes() -> bool:
    """
    Runs the slot check for EVERY route in [vfs-url], one after another.

    Each route gets its OWN fresh Chrome (launched and killed per attempt inside
    run() -> run_once_with_fresh_browser), and its own Telegram report is sent by
    the bot at the end of its run. One route failing does NOT stop the others —
    each is independent, with its own retries and its own failure alert.

    Returns True only if ALL routes succeeded.
    """
    routes = _all_routes()
    if not routes:
        logging.error("No routes configured in [vfs-url] — nothing to run.")
        return False

    logging.info(
        f"Running {len(routes)} route(s): "
        + ", ".join(f"{s}-{d}" for s, d in routes)
    )

    all_ok = True
    for idx, (source, dest) in enumerate(routes, start=1):
        logging.info(f"########## Route {idx}/{len(routes)}: {source}-{dest} ##########")
        try:
            # A fresh Chrome is opened and closed for this route inside run().
            ok = run(source, dest)
        except Exception as e:
            # run() handles its own errors, but guard so one route can never
            # abort the whole loop.
            logging.exception(f"Route {source}-{dest} crashed unexpectedly: {e}")
            ok = False
        all_ok = all_ok and ok
        logging.info(f"Route {source}-{dest} {'succeeded' if ok else 'FAILED'}.")

    logging.info(f"All routes done. Overall {'OK' if all_ok else 'with failures'}.")
    return all_ok


def _alert_failure(source: str, dest: str, error: str, attempts: int) -> None:
    """Sends a Telegram alert that the run failed (best-effort).

    Includes the account (email) that was used for this hour, so you know which
    credential failed. Message layout lives in src/utils/telegram_message.py.
    """
    login_url = _vfs_url(source, dest) or ""
    # The account in use this hour — the same one the bot tried (rotation is by
    # clock hour, so recomputing here gives the same email).
    from datetime import datetime
    from src.utils import credentials
    email, _ = credentials.get_credential(datetime.now().hour)
    msg = telegram_message.failure_alert(
        source, dest, error, attempts, login_url, email or ""
    )
    logging.error(msg)
    if telegram.is_configured():
        telegram.send_message(msg)
    else:
        logging.warning("Telegram not configured — failure alert logged only.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Self-healing supervisor for the VFS slot checker."
    )
    # By default, run EVERY route in [vfs-url], each in its own fresh Chrome,
    # pushing a Telegram report per route. Pass -sc/-dc to run just one route.
    parser.add_argument(
        "-sc", "--source-country-code", default=None,
        help="Run only this source country (with -dc). Omit to run all routes.",
    )
    parser.add_argument(
        "-dc", "--destination-country-code", default=None,
        help="Run only this destination country (with -sc). Omit to run all routes.",
    )
    args = parser.parse_args()

    initialize_config()
    initialize_logger()

    if args.source_country_code and args.destination_country_code:
        ok = run(args.source_country_code, args.destination_country_code)
    else:
        ok = run_all_routes()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
