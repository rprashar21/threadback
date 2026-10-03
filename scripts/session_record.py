#!/usr/bin/env python3
"""Shared, concurrency-safe merge helper for per-session JSON records.

Each Claude Code session gets one record at
``~/.claude/session-logs/<project-slug>/<session_id>.json``, made of
disjoint sections owned by different writers:

  - "start"      — written once by the SessionStart hook.
  - "checkpoint" — written/updated by the Stop hook. Deterministic activity
                   metadata only (transcript size/mtime) — no LLM, no
                   transcript content parsing.
  - "summary"    — written by the SessionEnd summarizer worker, or by
                   session_dashboard.py's lazy-summarization pass. The only
                   section two different writers can touch.

Concurrency guarantee:
  - Every read-modify-write is protected by an exclusive flock on a sibling
    ``.lock`` file, so no two writers interleave a read and a write.
  - "start" and "checkpoint" are guarded by wall-clock time: a write is
    applied only if its event timestamp is >= the section's stored
    timestamp.
  - "summary" is guarded by CONTENT COVERAGE, not time: a write is applied
    only if its ``summarized_through_bytes`` is >= what's already stored.
    This stops a slow summarizer run that read an earlier, smaller
    transcript snapshot from clobbering a summary that covered more content,
    regardless of which run happens to finish first.

``project_slug`` must match Claude Code's own project-directory naming
(``~/.claude/projects/<slug>/``) so session_dashboard.py's reconciliation
pass can match records to real transcripts by a plain directory-name lookup
— see `slugify_cwd`.

Used both as a library (imported by session_dashboard.py) and as a small
CLI the bash hooks call via `python3 session_record.py ...`.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from pathlib import Path

LOG_ROOT = Path.home() / ".claude" / "session-logs"
PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Mirrors Claude Code's own project-directory naming: every character that
# isn't alphanumeric becomes "-" (confirmed against real directories under
# ~/.claude/projects/, e.g. "/Users/x/Desktop/llms/python21" ->
# "-Users-x-Desktop-llms-python21"). Using the same rule here means a
# session's record directory and its real transcript directory always share
# one name, so reconciliation is a plain lookup, not a guess.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def slugify_cwd(cwd: str) -> str:
    return _NON_ALNUM_RE.sub("-", cwd)


# A project opts out of the recap by containing `<cwd>/.claude/recap.json`
# with {"exclude": true}. Nothing is deleted: existing records stay on disk
# and reappear as soon as the flag is removed or set to false. Only the
# session's exact cwd is checked (not its parents), so the rule stays
# predictable.
OPT_OUT_RELPATH = Path(".claude") / "recap.json"


def project_opted_out(cwd: str | None) -> bool:
    if not cwd:
        return False
    try:
        config = json.loads((Path(cwd) / OPT_OUT_RELPATH).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(config, dict) and config.get("exclude") is True


# --- Live (mid-session) summary gating ---------------------------------------
# Opt-in via RECAP_LIVE_SUMMARY=1. The Stop hook calls `live-tick`, which
# merges the checkpoint and, only when every gate below passes, spawns a
# detached worker to summarize the still-open session. Gates fire on real
# work (bytes and time), not on message counts, so chatty-but-small sessions
# cost nothing.

def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


LIVE_LOCK_STALE_SECONDS = 300


def live_enabled() -> bool:
    return os.environ.get("RECAP_LIVE_SUMMARY") == "1"


def live_refresh_due(record: dict, transcript_bytes: int, now_epoch: float) -> bool:
    """Pure gate decision (no I/O) so it is easy to test."""
    min_growth = _env_int("RECAP_LIVE_MIN_BYTES", 60 * 1024)
    min_interval = _env_int("RECAP_LIVE_MIN_MINUTES", 20) * 60
    max_refreshes = _env_int("RECAP_LIVE_MAX_REFRESHES", 6)

    covered = record.get("summary", {}).get("summarized_through_bytes", 0)
    if not isinstance(covered, int) or transcript_bytes - covered < min_growth:
        return False
    live = record.get("live", {})
    if live.get("attempts", 0) >= max_refreshes:
        return False
    if now_epoch - live.get("attempted_epoch", 0) < min_interval:
        return False
    return True


def _acquire_live_lock(lock: Path, now_epoch: float) -> bool:
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if now_epoch - lock.stat().st_mtime < LIVE_LOCK_STALE_SECONDS:
                return False
            lock.unlink()
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False
    os.close(fd)
    return True


def live_tick(project_slug: str, session_id: str, cwd: str, transcript_path: str,
              checkpoint: dict, event_ts: str) -> str:
    """Merge the checkpoint, then maybe spawn the live worker. Returns a
    short status word. Never raises for gate failures."""
    import subprocess
    import time

    merge_section(project_slug, session_id, "checkpoint", checkpoint, event_ts)
    if not live_enabled() or project_opted_out(cwd):
        return "checkpoint-only"
    record = read_record(project_slug, session_id)
    now_epoch = time.time()
    if not live_refresh_due(record, int(checkpoint.get("transcript_bytes", 0)), now_epoch):
        return "not-due"
    lock = LOG_ROOT / project_slug / f".{session_id}.live-lock"
    if not _acquire_live_lock(lock, now_epoch):
        return "in-flight"
    attempts = record.get("live", {}).get("attempts", 0) + 1
    merge_section(project_slug, session_id, "live",
                  {"attempts": attempts, "attempted_epoch": now_epoch}, event_ts)
    worker = Path(__file__).resolve().parent.parent / "hooks" / "session-live-worker.sh"
    subprocess.Popen(
        ["bash", str(worker), transcript_path, project_slug, session_id, cwd, str(lock)],
        start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return "spawned"


# record_path() joins these two values straight into a filesystem path.
# slugify_cwd() already guarantees project_slug never contains anything but
# alphanumerics/hyphens, but session_id reaches merge_section() straight from
# each hook's own untrusted stdin JSON payload (session-start-log.sh,
# session-checkpoint.sh) with no sanitization before this point — a
# session_id of "../../../etc/pwned" or "/tmp/absolute-poc" reaches
# `LOG_ROOT / project_slug / f"{session_id}.json"` and Path's own join
# semantics either escape LOG_ROOT entirely (relative traversal) or discard
# it altogether (an absolute-looking component replaces the whole path) —
# both reproduced directly against this function. Every path component this
# module builds a Path from is validated against this rule first.
_SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_path_component(value: str, label: str) -> str:
    if not value or not _SAFE_PATH_COMPONENT_RE.fullmatch(value):
        raise ValueError(
            f"invalid {label}: {value!r} — must be a single non-empty path "
            "component (letters, digits, '_', '-' only; no '/', '.', or empty string)"
        )
    return value


def record_path(project_slug: str, session_id: str) -> Path:
    project_slug = _validate_path_component(project_slug, "project_slug")
    session_id = _validate_path_component(session_id, "session_id")
    return LOG_ROOT / project_slug / f"{session_id}.json"


def _lock_path(record_file: Path) -> Path:
    return record_file.parent / f".{record_file.name}.lock"


def merge_section(
    project_slug: str,
    session_id: str,
    section: str,
    data: dict,
    event_ts: str,
) -> bool:
    """Merge `data` into `section` of the session's JSON record.

    Returns True if the write was applied, False if rejected as stale.
    `data` should not include "_ts" — it's set from `event_ts`.
    """
    record_file = record_path(project_slug, session_id)
    record_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(record_file)

    with open(lock_file, "a+") as lockfp:
        fcntl.flock(lockfp.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current = json.loads(record_file.read_text())
            except (OSError, json.JSONDecodeError):
                current = {}

            existing_section = current.get(section, {}) or {}

            if section == "summary":
                incoming_cov = data.get("summarized_through_bytes", -1)
                existing_cov = existing_section.get("summarized_through_bytes", -1)
                applied = incoming_cov >= existing_cov
            else:
                existing_ts = existing_section.get("_ts", "")
                applied = event_ts >= existing_ts

            if not applied:
                return False

            new_section = dict(data)
            new_section["_ts"] = event_ts

            # A `resume`/later SessionStart must never overwrite the
            # session's original start time.
            if section == "start" and "started_at" in existing_section:
                new_section["started_at"] = existing_section["started_at"]

            current[section] = new_section

            tmp = record_file.parent / f".{record_file.stem}.tmp-{os.getpid()}.json"
            tmp.write_text(json.dumps(current, indent=2))
            tmp.replace(record_file)
            return True
        finally:
            fcntl.flock(lockfp.fileno(), fcntl.LOCK_UN)


def read_record(project_slug: str, session_id: str) -> dict:
    try:
        return json.loads(record_path(project_slug, session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    p_merge = sub.add_parser("merge-section")
    p_merge.add_argument("--project-slug", required=True)
    p_merge.add_argument("--session-id", required=True)
    p_merge.add_argument("--section", required=True)
    p_merge.add_argument("--data", required=True, help="JSON object string")
    p_merge.add_argument("--event-ts", required=True)

    p_slug = sub.add_parser("slugify")
    p_slug.add_argument("cwd")

    p_opt = sub.add_parser("opted-out", help="exit 0 if the project opted out")
    p_opt.add_argument("cwd")

    p_tick = sub.add_parser("live-tick")
    p_tick.add_argument("--project-slug", required=True)
    p_tick.add_argument("--session-id", required=True)
    p_tick.add_argument("--cwd", required=True)
    p_tick.add_argument("--transcript-path", required=True)
    p_tick.add_argument("--data", required=True, help="checkpoint JSON object string")
    p_tick.add_argument("--event-ts", required=True)

    args = parser.parse_args()

    if args.action == "live-tick":
        try:
            print(live_tick(args.project_slug, args.session_id, args.cwd,
                            args.transcript_path, json.loads(args.data), args.event_ts))
        except (ValueError, OSError) as e:
            print(f"rejected: {e}", file=sys.stderr)
            return 1
        return 0

    if args.action == "opted-out":
        return 0 if project_opted_out(args.cwd) else 1

    if args.action == "slugify":
        print(slugify_cwd(args.cwd))
        return 0

    if args.action == "merge-section":
        try:
            data = json.loads(args.data)
            applied = merge_section(
                args.project_slug, args.session_id, args.section, data, args.event_ts
            )
        except ValueError as e:
            print(f"rejected: {e}", file=sys.stderr)
            return 1
        print("applied" if applied else "rejected-stale")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli())
