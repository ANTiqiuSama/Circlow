from __future__ import annotations

import json
import ctypes
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from winpty import PTY
except ImportError:
    PTY = None

try:
    import psutil
except ImportError:  # psutil is a hard requirement of the app; stay importable.
    psutil = None


ANSI_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
# "Weekly limit: 56%" (old) or, from Grok 1.0.77, a panel with
# "Weekly limit (SuperGrok)" over a bar and "76%" on the next row.
GROK_WEEKLY_RE = re.compile(r"weekly\s+limit[^\d%\n]{0,400}?(\d+(?:\.\d+)?)\s*%", re.I)
# Grok's own billing service failing ("Couldn't load usage: Internal error:
# Billing service error ..."). Partial redraws can drop letters, so match
# the parts that survive.
GROK_FAILED_RE = re.compile(r"load\s*usage\s*:\s*internal\s+error|billing\s+service\s+error", re.I)
GROK_READY_RE = re.compile(r"minimal\s*·\s*/help|\d+(?:\.\d+)?[km]?\s*/\s*\d+(?:\.\d+)?[km]?\s*\(\d+%\)", re.I)
RESET_PATTERNS = (
    re.compile(r"next\s+reset\s*[:：]\s*(.+)", re.I),
    re.compile(r"resets?(?:\s+(?:at|in)\s*[:：]?|\s*[:：])\s*(.+)", re.I),
)


# Newer TUIs place each word with a cursor move instead of a space.
CURSOR_STEP_RE = re.compile(r"\x1b\[\d*[CG]")


def clean_terminal_text(text: str) -> str:
    return ANSI_RE.sub("", CURSOR_STEP_RE.sub(" ", text)).replace("\r", "\n")


def parse_reset(text: str) -> int | None:
    normalized = clean_terminal_text(text)
    for pattern in RESET_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        # Panel rows are joined once cursor moves are stripped: stop at the
        # box edge or the padding after the date.
        value = re.split(r"\n|\s{2,}|│", match.group(1).strip(), maxsplit=1)[0].strip()
        for fmt in ("%B %d, %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(value, fmt)
                if parsed.year == 1900:
                    # No year shown.  A date a little in the past is a reset
                    # that just passed (the CLI can lag behind the server);
                    # only one well in the past belongs to next year.
                    parsed = parsed.replace(year=datetime.now().year)
                    if parsed.timestamp() < time.time() - 3 * 86400:
                        parsed = parsed.replace(year=parsed.year + 1)
                    elif parsed.timestamp() > time.time() + 300 * 86400:
                        parsed = parsed.replace(year=parsed.year - 1)   # "December 30" read in early January
                return int(parsed.timestamp())
            except ValueError:
                continue
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return None


def _create_terminal():
    own_console = False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handles = [kernel32.GetStdHandle(value) for value in (-10, -11, -12)]
        if any(handle in (None, ctypes.c_void_p(-1).value) for handle in handles):
            if not kernel32.AllocConsole():
                raise RuntimeError("无法初始化隐藏的 CLI 额度读取终端")
            own_console = True
            user32 = ctypes.windll.user32
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.ShowWindow(kernel32.GetConsoleWindow(), 0)
    try:
        return PTY(160, 40, timeout=5000), own_console
    except BaseException:
        if own_console:
            ctypes.windll.kernel32.FreeConsole()
        raise


def _child_processes(pid: int, executable: str) -> list:
    """Descendants of our own temporary session only (verified by image name)."""
    if psutil is None or not pid:
        return []
    try:
        root = psutil.Process(pid)
        if root.name().lower() != os.path.basename(executable).lower():
            return []
        return root.children(recursive=True)
    except (psutil.Error, OSError, ValueError):
        return []


def _terminate(processes: list) -> None:
    if not processes:
        return
    for process in processes:
        try:
            process.terminate()
        except psutil.Error:
            pass
    try:
        _, alive = psutil.wait_procs(processes, timeout=2)
        for process in alive:
            process.kill()
    except psutil.Error:
        pass


@dataclass(frozen=True)
class UsageProbe:
    """How to drive one CLI's interactive `/usage` in a throwaway PTY."""
    name: str                                   # used in user-facing errors
    ready: re.Pattern                           # prompt is up; slash commands work
    found: re.Pattern                           # quota figures have appeared
    complete: Callable[[str], bool]             # figures are fully rendered
    missing: str                                # what the timeout says we lacked
    command: str = "/usage"
    ready_after: float = 4.0                    # send anyway after this long
    retry_every: float = 2.0
    settle: float = 1.2                         # max wait for `complete` after `found`
    # One-shot prompt replies: keystrokes, or a tuple of them typed one after another.
    answers: tuple[tuple[re.Pattern, str | tuple[str, ...]], ...] = ()
    cwd: Callable[[], str | None] = lambda: None
    failed: re.Pattern | None = None            # the CLI says it cannot load usage
    failure: str = ""                           # what that failure tells the user


GROK_PROBE = UsageProbe(
    name="Grok",
    ready=GROK_READY_RE,
    found=GROK_WEEKLY_RE,
    complete=lambda text: parse_reset(text) is not None,
    missing="Weekly limit",
    failed=GROK_FAILED_RE,
    failure="Grok 官方额度服务出错，暂时读不到额度",
)


KEY_PAUSE = 0.3      # between the keystrokes of one reply, so the TUI sees each


def _type_reply(write: Callable[[str], Any], reply: str | tuple[str, ...]) -> None:
    keys = (reply,) if isinstance(reply, str) else reply
    for index, key in enumerate(keys):
        if index:
            time.sleep(KEY_PAUSE)
        write(key)


def _spawn_target(executable: str, args: list[str]) -> tuple[str, str]:
    """npm installs CLIs as .cmd shims, which need cmd.exe to run in a PTY."""
    if executable.lower().endswith((".cmd", ".bat")):
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), subprocess.list2cmdline(["/d", "/c", executable, *args])
    return executable, subprocess.list2cmdline(args)


