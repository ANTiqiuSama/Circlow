"""What each local agent is working on, and which agents share one task.

Requests, tool steps and thread titles come from the same local records that
tps_sources already reads; they stay in memory. Only the user's grouping
(session ids) is written to disk. Titles come from a local heuristic, or from
an optional OpenAI-compatible endpoint when one is configured.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Circlow"
GROUPS_PATH = DATA_DIR / "agent_groups.json"
SUMMARY_CONFIG = DATA_DIR / "agent_summary.json"
LIVE_SECONDS = 45          # A sub-agent without its own activity flag counts as working this long.
RECENT_SECONDS = 180       # Finished agents linger as dim bubbles so the field does not flicker.
GROUP_KEEP_SECONDS = 7 * 86400
log = logging.getLogger(__name__)

TRIVIAL = {"继续", "继续吧", "接着", "接着来", "好", "好的", "可以", "行", "嗯", "是", "对", "开始", "确认", "ok", "okay",
           "yes", "y", "go", "go on", "continue", "proceed", "next", "sure", "thanks", "谢谢", "完成了吗", "然后呢",
           "my request", "用户请求"}
FILLER = re.compile(r"^(?:请你?|麻烦你?|帮我|帮忙|给我|你好|你能|你可以|能不能|可不可以|我想要?|我需要|我要|现在|然后|接下来|另外|那就|那么|"
                    r"please|pls|can you|could you|would you|help me|i want to|i need to|now|then|also|hey|hi)[\s,，:：、]*", re.I)
CLAUSE = re.compile(r"[。！？!?\n；;，]+|(?<=[\u4e00-\u9fff]),")


def width(text: str) -> float:
    """Display units: CJK and full-width count 1, Latin roughly half."""
    return sum(1.0 if ord(char) > 0x2E80 else 0.55 for char in text)


def shorten(text: str, units: float) -> str:
    if width(text) <= units:
        return text
    out = ""
    for char in text:
        if width(out + char) > units - 0.6:
            break
        out += char
    return out.rstrip(" ,，、:：-") + "…"


def clean(text: str) -> str:
    """Turn a raw request into a readable one-line task phrase."""
    text = str(text or "")
    text = re.sub(r"```.*?(?:```|$)", " ", text, flags=re.S)
    text = re.sub(r"<([A-Za-z][\w-]*)[^>]*>.*?</\1>", " ", text, flags=re.S)
    text = re.sub(r"</?[A-Za-z][\w-]*[^>]*>", " ", text)
    text = re.sub(r"\[(?:Request interrupted|Image)[^\]]*\]", " ", text, flags=re.I)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-~ ]+)+[\\/]([\w.\-]+)", r"\1", text)
    text = re.sub(r"[`*#>|]+|(?<!\w)_+|_+(?!\w)", " ", text)
    best = ""
    for clause in CLAUSE.split(text):
        clause = re.sub(r"\s+", " ", clause).strip(" -—:：,，、")
        while True:
            stripped = FILLER.sub("", clause, count=1).strip()
            if stripped == clause:
                break
            clause = stripped
        if not trivial(clause):
            best = clause
            break
    return best


def trivial(text: str) -> bool:
    text = (text or "").strip().lower().rstrip("。.!！?？~")
    return len(text) < 2 or text in TRIVIAL


def _basename(value: str) -> str:
    return re.split(r"[\\/]", str(value).rstrip("\\/"))[-1]


def _command_action(command: str) -> str:
    command = re.sub(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", command or "").strip()
    # Skip PowerShell/shell variable set-up ("$o = ...; python ...") to reach the real command.
    parts = [part.strip() for part in re.split(r"\s*(?:;|&&)\s*", command) if part.strip()]
    useful = [part for part in parts if not re.match(r"^(?:\$[\w:]+\s*=|set\s+\w+=|export\s+\w+=|cd\s)", part, re.I)]
    if useful and useful[0] != command:
        command = "; ".join(useful)
    lower = command.lower()
    if re.search(r"\b(pytest|unittest|jest|vitest|mocha|go test|cargo test|npm (?:run )?test|test_\w+)\b", lower):
        return "运行测试"
    if re.search(r"\b(pip3? install|npm (?:i|install)|pnpm (?:i|add|install)|yarn add|uv (?:pip|add))\b", lower):
        return "安装依赖"
    if re.search(r"\b(build|pyinstaller|tsc|webpack|vite build|cargo build|make)\b", lower):
        return "构建"
    words = re.findall(r"[^\s\"']+", command)
    if not words:
        return "运行命令"
    tool = _basename(words[0]).lower().removesuffix(".exe")
    if tool == "git" and len(words) > 1:
        return f"git {words[1]}"
    if tool in ("rg", "grep", "findstr", "find", "ls", "dir", "cat", "type", "sed", "head", "tail", "get-content", "select-string", "wc", "tree"):
        return "查看代码"
    if tool in ("curl", "wget", "invoke-webrequest"):
        return "请求网络"
    return f"运行 {shorten(tool, 10)}"


def tool_action(name: str, arguments: Any = None) -> str:
    """Short verb phrase for a tool call."""
    name = str(name or "")
    lower = name.lower()
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            arguments = parsed if isinstance(parsed, dict) else {"input": arguments}
        except ValueError:
            arguments = {"input": arguments}
    arguments = arguments if isinstance(arguments, dict) else {}
    if lower in ("exec", "functions.exec"):
        code = str(arguments.get("input") or arguments.get("code") or "")
        if "exec_command" in code:
            match = re.search(r'''(?:cmd|command)["']?\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', code)
            if match:
                value = match.group(1)
                try:
                    command = json.loads(value) if value.startswith('"') else value[1:-1]
                except ValueError:
                    command = value[1:-1]
                return _command_action(command)
            return "运行命令"
        if "write_stdin" in code:
            return "等待命令完成"
        if "apply_patch" in code:
            return "编辑代码"
        if "view_image" in code:
            return "查看图像"
        if "web__" in code:
            return "查资料"
    # Antigravity names its arguments in PascalCase (Path, CommandLine, ...).
    path = (arguments.get("file_path") or arguments.get("path") or arguments.get("filePath") or arguments.get("Path")
            or arguments.get("AbsolutePath") or arguments.get("TargetFile") or "")
    if lower.startswith("mcp__"):
        return f"调用 {shorten(lower.split('__')[1], 10)}"
    if "patch" in lower or lower in ("edit", "multiedit", "write", "str_replace", "str_replace_editor", "notebookedit", "create_file",
                                     "write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content"):
        match = re.search(r"\*\*\* (?:Update|Add) File: (.+)", str(arguments.get("input") or ""))
        target = _basename(match.group(1).strip()) if match else _basename(path) if path else ""
        return f"编辑 {shorten(target, 14)}" if target else "编辑代码"
    if lower in ("read", "view", "read_file", "open_file", "view_image", "view_file"):
        return f"阅读 {shorten(_basename(path), 14)}" if path else "阅读文件"
    if lower in ("grep", "glob", "search", "codebase_search", "find", "list_dir", "ls", "grep_search", "find_by_name"):
        return "检索代码"
    if lower in ("task", "agent", "spawn_agent", "subagent", "delegate", "send_input", "invoke_subagent", "send_message"):
        return "派发子任务"
    if "todo" in lower or "plan" in lower:
        return "规划步骤"
    if "web" in lower or "fetch" in lower or "browser" in lower:
        return "查资料"
    if lower in ("shell", "bash", "exec_command", "local_shell", "local_shell_call", "shell_command", "run_command", "powershell", "execute"):
        command = arguments.get("cmd") or arguments.get("command") or arguments.get("CommandLine") or arguments.get("input") or ""
        if isinstance(command, list):
            command = " ".join(str(part) for part in command if part not in ("bash", "-lc", "sh", "-c", "powershell", "-Command"))
        return _command_action(str(command))
    return f"调用 {shorten(name, 12)}" if name else ""


GROK_KINDS = {"read": "阅读文件", "edit": "编辑代码", "delete": "编辑代码", "move": "编辑代码", "search": "检索代码",
              "execute": "运行命令", "think": "思考中", "fetch": "查资料"}


def _find(value: Any, wanted: str, depth: int = 0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        if value.get(wanted):
            return value[wanted]
        for item in value.values():
            found = _find(item, wanted, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value[:20]:
            found = _find(item, wanted, depth + 1)
            if found:
                return found
    elif isinstance(value, str) and value.startswith("{") and depth == 0:
        try:
            return _find(json.loads(value), wanted, depth + 1)
        except ValueError:
            return None
    return None


def _moment(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


@dataclass
class Note:
    key: str
    title: str = ""          # client-generated thread title
    prompt: str = ""         # latest substantive request, cleaned
    request: str = ""        # same request, lightly trimmed for an optional summarizer
    action: str = ""
    action_at: float = 0.0
    cwd: str = ""
    parent: str = ""
    role: str = ""
    active_at: float = 0.0
    buffer: str = ""
    working: bool = False    # full task lifetime, including tools between output chunks
    schedule_id: str = ""
    turn_origin: str = ""
    first: str = ""          # first substantive request: what a scheduled run is started with
    started: float = 0.0
    origin: str = ""         # launcher markers (originator, entrypoint, thread source)


class AgentNotes:
    """Fed record by record from tps_sources on its own thread."""

    def __init__(self):
        self.notes: dict[str, Note] = {}

    def note(self, key: str) -> Note:
        return self.notes.setdefault(key, Note(key))

    def ask(self, key: str, text: str, moment: float | None = None):
        note = self.note(key)
        envelope = re.search(r"<heartbeat>.*?<automation_id>([^<]+)</automation_id>.*?</heartbeat>", str(text), re.S)
        if envelope:
            note.schedule_id = envelope.group(1).strip()
            note.turn_origin = f"heartbeat:{note.schedule_id}"
            instructions = re.search(r"<instructions>(.*?)</instructions>", str(text), re.S)
            text = instructions.group(1) if instructions else "定时任务"
        cleaned = clean(text)
        if cleaned:
            if not envelope:
                note.schedule_id = note.turn_origin = ""
            if not note.first:
                note.first, note.started = cleaned, moment or note.started
            note.prompt, note.request = cleaned, re.sub(r"\s+", " ", str(text)).strip()[:600]
            note.action, note.action_at = "", moment or note.action_at
        if moment:
            self.touch(key, moment)

    def act(self, key: str, action: str, moment: float | None):
        note = self.note(key)
        if action and (moment or 0) >= note.action_at:
            note.action, note.action_at = action, moment or note.action_at
        if moment:
            self.touch(key, moment)

    def touch(self, key: str, moment: float):
        note = self.note(key)
        note.active_at = max(note.active_at, moment)

    def describe(self, key: str, *, title: str = "", cwd: str = "", parent: str = "", role: str = "", origin: str = ""):
        note = self.note(key)
        if origin and origin not in note.origin:
            note.origin = (note.origin + " " + origin)[-400:]
        cleaned = clean(title) if title else ""
        note.title = cleaned or note.title
        note.cwd = cwd or note.cwd
        note.parent = parent if parent and parent != key else note.parent
        note.role = role or note.role

    def codex(self, key: str, record: dict):
        payload = record.get("payload") or {}
        moment = _moment(record.get("timestamp"))
        kind = record.get("type")
        event = payload.get("type")
        if kind == "event_msg" and event in ("task_started", "task_complete", "turn_aborted", "error"):
            self.note(key).working = event == "task_started"
            if event == "task_started":
                self.note(key).schedule_id = self.note(key).turn_origin = ""
            if moment:
                self.touch(key, moment)
        if kind == "session_meta":
            markers = [str(payload.get("originator") or ""), json.dumps(payload.get("source") or "", ensure_ascii=False)]
            markers += [f"{name}={value}" for name, value in payload.items() if SCHEDULE_MARK.search(str(name))]
            self.describe(key, cwd=str(payload.get("cwd") or ""), parent=str(_find(payload.get("source"), "parent_thread_id") or ""),
                          role=str(_find(payload, "agent_nickname") or _find(payload, "agent_role") or ""),
                          origin=" ".join(marker for marker in markers if marker and marker != '""')[:300])
        elif kind == "turn_context":
            self.describe(key, cwd=str(payload.get("cwd") or ""))
        elif kind == "event_msg" and payload.get("type") == "user_message":
            self.ask(key, payload.get("message") or "", moment)
        elif kind == "event_msg" and payload.get("type") in ("agent_reasoning", "agent_reasoning_raw_content"):
            self.act(key, "思考中", moment)
        elif kind == "response_item":
            item = payload.get("type")
            if item == "message" and payload.get("role") == "user":
                content = payload.get("content") or []
                text = content if isinstance(content, str) else "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
                self.ask(key, text, moment)
            elif item == "reasoning":
                self.act(key, "思考中", moment)
            elif item in ("function_call", "custom_tool_call"):
                self.act(key, tool_action(payload.get("name"), payload.get("arguments") or payload.get("input")), moment)
            elif item == "local_shell_call":
                self.act(key, tool_action("local_shell", (payload.get("action") or {})), moment)
            elif item == "web_search_call":
                self.act(key, "查资料", moment)
            elif item == "message" and payload.get("role") == "assistant" and moment:
                self.touch(key, moment)

    def claude(self, key: str, record: dict, parent: str = ""):
        moment = _moment(record.get("timestamp"))
        if record.get("type") == "summary" and record.get("summary"):
            self.describe(key, title=str(record["summary"]))
            return
        if record.get("type") not in ("user", "assistant"):
            return
        self.describe(key, cwd=str(record.get("cwd") or ""), parent=parent, origin=str(record.get("entrypoint") or ""))
        content = (record.get("message") or {}).get("content")
        if record.get("type") == "user":
            if record.get("isMeta"):
                return
            if isinstance(content, str):
                self.ask(key, content, moment)
            elif isinstance(content, list):
                text = " ".join(str(block.get("text") or "") for block in content
                                if isinstance(block, dict) and block.get("type") == "text")
                if text.strip():
                    self.ask(key, text, moment)
                elif moment:
                    self.touch(key, moment)
            return
        steps = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
        tools = [block for block in steps if block.get("type") == "tool_use"]
        if tools:
            self.act(key, tool_action(tools[-1].get("name"), tools[-1].get("input")), moment)
        elif any(block.get("type") == "thinking" for block in steps):
            self.act(key, "思考中", moment)
        elif moment:
            self.touch(key, moment)

    def grok(self, key: str, record: dict):
        update = (record.get("params") or {}).get("update") or {}
        moment = _moment(record.get("timestamp"))
        kind = update.get("sessionUpdate")
        note = self.note(key)
        if kind == "user_message_chunk":
            content = update.get("content") or {}
            if len(note.buffer) < 20_000:      # a huge paste arrives in many chunks; only the start is used
                note.buffer += str(content.get("text") or "") if isinstance(content, dict) else ""
            return
        if note.buffer:
            self.ask(key, note.buffer, moment)
            note.buffer = ""
        if kind in ("tool_call", "tool_call_update") and (update.get("title") or update.get("kind")):
            action = GROK_KINDS.get(str(update.get("kind") or ""), "") or tool_action(str(update.get("title") or ""))
            self.act(key, action, moment)
        elif kind == "agent_thought_chunk":
            self.act(key, "思考中", moment)
        elif moment:
            self.touch(key, moment)

    def prune(self, keep: set, now: float):
        for key, note in list(self.notes.items()):
            if key not in keep and note.active_at < now - 2 * 86400:
                self.notes.pop(key, None)

    def recurring(self) -> dict[str, float]:
        """Requests that started separate sessions at clearly different times."""
        runs: dict[str, list[float]] = {}
        for note in self.notes.values():
            if note.first and note.started and not note.parent:
                runs.setdefault(note.first, []).append(note.started)
        return {prompt: max(starts) for prompt, starts in runs.items() if len(starts) > 1 and max(starts) - min(starts) >= RECUR_SPREAD}

    def export(self, activity: dict, context: dict, rates: dict, now: float, provider_of=lambda model: "other",
               schedules: list | None = None) -> list[dict]:
        """Agents that are working or finished within RECENT_SECONDS. Scheduled
        runs are flagged with their schedule and never linger once finished."""
        result = []
        schedules = schedules or []
        keys = set(activity) | {key for key, note in self.notes.items() if note.active_at > now - RECENT_SECONDS}
        for key in keys:
            note = self.notes.get(key) or Note(key)
            act = activity.get(key)
            if act:
                note.active_at = max(note.active_at, act[1])
                self.notes.setdefault(key, note)
            info = context.get(key) or context.get(note.parent) or {}
            if not (act or info or note.parent):
                continue
            live = (bool(act and act[2] != "刚完成生成") or
                    note.working and now - note.active_at < 600 or
                    not act and bool(note.parent) and now - note.active_at < LIVE_SECONDS)
            source = str(info.get("source") or "")
            if act:
                provider = act[0]
            elif note.parent in activity:
                provider = activity[note.parent][0]
            else:
                provider = provider_of(str(info.get("model") or ""))
            if provider == "other" and source == "Claude Code":
                provider = "claude"
            action = note.action if note.action and note.action_at >= note.active_at - 120 else ""
            schedule = match_schedule(note, schedules)
            if schedule and not live:
                continue
            if not live and not (note.prompt or note.title or note.request or action or note.parent):
                # A session that only started and ended (the tray's own Grok /usage
                # read every 30 min, a CLI opened and closed) is not a task.
                continue
            result.append({
                "key": key, "provider": provider, "source": source, "model": str(info.get("model") or ""),
                "title": note.prompt or note.title or "", "thread_title": note.title, "request": note.request or note.title,
                "doing": action if live else "刚完成", "live": live, "at": note.active_at,
                "parent": note.parent, "role": note.role, "folder": _basename(note.cwd) if note.cwd else "",
                "tps": rates.get(key), "schedule": schedule, "origin": " ".join(x for x in (note.origin, note.turn_origin) if x),
            })
        result.sort(key=lambda item: (not item["live"], -item["at"]))
        return result


# ----- scheduled tasks ----------------------------------------------------------

SCHEDULE_MARK = re.compile(r"automation|schedul|cron|heartbeat|定时", re.I)
RECUR_SPREAD = 1800        # Same request starting sessions this far apart = a recurring job.


def _same_task(first: str, other: str) -> bool:
    if not first or not other:
        return False
    if first == other:
        return True
    shorter, longer = sorted((first, other), key=len)
    return len(shorter) >= 8 and shorter in longer


def match_schedule(note: Note, schedules: list) -> str:
    """Key of the schedule this session runs for, or ""."""
    for schedule in schedules:
        if (note.schedule_id and schedule.get("id") == note.schedule_id or
                schedule.get("session") == note.key or _same_task(note.prompt or note.first, schedule.get("prompt", ""))):
            return schedule["key"]
    if note.schedule_id:
        return "heartbeat:" + note.schedule_id
    if note.origin and SCHEDULE_MARK.search(note.origin):
        return "origin:" + (note.first or note.title or note.key)
    return ""


def _pick(row: dict, *names: str):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        for key, value in lowered.items():
            if name in key and value not in (None, ""):
                return value
    return None


def _frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.S)
    if not match:
        return {}, text
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            fields[name.strip().lower()] = value.strip().strip("'\"")
    return fields, match.group(2)


def _definition(row: dict, source: str, fallback: str) -> dict | None:
    prompt = clean(str(_pick(row, "prompt", "instruction", "message", "task", "description") or ""))
    name = str(_pick(row, "name", "title", "label") or "")
    if not (prompt or name):
        return None
    status = _pick(row, "enabled", "status", "active")
    if status is False or status == 0 or str(status or "").lower() in ("0", "false", "disabled", "paused", "deleted", "archived"):
        return None
    return {"key": f"{source}:{name or fallback}", "id": str(row.get("id") or fallback), "title": clean(name) or name or prompt, "prompt": prompt, "source": source,
            "last_at": _moment(_pick(row, "last_run", "lastrun", "last_triggered")) or 0.0,
            "next_at": _moment(_pick(row, "next_run", "nextrun", "next_trigger")) or 0.0}


def read_schedules(codex_root: Path, claude_root: Path) -> list[dict]:
    """Scheduled task definitions the local clients keep, best effort:
    Codex automations (state tables or files) and Claude scheduled tasks."""
    found: list[dict] = []
    for state in (codex_root / "sqlite/codex-dev.db", codex_root / "state_5.sqlite"):
        if not state.is_file():
            continue
        try:
            import sqlite3
            db = sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
            try:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%automation%'")]
                for table in [name for name in tables if not re.search(r"run|log|history|event", name, re.I)][:4]:
                    cursor = db.execute(f'SELECT * FROM "{table}" LIMIT 100')
                    names = [column[0] for column in cursor.description]
                    for index, row in enumerate(cursor.fetchall()):
                        item = _definition(dict(zip(names, row)), "Codex", f"{table}{index}")
                        if item:
                            found.append(item)
            finally:
                db.close()
        except Exception as error:   # unknown schemas must never break the panel
            log.debug("codex automations unreadable: %s", type(error).__name__)
    for folder, source in ((codex_root / "automations", "Codex"), (claude_root / "scheduled-tasks", "Claude")):
        if not folder.is_dir():
            continue
        try:
            paths = (sorted(folder.glob("*/automation.toml")) + sorted(folder.glob("*/automation.json"))
                     if source == "Codex" else sorted(folder.glob("**/*")))[:200]
        except OSError:
            continue
        for path in paths:
            try:
                if path.suffix.lower() == ".json":
                    value = json.loads(path.read_text(encoding="utf-8"))
                    rows = value if isinstance(value, list) else [value]
                elif path.suffix.lower() == ".toml":
                    import tomllib
                    rows = [tomllib.loads(path.read_text(encoding="utf-8"))]
                elif path.suffix.lower() == ".md":
                    fields, body = _frontmatter(path.read_text(encoding="utf-8"))
                    rows = [{"name": fields.get("name") or path.parent.name, "description": fields.get("description", ""), "prompt": body[:600]}]
                else:
                    continue
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            for index, row in enumerate(rows):
                if isinstance(row, dict):
                    item = _definition(row, source, f"{path.stem}{index}")
                    if item:
                        found.append(item)
    unique: dict[str, dict] = {}
    for item in found:
        unique.setdefault(item["key"], item)
    return list(unique.values())


def schedule_chips(schedules: list[dict], agents: list[dict], now: float) -> list[dict]:
    """One entry per scheduled task: running now, or when it last ran."""
    chips: dict[str, dict] = {}
    for schedule in schedules:
        chips[schedule["key"]] = {"key": schedule["key"], "title": schedule.get("title") or schedule.get("prompt") or "定时任务",
                                  "running": False, "provider": "", "last_at": schedule.get("last_at") or 0.0,
                                  "next_at": schedule.get("next_at") or 0.0}
    for agent in agents:
        key = agent.get("schedule")
        if not key or not agent["live"]:
            continue
        chip = chips.setdefault(key, {"key": key, "title": agent.get("title") or "定时任务", "running": False,
                                      "provider": "", "last_at": 0.0, "next_at": 0.0})
        chip.update(running=True, provider=agent["provider"], last_at=max(chip["last_at"], agent["at"]))
    return sorted(chips.values(), key=lambda chip: (not chip["running"], -chip["last_at"]))


# ----- grouping ------------------------------------------------------------------

@dataclass(frozen=True)
class Cluster:
    key: str
    members: tuple[dict, ...]
    manual: bool = False

    @property
    def live(self) -> bool:
        return any(member["live"] for member in self.members)


def _bigrams(text: str) -> set:
    text = re.sub(r"\s+", "", text.lower())
    return {text[index:index + 2] for index in range(max(0, len(text) - 1))}


def agent_title(agent: dict) -> str:
    title = agent.get("title") or ""
    if title:
        return title
    if agent.get("role"):
        return str(agent["role"])
    if agent.get("folder"):
        return f"{agent['folder']} 项目"
    model = str(agent.get("model") or "").split("/")[-1]
    return f"{agent.get('source') or '会话'}" + (f" · {model}" if model else "")


def group_title(members: tuple[dict, ...]) -> str:
    """The orchestrator's request when known, else the member most similar to the rest."""
    keys = {member["key"] for member in members}
    for member in members:
        if not member.get("parent") or member["parent"] not in keys:
            if any(other.get("parent") == member["key"] for other in members) and member.get("title"):
                return member["title"]
    titled = [member for member in members if member.get("title")]
    if not titled:
        return agent_title(members[0])
    grams = [_bigrams(member["title"]) for member in titled]
    def score(index):
        return sum(len(grams[index] & other) / (len(grams[index] | other) or 1) for other in grams)
    return titled[max(range(len(titled)), key=lambda index: (score(index), -len(titled[index]["title"])))]["title"]


