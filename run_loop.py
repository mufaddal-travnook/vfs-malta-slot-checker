"""Run the VFS Malta slot checker on a fixed interval.

A simple, dependency-free scheduler for hosts where you'd rather not manage
cron. Each cycle runs the same flow as `python -m src.main` and then sleeps.

Usage:
    python run_loop.py                 # default: every 30 minutes
    python run_loop.py --interval 15   # every 15 minutes
    python run_loop.py --once          # run a single cycle and exit

Interval can also be set via the SLOT_CHECK_INTERVAL_MIN environment variable.
A crash in one cycle is logged and swallowed so the loop keeps running.
"""

import argparse
import logging
import os
import time

from src.main import initialize_logger
from src.utils.config_reader import initialize_config
from src.vfs_bot.vfs_bot_factory import get_vfs_bot


def run_once(source: str = "AE", dest: str = "MT") -> None:
    """Runs a single slot-check cycle."""
    try:
        get_vfs_bot(source, dest).run()
    except Exception as e:  # never let one bad cycle kill the loop
        logging.exception(f"Slot-check cycle failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="VFS Malta slot checker scheduler.")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SLOT_CHECK_INTERVAL_MIN", "30")),
        help="Minutes between checks (default: 30, or SLOT_CHECK_INTERVAL_MIN).",
    )
    parser.add_argument("--source", default="AE", help="Source country code (default: AE).")
    parser.add_argument("--dest", default="MT", help="Destination country code (default: MT).")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    args = parser.parse_args()

    initialize_logger()
    initialize_config()

    if args.once:
        run_once(args.source, args.dest)
        return

    interval_s = max(60, args.interval * 60)
    logging.info(f"Slot-check loop started — every {args.interval} min.")
    while True:
        run_once(args.source, args.dest)
        logging.info(f"Sleeping {args.interval} min until next check...")
        time.sleep(interval_s)


if __name__ == "__main__":
    main()