def run_temporary_usage(executable: str, args: list[str], timeout: float = 25.0, probe: UsageProbe = GROK_PROBE) -> str:
    """Open a temporary PTY session, issue `/usage`, and return its visible text."""
    if os.name != "nt":
        return _run_posix_usage(executable, args, timeout, probe)
    if PTY is None:
        raise RuntimeError("缺少 pywinpty，无法读取 CLI /usage 临时窗口")

    # The native PTY constructor needs standard console handles even when
    # called by a windowed EXE. Create and release a hidden console as needed.
    terminal, own_console = _create_terminal()
    appname, cmdline = _spawn_target(executable, args)
    captured: list[str] = []
    started_at = time.monotonic()
    deadline = started_at + timeout
    last_request_at = None
    first_quota_at = None
    answered: set[int] = set()
    try:
        cwd = probe.cwd()
        spawned = terminal.spawn(appname, cmdline, cwd) if cwd else terminal.spawn(appname, cmdline)
        if not spawned:
            raise RuntimeError(f"无法启动 {probe.name} 额度读取会话")
        while time.monotonic() < deadline:
            captured.append(terminal.read(blocking=False))
            current = clean_terminal_text("".join(captured))
            now = time.monotonic()
            for index, (pattern, reply) in enumerate(probe.answers):
                if index not in answered and pattern.search(current):
                    answered.add(index)
                    _type_reply(terminal.write, reply)
                    last_request_at = None  # the prompt may only appear after this
            if probe.found.search(current):
                if first_quota_at is None:
                    first_quota_at = now
                if probe.complete(current) or now - first_quota_at >= probe.settle:
                    return current
            elif probe.failed and probe.failed.search(current):
                raise RuntimeError(probe.failure)
            elif (probe.ready.search(current) or now - started_at >= probe.ready_after) and (
                last_request_at is None or now - last_request_at >= probe.retry_every
            ):
                # /usage before session initialization reports "unavailable".
                # Wait for the prompt, then retry only this local slash command.
                terminal.write(probe.command)
                time.sleep(0.1)
                terminal.write("\r")
                last_request_at = time.monotonic()
            if not terminal.isalive():
                raise RuntimeError(f"{probe.name} 额度读取会话提前退出，请检查 CLI 登录状态")
            time.sleep(0.1)
        raise TimeoutError(f"{probe.name} 会话在 {timeout:g} 秒内未返回 {probe.missing}")
    finally:
        try:
            terminal.write("\x1b")
            terminal.write("\x03")
            terminal.write("\x03")
        except Exception:
            pass
        # CLIs can start helpers (MCP servers, node). Collect them first:
        # once the parent is gone Windows no longer links them to it.
        children = _child_processes(terminal.pid, appname)
        try:
            if terminal.isalive():
                os.kill(terminal.pid, signal.SIGTERM)
        except Exception:
            pass
        _terminate(children)
        if own_console:
            ctypes.windll.kernel32.FreeConsole()


