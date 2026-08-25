from __future__ import annotations

import argparse
import logging
import sys

from entropy_bot import __version__
from entropy_bot.config import load_settings
from entropy_bot.errors import EntropyBotError


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m entropy_bot",
        description="Paper-first EntropyIO HIP-3 bot (io:ANTH, io:SNDK) on Hyperliquid.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Print io meta + books. No key required.")
    paper = sub.add_parser("paper", help="Shadow-quote ALO on one WS. Never signs.")
    paper.add_argument("--seconds", type=float, default=None, help="Stop after N seconds")
    live = sub.add_parser("live", help="Sign isolated ALO quotes. Requires LIVE=1 and a key.")
    live.add_argument("--seconds", type=float, default=None, help="Stop after N seconds")
    sub.add_parser("cancel", help="Cancel resting bot cloIDs on ANTH/SNDK.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "status":
            from entropy_bot.status import run_status

            return run_status(settings)
        if args.command == "paper":
            from entropy_bot.paper import run_paper

            return run_paper(settings, seconds=args.seconds)
        if args.command == "live":
            from entropy_bot.live import run_live

            return run_live(settings, seconds=args.seconds)
        if args.command == "cancel":
            from entropy_bot.cancel import run_cancel

            return run_cancel(settings)
        parser.error(f"unknown command {args.command}")
        return 2
    except EntropyBotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