HELPER_MARK = re.compile(r"subagent|sub_agent|guardian|review|compact|memory", re.I)


def _anonymous(agent: dict) -> bool:
    return not (agent.get("title") or agent.get("thread_title") or agent.get("role") or agent.get("folder") or agent.get("parent"))


def _helpers(agents: list[dict]) -> list[tuple[dict, dict]]:
    """Side threads one client process starts for itself (title generation,
    approval reviews, compaction, sub-agents whose parent link is missing).
    They have no request or folder of their own, or are marked as a
    sub-agent without a parent; each joins the main session of the same
    client (same folder when known) that was active most recently."""
    hosts = [agent for agent in agents if not agent.get("parent") and not _anonymous(agent) and
             not HELPER_MARK.search(str(agent.get("origin") or ""))]
    pairs = []
    for agent in agents:
        if agent.get("parent") or agent in hosts:
            continue
        orphan = bool(HELPER_MARK.search(str(agent.get("origin") or "")))
        if not (_anonymous(agent) or orphan):
            continue
        same = [host for host in hosts if host["provider"] == agent["provider"] and host["key"] != agent["key"] and
                (not agent.get("folder") or host.get("folder") == agent["folder"]) and
                (not agent.get("pane") or host.get("pane") in (None, "", agent["pane"]))]
        if same:
            pairs.append((agent, max(same, key=lambda host: (host["live"], host.get("at") or 0))))
    return pairs


