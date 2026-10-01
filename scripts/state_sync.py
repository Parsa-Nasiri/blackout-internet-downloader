#!/usr/bin/env python3
"""State persistence + workflow chaining for GitHub Actions.

Sub-commands
------------
pull       restore ``.state/`` from the ``bot-state`` branch (if it exists)
push       commit ``.state/`` to the ``bot-state`` branch
heartbeat  write ``.state/heartbeat.json`` (called from the runner loop)
dispatch   trigger ``bot.yml`` again (hand-off to the next 6-hour window)
alive      exit 0 if a bot run looks healthy, 1 otherwise (used by the watchdog)

Environment
-----------
GITHUB_REPOSITORY  owner/repo            (set by Actions)
GITHUB_TOKEN       token with contents+actions write
GH_PAT             optional PAT; preferred for dispatch so the new run is not
                   attributed to the current run's token
GITHUB_REF_NAME    branch to dispatch    (default: main)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.getenv("STATE_DIR", REPO_ROOT / ".state"))
STATE_BRANCH = os.getenv("STATE_BRANCH", "bot-state")
WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "bot.yml")
API = "https://api.github.com"

# A run is considered dead if the heartbeat is older than this.
STALE_AFTER_SECONDS = int(os.getenv("STALE_AFTER_SECONDS", str(20 * 60)))


def _repo() -> str:
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    return repo


def _token() -> str:
    token = os.getenv("GH_PAT") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("No GitHub token available (set GH_PAT or GITHUB_TOKEN)")
    return token


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _remote_url() -> str:
    return f"https://x-access-token:{_token()}@github.com/{_repo()}.git"


# --------------------------------------------------------------------------
# pull / push via a throwaway clone of the state branch
# --------------------------------------------------------------------------
def cmd_pull(_args: argparse.Namespace) -> int:
    """Restore .state from the state branch (no-op if the branch is absent)."""
    tmp = REPO_ROOT / ".state_clone"
    shutil.rmtree(tmp, ignore_errors=True)

    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", STATE_BRANCH,
         _remote_url(), str(tmp)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::notice::no '{STATE_BRANCH}' branch yet — starting with fresh state")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    source = tmp / ".state"
    if source.is_dir():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            target = STATE_DIR / item.name
            if item.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        print(f"restored state: {[p.name for p in source.iterdir()]}")
    else:
        print("state branch has no .state directory yet")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Commit the current .state directory to the state branch."""
    if not STATE_DIR.is_dir():
        print("nothing to push (no .state directory)")
        return 0

    tmp = REPO_ROOT / ".state_clone"
    shutil.rmtree(tmp, ignore_errors=True)

    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", STATE_BRANCH,
         _remote_url(), str(tmp)],
        capture_output=True, text=True,
    )
    if clone.returncode != 0:
        # Branch does not exist — create an orphan branch.
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        subprocess.run(["git", "-C", str(tmp), "checkout", "-q", "--orphan", STATE_BRANCH], check=True)
        subprocess.run(["git", "-C", str(tmp), "remote", "add", "origin", _remote_url()], check=True)

    # Replace the .state directory in the clone.
    target = tmp / ".state"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(STATE_DIR, target)

    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email",
                    "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "add", "-A", ".state"], check=True)

    commit = subprocess.run(
        ["git", "-C", str(tmp), "commit", "-q", "-m", args.message or f"state: {int(time.time())}"],
        capture_output=True, text=True,
    )
    if commit.returncode != 0 and "nothing to commit" in (commit.stdout + commit.stderr):
        print("state unchanged — nothing to commit")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    push = subprocess.run(
        ["git", "-C", str(tmp), "push", "-q", "origin", f"HEAD:refs/heads/{STATE_BRANCH}"],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        print(f"::warning::state push failed: {push.stderr.strip()}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    print("state pushed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


# --------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------
def heartbeat_path() -> Path:
    # Shared with the bot loop so the two writers cannot drift apart.
    sys.path.insert(0, str(REPO_ROOT))
    from rubika_dl import heartbeat as _hb  # noqa: PLC0415

    return _hb.heartbeat_path()


def cmd_heartbeat(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from rubika_dl import heartbeat as _hb  # noqa: PLC0415

    _hb.write(args.status or "running")
    return 0


def read_heartbeat() -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from rubika_dl import heartbeat as _hb  # noqa: PLC0415

    return _hb.read()


def heartbeat_age() -> float | None:
    sys.path.insert(0, str(REPO_ROOT))
    from rubika_dl import heartbeat as _hb  # noqa: PLC0415

    return _hb.age()


# --------------------------------------------------------------------------
# workflow dispatch / health
# --------------------------------------------------------------------------
def active_runs(workflow: str = WORKFLOW_FILE) -> list[dict]:
    response = requests.get(
        f"{API}/repos/{_repo()}/actions/workflows/{workflow}/runs",
        headers=_headers(),
        params={"status": "in_progress", "per_page": 20},
        timeout=30,
    )
    response.raise_for_status()
    runs = response.json().get("workflow_runs", [])
    queued = requests.get(
        f"{API}/repos/{_repo()}/actions/workflows/{workflow}/runs",
        headers=_headers(),
        params={"status": "queued", "per_page": 20},
        timeout=30,
    )
    if queued.ok:
        runs += queued.json().get("workflow_runs", [])
    return runs


def cmd_dispatch(args: argparse.Namespace) -> int:
    ref = args.ref or os.getenv("GITHUB_REF_NAME", "main")
    body = {"ref": ref, "inputs": {"handoff": "true", "from_run": os.getenv("GITHUB_RUN_ID", "")}}
    response = requests.post(
        f"{API}/repos/{_repo()}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        headers=_headers(),
        json=body,
        timeout=30,
    )
    if response.status_code in (201, 204):
        print(f"dispatched {WORKFLOW_FILE} on {ref}")
        return 0
    print(f"::error::dispatch failed ({response.status_code}): {response.text[:300]}", file=sys.stderr)
    return 1


def cmd_alive(_args: argparse.Namespace) -> int:
    """Health check used by the watchdog: is a live bot run present?"""
    runs = active_runs()
    if not runs:
        print("no active run")
        return 1

    age = heartbeat_age()
    if age is None:
        # No heartbeat yet — the run may still be starting up.
        print("active run but no heartbeat file yet")
        return 0

    if age > STALE_AFTER_SECONDS:
        print(f"heartbeat is stale ({age:.0f}s > {STALE_AFTER_SECONDS}s)")
        return 1

    print(f"healthy (heartbeat {age:.0f}s old, {len(runs)} run(s))")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="state_sync")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pull", help="restore .state from the state branch").set_defaults(func=cmd_pull)

    push = sub.add_parser("push", help="commit .state to the state branch")
    push.add_argument("-m", "--message", default=None)
    push.set_defaults(func=cmd_push)

    hb = sub.add_parser("heartbeat", help="write a heartbeat file")
    hb.add_argument("--status", default="running")
    hb.set_defaults(func=cmd_heartbeat)

    dispatch = sub.add_parser("dispatch", help="trigger the next workflow run")
    dispatch.add_argument("--ref", default=None)
    dispatch.set_defaults(func=cmd_dispatch)

    sub.add_parser("alive", help="exit 0 if the bot looks healthy").set_defaults(func=cmd_alive)
    return parser


def main(argv: list[str] | None = None) -> int:
    if requests is None and (argv or sys.argv[1:]):
        cmd = (argv or sys.argv[1:])[0]
        if cmd in {"dispatch", "alive"}:
            raise SystemExit("the 'requests' package is required for this command")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
