"""Self-healing supervisor for the VFS Malta slot checker (EC2 / hourly cron).

Each invocation is ONE hourly run. The supervisor:

  1. Launches a fresh, real Chrome (CDP) that IT owns.
  2. Points the bot at that Chrome and runs the full slot-check flow.
  3. KILLS Chrome on the way out — success or failure — so no zombie processes
     accumulate across hourly runs.
  4. On a retryable failure (Cloudflare not passed / Sign In disabled / page
     closed / dashboard not reached / CDP connect failure / any unexpected
     error), it tears everything down and tries again with a brand-new browser,
     up to MAX_ATTEMPTS times with a short backoff.
  5. If every attempt fails, it sends a Telegram alert so you know that hour
     needs attention, and exits non-zero.

Run directly:   python -m src.supervisor
On EC2 it's invoked under xvfb-run by run_ec2.sh (see that script).
"""

import argparse
import logging
import sys
import time

from src.main import initialize_logger
from src.utils import telegram
from src.utils.chrome_launcher import ChromeProcess
from src.utils.config_reader import (
    get_config_value,
    initialize_config,
    set_config_value,
)
from src.vfs_bot.vfs_bot import RetryableError
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
    chrome = ChromeProcess(port=CDP_PORT, url=url)
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


def _alert_failure(source: str, dest: str, error: str, attempts: int) -> None:
    """Sends a Telegram alert that the hourly run failed (best-effort)."""
    msg = (
        f"⚠️ VFS slot check FAILED for {source.upper()}→{dest.upper()} "
        f"after {attempts} attempt(s).\n\nLast error:\n{error}"
    )
    logging.error(msg)
    if telegram.is_configured():
        telegram.send_message(msg)
    else:
        logging.warning("Telegram not configured — failure alert logged only.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Self-healing supervisor for the VFS Malta slot checker."
    )
    parser.add_argument("-sc", "--source-country-code", default="AE")
    parser.add_argument("-dc", "--destination-country-code", default="MT")
    args = parser.parse_args()

    initialize_config()
    initialize_logger()

    ok = run(args.source_country_code, args.destination_country_code)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