class AgentGroups:
    """User-made groups plus automatic parent/sub-agent links."""

    def __init__(self, path: Path | None = None):
        self.path = path or GROUPS_PATH
        self.lock = threading.Lock()
        self.groups: dict[str, dict] = {}
        self.detached: dict[str, float] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.groups = {str(key): {"members": list(value.get("members") or []), "used": float(value.get("used") or 0)}
                           for key, value in (data.get("groups") or {}).items()}
            self.detached = {str(key): float(value) for key, value in (data.get("detached") or {}).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def _save(self):
        now = time.time()
        self.groups = {key: value for key, value in self.groups.items() if value["used"] > now - GROUP_KEEP_SECONDS and len(value["members"]) > 1}
        self.detached = {key: value for key, value in self.detached.items() if value > now - GROUP_KEEP_SECONDS}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"groups": self.groups, "detached": self.detached}, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            log.warning("could not save agent groups", exc_info=True)

    def compose(self, agents: list[dict]) -> list[Cluster]:
        with self.lock:
            parent = {agent["key"]: agent["key"] for agent in agents}

            def root(key):
                parent.setdefault(key, key)
                while parent[key] != key:
                    parent[key] = parent[parent[key]]
                    key = parent[key]
                return key

            def join(a, b):
                parent[root(a)] = root(b)

            owner = {}
            for gid, group in self.groups.items():
                for member in group["members"]:
                    owner[member] = gid
                    join(member, "group:" + gid)
            for agent in agents:
                if agent.get("parent") and agent["key"] not in self.detached and agent["key"] not in owner:
                    join(agent["key"], agent["parent"] if agent["parent"] not in owner else "group:" + owner[agent["parent"]])
            # One herdr pane is one agent process: everything it ran is one bubble.
            panes: dict[str, str] = {}
            for agent in agents:
                pane = agent.get("pane")
                if pane and agent["key"] not in self.detached and agent["key"] not in owner:
                    if pane in panes:
                        join(agent["key"], panes[pane])
                    else:
                        panes[pane] = agent["key"]
            for agent, host in _helpers(agents):
                if agent["key"] not in self.detached and agent["key"] not in owner:
                    join(agent["key"], host["key"])
            buckets: dict[str, list[dict]] = {}
            for agent in agents:
                buckets.setdefault(root(agent["key"]), []).append(agent)
            clusters = []
            for members in buckets.values():
                gid = next((owner[m["key"]] for m in members if m["key"] in owner), None)
                pane = next((m["pane"] for m in members if m.get("pane")), "")
                if gid:
                    key = "group:" + gid
                elif pane and len(members) > 1:
                    key = "pane:" + pane
                elif len(members) > 1:
                    key = "auto:" + min(m.get("parent") or m["key"] for m in members)
                else:
                    key = members[0]["key"]
                clusters.append(Cluster(key, tuple(members), bool(gid)))
            clusters.sort(key=lambda c: (not c.live, -max(m["at"] for m in c.members)))
            return clusters

    def merge(self, first: Cluster, second: Cluster) -> str:
        members = sorted({m["key"] for m in first.members + second.members})
        with self.lock:
            for gid in [key for key, group in self.groups.items() if set(group["members"]) & set(members)]:
                old = self.groups.pop(gid)
                members = sorted(set(members) | set(old["members"]))
            gid = hashlib.sha1("|".join(members).encode()).hexdigest()[:10]
            self.groups[gid] = {"members": members, "used": time.time()}
            for member in members:
                self.detached.pop(member, None)
            self._save()
        return "group:" + gid

    def split(self, cluster: Cluster) -> list[str]:
        with self.lock:
            if cluster.key.startswith("group:"):
                self.groups.pop(cluster.key[6:], None)
            now = time.time()
            for member in cluster.members:
                if member.get("parent"):
                    self.detached[member["key"]] = now
            self._save()
        return [member["key"] for member in cluster.members]


