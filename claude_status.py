"""Claude Code's documented status-line rate limits, without OAuth access."""
from __future__ import annotations

import json
import math
import os
import time
import sys
from pathlib import Path

import providers


def data_dir() -> Path:
    return providers._local_appdata() / "CodexQuotaTray"


def bridge_installed() -> bool:
    try:
        settings = json.loads((providers.claude_home() / "settings.json").read_text(encoding="utf-8-sig"))
        command = (settings.get("statusLine") or {}).get("command", "")
        return "codexquotatray-statusline.ps1" in command and (data_dir() / "codexquotatray-statusline.ps1").is_file()
    except (OSError, ValueError, AttributeError):
        return False


def _bundled_receiver() -> Path:
    return Path(__file__).resolve().parent / "claude_status.ps1"


def refresh_receiver() -> bool:
    """Keep an installed receiver in step with this build; never installs one."""
    try:
        if not bridge_installed():
            return False
        receiver, bundled = data_dir() / "codexquotatray-statusline.ps1", _bundled_receiver().read_bytes()
        if receiver.read_bytes() == bundled:
            return False
        temporary = receiver.with_suffix(".tmp")
        temporary.write_bytes(bundled)
        os.replace(temporary, receiver)
        return True
    except OSError:
        return False


def install() -> None:
    """Explicit installation. Preserve unrelated settings and custom status lines."""
    if os.name != "nt":
        raise OSError("当前实时额度接入脚本适用于 Windows")
    path = providers.claude_home() / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    previous = settings.get("statusLine") or {}
    command = previous.get("command", "")
    if previous and "codexquotatray-statusline.ps1" not in command:
        raise ValueError("已有自定义 Claude statusLine，保留现有配置；请手动接入 claude_status.ps1")
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    receiver = folder / "codexquotatray-statusline.ps1"
    receiver.write_bytes(_bundled_receiver().read_bytes())
    settings["statusLine"] = {"type": "command", "command":
        f'powershell.exe -NoLogo -NoProfile -NonInteractive -File "{receiver.as_posix()}"'}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".quota-tray.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


WINDOWS = ("five_hour", "seven_day")
MAX_AGE_SECONDS = 7 * 86400  # a weekly reading stays meaningful until its own reset


NEWEST_FILES = 128           # sessions whose files are considered, newest first
PRUNE_EVERY = 3600           # how often files older than MAX_AGE_SECONDS are deleted
_parsed: dict[str, tuple[int, int, tuple | None]] = {}   # path -> (mtime_ns, size, parsed record)
_listed: dict[str, tuple[int, list]] = {}                 # folder -> (its mtime_ns, the entries last listed)
_pruned_at = 0.0


def _scan(folder: Path, wall: float) -> list[os.DirEntry]:
    """The NEWEST_FILES newest status files written within MAX_AGE_SECONDS.
    One directory listing (scandir carries the timestamps on Windows), no
    stat per file: the folder keeps one file per Claude session ever seen."""
    # The receiver writes each file in place by renaming a temporary one, so
    # every change moves the folder's own mtime; while that stands still the
    # last listing is reused.
    stamp = os.stat(folder).st_mtime_ns
    cached = _listed.get(str(folder))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    fresh = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                seen = entry.stat().st_mtime
            except OSError:
                continue
            if seen >= wall - MAX_AGE_SECONDS:
                fresh.append((seen, entry))
    fresh.sort(key=lambda item: item[0], reverse=True)
    newest = [entry for _, entry in fresh[:NEWEST_FILES]]
    _listed[str(folder)] = (stamp, newest)
    return newest


def _parse(entry: os.DirEntry) -> tuple | None:
    """(updated_at, [(window, seen, used, reset)]) of one status file, cached
    until the file changes; None for a file that holds no usable reading."""
    try:
        stat = entry.stat()
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _parsed.get(entry.path)
    if cached is not None and cached[:2] == key:
        return cached[2]
    record = None
    try:
        if stat.st_size <= 16384:
            row = json.loads(Path(entry.path).read_text(encoding="utf-8-sig"))
            at = float(row["updated_at"])
            limits = row.get("rate_limits")
            if math.isfinite(at) and isinstance(limits, dict):
                readings = []
                for name in WINDOWS:
                    value = limits.get(name)
                    if not isinstance(value, dict) or isinstance(value.get("used_percentage"), bool):
                        continue
                    used = float(value["used_percentage"])
                    seen = float(value.get("observed_at", at))
                    reset = value.get("resets_at")
                    reset = int(reset) if reset is not None else None
                    if math.isfinite(used) and 0 <= used <= 100 and math.isfinite(seen) and seen <= at + 1:
                        readings.append((name, seen, used, reset))
                if readings:
                    record = (at, readings)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        record = None
    _parsed[entry.path] = (*key, record)
    return record


