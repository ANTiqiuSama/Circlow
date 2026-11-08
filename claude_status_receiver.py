"""macOS twin of claude_status.ps1, run by the system Python on every status
line redraw (`/usr/bin/python3 -I -S circlow-statusline.py`), standard
library only, so it starts in a few milliseconds.

Receives only Claude Code's documented status-line JSON; keeps the numeric
fields of every session in one rolling file, claude-status.json, next to this
script: {"schema":3,"updated_at":…,"sessions":{"<session_id>":{"updated_at":…,"rate_limits":{…}}}}.
Sessions not heard from for KEEP_SECONDS are dropped, and at most
KEEP_SESSIONS newest are kept, so the file never needs cleaning up.
"""
import fcntl
import json
import os
import re
import sys
import time

KEEP_SECONDS = 7 * 86400
KEEP_SESSIONS = 500
WINDOWS = ("five_hour", "seven_day")


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def merge(existing, session, payload, now):
    """The new rolling record and the windows this status line reported."""
    sessions, previous = {}, None
    for key, entry in ((existing or {}).get("sessions") or {}).items():
        if not isinstance(entry, dict) or number(entry.get("updated_at")) is None:
            continue
        if key == session:
            previous = entry
        elif entry["updated_at"] >= now - KEEP_SECONDS:
            sessions[key] = entry
    limits, live = {}, {}
    reported = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
    for name in WINDOWS:
        window = reported.get(name) if isinstance(reported.get(name), dict) else {}
        used = number(window.get("used_percentage"))
        if used is not None and 0 <= used <= 100:
            limits[name] = {"used_percentage": used, "resets_at": window.get("resets_at"), "observed_at": now}
            live[name] = used
            continue
        # Claude Code omits a window once its reading passes the reset; keep
        # the last reading so the tray sees the rollover instead of a gap.
        old = ((previous or {}).get("rate_limits") or {}).get(name)
        if isinstance(old, dict) and number(old.get("used_percentage")) is not None:
            seen = number(old.get("observed_at"))
            limits[name] = {"used_percentage": old["used_percentage"], "resets_at": old.get("resets_at"),
                            "observed_at": seen if seen is not None else previous["updated_at"]}
    sessions[session] = {"updated_at": now, "rate_limits": limits}
    if len(sessions) > KEEP_SESSIONS:
        newest = sorted(sessions, key=lambda key: sessions[key]["updated_at"], reverse=True)[:KEEP_SESSIONS]
        sessions = {key: sessions[key] for key in newest}
    return {"schema": 3, "updated_at": now, "sessions": sessions}, live


def main():
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    session = str(payload.get("session_id") or "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", session):
        return
    folder = os.path.dirname(os.path.abspath(__file__))
    destination = os.path.join(folder, "claude-status.json")
    now = time.time()
    # Several sessions redraw their status lines at once; one writer at a time.
    with open(os.path.join(folder, "claude-status.lock"), "a") as lock:
        deadline = now + 3
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    break                     # write anyway, as the Windows receiver does
                time.sleep(0.02)
        try:
            with open(destination, encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, ValueError):
            existing = None
        record, live = merge(existing if isinstance(existing, dict) else None, session, payload, now)
        temporary = f"{destination}.{os.getpid()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(record, handle, separators=(",", ":"))
            os.replace(temporary, destination)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    parts = ["Claude Code"]
    if "five_hour" in live:
        parts.append(f"5h left {100 - live['five_hour']:.0f}%")
    if "seven_day" in live:
        parts.append(f"7d left {100 - live['seven_day']:.0f}%")
    print(" | ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not spill stdin, terminal content or credentials to stdout/stderr.
        pass
