"""Optional herdr (https://github.com/herdrdev/herdr) adapter.

herdr runs coding agents in terminal panes and tracks each one's state.  When
it is installed and its server is running, `herdr agent list` tells us which
native sessions belong to the same pane, so everything one agent process does
(sub-agents, helper threads, resumed sessions) folds into one bubble, and
agents without readable local logs still get a bubble.

Nothing here is required: without herdr the agents pass through unchanged.
Set CODEX_QUOTA_HERDR=0 to switch it off, CODEX_QUOTA_HERDR_EXE to point at
the executable and CODEX_QUOTA_HERDR_SESSION to read a named herdr session.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import process_jail

log = logging.getLogger(__name__)


def _on_disk_name(path: str) -> str:
    """The path with the file name spelt as on disk. shutil.which appends the
    PATHEXT extension in its case ("herdr.EXE"), and Herdr's managed launcher
    refuses to run unless it is invoked as bin/herdr.exe."""
    candidate = Path(path)
    try:
        for entry in candidate.parent.iterdir():
            if entry.name.lower() == candidate.name.lower():
                return str(entry)
    except OSError:
        pass
    return path

POLL_SECONDS = 2.5
QUIET_POLL_SECONDS = 10.0   # herdr runs but has no agent panes
IDLE_POLL_SECONDS = 30.0    # herdr installed but its server is not running / unreadable
# Every poll starts a process (plus its job and pipe readers); polling an idle
# or stopped herdr every 2.5 s was ~35 000 process launches a day for nothing.
LOOKUP_SECONDS = 60.0       # how often to look for a newly installed herdr
DONE_SECONDS = 20.0         # a pane that just finished keeps a "刚完成" bubble this long
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# herdr agent names -> tray providers
PROVIDERS = {
    "codex": "codex", "claude": "claude", "grok": "grok",
    "gemini": "gemini", "antigravity": "gemini", "antigravity-cli": "gemini", "agy": "gemini",
    "mimo": "mimo", "mimo-code": "mimo", "mimocode": "mimo",
}
STATUS_TEXT = {"working": "工作中", "blocked": "等你确认", "done": "刚完成", "idle": "空闲"}


def provider_of(name: str) -> str:
    name = (name or "").strip().lower()
    if name in PROVIDERS:
        return PROVIDERS[name]
    for prefix, provider in PROVIDERS.items():
        if name.startswith(prefix):
            return provider
    return "other"


def _folder(path: str) -> str:
    path = str(path or "").rstrip("/\\")
    return re.split(r"[/\\]", path)[-1] if path else ""


def parse_agents(text: str) -> list[dict[str, Any]]:
    """AgentInfo rows from `herdr agent list` (the socket response JSON)."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(value, dict):
        value = value.get("result", value)
        value = value.get("agents", []) if isinstance(value, dict) else value
    return [row for row in value if isinstance(row, dict) and row.get("pane_id")] if isinstance(value, list) else []


def session_ids(row: dict) -> set[str]:
    """Native session identifiers herdr reported for one pane."""
    session = row.get("agent_session") or {}
    value = str(session.get("value") or "") if isinstance(session, dict) else ""
    ids = set(UUID.findall(value))
    if value:
        ids.add(value)
        ids.add(Path(value).stem)
    return {item for item in ids if item}


def find_exe() -> str | None:
    """herdr on PATH, or in the folders its installers use."""
    found = os.environ.get("CODEX_QUOTA_HERDR_EXE") or shutil.which("herdr")
    if not found and os.name == "nt":
        local, home = os.environ.get("LOCALAPPDATA"), os.environ.get("USERPROFILE")
        for candidate in ((Path(local) / "Programs" / "Herdr" / "bin" / "herdr.exe") if local else None,
                          (Path(local) / "herdr" / "bin" / "herdr.exe") if local else None,
                          (Path(home) / ".local" / "bin" / "herdr.exe") if home else None):
            if candidate is not None and candidate.is_file():
                found = str(candidate)
                break
    return _on_disk_name(found) if found and os.name == "nt" else found


# ----- opening an agent (tray click) -------------------------------------------------

AGENT_START_SECONDS = 45     # herdr waits up to 30 s for the agent to be ready
SERVER_WAIT_SECONDS = 15     # a freshly opened Herdr window starts its server


def _session_args() -> list[str]:
    session = os.environ.get("CODEX_QUOTA_HERDR_SESSION", "").strip()
    return ["--session", session] if session else []


HERDR_NAMES = {"herdr.exe", "herdr"}      # Windows, macOS / Linux


