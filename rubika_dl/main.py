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


# botapi.rubika.ir drops packets from most networks outside Iran — GitHub
# runners, and probes from 14 foreign countries (BR CA CH CY ES IL ID KZ NL RU
# SG TR UK US) all time out — while an Iranian IP (and a few scrubbed paths)
# connect fine. A bare TCP probe detects it before getMe burns minutes on it.
_GEO_BLOCKED = (
    "       The network path to botapi.rubika.ir is broken from this machine:\n"
    "       connections are dropped before TLS. GitHub-hosted runners and most\n"
    "       foreign VPSes time out; only some paths (an Iranian IP) work.\n"
    "       Fix: run the bot where Rubika is reachable — a VPS inside Iran, a\n"
    "       self-hosted GitHub runner in Iran, or set RUBIKA_API_PROXY to a\n"
    "       proxy whose exit can reach Rubika (e.g. an Iranian exit)."
)


def _probe_api_host(timeout: float = 8.0) -> tuple[str, str] | None:
    """TCP-connect to the API host.

    Returns ``(category, detail)`` when unreachable — category is one of
    ``timeout`` / ``unreachable`` (packets dropped, almost certainly the
    geo-block above) or ``refused`` (host answered with RST, so the path works
    and the endpoint itself is wrong) — or None if the socket opened.

    Skipped when API_PROXY is set: the real getMe call then tests the whole
    chain through the proxy, while a direct probe would measure the wrong path.
    """
    if config.API_PROXY:
        return None
    from urllib.parse import urlparse
    import socket

    parsed = urlparse(config.API_BASE)
    host, port = parsed.hostname, parsed.port or 443
    if not host:
        return "refused", f"RUBIKA_API_BASE has no host: {config.API_BASE!r}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except (TimeoutError, socket.timeout):
        return "timeout", (
            f"connection to {host}:{port} timed out after {timeout:g}s — "
            "no route answers"
        )
    except ConnectionRefusedError:
        return "refused", f"connection to {host}:{port} was refused"
    except OSError as exc:
        return "unreachable", f"could not connect to {host}:{port} — {exc}"


def cmd_check(_args: argparse.Namespace) -> int:
    setup_logging()
    problems = config.validate()
    for problem in problems:
        print(f"[FAIL] {problem}")
    if problems:
        return 2

    print("[OK] configuration looks valid")
    print(f"[OK] token: {_mask(config.BOT_TOKEN)}")

    probe = _probe_api_host()
    if probe:
        kind, detail = probe
        print(f"[FAIL] {detail}")
        print(_GEO_BLOCKED if kind in ("timeout", "unreachable")
              else "       The host answers but the port is closed — check "
                   "RUBIKA_API_BASE.")
        return 1

    try:
        client = RubikaClient()
        me = client.get_me()
    except RubikaError as exc:
        print(f"[FAIL] could not reach the API: {exc}")
        message = str(exc).lower()
        if "timed out" in message or "connection" in message:
            print(_GEO_BLOCKED)
        else:
            print("       The token is only checked by this live call; if it was "
                  "copied with a stray character, paste it again from BotFather.")
        return 1
    print(f"[OK] authenticated as @{me.get('username')} (bot_id={me.get('bot_id')})")
    print(f"[OK] API base: {config.API_BASE}")
    if config.API_PROXY:
        print(f"[OK] API proxy: {config.API_PROXY}")
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