def _run_posix_usage(executable: str, args: list[str], timeout: float, probe: UsageProbe) -> str:
    """macOS PTY counterpart; retain quota text only in memory for parsing."""
    import pexpect

    child = pexpect.spawn(executable, args, encoding="utf-8", codec_errors="replace",
                          echo=False, timeout=0.1, dimensions=(40, 160), maxread=4096,
                          cwd=probe.cwd())
    captured: list[str] = []
    started = time.monotonic()
    first_quota_at = last_request_at = None
    answered: set[int] = set()
    try:
        while time.monotonic() - started < timeout:
            try:
                piece = child.read_nonblocking(4096, timeout=0.1)
                captured.append(piece)
                if sum(map(len, captured)) > 2_000_000:
                    raise ValueError(f"{probe.name} 输出超出 2 MB 上限")
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                raise RuntimeError(f"{probe.name} 额度读取会话提前退出，请检查 CLI 登录状态") from None
            current = clean_terminal_text("".join(captured))
            now = time.monotonic()
            for index, (pattern, reply) in enumerate(probe.answers):
                if index not in answered and pattern.search(current):
                    answered.add(index)
                    _type_reply(child.send, reply)
                    last_request_at = None
            if probe.found.search(current):
                if first_quota_at is None:
                    first_quota_at = now
                if probe.complete(current) or now - first_quota_at >= probe.settle:
                    return current
            elif probe.failed and probe.failed.search(current):
                raise RuntimeError(probe.failure)
            elif (probe.ready.search(current) or now - started >= probe.ready_after) and (
                    last_request_at is None or now - last_request_at >= probe.retry_every):
                child.send(probe.command + "\r")
                last_request_at = now
            if not child.isalive():
                raise RuntimeError(f"{probe.name} 额度读取会话提前退出，请检查 CLI 登录状态")
        raise TimeoutError(f"{probe.name} 会话在 {timeout:g} 秒内未返回 {probe.missing}")
    finally:
        descendants = _child_processes(child.pid, executable)
        try:
            child.terminate(force=True)
        finally:
            _terminate(descendants)


def parse_grok_usage(text: str) -> dict[str, Any]:
    clean = clean_terminal_text(text)
    match = GROK_WEEKLY_RE.search(clean)
    if not match:
        raise ValueError("Grok /usage 未找到 Weekly limit")
    used = max(0.0, min(100.0, float(match.group(1))))
    return {
        "used_percent": used,
        "remaining_percent": max(0.0, min(100.0, 100.0 - used)),
        "resets_at": parse_reset(clean),
        "window_minutes": 10080,
    }


# Antigravity reports separate quota pools; the tray ring shows the first one
# present in this order. Matched against group name + bucket id/name.
AGY_POOLS = (
    ("argon", "Argon", ("argon",)),
    ("claude_gpt", "Claude/GPT", ("claude", "gpt", "anthropic", "openai")),
    ("gemini", "Gemini", ("gemini",)),
)


def _bucket_window(text: str) -> int | None:
    text = text.lower()
    match = re.search(r"(\d+)\s*h(?:our)?s?\b", text)
    if match:
        return int(match.group(1)) * 60
    if "week" in text:
        return 10080
    if "day" in text or "daily" in text:
        return 1440
    if "month" in text:
        return 43200
    return None


def _bucket_reset(value) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000 if value > 1e11 else value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def _pool_of(text: str) -> tuple[str, str]:
    lower = text.lower()
    for key, label, words in AGY_POOLS:
        if any(word in lower for word in words):
            return key, label
    return "", ""


