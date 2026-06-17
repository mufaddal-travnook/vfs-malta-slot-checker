import argparse
import logging
import sys

from src.utils.config_reader import initialize_config
from src.vfs_bot.vfs_bot import LoginError
from src.vfs_bot.vfs_bot_factory import UnsupportedCountryError, get_vfs_bot


def main() -> None:
    """
    Entry point for the VFS Malta Slot Checker.

    Logs in, reaches Step 1, reads the earliest-slot banner for each configured
    combination, and reports the results via Telegram. Defaults to the UAE ->
    Malta route (AE / MT); override with -sc / -dc if needed.
    """
    initialize_logger()
    initialize_config()

    parser = argparse.ArgumentParser(
        description="VFS Malta Slot Checker: reports earliest appointment slots."
    )
    parser.add_argument(
        "-sc",
        "--source-country-code",
        type=str,
        default="AE",
        help="ISO 3166-1 alpha-2 source country code (default: AE).",
        metavar="<country_code>",
    )
    parser.add_argument(
        "-dc",
        "--destination-country-code",
        type=str,
        default="MT",
        help="ISO 3166-1 alpha-2 destination country code (default: MT).",
        metavar="<country_code>",
    )

    args = parser.parse_args()
    try:
        bot = get_vfs_bot(args.source_country_code, args.destination_country_code)
        bot.run()
    except (UnsupportedCountryError, LoginError) as e:
        logging.error(e)
    except Exception as e:
        logging.exception(e)


def initialize_logger():
    file_handler = logging.FileHandler("app.log", mode="a")
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
        )
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        handlers=[file_handler, stream_handler],
    )


if __name__ == "__main__":
    main()