def _clients(processes=None) -> list:
    """Herdr windows: TUI clients, `herdr` with no command (for this session)."""
    import psutil
    wanted = _session_args()
    found = []
    for process in processes if processes is not None else psutil.process_iter(["name", "cmdline"]):
        try:
            info = process.info
            if (info.get("name") or "").lower() not in HERDR_NAMES:
                continue
            args = list(info.get("cmdline") or [])[1:]
        except (KeyError, AttributeError, TypeError):
            continue
        if args == wanted or (not wanted and not args):
            found.append(process)
    return found


def client_open(processes=None) -> bool:
    """A Herdr window (the TUI client, `herdr` with no command) is running."""
    return bool(_clients(processes))


def _hosts(processes=None) -> list:
    """Each client and its ancestors, nearest first: the terminal hosting it."""
    import psutil
    hosts = []
    for process in _clients(processes):
        try:
            hosts += [process] + list(process.parents())
        except (AttributeError, psutil.Error):
            continue
    return hosts


def client_window(processes=None, windows=None) -> int | None:
    """The terminal window hosting a Herdr client: a visible, titled top-level
    window owned by the client or one of its ancestors (Windows Terminal)."""
    hosts = [process.pid for process in _hosts(processes)]
    if not hosts:
        return None
    rows = windows if windows is not None else _top_windows()
    for pid in hosts:                       # the nearest host wins
        for hwnd, owner, visible, title in rows:
            if owner == pid and visible and title:
                return hwnd
    return None