def parse_agy_pools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every quota pool in `agy /usage --output-format json`, in display
    priority (Argon, Claude/GPT, Gemini, then unrecognised groups). Each pool
    takes its binding bucket: whichever window has the least left."""
    data = payload.get("command", {}).get("data", {}) if isinstance(payload.get("command"), dict) else {}
    pools: dict[str, dict[str, Any]] = {}
    for group in data.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or group.get("title") or group.get("id") or "")
        for bucket in group.get("buckets", []) or []:
            if not isinstance(bucket, dict) or bucket.get("remaining_fraction") is None:
                continue
            bucket_text = " ".join(str(bucket.get(name) or "") for name in ("id", "name", "display_name", "label", "title"))
            # A bucket naming its own model family wins over the group it sits in.
            key, label = _pool_of(bucket_text)
            if not key:
                key, label = _pool_of(group_name)
            if not key:
                key = label = group_name.removesuffix(" Models").strip() or str(bucket.get("id") or "其他")
            remaining = max(0.0, min(100.0, float(bucket["remaining_fraction"]) * 100.0))
            window = _bucket_window(bucket_text) or _bucket_window(str(bucket.get("window") or ""))
            entry = {"key": key, "label": label, "remaining_percent": remaining, "used_percent": 100.0 - remaining,
                     "resets_at": _bucket_reset(bucket.get("reset_time") or bucket.get("resets_at")),
                     "window_minutes": window, "bucket": str(bucket.get("id") or "")}
            current = pools.get(key)
            if current is None or remaining < current["remaining_percent"] or (
                    remaining == current["remaining_percent"] and (window or 0) < (current["window_minutes"] or 1e9)):
                pools[key] = entry
    order = {key: index for index, (key, _, _) in enumerate(AGY_POOLS)}
    return sorted(pools.values(), key=lambda pool: order.get(pool["key"], len(order)))


def parse_agy_usage_json(payload: dict[str, Any]) -> dict[str, Any]:
    pools = parse_agy_pools(payload)
    if not pools:
        raise ValueError("Agy /usage JSON 未找到额度池")
    first = pools[0]
    return {
        "used_percent": first["used_percent"],
        "remaining_percent": first["remaining_percent"],
        "resets_at": first["resets_at"],
        "window_minutes": first["window_minutes"] or 300,
        "pool": first["label"],
        "pools": pools,
    }


def run_agy_usage(executable: str, timeout: float = 10.0) -> dict[str, Any]:
    env = os.environ.copy()
    env["AGY_DISABLE_AUTO_UPDATE"] = "1"
    import process_jail
    process = process_jail.run(
        [executable, "--print", "/usage", "--output-format", "json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"agy 退出码 {process.returncode}")
    payload = json.loads(process.stdout)
    if payload.get("status") != "SUCCESS":
        raise RuntimeError(str(payload.get("response") or "agy /usage 未成功"))
    return parse_agy_usage_json(payload)


# ----- Claude Code ---------------------------------------------------------------
#
# `/usage` in Claude Code (subscription plans) renders, per window:
#
#   Current session
#   █████████▌                                   19% used
#   Resets 5pm (Asia/Shanghai)
#
#   Current week (all models)
#   ███▌                                         7% used
#   Resets Oct 23, 3:30pm (Asia/Shanghai)
#
# plus model-specific weekly windows.  We read the session (5 h) window as the
# primary and "week (all models)" as the secondary window.

CLAUDE_SECTION_RE = re.compile(r"current\s+(session|week)\b[^\n]*", re.I)
CLAUDE_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(used|left|remaining)", re.I)
CLAUDE_RESET_RE = re.compile(r"resets?\s+(?:at\s+|on\s+)?([^\n]+)", re.I)
CLAUDE_READY_RE = re.compile(r"for\s+shortcuts|/help\s+for\s+help|bypass\s+permissions|accept\s+edits", re.I)
CLAUDE_TRUST_RE = re.compile(r"do\s+you\s+trust\s+the\s+files", re.I)
# The workspace dialog now opens on "No, exit": step down to "Yes" first.
CLAUDE_TRUST_NO_RE = re.compile(r"❯\s*no,\s*exit", re.I)
CLAUDE_TRUST_YES_RE = re.compile(r"❯\s*yes,\s*i\s+trust\s+this\s+folder", re.I)
CLAUDE_TIME_RE = re.compile(
    r"^(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?"
    r"(?:(?P<month>[a-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?",
    re.I,
)
MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _zone(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # tzdata missing or unknown name: fall back to local time
        return None


def parse_claude_reset(value: str, now: datetime | None = None) -> int | None:
    """'5pm (Asia/Shanghai)', 'Oct 23, 3:30pm (UTC)', '15:00' -> epoch seconds."""
    value = value.strip()
    zone_match = re.search(r"\(([A-Za-z]+(?:/[A-Za-z0-9_+\-]+)*)\)", value)
    zone = _zone(zone_match.group(1)) if zone_match else None
    body = value[:zone_match.start()].strip() if zone_match else value
    match = CLAUDE_TIME_RE.match(body)
    if not match:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    ampm = (match["ampm"] or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    now = now or datetime.now(timezone.utc)
    current = now.astimezone(zone) if zone else now.astimezone()
    if match["month"]:
        month = MONTHS.get(match["month"][:3].lower())
        if not month:
            return None
        try:
            moment = current.replace(month=month, day=int(match["day"]), hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if moment < current - timedelta(days=1):  # e.g. "Jan 2" seen on Dec 30
            moment = moment.replace(year=moment.year + 1)
    else:
        moment = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment <= current - timedelta(minutes=1):
            moment += timedelta(days=1)
    return int(moment.timestamp())


def _claude_sections(text: str) -> dict[str, str]:
    clean = clean_terminal_text(text)
    matches = list(CLAUDE_SECTION_RE.finditer(clean))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        heading = match.group(0).lower()
        if match.group(1).lower() == "session":
            name = "session"
        elif "all models" in heading or "(" not in heading:
            name = "week"
        else:
            name = "week:" + heading.split("(", 1)[1].split(")", 1)[0].strip()
        # The TUI redraws; the latest rendering of a section wins.
        sections[name] = clean[match.end():end]
    return sections


def _claude_window(body: str) -> tuple[float | None, int | None]:
    percent = CLAUDE_PERCENT_RE.search(body)
    used = None
    if percent:
        value = max(0.0, min(100.0, float(percent.group(1))))
        used = value if percent.group(2).lower() == "used" else 100.0 - value
    reset = CLAUDE_RESET_RE.search(body)
    return used, (parse_claude_reset(reset.group(1)) if reset else None)


def claude_usage_complete(text: str) -> bool:
    sections = _claude_sections(text)
    return all(name in sections and _claude_window(sections[name])[0] is not None for name in ("session", "week"))


def parse_claude_usage(text: str) -> dict[str, Any]:
    sections = _claude_sections(text)
    session_used, session_reset = _claude_window(sections.get("session", ""))
    week_used, week_reset = _claude_window(sections.get("week", ""))
    if session_used is None and week_used is None:
        raise ValueError("Claude /usage 未找到订阅额度（API Key 计费账号没有 5 小时/每周额度）")
    result: dict[str, Any] = {
        "used_percent": session_used,
        "remaining_percent": None if session_used is None else 100.0 - session_used,
        "resets_at": session_reset,
        "window_minutes": 300,
    }
    if week_used is not None:
        result.update({
            "secondary_used_percent": week_used,
            "secondary_remaining_percent": 100.0 - week_used,
            "secondary_resets_at": week_reset,
            "secondary_window_minutes": 10080,
        })
    return result


def _claude_probe_dir() -> str:
    """An empty, app-owned folder: trusting it grants Claude Code nothing."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "Circlow", "claude-probe")
    os.makedirs(folder, exist_ok=True)
    return folder


CLAUDE_PROBE = UsageProbe(
    name="Claude Code",
    ready=CLAUDE_READY_RE,
    found=CLAUDE_PERCENT_RE,
    complete=claude_usage_complete,
    missing="订阅额度（Current session / Current week）",
    ready_after=6.0,
    retry_every=4.0,
    settle=2.0,
    # The folder is the empty claude-probe one. Newer Claude Code redraws only
    # the ❯ marker after ↓, so "❯ Yes, I trust" never shows: confirm right away.
    answers=((CLAUDE_TRUST_RE, "\r"), (CLAUDE_TRUST_NO_RE, ("\x1b[B", "\r")), (CLAUDE_TRUST_YES_RE, "\r")),
    cwd=_claude_probe_dir,
)
# No MCP servers, no IDE hookup: the session only needs to render /usage.
# With bypassPermissions as the user's default, Claude Code first asks whether
# to switch the default to auto mode, preselecting "Yes"; a probe must never
# answer that, so its own session runs with the plain default mode. (Windows
# may start Claude through a .cmd shim that mangles the JSON quoting.)
CLAUDE_ARGS = ["--strict-mcp-config"] + (
    ["--settings", '{"permissions":{"defaultMode":"default"}}'] if os.name != "nt" else [])
