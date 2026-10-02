"""CLI entry point: ``python -m rubika_dl [run|check|version]``."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, config, heartbeat
from .bot import Bot
from .log import setup_logging
from .rubika_api import RubikaClient, RubikaError


def _deadline_seconds() -> float | None:
    import os

    raw = os.getenv("RUN_MINUTES") or os.getenv("RUN_DEADLINE_MINUTES")
    if not raw:
        return None
    try:
        return max(1.0, float(raw)) * 60.0
    except ValueError:
        return None


def cmd_run(args: argparse.Namespace) -> int:
    setup_logging()
    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 2

    config.ensure_dirs()

    deadline = None
    seconds = _deadline_seconds()
    if seconds:
        deadline = time.monotonic() + seconds

    bot = Bot()
    bot.install_signal_handlers()
    # The runner writes the first heartbeat before this starts; from here the
    # bot's housekeeper keeps it fresh.
    heartbeat.write("starting")
    try:
        bot.run(deadline=deadline)
    except RubikaError as exc:
        heartbeat.write("failed")
        print(f"fatal: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        heartbeat.write("crashed")
        raise
    heartbeat.write("finished")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    setup_logging()
    problems = config.validate()
    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return 2

    print("[OK] configuration looks valid")
    print(f"[OK] token: {_mask(config.BOT_TOKEN)}")
    try:
        client = RubikaClient()
        me = client.get_me()
    except RubikaError as exc:
        print(f"[FAIL] could not reach the API: {exc}")
        print("       The token is only checked by this live call; if it was "
              "copied with a stray character, paste it again from BotFather.")
        return 1
    print(f"[OK] authenticated as @{me.get('username')} (bot_id={me.get('bot_id')})")
    print(f"[OK] API base: {config.API_BASE}")
    print(f"[OK] upload ceiling: {config.MAX_UPLOAD_SIZE // (1024*1024)} MiB")
    return 0


def _mask(token: str) -> str:
    """Show enough of the token to spot a copy/paste mistake, never all of it."""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}…{token[-2:]} ({len(token)} chars)"


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"blackout-downloader {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rubika_dl", description="BlackOut Internet Downloader")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the bot (default)")
    sub.add_parser("check", help="validate config and token")
    sub.add_parser("version", help="print the version")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    if command == "run":
        return cmd_run(args)
    if command == "check":
        return cmd_check(args)
    if command == "version":
        return cmd_version(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