def _top_windows() -> list[tuple[int, int, bool, str]]:
    """(hwnd, pid, visible, title) for every top-level window."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    rows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rows.append((int(hwnd), pid.value, bool(user32.IsWindowVisible(hwnd)), title.value))
        return True

    user32.EnumWindows(visit, 0)
    return rows


def client_app(processes=None) -> str | None:
    """macOS: the .app bundle of the terminal hosting a Herdr client."""
    import psutil
    for process in _hosts(processes):
        try:
            path = process.exe()
        except (AttributeError, psutil.Error):
            continue
        marker = path.find(".app/Contents/MacOS/")
        if marker > 0:
            return path[:marker + len(".app")]
    return None


def raise_client(popen=subprocess.Popen) -> bool:
    """Restore a minimised Herdr window and bring it to the front. Call it
    from the tray click itself: Windows lets the clicked app take the
    foreground only until the next input."""
    if sys.platform == "darwin":
        bundle = client_app()
        if bundle:
            popen(["open", bundle], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return bool(bundle)
    hwnd = client_window()
    if hwnd is None or os.name != "nt":
        return False
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    if user32.SetForegroundWindow(hwnd):
        return True
    # Refused (the click's foreground grant already lapsed): share input with
    # the current foreground thread for a moment, which lets the switch through.
    ours = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True)
    try:
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)


def terminal_script(command: list[str]) -> list[str]:
    """macOS: osascript arguments that run `command` in a new Terminal window, in front."""
    import shlex
    line = shlex.join(command).replace("\\", "\\\\").replace('"', '\\"')
    return ["osascript", "-e", f'tell application "Terminal" to do script "{line}"',
            "-e", 'tell application "Terminal" to activate']


def open_window(exe: str, popen=subprocess.Popen) -> None:
    """A new terminal window showing Herdr (Windows Terminal when available,
    Terminal on macOS)."""
    command = [exe] + _session_args()
    home = str(Path.home())
    if sys.platform == "darwin":
        popen(terminal_script(command), cwd=home, close_fds=True)
        return
    terminal = shutil.which("wt") if os.name == "nt" else None
    if terminal:
        popen([terminal, "-w", "new", "-d", home, "--title", "herdr", *command], cwd=home, close_fds=True)
    else:
        popen(command, cwd=home, close_fds=True, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def _call(exe: str, args: list[str], runner, timeout: float) -> dict:
    """One herdr CLI request. Errors arrive as JSON on stderr with status 1."""
    result = runner([exe] + _session_args() + args, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = (result.stdout or "").strip() or (result.stderr or "").strip()
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {"error": {"code": "unreadable", "message": text[:200]}}
    if result.returncode != 0 and "error" not in payload:
        payload = {"error": {"code": f"exit_{result.returncode}", "message": text[:200]}}
    return payload


def _error(payload: dict) -> tuple[str, str] | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not error:
        return None
    return str(error.get("code") or ""), str(error.get("message") or error.get("code") or "未知错误")


class AgentSlow(OSError):
    """Herdr launched the agent but did not see it get ready in time. The new
    workspace is focused with the agent in it, so this is not a failure."""


def start_agent(kind: str, *, exe: str | None = None, runner: Callable | None = None, popen=subprocess.Popen,
                window_open: Callable[[], bool] | None = None, sleep=time.sleep, cwd: str | None = None) -> str:
    """Open Herdr and start a `kind` agent in a new focused workspace; returns
    its name (kind, kind-2, ...). Raises OSError with a readable message."""
    exe = exe or find_exe()
    if not exe:
        raise FileNotFoundError("未找到 herdr，可设置 CODEX_QUOTA_HERDR_EXE")
    runner = runner or process_jail.run
    if not (window_open or client_open)():
        open_window(exe, popen)           # the window starts the server if it was stopped
    for _ in range(int(SERVER_WAIT_SECONDS / 0.5)):
        if _error(_call(exe, ["workspace", "list"], runner, 5)) is None:
            break
        sleep(0.5)
    else:
        raise OSError("herdr 服务未就绪")
    home = cwd or str(Path.home())
    # Each click gets its own focused workspace, named after the agent.
    created = _call(exe, ["workspace", "create", "--label", kind, "--cwd", home, "--focus"], runner, 10)
    failure = _error(created)
    if failure:
        raise OSError(f"herdr 无法新建工作区：{failure[1]}")
    pane = ((created.get("result") or {}).get("root_pane") or {}).get("pane_id")
    if not pane:
        raise OSError("herdr 没有返回新窗格")
    listed = (_call(exe, ["agent", "list"], runner, 5).get("result") or {}).get("agents") or []
    taken = {str(row.get("name")) for row in listed if isinstance(row, dict) and row.get("name")}
    names = [kind] + [f"{kind}-{index}" for index in range(2, 100)]
    for name in (name for name in names if name not in taken):
        started = _call(exe, ["agent", "start", name, "--kind", kind, "--pane", pane], runner, AGENT_START_SECONDS)
        failure = _error(started)
        if failure is None:
            return name
        if "timed out" in failure[1].lower():
            raise AgentSlow(f"{name} 已在 herdr 中启动，就绪检测超时：{failure[1]}")
        if failure[0] != "agent_name_taken":        # another client took the name meanwhile: try the next
            raise OSError(f"herdr 无法启动 {kind}：{failure[1]}")
    raise OSError(f"herdr 中已有太多 {kind}")


class Herdr:
    def __init__(self, exe: str | None = None, runner: Callable | None = None, enabled: bool | None = None):
        if enabled is None:
            enabled = os.environ.get("CODEX_QUOTA_HERDR", "1").strip().lower() not in ("0", "false", "off", "no")
        self.enabled = enabled
        self.exe = exe or os.environ.get("CODEX_QUOTA_HERDR_EXE") or None
        self.session = os.environ.get("CODEX_QUOTA_HERDR_SESSION", "").strip()
        self.runner = runner or process_jail.run
        self.rows: list[dict[str, Any]] = []
        self.polled_at = 0.0
        self.interval = POLL_SECONDS
        self.looked_at = 0.0
        self.status = "未启用" if not enabled else "未发现"
        self.finished: dict[str, float] = {}       # pane -> when herdr last saw it working/blocked
        self.linked: dict[str, set] = {}           # pane -> native session keys matched to it

    # ----- reading ------------------------------------------------------------------
    def _locate(self, now: float) -> str | None:
        if self.exe:
            return self.exe
        if now - self.looked_at < LOOKUP_SECONDS:
            return None
        self.looked_at = now
        self.exe = find_exe()
        return self.exe

    def poll(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        if not self.enabled or now - self.polled_at < self.interval:
            return self.rows
        self.polled_at = now
        exe = self._locate(now)
        if not exe:
            self.rows = []
            return self.rows
        command = [exe] + (["--session", self.session] if self.session else []) + ["agent", "list"]
        try:
            result = self.runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            self.exe, self.rows, self.status = None, [], "未发现"
            return self.rows
        except (OSError, subprocess.SubprocessError):
            self.rows, self.status, self.interval = [], "读取失败", IDLE_POLL_SECONDS
            return self.rows
        if result.returncode != 0:
            self.rows, self.status, self.interval = [], "服务未运行", IDLE_POLL_SECONDS    # installed, no server running
            return self.rows
        self.rows = parse_agents(result.stdout)
        self.status = "已接入"
        self.interval = POLL_SECONDS if self.rows else QUIET_POLL_SECONDS
        return self.rows

    def working(self, provider: str, known: set) -> int:
        """Panes of this client herdr reports busy whose session the local records do not show as active."""
        count = 0
        for row in self.rows:
            if str(row.get("agent_status")) in ("working", "blocked") and provider_of(str(row.get("agent") or "")) == provider \
                    and not (session_ids(row) | self.linked.get(str(row["pane_id"]), set())) & known:
                count += 1
        return count

    # ----- merging into the agent list ------------------------------------------------
    def attach(self, agents: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
        """Tag native agents with their herdr pane and add panes without native logs."""
        now = now or time.time()
        rows = self.poll(now)
        if not rows:
            return agents
        agents = [dict(agent) for agent in agents]
        self.linked = {}
        by_key = {agent["key"]: agent for agent in agents}
        claimed: set[str] = set()
        extra = []
        unmatched = []
        for row in rows:
            pane = str(row["pane_id"])
            status = str(row.get("agent_status") or "unknown")
            if status in ("working", "blocked"):
                self.finished[pane] = now
            matched = [by_key[item] for item in session_ids(row) if item in by_key]
            if matched:
                for agent in matched:
                    self._tag(agent, row, pane, status)
                    claimed.add(agent["key"])
            else:
                unmatched.append((row, pane, status))
        for row, pane, status in unmatched:
            # No session id (herdr integration not installed): same client in the
            # same folder is the pane's agent when exactly one pane fits.
            provider = provider_of(str(row.get("agent") or ""))
            folder = _folder(row.get("foreground_cwd") or row.get("cwd") or "")
            rivals = [other for other, _, _ in unmatched if provider_of(str(other.get("agent") or "")) == provider and
                      _folder(other.get("foreground_cwd") or other.get("cwd") or "") == folder]
            fits = [agent for agent in agents if agent["key"] not in claimed and not agent.get("parent") and
                    agent.get("provider") == provider and folder and agent.get("folder") == folder] if len(rivals) == 1 else []
            if not fits and sum(provider_of(str(other.get("agent") or "")) == provider for other, _, _ in unmatched) == 1:
                # Clients that record no folder (Antigravity / agy): the one live
                # session of that client without a folder.
                loose = [agent for agent in agents if agent["key"] not in claimed and not agent.get("parent") and
                         agent.get("provider") == provider and not agent.get("folder") and agent.get("live")]
                fits = loose if len(loose) == 1 else []
            if provider != "other" and fits:
                agent = max(fits, key=lambda item: item.get("at") or 0)
                self._tag(agent, row, pane, status)
                claimed.add(agent["key"])
                continue
            synthetic = self._synthetic(row, pane, status, provider, folder, now)
            if synthetic:
                extra.append(synthetic)
        self.finished = {pane: moment for pane, moment in self.finished.items() if moment > now - 3600}
        result = agents + extra
        result.sort(key=lambda item: (not item["live"], -(item.get("at") or 0)))
        return result

    def _tag(self, agent: dict, row: dict, pane: str, status: str):
        self.linked.setdefault(pane, set()).add(agent["key"])
        agent["pane"] = pane
        agent["herdr_status"] = status
        if status == "blocked":
            agent["live"], agent["doing"] = True, STATUS_TEXT["blocked"]
        elif status == "working" and not agent.get("live"):
            agent["live"], agent["doing"] = True, agent.get("doing") if agent.get("doing") not in ("", "刚完成") else STATUS_TEXT["working"]
        if not agent.get("folder"):
            agent["folder"] = _folder(row.get("foreground_cwd") or row.get("cwd") or "")

    def _synthetic(self, row: dict, pane: str, status: str, provider: str, folder: str, now: float) -> dict | None:
        recent = now - self.finished.get(pane, 0) < DONE_SECONDS
        if status not in ("working", "blocked") and not (status == "done" and recent):
            return None
        name = str(row.get("display_agent") or row.get("agent") or "agent")
        title = str(row.get("title") or row.get("terminal_title_stripped") or row.get("name") or "").strip()
        live = status in ("working", "blocked")
        return {
            "key": "herdr:" + pane, "provider": provider, "source": f"herdr · {name}", "model": "",
            "title": title, "thread_title": title, "request": title, "doing": STATUS_TEXT.get(status, "工作中") if live else "刚完成",
            "live": live, "at": self.finished.get(pane, now), "parent": "", "role": "", "folder": folder,
            "tps": None, "schedule": "", "origin": "herdr", "pane": pane, "herdr_status": status,
        }