# ----- optional summarizer ----------------------------------------------------------

def is_mimo_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "xiaomimimo.com" or host.endswith(".xiaomimimo.com")


def summary_config() -> dict | None:
    """OpenAI-compatible chat completions endpoint, opt-in only."""
    config = {"url": os.environ.get("CIRCLOW_SUMMARY_URL", ""), "key": os.environ.get("CIRCLOW_SUMMARY_KEY", ""),
              "model": os.environ.get("CIRCLOW_SUMMARY_MODEL", "")}
    if not config["url"]:
        try:
            stored = json.loads(SUMMARY_CONFIG.read_text(encoding="utf-8"))
            if stored.get("enabled") is False:
                return None
            config.update({key: str(stored.get(key) or "") for key in config})
        except (OSError, ValueError, AttributeError):
            return None
    if not config["url"] or not config["model"]:
        return None
    if is_mimo_url(config["url"]) and not config["key"]:
        return None
    url = config["url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return {**config, "url": url}


SINGLE_PROMPT = ("你为本地编程 agent 的状态气泡写标签。根据请求和最近动作，只输出 JSON："
                 '{"title":"不超过10个字的任务名","doing":"不超过10个字的当前动作"}。中文名词短语，不要标点。')
GROUP_PROMPT = ("这些编程 agent 正在并行完成同一个任务的不同部分。只输出 JSON："
                '{"title":"不超过10个字的总任务名","doing":"不超过12个字的整体进展","parts":{"<id>":"不超过6个字的分工"}}。中文，不要标点。')


def _parse_reply(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", flags=re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    result = {key: shorten(clean(str(value[key])) or str(value[key]).strip(), 14) for key in ("title", "doing") if value.get(key)}
    if isinstance(value.get("parts"), dict):
        result["parts"] = {str(k): shorten(str(v).strip(), 8) for k, v in value["parts"].items() if v}
    return result


class Summarizer:
    """One background request at a time; results cached by their exact input."""

    def __init__(self, config: dict | None = None, opener=None):
        self.config = config
        self.opener = opener or urllib.request.urlopen
        self.results: dict[str, dict] = {}
        self.pending: set[str] = set()
        self.queue: queue.Queue = queue.Queue()
        self.failed_until = 0.0
        self.thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config)

    @staticmethod
    def signature(cluster: Cluster) -> str:
        parts = [f"{m['key']}:{m.get('request') or m.get('title')}" for m in cluster.members]
        return hashlib.sha1(("|".join(sorted(parts)) + str(len(cluster.members) > 1)).encode()).hexdigest()

    def lookup(self, cluster: Cluster) -> tuple[dict, bool]:
        """(result so far, recognising). Queues work for unseen inputs."""
        if not self.enabled:
            return {}, False
        signature = self.signature(cluster)
        if signature in self.results:
            return self.results[signature], False
        if signature not in self.pending and time.time() >= self.failed_until:
            if not any(m.get("request") or m.get("title") for m in cluster.members):
                return {}, False
            self.pending.add(signature)
            self.queue.put((signature, cluster))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name="agent-summary", daemon=True)
                self.thread.start()
            return self.results.get(signature, {}), signature not in self.results
        return {}, signature in self.pending

    def _payload(self, cluster: Cluster) -> dict:
        items = [{"id": m["key"][-6:], "request": (m.get("request") or m.get("title") or "")[:400],
                  "folder": m.get("folder") or "", "doing": m.get("doing") or "", "role": m.get("role") or ""}
                 for m in cluster.members[:6]]
        grouped = len(cluster.members) > 1
        payload = {"model": self.config["model"], "temperature": 0.2, "max_tokens": 160, "stream": False,
                "messages": [{"role": "system", "content": GROUP_PROMPT if grouped else SINGLE_PROMPT},
                             {"role": "user", "content": json.dumps(items if grouped else items[0], ensure_ascii=False)}]}
        if is_mimo_url(self.config["url"]):
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            payload["thinking"] = {"type": "disabled"}
        return payload

    def request(self, cluster: Cluster) -> dict:
        body = json.dumps(self._payload(cluster), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.get("key"):
            if is_mimo_url(self.config["url"]):
                headers["api-key"] = self.config["key"]
            else:
                headers["Authorization"] = f"Bearer {self.config['key']}"
        request = urllib.request.Request(self.config["url"], data=body, headers=headers, method="POST")
        with self.opener(request, timeout=12) as response:
            reply = json.loads(response.read().decode("utf-8"))
        text = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        result = _parse_reply(text)
        if "parts" in result:
            short = {m["key"][-6:]: m["key"] for m in cluster.members}
            result["parts"] = {short.get(k, k): v for k, v in result["parts"].items()}
        return result

    def _work(self):
        while True:
            try:
                signature, cluster = self.queue.get(timeout=30)
            except queue.Empty:
                return
            try:
                self.results[signature] = self.request(cluster)
            except Exception as error:
                log.info("agent summary unavailable: %s", type(error).__name__)
                self.failed_until = time.time() + 300
                self.results[signature] = {}
            finally:
                self.pending.discard(signature)
            if len(self.results) > 400:
                for stale in list(self.results)[:200]:
                    self.results.pop(stale, None)


def describe(cluster: Cluster, summary: dict | None = None) -> tuple[str, str, str]:
    """(bubble title, line under the bubble, longer hover detail)."""
    summary = summary or {}
    members = cluster.members
    if len(members) == 1:
        agent = members[0]
        title = summary.get("title") or agent_title(agent)
        doing = agent.get("doing") or (summary.get("doing") if agent["live"] else "") or ("生成中" if agent["live"] else "刚完成")
        detail = agent.get("title") or agent.get("thread_title") or title
        return title, doing, detail
    title = summary.get("title") or group_title(members)
    live = [m for m in members if m["live"]]
    parts = summary.get("parts") or {}
    roles = []
    for member in live or members:
        role = parts.get(member["key"]) or member.get("role") or member.get("doing") or ""
        if role and role not in roles and role != "刚完成":
            roles.append(role)
    doing = summary.get("doing") or f"{len(live) or len(members)} 路并行" + (f" · {'、'.join(roles[:2])}" if roles else "")
    detail = " / ".join(shorten(agent_title(m), 12) for m in members[:4])
    return title, doing, detail