def prune(folder: Path, wall: float) -> int:
    """Delete status files older than MAX_AGE_SECONDS (one per Claude session
    ever seen, so the folder grows without end); returns how many went."""
    removed = 0
    try:
        with os.scandir(folder) as entries:
            stale = [entry.path for entry in entries if entry.name.endswith(".json")
                     and entry.stat().st_mtime < wall - MAX_AGE_SECONDS]
    except OSError:
        return 0
    for path in stale:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
        _parsed.pop(path, None)
    return removed


def latest(now: float | None = None, folder: Path | None = None) -> dict | None:
    """Native account quota merged across sessions, one value per window.

    Each window comes from its newest observation in any session, so a
    session that left a window out does not hide another session's reading.
    Claude Code drops a window from its status line once the reading it holds
    has passed its reset; a window that is missing or past its reset is a new
    window that has not started yet: nothing used, reset unknown ("idle").
    """
    global _pruned_at
    now = now or time.time()
    wall = time.time()
    target = folder or data_dir() / "claude-status"
    try:
        entries = _scan(target, wall)
    except OSError:
        return None
    if folder is None and wall - _pruned_at >= PRUNE_EVERY:
        _pruned_at = wall
        prune(target, wall)
        for path in [path for path in _parsed if not os.path.exists(path)]:
            _parsed.pop(path, None)
    reports, observed = [], {}          # window -> [(seen, used, reset)] from every session
    for entry in entries:
        record = _parse(entry)
        if record is None:
            continue
        at, readings = record
        if not now - MAX_AGE_SECONDS <= at <= now + 30:
            continue
        for name, seen, used, reset in readings:
            observed.setdefault(name, []).append((seen, used, reset))
        reports.append(at)
    if not reports:
        return None
    observed = {name: _furthest(rows) for name, rows in observed.items()}
    windows = {}
    for name in WINDOWS:
        _, used, reset = observed.get(name, (None, None, None))
        if used is None or (reset is not None and reset <= now):
            windows[name] = {"used_percentage": 0.0, "resets_at": None, "idle": True}
        else:
            windows[name] = {"used_percentage": used, "resets_at": reset}
    # Sessions re-save held readings with a new updated_at; observed_at is
    # when Claude Code last actually reported a number.
    seen = max((value[0] for value in observed.values()), default=max(reports))
    return {"updated_at": max(reports), "observed_at": seen, "rate_limits": windows}


RESET_JITTER = 180


def _furthest(rows: list[tuple[float, float, int | None]]) -> tuple[float, float, int | None]:
    """The account's figure for one window out of every session's reading.

    A session re-reports the limits from its own last request each time its
    status line redraws, with a fresh timestamp, so the newest timestamp is
    often an idle session's old figure. Usage only grows inside a reset
    window: the newest window wins, and in it the highest figure."""
    resets = [reset for _, _, reset in rows if reset is not None]
    if resets:
        newest = max(resets)
        rows = [row for row in rows if row[2] is not None and abs(row[2] - newest) <= RESET_JITTER]
    used = max(row[1] for row in rows)
    seen = max(row[0] for row in rows if row[1] == used)
    reset = max((row[2] for row in rows if row[1] == used and row[2] is not None), default=None)
    return seen, used, reset


def desktop_roots() -> list[Path]:
    override = os.environ.get("CODEX_QUOTA_CLAUDE_DESKTOP_DIR")
    if override:
        return [Path(override)]
    roots = []
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library/Application Support/Claude")
    else:
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "Claude")
        if os.environ.get("LOCALAPPDATA"):
            packages = Path(os.environ["LOCALAPPDATA"]) / "Packages"
            roots.extend(path / "LocalCache/Roaming/Claude" for path in packages.glob("Claude_*"))
    return roots


def recent_desktop_usage(now: float, seconds: float = 1800, roots: list[Path] | None = None) -> float:
    """Evidence of recent Desktop usage, never process presence or a login alone.

    Read only numeric quota history. Compare within the same opaque account
    scope; do not return, log or retain that scope's identity.
    """
    latest_use = 0.0
    for root in desktop_roots() if roots is None else roots:
        path = root / "plan-usage-history.json"
        try:
            if path.stat().st_size > 2_000_000:
                continue
            rows = json.loads(path.read_text(encoding="utf-8"))["samples"]
            previous = {}
            for row in rows[-2000:]:
                at = float(row["t"]) / 1000
                scope = row.get("org")
                values = {key: float(value) for key, value in (row.get("u") or {}).items()
                          if key in ("fh", "sd") and not isinstance(value, bool)}
                if not scope or not values or not math.isfinite(at) or any(not math.isfinite(v) or not 0 <= v <= 100 for v in values.values()):
                    continue
                old = previous.get(scope)
                if old and old[0] < at and now - seconds <= at <= now + 30 and any(
                        key in old[1] and value > old[1][key] for key, value in values.items()):
                    latest_use = max(latest_use, at)
                previous[scope] = (at, values)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return latest_use
