"""Passive, numeric-only readers for local AI generation records.

Antigravity's numeric wire fields follow the independently verified Tokscale
database layout. No credential files, model prompts, or response text are saved;
the short task notes behind the agent bubbles (agent_tasks) live in memory only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

import providers
from herdr_source import Herdr
from agent_tasks import AgentNotes, RECUR_SPREAD, clean as clean_request, read_schedules, tool_action


PERFORMANCE_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray/performance.json"
RECENT_SECONDS = 86400          # Only files touched within a day can matter.
FORGET_SECONDS = 2 * 86400      # Readers idle this long are released.
SLOW_DISCOVERY_SECONDS = 30     # Process list and full-history globbing.
PROVIDER_NAMES = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini", "claude": "Claude Code", "mimo": "Xiaomi MiMo", "other": "其他模型"}
CLAUDE_IDLE_FLUSH = 30          # Emit an unfinished Claude message after this much silence.
CLAUDE_STALE_ACTIVITY = 90      # Drop "generating" when a session went quiet this long.


def number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def timestamp(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    result = number(value)
    if result is not None and result > 10_000_000_000:
        result /= 1000
    return result


def provider_for(model: str) -> str:
    model = model.lower()
    if "mimo" in model:
        return "mimo"
    if "claude" in model or "anthropic" in model:
        return "claude"
    if "gemini" in model:
        return "gemini"
    if "grok" in model:
        return "grok"
    if "gpt" in model and "gpt-oss" not in model or "codex" in model:
        return "codex"
    return "other"


def logged_model(body: str) -> str:
    match = re.search(r"model=([A-Za-z0-9_.:/+-]{1,80})", body or "")
    return match.group(1) if match else ""


def key(*parts: Any) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


def readonly_db(path: Path):
    # A client mid-checkpoint (MiMo Code, Codex) briefly holds the WAL lock;
    # 0.3 s often failed the first read after it started. Still short enough
    # for the 1 s TPS loop, and a locked database is simply retried next round.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.5)


def proto_fields(data: bytes) -> dict[int, list[int | bytes]]:
    """Bounded protobuf wire reader; opaque strings are never interpreted."""
    result: dict[int, list[int | bytes]] = {}
    position = 0
    if len(data) > 8_000_000:
        raise ValueError("protobuf record too large")

    def varint() -> int:
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError("truncated varint")
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("invalid varint")

    while position < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError("invalid field")
        if wire == 0:
            value: int | bytes = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else 8 if wire == 1 else 4
            if position + length > len(data):
                raise ValueError("truncated field")
            value = data[position:position + length]
            position += length
        else:
            raise ValueError("unsupported wire type")
        result.setdefault(field, []).append(value)
    return result


def proto_message(data: bytes, field: int) -> bytes:
    value = proto_fields(data).get(field, [b""])[0]
    return value if isinstance(value, bytes) else b""


def proto_int(data: bytes, field: int, default=0) -> int:
    value = proto_fields(data).get(field, [default])[0]
    return value if isinstance(value, int) and value < 2**63 else default


def proto_seconds(data: bytes, *, epoch=False) -> float | None:
    if not data:
        return None
    seconds = proto_int(data, 1)
    nanos = proto_int(data, 2)
    if nanos >= 1_000_000_000 or epoch and not 1_000_000_000 < seconds < 4_000_000_000:
        return None
    value = seconds + nanos / 1e9
    return value if epoch or value <= 86400 else None


log = logging.getLogger(__name__)
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
MIN_TPS_TOKENS = 16      # shorter replies give meaningless rates
MIN_TPS_SECONDS = 0.3
TPS_GROUP_SECONDS = 90
HOLD_TPS_SECONDS = 1800  # while still working, show the last rate measured this recently   # replies finished this close together are averaged


@dataclass(frozen=True)
class Observation:
    identity: str
    source: str
    session: str
    provider: str
    model: str
    ended_at: float
    output_tokens: int
    generation_seconds: float | None = None
    basis: str = "缺少生成耗时"
    count_in_rate: bool = True
    native_tps: float | None = None
    input_tokens: int | None = None      # uncached input (for MiMo Credit accounting)
    cached_tokens: int | None = None     # cache-hit input


class JsonlReader:
    def __init__(self, path: Path, initial_bytes=1_500_000):
        self.path = path
        self.position = max(0, path.stat().st_size - initial_bytes)
        self.skip_first = self.position > 0
        self.pending = b""
        self.skipped = 0

    def read(self):
        size = self.path.stat().st_size
        if size < self.position:
            self.position = 0
            self.pending = b""
            self.skip_first = False
        # Idle histories vastly outnumber active streams. Avoid opening each
        # file and allocating an 8 MB read buffer when already at EOF.
        if size == self.position:
            return
        with self.path.open("rb") as stream:
            stream.seek(self.position)
            chunk = stream.read(8_000_000)
            self.position = stream.tell()
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        if len(self.pending) > 8_000_000:
            self.pending = b""
            self.skip_first = True
            self.skipped += 1
        if self.skip_first and lines:
            lines.pop(0)
            self.skip_first = False
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value
            except (ValueError, UnicodeDecodeError):
                self.skipped += 1


class TpsMonitor:
    def __init__(self, home: Path | None = None, store: Any = None):
        self.home = home or Path.home()
        self.codex_root = self.home / ".codex" if home else Path(os.environ.get("CODEX_HOME", self.home / ".codex"))
        self.grok_root = self.home / ".grok" if home else Path(os.environ.get("GROK_HOME", self.home / ".grok"))
        self.claude_root = self.home / ".claude" if home else providers.claude_home()
        self.claude_pending: dict[str, dict[str, Any]] = {}
        self.claude_last_input: dict[str, float] = {}   # session -> last user / tool-result record
        self.claude_request_starts: dict[tuple[str, str], float] = {}
        self.claude_desktop_last_use = 0.0
        self.home_override = home is not None
        self.mimocode_paths: list[Path] | None = None
        self.mimocode_scanned = 0.0
        self.mimocode_cursor: dict[Path, float] = {}
        self.antigravity_bridge = (self.home / "AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/codex-grok-bridge/app/src/antigravity.mjs").is_file()
        self.grok_scanned: dict[Path, float] = {}       # Grok project folder -> its mtime when last listed
        self.grok_paths: set[Path] = set()
        self.antigravity_cache: tuple[float, list] = (0.0, [])
        self.readers: dict[Path, JsonlReader] = {}
        self.context: dict[str, dict[str, Any]] = {}
        self.events: deque[Observation] = deque()
        self.added_total = 0               # observations ever added (consumers read only the new tail)
        self.source_seen: dict[tuple[str, str], float] = {}   # (provider, source) -> newest ended_at
        self.seen: set[str] = set()
        self.completed_at: dict[str, float] = {}
        self.client_stream_starts: dict[str, list[float]] = {}
        self.client_stream_ends: dict[str, list[float]] = {}   # output items finished streaming
        self.file_sessions: dict[Path, str] = {}
        self.started_at = time.time()
        self.grok_counters: dict[tuple[str, str], int] = {}
        self.grok_native_reader = JsonlReader(self.grok_root / "logs/unified.jsonl") if (self.grok_root / "logs/unified.jsonl").is_file() else None
        self.activity: dict[str, tuple[str, float, str]] = {}
        self.last_discovery = 0.0
        self.last_log_id = 0
        self.database_versions: dict[Path, tuple[int, int]] = {}
        self.health: dict[str, dict[str, Any]] = {}
        self.legacy_totals: dict[str, int] = {}
        # Latest Codex rate-limit figures written into session records after
        # each response (from the server's response headers): (moment, dict).
        self.codex_rate_limits: tuple[float, dict] | None = None
        self.codex_limit_hit_at = 0.0       # a "usage limit reached" error was recorded
        self.software: list[dict[str, Any]] = []
        self.last_slow_discovery = 0.0
        self.usage_versions: dict[Path, tuple[int, int]] = {}
        self.notes = AgentNotes()          # what each agent is doing; memory only
        self.thread_columns: set[str] | None = None
        self.schedules: list[dict[str, Any]] = []       # scheduled task definitions + recurring requests
        self.codex_recurring: dict[str, float] = {}
        # herdr panes group one agent process's sessions; off for test homes.
        self.herdr = Herdr(enabled=False if home is not None else None)
        # Output records saved before the last exit (usage_store). They start
        # the day's history; re-read log tails skip them by identity, and they
        # are not counted in added_total, so ledgers that already took them
        # (MiMo) do not take them again.
        self.store = store
        if store is not None:
            for observation in store.restore(self.started_at):
                self.seen.add(observation.identity)
                self.events.append(observation)
                pair = (observation.provider, observation.source)
                if self.source_seen.get(pair, 0.0) < observation.ended_at:
                    self.source_seen[pair] = observation.ended_at

    def add(self, observation: Observation, now: float):
        if observation.identity in self.seen or not now - 86400 <= observation.ended_at <= now + 5:
            return
        if observation.output_tokens < 0 or observation.output_tokens > 100_000_000:
            return
        self.seen.add(observation.identity)
        self.events.append(observation)
        self.added_total += 1
        if self.store is not None:
            self.store.note(observation)
        pair = (observation.provider, observation.source)
        if self.source_seen.get(pair, 0.0) < observation.ended_at:
            self.source_seen[pair] = observation.ended_at

    @staticmethod
    def _recent(path: Path, now: float) -> bool:
        try:
            return path.stat().st_mtime > now - RECENT_SECONDS
        except OSError:
            return False

    def _forget_idle(self, now: float):
        """Release readers and per-session state that can no longer matter."""
        for path in list(self.readers):
            try:
                idle = path.stat().st_mtime < now - FORGET_SECONDS
            except OSError:
                idle = True
            if idle:
                self.readers.pop(path, None)
                self.file_sessions.pop(path, None)
                self.usage_versions.pop(path, None)
        for streams in (self.client_stream_starts, self.client_stream_ends):
            for session, points in list(streams.items()):
                if not points or points[-1] < now - 600:
                    streams.pop(session, None)
        for session, moment in list(self.completed_at.items()):
            if moment < now - FORGET_SECONDS:
                self.completed_at.pop(session, None)
        # One entry per session ever seen; without pruning these grew for the
        # life of the process (every Claude session, every 30-min Grok quota read).
        for session, moment in list(self.claude_last_input.items()):
            if moment < now - FORGET_SECONDS:
                self.claude_last_input.pop(session, None)
        self.claude_request_starts = {key: moment for key, moment in self.claude_request_starts.items()
                                     if moment >= now - FORGET_SECONDS}
        grok_live = {path.parent.name for path in self.readers if path.name == "updates.jsonl"}
        for counter in [counter for counter in self.grok_counters if counter[0] not in grok_live]:
            self.grok_counters.pop(counter, None)
        live = set(self.file_sessions.values()) | set(self.activity) | set(self.client_stream_starts)
        self.notes.prune(live, now)
        for session in list(self.context):
            if session not in live and session not in self.completed_at:
                self.context.pop(session, None)
                self.legacy_totals.pop(session, None)

    def _register(self, path: Path, session: str, source: str, model="", provider=""):
        if path not in self.readers and path.is_file():
            self.readers[path] = JsonlReader(path)
        self.context.setdefault(session, {}).update({"source": source, "model": model, "provider": provider})
        self.file_sessions[path] = session

    def discover(self, now: float):
        state = self.codex_root / "state_5.sqlite"
        if state.is_file():
            try:
                with closing(readonly_db(state)) as db:
                    rows = db.execute("SELECT id,rollout_path,model,model_provider FROM threads ORDER BY updated_at DESC LIMIT 512").fetchall()
                for session, filename, model, provider in rows:
                    # Registering all 512 threads used to parse ~1.5 MB of
                    # each history on the first poll; only recent ones matter.
                    if filename and (Path(filename) in self.readers or self._recent(Path(filename), now)):
                        self._register(Path(filename), session, "Codex", model or "", provider or "")
                self._codex_titles(state, now)
                self.health["Codex"] = {"status": "已接入", "sessions": len(rows)}
            except (OSError, sqlite3.Error) as error:
                self.health["Codex"] = {"status": "读取失败", "error": type(error).__name__}
        elif now - self.last_slow_discovery >= SLOW_DISCOVERY_SECONDS:
            for path in self.codex_root.glob("sessions/*/*/*/rollout-*.jsonl"):
                if self._recent(path, now):
                    # Same thread id the stream logs use, not the "HH-MM-SS-<id>" tail of
                    # the file name (which made one session show up as two bubbles).
                    found = THREAD_ID.findall(path.stem)
                    self._register(path, found[-1] if found else path.stem.split("T", 1)[-1], "Codex")
        grok_paths = self._grok_sessions(now)
        for path in grok_paths:
            self._register(path, path.parent.name, "Grok CLI", "grok")
            if path.with_name("events.jsonl").is_file():
                self._register(path.with_name("events.jsonl"), path.parent.name, "Grok CLI", "grok")
        self.health["Grok CLI"] = {"status": "已接入" if grok_paths else "未发现记录", "sessions": len(grok_paths)}
        self._discover_claude(now)
        if now - self.last_slow_discovery >= SLOW_DISCOVERY_SECONDS:
            # Walking every process (and node command lines) every 2 s was the
            # single most expensive thing this app did while idle.
            self.software = running_software()
            self._forget_idle(now)
            self._refresh_schedules(state, now)
            self.last_slow_discovery = now
        self.last_discovery = now

    def _grok_sessions(self, now: float) -> list[Path]:
        """Recent Grok sessions (sessions/<project>/<id>/updates.jsonl). A
        project folder's mtime changes when a session folder is added, so only
        folders changed since their last listing are listed again; listing all
        ~1,800 sessions every two seconds was a fifth of this loop's time."""
        sessions = self.grok_root / "sessions"
        try:
            projects = [folder for folder in sessions.iterdir() if folder.is_dir()]
        except OSError:
            self.grok_paths = set()
            return []
        seen = set()
        for folder in projects:
            try:
                stamp = folder.stat().st_mtime
            except OSError:
                continue
            seen.add(folder)
            if self.grok_scanned.get(folder) == stamp:
                continue
            self.grok_scanned[folder] = stamp
            self.grok_paths = {path for path in self.grok_paths if path.parent.parent != folder}
            self.grok_paths.update(path for path in folder.glob("*/updates.jsonl") if path in self.readers or self._recent(path, now))
        for folder in [folder for folder in self.grok_scanned if folder not in seen]:
            self.grok_scanned.pop(folder, None)
            self.grok_paths = {path for path in self.grok_paths if path.parent.parent != folder}
        # Sessions that went quiet since their folder was listed drop out as
        # readers are forgotten; ones still open stay registered.
        return sorted(path for path in self.grok_paths if path in self.readers or self._recent(path, now))

    def _refresh_schedules(self, state: Path, now: float):
        if state.is_file():
            try:
                with closing(readonly_db(state)) as db:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                    text = [name for name in ("first_user_message", "title") if name in columns]
                    if text and "created_at" in columns:
                        rows = db.execute(f"SELECT {','.join(text)},created_at FROM threads ORDER BY created_at DESC LIMIT 2000").fetchall()
                        runs: dict[str, list[float]] = {}
                        for row in rows:
                            prompt = next((cleaned for value in row[:-1] if value
                                           and (cleaned := clean_request(str(value)))), "")
                            started = timestamp(row[-1])
                            if prompt and started and started > now - 7 * 86400:
                                runs.setdefault(prompt, []).append(started)
                        self.codex_recurring = {prompt: max(starts) for prompt, starts in runs.items()
                                                if len(starts) > 1 and max(starts) - min(starts) >= RECUR_SPREAD}
            except sqlite3.Error:
                pass
        recurring = {**self.codex_recurring, **self.notes.recurring()}
        schedules = read_schedules(self.codex_root, self.claude_root)
        known = [item["prompt"] for item in schedules]
        for prompt, last in recurring.items():
            if prompt not in known:
                schedules.append({"key": "recur:" + prompt, "title": prompt, "prompt": prompt, "source": "重复运行", "last_at": last, "next_at": 0.0})
        self.schedules = schedules

    def _codex_titles(self, state: Path, now: float):
        """Thread title, folder and spawning parent of live Codex threads."""
        live = [session for session, note in self.notes.notes.items()
                if self.context.get(session, {}).get("source") == "Codex" and
                (session in self.activity or note.active_at > now - 180)]
        if not live:
            return
        try:
            self._read_codex_titles(state, live)
        except sqlite3.Error:
            pass

    def _read_codex_titles(self, state: Path, live: list):
        with closing(readonly_db(state)) as db:
            if self.thread_columns is None:
                self.thread_columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            wanted = [name for name in ("title", "cwd", "first_user_message", "source", "agent_nickname", "agent_role") if name in self.thread_columns]
            if not wanted:
                return
            marks = ",".join("?" * len(live))
            rows = db.execute(f"SELECT id,{','.join(wanted)} FROM threads WHERE id IN ({marks})", live).fetchall()
        for row in rows:
            value = dict(zip(wanted, row[1:]))
            parent = _find_text(value.get("source"), "parent_thread_id")
            self.notes.describe(row[0], title=str(value.get("title") or value.get("first_user_message") or ""),
                                cwd=str(value.get("cwd") or ""), parent=parent,
                                role=str(value.get("agent_nickname") or value.get("agent_role") or ""),
                                origin=str(value.get("source") or "")[:200])
            note = self.notes.note(row[0])
            if not note.first:
                note.first = clean_request(str(value.get("first_user_message") or value.get("title") or ""))

    def _discover_claude(self, now: float):
        """Claude Code transcripts: ~/.claude/projects/<project>/<session>.jsonl
        (+ <session>/subagents/*.jsonl).  A project folder's mtime changes when
        a session file is created, so quiet folders are only rescanned slowly."""
        if not self.home_override:
            import claude_status
            self.claude_desktop_last_use = claude_status.recent_desktop_usage(now)
        projects = self.claude_root / "projects"
        if not projects.is_dir():
            return
        slow = now - self.last_slow_discovery >= SLOW_DISCOVERY_SECONDS
        count = 0
        try:
            folders = [folder for folder in projects.iterdir() if folder.is_dir()]
        except OSError:
            self.health["Claude Code"] = {"status": "读取失败"}
            return
        for folder in folders:
            if not (slow or self._recent(folder, now)):
                continue
            paths = [(path, path.stem) for path in folder.glob("*.jsonl")]
            paths += [(path, path.parent.parent.name) for path in folder.glob("*/subagents/*.jsonl")]
            for path, session in paths:
                if path in self.readers or self._recent(path, now):
                    self._register(path, session, "Claude Code", "", "claude")
                    count += 1
        known = sum(1 for path in self.readers if self.context.get(self.file_sessions.get(path, ""), {}).get("source") == "Claude Code")
        self.health["Claude Code"] = {"status": "已接入" if known or count else "未发现记录", "sessions": max(known, count)}

    def _claude_record(self, record: dict, session: str, now: float):
        """Numbers only: message id, model, output token count, timestamps."""
        moment = timestamp(record.get("timestamp"))
        if moment is None:
            return
        kind = record.get("type")
        if kind == "api-request" and record.get("id"):
            self.claude_request_starts[(session, str(record["id"]))] = moment
            if moment > now - 120:
                model = (record.get("params") or {}).get("model", "")
                provider = provider_for(model)
                self.activity[session] = (provider if provider != "other" else "claude", moment, "请求中")
            return
        if kind not in ("assistant", "user"):
            return
        # Any later record means earlier messages of this session are final.
        message = record.get("message") or {}
        current = message.get("id") if kind == "assistant" else None
        for mid, pending in list(self.claude_pending.items()):
            if pending["session"] == session and mid != current:
                self._claude_emit(mid, now)
        if kind == "user":
            self.claude_last_input[session] = moment
            if moment > now - 120:
                self.activity[session] = ("claude", moment, "请求中")
            return
        model = message.get("model") or ""
        usage = message.get("usage") or {}
        output = number(usage.get("output_tokens"))
        if output is None or not current or model == "<synthetic>":
            return
        pending = self.claude_pending.setdefault(current, {
            "session": session, "model": model, "last": moment, "output": 0, "stop": None, "seen_at": now,
            "input": 0, "cached": 0, "requested": self.claude_last_input.get(session),
        })
        request_start = self.claude_request_starts.get((session, str(record.get("requestRef") or "")))
        if request_start is not None:
            pending["api_requested"] = request_start
        self.context.setdefault(session, {}).update({"model": model, "provider": provider_for(model)})
        uncached = (number(usage.get("input_tokens"), 0) or 0) + (number(usage.get("cache_creation_input_tokens"), 0) or 0)
        pending["input"] = max(pending["input"], int(uncached))
        pending["cached"] = max(pending["cached"], int(number(usage.get("cache_read_input_tokens"), 0) or 0))
        # Content blocks of one message are written as separate lines; the
        # usage on the last one is final, so keep the largest count seen.
        pending["last"] = max(pending["last"], moment)
        pending["output"] = max(pending["output"], int(output))
        pending["stop"] = message.get("stop_reason") or pending["stop"]
        pending["seen_at"] = now
        if moment > now - 120:
            kind = provider_for(model)
            self.activity[session] = (kind if kind != "other" else "claude", moment, "生成中")

    def _claude_emit(self, mid: str, now: float):
        pending = self.claude_pending.pop(mid, None)
        if not pending or pending["output"] <= 0:
            return
        provider = provider_for(pending["model"])
        provider = provider if provider != "other" else "claude"   # Claude Code sessions default to Claude
        tps, basis, seconds = None, "Claude Code 输出计数（缺少本次 API 请求时间）", None
        requested = pending.get("api_requested") or (pending.get("requested") if provider == "mimo" else None)
        if requested and pending["output"] >= MIN_TPS_TOKENS:
            # Claude Code routed to MiMo: the transcript has no streaming times,
            # only when the request input was written and when the reply ended.
            elapsed = pending["last"] - requested
            if MIN_TPS_SECONDS <= elapsed <= 600:
                tps, seconds = round(pending["output"] / elapsed, 1), elapsed
                basis = ("Claude Code API 端到端速率（真实输出 token ÷ 本次 API 请求至输出完成，含首字延迟）"
                         if pending.get("api_requested") else
                         "Claude Code 端到端速率（真实输出token数÷请求发出至回复结束，含首字延迟，偏低）")
        self.add(Observation(key("claude", mid), "Claude Code", pending["session"], provider, pending["model"],
                             pending["last"], pending["output"], seconds, basis, True, tps,
                             pending.get("input", 0), pending.get("cached", 0)), now)
        if pending["stop"] == "end_turn":
            if pending["last"] > now - 3:
                self.activity[pending["session"]] = (provider, pending["last"], "刚完成生成")
            elif self.activity.get(pending["session"], (None, 0))[1] <= pending["last"]:
                self.activity.pop(pending["session"], None)

    def _claude_flush(self, now: float):
        for mid, pending in list(self.claude_pending.items()):
            if pending["stop"] == "end_turn" or now - pending["seen_at"] >= CLAUDE_IDLE_FLUSH:
                self._claude_emit(mid, now)
        waiting = {pending["session"] for pending in self.claude_pending.values()}
        for session, (provider, moment, state) in list(self.activity.items()):
            claude_session = self.context.get(session, {}).get("source") == "Claude Code"
            if (provider == "claude" or claude_session) and state != "刚完成生成" and session not in waiting and now - moment > CLAUDE_STALE_ACTIVITY:
                self.activity.pop(session, None)

    def _mimocode_databases(self) -> list[Path]:
        roots = [self.home / ".local/share/mimocode"]
        if not self.home_override:
            for env in ("XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
                if os.environ.get(env):
                    roots.append(Path(os.environ[env]) / "mimocode")
        found = []
        for root in roots:
            try:
                found += [path for path in root.glob("mimocode*.db") if path.is_file()]
            except OSError:
                continue
        return found

    @staticmethod
    def _mimocode_spans(path: Path, message_ids: list) -> dict:
        """Generation seconds per message from its parts (OpenCode format):
        text / reasoning parts carry time.start / time.end; a tool part's
        state.time.start marks the end of streaming its arguments."""
        if not message_ids:
            return {}
        db = readonly_db(path)
        try:
            marks = ",".join("?" * len(message_ids))
            rows = db.execute(f"SELECT message_id, data FROM part WHERE message_id IN ({marks})", list(message_ids)).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            db.close()
        bounds: dict = {}
        for message_id, data in rows:
            try:
                part = json.loads(data)
            except (TypeError, ValueError):
                continue
            kind = part.get("type")
            if kind in ("text", "reasoning"):
                times = part.get("time") or {}
                start, end = timestamp(times.get("start")), timestamp(times.get("end"))
            elif kind == "tool":
                start = end = timestamp(((part.get("state") or {}).get("time") or {}).get("start"))
            else:
                continue
            if start is None or end is None or end < start:
                continue
            low, high = bounds.get(message_id, (start, end))
            bounds[message_id] = (min(low, start), max(high, end))
        return {message_id: high - low for message_id, (low, high) in bounds.items()}

    def _mimocode_titles(self, path: Path, sessions: set):
        """Session title, folder and parent (sub-agent) of recently written sessions."""
        sessions = [session for session in sessions if session in self.activity][:50]
        if not sessions:
            return
        db = readonly_db(path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(session)")}
            wanted = [name for name in ("title", "directory", "parent_id") if name in columns]
            if not wanted:
                return
            marks = ",".join("?" * len(sessions))
            rows = db.execute(f"SELECT id,{','.join(wanted)} FROM session WHERE id IN ({marks})", sessions).fetchall()
        except sqlite3.Error:
            return
        finally:
            db.close()
        for row in rows:
            value = dict(zip(wanted, row[1:]))
            title = str(value.get("title") or "")
            if re.match(r"^(New session|Child session)\b", title):
                title = ""
            self.notes.describe(str(row[0]), title=title, cwd=str(value.get("directory") or ""), parent=str(value.get("parent_id") or ""))

    def _mimocode(self, now: float):
        """MiMo Code (an OpenCode fork) stores each message as JSON in SQLite;
        read token counts of completed assistant messages only."""
        if self.mimocode_paths is None or now - self.mimocode_scanned >= SLOW_DISCOVERY_SECONDS:
            self.mimocode_paths = self._mimocode_databases()
            self.mimocode_scanned = now
        for path in self.mimocode_paths:
            try:
                version = (path.stat().st_mtime_ns, path.stat().st_size)
                wal = path.with_name(path.name + "-wal")
                if wal.exists():
                    version += (wal.stat().st_mtime_ns, wal.stat().st_size)
            except OSError:
                continue
            if self.database_versions.get(path) == version:
                continue
            self.database_versions[path] = version
            since = self.mimocode_cursor.get(path, (now - 86400) * 1000)
            newest = since
            db = readonly_db(path)
            try:
                rows = db.execute("SELECT id, session_id, time_updated, data FROM message WHERE time_updated > ? ORDER BY time_updated LIMIT 2000", (since,)).fetchall()
            finally:
                db.close()
            finished = []
            for ident, session, updated, data in rows:
                newest = max(newest, updated or 0)
                try:
                    message = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if message.get("role") != "assistant":
                    continue
                completed = timestamp(((message.get("time") or {}).get("completed")))
                tokens = message.get("tokens") or {}
                moment = timestamp(updated)
                if completed is None and moment and moment > now - 120:
                    self.activity[str(session)] = ("mimo" if provider_for(str(message.get("modelID") or "")) == "mimo" else "other", moment, "生成中")
                if completed is None or not tokens:
                    continue
                finished.append((ident, session, message, completed, tokens))
            spans = self._mimocode_spans(path, [item[0] for item in finished])
            for ident, session, message, completed, tokens in finished:
                model = str(message.get("modelID") or "")
                provider_id = str(message.get("providerID") or "").lower()
                provider = provider_for(model)
                if provider == "mimo" and "token-plan" not in provider_id and provider_id not in ("", "xiaomi"):
                    provider = "other"   # e.g. the free MiMo Auto channel: not plan Credits
                cache = tokens.get("cache") or {}
                output = (number(tokens.get("output"), 0) or 0) + (number(tokens.get("reasoning"), 0) or 0)
                tps, basis = None, "读取MiMo Code真实计数；该消息缺少流式时间"
                span = spans.get(ident)
                seconds = None
                if span and output >= MIN_TPS_TOKENS and span >= MIN_TPS_SECONDS:
                    tps, seconds = round(output / span, 1), span
                    basis = "MiMo Code 流式时间（真实输出token数÷首个输出至最后输出，不含首字延迟和工具执行）"
                self.add(Observation(key("mimocode", ident), "MiMo Code", str(session), provider, model, completed, int(output), seconds,
                                     basis, True, tps,
                                     int((number(tokens.get("input"), 0) or 0) + (number(cache.get("write"), 0) or 0)),
                                     int(number(cache.get("read"), 0) or 0)), now)
            self.mimocode_cursor[path] = newest
            self._mimocode_titles(path, {str(row[1]) for row in rows})
            for _, session, _, completed, _ in finished:
                if self.activity.get(str(session), (None, 0, None))[1] <= completed:
                    if completed > now - 3:
                        self.activity[str(session)] = ("mimo", completed, "刚完成生成")
                    else:
                        self.activity.pop(str(session), None)
        if self.mimocode_paths:
            self.health["MiMo Code"] = {"status": "已接入", "databases": len(self.mimocode_paths)}

    def _codex_logs(self, now: float):
        path = self.codex_root / "logs_2.sqlite"
        if not path.is_file():
            return
        with closing(readonly_db(path)) as db:
            maximum = db.execute("SELECT max(id) FROM logs").fetchone()[0] or 0
            begin = self.last_log_id or max(0, maximum - 20000)
            rows = db.execute("SELECT id,ts,ts_nanos,target,feedback_log_body,thread_id FROM logs WHERE id>? AND target='codex_core::stream_events_utils' ORDER BY id", (begin,)).fetchall()
        for _, seconds, nanos, target, body, session in rows:
            if not session:
                continue
            moment = seconds + nanos / 1e9
            if moment < now - 600:
                continue
            context = self.context.setdefault(session, {})
            observed = logged_model(body or "")
            model = str(context.get("model") or "")
            # Threads absent from the recent threads table still carry model=
            # in the client span. Without it, live GPT sessions become "other"
            # and the Codex icon never switches to its TPS number.
            if observed and (not model or provider_for(model) == "other" and provider_for(observed) != "other"):
                model = observed
                context["model"] = observed
                context.setdefault("source", "Codex")
            provider = provider_for(model)
            # These client stream events carry a thread ID. Outgoing UI delta
            # logs do not, so they cannot safely be matched across concurrent chats.
            match = re.search(r'Output item item_type="([^"]+)" item_id="([^"]+)"', body or "")
            if match and "handle_output_item_done:" in body:
                ends = self.client_stream_ends.setdefault(session, [])
                ends.append(moment)
                self.client_stream_ends[session] = [point for point in ends if point > now - 600]
            if match and "handle_output_item_done:" not in body:
                points = self.client_stream_starts.setdefault(session, [])
                points.append(moment)
                self.client_stream_starts[session] = [point for point in points if point > now - 600]
                if moment > self.completed_at.get(session, 0):
                    self.activity[session] = (provider, moment, "生成中")
        self.last_log_id = maximum

    def _codex_record(self, record: dict, session: str, now: float):
        moment = timestamp(record.get("timestamp"))
        if moment is None:
            return
        payload = record.get("payload") or {}
        context = self.context[session]
        kind = record.get("type")
        if kind == "response_item":
            item_type = payload.get("type")
            if item_type in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call", "image_generation_call", "computer_call"):
                context["response_has_tool_call"] = True
            if item_type == "reasoning":
                context["response_has_reasoning"] = True
            return
        if kind == "turn_context":
            context["model"] = str(payload.get("model") or context.get("model") or "unknown")
            return
        event = payload.get("type")
        if event == "task_started" and moment > now - 600:
            self.activity[session] = (provider_for(context.get("model", "")), moment, "客户端工作中")
        if event in ("task_complete", "turn_aborted", "error") and self.activity.get(session, (None, 0, None))[1] <= moment:
            self.activity.pop(session, None)
        if kind == "event_msg" and provider_for(context.get("model", "")) in ("codex", "other"):
            limits = payload.get("rate_limits") if event == "token_count" else None
            if isinstance(limits, dict) and limits.get("limit_id") in (None, "codex") and \
                    (self.codex_rate_limits is None or moment >= self.codex_rate_limits[0]):
                self.codex_rate_limits = (moment, limits)
            if event == "error" and moment > now - 600:
                text = str(payload.get("message") or "").lower()
                if "usage limit" in text or "rate limit" in text or "usage_limit" in text:
                    self.codex_limit_hit_at = max(self.codex_limit_hit_at, moment)
        if kind == "token_usage_record":
            if payload.get("thread_id") and payload["thread_id"] != session:
                return  # A fork's replay belongs to the original thread.
            usage = payload.get("usage") or {}
            output = number(usage.get("output_tokens"))
            if output is None:
                return
            response = payload.get("response_id")
            identity = key("response", response) if response else key("codex", session, moment, output)
            previous = self.completed_at.get(session, 0)
            model = context.get("model") or "unknown"
            bridge_copy = provider_for(model) == "gemini" and context.get("provider") == "grok" and self.antigravity_bridge
            source = "Codex · Gemini桥接回传" if bridge_copy else "Codex"
            client_tps = seconds = None
            basis = "客户端暂未取得完整流式计数"
            if provider_for(model) in ("codex", "mimo", "gemini"):
                points = [point for point in self.client_stream_starts.get(session, []) if previous < point <= moment]
                if points:
                    # End at the last output item that finished streaming; the
                    # usage record itself is written a little later.
                    ends = [point for point in self.client_stream_ends.get(session, []) if points[0] < point <= moment + 2]
                    end = max(ends) if ends else moment
                    if output >= MIN_TPS_TOKENS and end - points[0] >= MIN_TPS_SECONDS:
                        seconds = end - points[0]
                        client_tps = round(output / seconds, 1)
                        basis = ("客户端流式统计（真实输出token数÷首个输出项开始至最后输出项完成，含思考和工具调用输出）" if ends
                                 else "客户端流式统计（真实输出token数÷首个输出项至计数完成的时间，含思考和工具调用输出）")
            cached = number(usage.get("cached_input_tokens"), 0) or 0
            uncached = max(0, (number(usage.get("input_tokens"), 0) or 0) - cached)  # OpenAI input includes cache hits
            self.add(Observation(identity, source, session, provider_for(model), model, moment, int(output), seconds, basis, True, client_tps,
                                 int(uncached), int(cached)), now)
            context["response_has_tool_call"] = False
            context["response_has_reasoning"] = False
            self.completed_at[session] = max(moment, previous)
            if self.activity.get(session, (None, 0, None))[1] <= moment:
                if moment > now - 3:
                    self.activity[session] = (provider_for(model), moment, "刚完成生成")
                else:
                    self.activity.pop(session, None)
            context["modern_usage"] = True
        elif event == "token_count" and not context.get("modern_usage"):
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or {}
            total = number((info.get("total_token_usage") or {}).get("output_tokens"))
            output = number(usage.get("output_tokens"))
            if total is None or output is None:
                return
            previous = self.legacy_totals.get(session)
            self.legacy_totals[session] = int(total)
            if previous is not None and total <= previous:
                return
            model = context.get("model") or "unknown"
            self.add(Observation(key("legacy", session, total), "Codex", session, provider_for(model), model, moment, int(output)), now)

    def _grok_record(self, record: dict, session: str, now: float):
        update = (record.get("params") or {}).get("update") or {}
        moment = timestamp(record.get("timestamp"))
        if moment is None:
            return
        kind = update.get("sessionUpdate")
        if kind in ("agent_thought_chunk", "agent_message_chunk"):
            self.activity[session] = ("grok", moment, "生成中")
        if kind != "turn_completed":
            return
        self.activity.pop(session, None)
        usage = update.get("usage") or {}
        models = usage.get("modelUsage") or {"grok": usage}
        for model, value in models.items():
            output = number(value.get("outputTokens"))
            if output is None:
                continue
            self.add(Observation(key("grok", session, update.get("prompt_id") or moment, model), "Grok CLI", session, provider_for(model), model, moment, int(output), None, "读取CLI输出计数；不推算TPS", moment <= self.started_at), now)
            if moment > now - 3:
                self.activity[session] = (provider_for(model), moment, "刚完成生成")

    def _grok_usage_counter(self, path: Path, now: float):
        usage_path = path.with_name("usage.json")
        if not usage_path.is_file():
            return
        value = json.loads(usage_path.read_text(encoding="utf-8"))
        moment = timestamp(value.get("updatedAt"))
        models = (value.get("session") or {}).get("modelUsage") or {}
        for model, usage in models.items():
            total = number(usage.get("outputTokens"))
            if total is None:
                continue
            counter_key = (path.parent.name, model)
            previous = self.grok_counters.get(counter_key)
            self.grok_counters[counter_key] = int(total)
            if previous is None:
                summary_path = path.with_name("summary.json")
                if summary_path.is_file():
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    created = timestamp(summary.get("created_at") or summary.get("createdAt"))
                    if created and created >= self.started_at:
                        previous = 0
            if previous is not None and moment and total > previous:
                self.add(Observation(key("grok-counter", *counter_key, total), "Grok CLI", counter_key[0], provider_for(model), model, moment, int(total - previous)), now)

    def _usage_changed(self, path: Path) -> bool:
        """Parse usage.json only after it changes instead of every second."""
        try:
            info = path.with_name("usage.json").stat()
        except OSError:
            return False
        version = (info.st_mtime_ns, info.st_size)
        if self.usage_versions.get(path) == version:
            return False
        self.usage_versions[path] = version
        return True

    def _grok_activity(self, record: dict, session: str, now: float):
        moment = timestamp(record.get("ts"))
        if moment is None or moment < now - 600:
            return
        kind = record.get("type")
        if kind in ("loop_started", "first_token"):
            self.activity[session] = ("grok", moment, "生成中")
        elif kind == "turn_ended" and self.activity.get(session, (None, 0, None))[1] <= moment:
            if moment > now - 3:
                self.activity[session] = ("grok", moment, "刚完成生成")
            else:
                self.activity.pop(session, None)

    def _grok_native_tps(self, now: float):
        if self.grok_native_reader is None:
            return
        for record in self.grok_native_reader.read():
            if record.get("msg") != "shell.turn.inference_done":
                continue
            data = record.get("ctx") or {}
            tps = number(data.get("tokens_per_sec"))
            output = number(data.get("completion_tokens"))
            ended = timestamp(record.get("ts"))
            if tps is None or output is None or ended is None:
                continue
            session = str(record.get("sid") or "grok-client")
            self.add(Observation(key("grok-native", session, data.get("loop_index"), ended), "Grok CLI 原生统计", session, "grok", "grok", ended, int(output), None, "CLI原生 tokens_per_sec", False, tps), now)

    def _antigravity_roots(self) -> list[tuple[Path, str]]:
        roots = [(self.home / ".gemini/antigravity/conversations", "Antigravity 桌面"), (self.home / ".gemini/antigravity-cli/conversations", "Antigravity CLI")]
        # Codex's isolated agy profiles live in TEMP, which holds tens of
        # thousands of entries; list it at the slow-discovery pace, not every second.
        listed, profiles = self.antigravity_cache
        if time.time() - listed >= SLOW_DISCOVERY_SECONDS:
            temporary = Path(os.environ.get("TEMP", self.home / "AppData/Local/Temp"))
            try:
                profiles = sorted(temporary.glob("codex-agy-model-*"))
            except OSError:
                profiles = []
            self.antigravity_cache = (time.time(), profiles)
        for profile in profiles:
            roots.append((profile / "profile/antigravity-cli/conversations", "Antigravity CLI · Codex隔离配置"))
        return roots

    def _antigravity(self, now: float):
        for root, source in self._antigravity_roots():
            paths = list(root.glob("*.db"))
            good = 0
            skipped = 0
            for path in paths:
                wal = path.with_name(path.name + "-wal")
                version = (path.stat().st_mtime_ns, wal.stat().st_mtime_ns if wal.exists() else 0)
                if self.database_versions.get(path) == version:
                    good += 1
                    continue
                if path.stat().st_mtime < now - 86400 and version[1] / 1e9 < now - 86400:
                    self.database_versions[path] = version   # nothing from the last day
                    good += 1
                    continue
                try:
                    rows, steps = read_antigravity_db(path)
                except sqlite3.Error:
                    skipped += 1
                    continue
                skipped += self._antigravity_rows(path, source, rows, steps, now)
                self.database_versions[path] = version
                good += 1
            self.health[source] = {"status": "已接入" if good else "未发现记录", "sessions": good, "skipped": skipped}

    def _antigravity_rows(self, path: Path, source: str, rows: list, steps: list, now: float) -> int:
        skipped = 0
        by_response: dict[bytes, float] = {}
        by_index: dict[int, float] = {}
        # Model reply steps (step_type 15) per generation: (first created, last completed).
        spans: dict[Any, tuple[float, float]] = {}
        conversation = path.stem
        request, doing = None, None          # newest user message / in-progress step
        last_tool = None                     # newest finished tool call: the last action
        for _, status, metadata, step_type, body in steps:
            try:
                completed = proto_seconds(proto_message(metadata, 7), epoch=True)
                created = proto_seconds(proto_message(metadata, 1), epoch=True)
                moment = completed or created
                association = proto_message(metadata, 20)
                generation_index = proto_int(association, 3, -1) if association else -1
                response = proto_message(proto_message(metadata, 9), 11)
                if moment:
                    if response:
                        by_response[response] = max(moment, by_response.get(response, 0))
                    if generation_index >= 0:
                        by_index[generation_index] = max(moment, by_index.get(generation_index, 0))
                if step_type == 15 and created and completed and completed > created:
                    for span_key in ((response,) if response else ()) + ((generation_index,) if generation_index >= 0 else ()):
                        first, last = spans.get(span_key, (created, completed))
                        spans[span_key] = (min(first, created), max(last, completed))
                # Status 1/2 is a step still generating or running (a tool call in
                # progress carries a provisional #7 time); older builds leave #7 unset.
                running = status in (1, 2) or status not in (3, 4, 5) and not completed
                if running and generation_index >= 0 and created and created > now - 600:
                    self.activity[path.stem] = ("gemini", created, "请求中")
                    tool, arguments = agy_tool(metadata)
                    if doing is None and tool:
                        doing = (tool_action(tool, arguments) or "调用工具", created)
                    elif doing is None and step_type == AGY_REPLY_STEP:
                        doing = ("思考中", created)
                    elif doing is None and step_type is not None and step_type not in AGY_USER_STEPS:
                        doing = ("调用工具", created)
                elif last_tool is None and step_type not in AGY_USER_STEPS:
                    tool, arguments = agy_tool(metadata)
                    if tool and moment:
                        last_tool = (tool_action(tool, arguments), moment)
                if body and request is None and step_type in AGY_USER_STEPS:
                    # Steps are newest first: the first user step is the current request.
                    text = antigravity_request(body)
                    if text:
                        request = (text, created or moment)
            except (ValueError, TypeError):
                skipped += 1
        for index, blob in reversed(rows):
            try:
                chat_model = proto_message(blob, 1)
                usage = proto_message(chat_model, 4)
                text = proto_int(usage, 9)
                thoughts = proto_int(usage, 10)
                total = proto_int(usage, 3, -1)
                # #3 (total output) is absent in some agy versions; when it is
                # present it must agree with text + thinking.
                if total >= 0 and total != text + thoughts or text + thoughts <= 0:
                    skipped += 1
                    continue
                output = text + thoughts
                response = proto_message(usage, 11)
                ended = by_response.get(response) or by_index.get(index)
                if ended is None:
                    skipped += 1
                    continue
                model = proto_message(chat_model, 19).decode("utf-8", errors="replace") or proto_message(chat_model, 21).decode("utf-8", errors="replace") or "unknown"
                if len(model) > 100:
                    model = "unknown"
                span = spans.get(response) if response else None
                span = span or spans.get(index)
                tps, basis = None, "客户端未提供原生TPS；保留输出计数"
                if span and output >= MIN_TPS_TOKENS and span[1] - span[0] >= MIN_TPS_SECONDS:
                    tps = round(output / (span[1] - span[0]), 1)
                    basis = "本地步骤时间（输出token数÷回复步骤创建至完成的时间，含首字延迟）"
                identity = key("response", response.decode("utf-8", errors="replace")) if response else key("antigravity", path.stem, index)
                # Every Antigravity reply (Gemini, Claude Opus or GPT pools alike) draws on
                # the Gemini/Antigravity quota: its TPS, bubbles and colour belong there.
                provider = "gemini"
                self.add(Observation(identity, source, path.stem, provider, model, ended, output,
                                     span[1] - span[0] if tps else None, basis, True, tps), now)
                if ended > now - 3:
                    self.activity[path.stem] = (provider, ended, "刚完成生成")
                if conversation not in self.context or not self.context[conversation].get("model"):
                    self.context.setdefault(conversation, {}).update({"source": source, "model": model, "provider": provider})
            except (ValueError, TypeError):
                skipped += 1
        self.context.setdefault(conversation, {}).setdefault("source", source)
        if request:
            self.notes.ask(conversation, *request)
        if doing:
            self.notes.act(conversation, *doing)
        elif last_tool and last_tool[0]:
            self.notes.act(conversation, *last_tool)
        title = antigravity_title(self.home, conversation)
        if title:
            self.notes.describe(conversation, title=title)
        return skipped

    def poll(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        if now - self.last_discovery >= 2:
            self.discover(now)
        try:
            self._codex_logs(now)
        except (OSError, sqlite3.Error):
            self.health["Codex 流式日志"] = {"status": "不可读取"}
        for path, reader in list(self.readers.items()):
            session = self.file_sessions[path]
            try:
                source = self.context.get(session, {}).get("source")
                if source == "Claude Code":
                    agent = path.stem if path.parent.name == "subagents" else session
                    for record in reader.read():
                        self.notes.claude(agent, record, session if agent != session else "")
                        self._claude_record(record, session, now)
                elif source == "Grok CLI":
                    session = path.parent.name
                    for record in reader.read():
                        if path.name == "updates.jsonl":
                            self.notes.grok(session, record)
                            self._grok_record(record, session, now)
                        else:
                            self._grok_activity(record, session, now)
                    if path.name == "updates.jsonl" and self._usage_changed(path):
                        self._grok_usage_counter(path, now)
                else:
                    records = list(reader.read())
                    self.context.setdefault(session, {})
                    if any(r.get("type") == "token_usage_record" for r in records):
                        self.context[session]["modern_usage"] = True
                    for record in records:
                        self.notes.codex(session, record)
                        self._codex_record(record, session, now)
            except (OSError, ValueError, TypeError):
                self.health[self.context.get(session or "", {}).get("source", "记录读取")] = {"status": "部分记录不可读取"}
        self._claude_flush(now)
        try:
            self._mimocode(now)
        except (OSError, sqlite3.Error):
            self.health["MiMo Code"] = {"status": "部分数据库不可读取"}
        try:
            self._antigravity(now)
        except (OSError, sqlite3.Error):
            self.health["Antigravity"] = {"status": "部分数据库不可读取"}
        try:
            self._grok_native_tps(now)
        except (OSError, ValueError, TypeError):
            self.health["Grok 原生速率"] = {"status": "暂不可读取"}
        while self.events and self.events[0].ended_at < now - 86400:
            self.seen.discard(self.events.popleft().identity)
        if self.store is not None:
            self.store.save(now)
        self.activity = {s: v for s, v in self.activity.items() if now - v[1] < (3 if v[2] == "刚完成生成" else 600)}
        return self.snapshot(now)

    def agents(self, now: float) -> list[dict[str, Any]]:
        """Working (and just finished) agents with their latest measured TPS."""
        rates: dict[str, float] = {}
        for event in reversed(self.events):
            if event.ended_at < now - 300:
                break
            if event.native_tps is not None:
                rates.setdefault(event.session, event.native_tps)
        agents = self.notes.export(self.activity, self.context, rates, now, provider_for, self.schedules)
        try:
            agents = self.herdr.attach(agents, now)
        except Exception:      # an optional extra must never hide the native agents
            log.debug("herdr attach failed", exc_info=True)
        self.health["herdr"] = {"status": self.herdr.status, "panes": len(self.herdr.rows)}
        return agents

    def snapshot(self, now: float) -> dict[str, Any]:
        # The installed Codex bridge emits a second copy of agy's usage. Only
        # compare its known isolated agy channel, one-to-one, with exact output
        # counts and near-identical completion time; unrelated CLI runs remain.
        # This runs every second over up to a day of observations. One pass
        # collects the copies to de-duplicate, the sources per provider and the
        # last HOLD_TPS_SECONDS; everything else works on that short window
        # (it used to be ~20 full passes, the bulk of the TPS thread's CPU).
        copy_source, native_source = "Codex · Gemini桥接回传", "Antigravity CLI · Codex隔离配置"
        copies, native, window = [], [], []
        cutoff = now - max(HOLD_TPS_SECONDS, 300)
        for e in self.events:
            if e.ended_at >= cutoff:
                window.append(e)
            if e.source == copy_source:
                copies.append(e)
            elif e.source == native_source:
                native.append(e)
        sources: dict[str, set] = {}
        for (provider, source), newest in list(self.source_seen.items()):
            if newest < now - 86400:
                self.source_seen.pop((provider, source), None)     # its events have left the deque
            elif source != copy_source:
                sources.setdefault(provider, set()).add(source)
        duplicate_ids = set()
        available = list(native)
        for copy in copies:
            match = next((e for e in available if e.output_tokens == copy.output_tokens and e.provider == copy.provider and abs(e.ended_at - copy.ended_at) <= 3), None)
            if match:
                duplicate_ids.add(copy.identity)
                available.remove(match)
            else:
                sources.setdefault(copy.provider, set()).add(copy.source)
        providers = {}
        for provider, label in PROVIDER_NAMES.items():
            events = [e for e in window if e.provider == provider]
            recent = [e for e in events if e.count_in_rate and now - 60 <= e.ended_at <= now and e.identity not in duplicate_ids]
            # A deduplicated bridge copy still carries the client's stream timing.
            samples = [e for e in events if now - 300 <= e.ended_at <= now and e.native_tps is not None]
            latest = max(samples, key=lambda e:e.ended_at) if samples else None
            active = sum(1 for value in self.activity.values() if value[0] == provider)
            # herdr sees agents working before (and between) their local records.
            active += self.herdr.working(provider, set(self.activity))
            output = sum(e.output_tokens for e in recent)
            generation = latest.native_tps if latest else None
            basis_text = latest.basis if latest else None
            if latest is not None and latest.generation_seconds:
                # Token-weighted over replies finished within TPS_GROUP_SECONDS of
                # the latest: short tool-call replies no longer swing the figure.
                group = [e for e in samples if e.generation_seconds and e.ended_at >= latest.ended_at - TPS_GROUP_SECONDS
                         and e.source == latest.source]
                if len(group) > 1:
                    generation = round(sum(e.output_tokens for e in group) / sum(e.generation_seconds for e in group), 1)
                    basis_text = f"{latest.basis}；最近 {len(group)} 次回复按 token 加权"
            if generation is None and active:
                # A long reply or tool run has no finished sample for minutes; keep
                # the last measured rate (up to HOLD_TPS_SECONDS) instead of 0.
                held = [e for e in events if e.native_tps is not None and now - HOLD_TPS_SECONDS <= e.ended_at <= now]
                if held:
                    latest = max(held, key=lambda e: e.ended_at)
                    generation, basis_text = latest.native_tps, f"{latest.basis}；仍在工作，保留上次测得的速率"
            providers[provider] = {"label": label, "active_sessions": active, "generation_tps": generation, "output_tps_60": round(output / 60, 2), "output_tokens_60": output, "generation_samples": len(samples), "basis": [basis_text] if latest else ["客户端暂未提供可核验TPS"], "models": sorted({e.model for e in samples}), "sources": sorted(sources.get(provider, ())), "last_sample_at": latest.ended_at if latest else None, "display_tps": generation, "display_model": latest.model if latest else None, "display_basis": basis_text if latest else "客户端暂未取得TPS"}
        return {"generated_at": now, "window_seconds": 60, "providers": providers, "total_output_tps_60": round(sum(p["output_tokens_60"] for p in providers.values()) / 60, 2), "software": self.software, "source_health": self.health, "measurement_note": "Grok直读CLI原生TPS；Codex使用客户端真实输出token数÷首个输出项至计数完成的流式时间，含思考及工具调用输出，完成时结算。使用时显示最近完成的TPS，缺少完整计数时保留额度；不使用字符数或近似分词器。60秒输出速率为已报告输出数之和÷60，不计输入/缓存，不重复加思考token。"}


def _find_text(value: Any, wanted: str) -> str:
    if isinstance(value, str) and value.startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if isinstance(value, dict):
        for name, item in value.items():
            found = item if name == wanted and item else _find_text(item, wanted)
            if found:
                return str(found)
    return ""


# Antigravity keeps the Cascade step enum: 14 is the user's message, 15 the
# model's reply. CODEX_QUOTA_AGY_USER_STEPS overrides (comma separated).
AGY_USER_STEPS = {int(part) for part in (os.environ.get("CODEX_QUOTA_AGY_USER_STEPS") or "14").split(",") if part.strip().isdigit()}
AGY_REPLY_STEP = 15
STEP_PAYLOAD_COLUMNS = ("step_payload", "step", "data", "payload", "content", "proto", "value", "blob")


def agy_tool(metadata: bytes) -> tuple[str, str]:
    """(tool name, JSON arguments) of the tool call a step carries, if any.
    Antigravity keeps it in step metadata #4: {1: call id, 2: name, 3: args}."""
    call = proto_message(metadata, 4)
    if not call:
        return "", ""
    name = proto_message(call, 2).decode("utf-8", errors="replace")
    if not name or len(name) > 80 or not re.fullmatch(r"[\w.:-]+", name):
        return "", ""
    return name, proto_message(call, 3).decode("utf-8", errors="replace")


def _antigravity_query(db) -> tuple[list, list]:
    rows = db.execute("SELECT idx,data FROM gen_metadata ORDER BY idx DESC LIMIT 300").fetchall()
    columns = [row[1] for row in db.execute("PRAGMA table_info(steps)")]
    kind = "step_type" if "step_type" in columns else "NULL"
    body = next((name for name in STEP_PAYLOAD_COLUMNS if name in columns), None)
    # The step body (prompt text) is only read for user-message steps.
    payload = f"CASE WHEN {kind} IN ({','.join(map(str, sorted(AGY_USER_STEPS)))}) THEN {body} END" if body and kind != "NULL" and AGY_USER_STEPS else "NULL"
    steps = db.execute(f"SELECT idx,status,metadata,{kind},{payload} FROM steps WHERE metadata IS NOT NULL "
                       "ORDER BY idx DESC LIMIT 3000").fetchall()
    return rows, steps


def _looks_human(text: str) -> bool:
    if len(text) < 2 or len(text) > 8000:
        return False
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text):
        return False
    if re.fullmatch(r"[0-9a-fA-F-]{16,}|[A-Za-z0-9+/=_-]{24,}|[\w.-]+(?:[\\/][\w .-]+)+|[a-z][\w.-]*", text):
        return False            # ids, hashes, base64, bare paths, enum-like tokens
    return bool(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]|[A-Za-z]{2,}\W+[A-Za-z]{2,}", text))


def proto_texts(data: bytes, depth: int = 0) -> list[str]:
    """Human-looking strings inside a protobuf message, in field order."""
    found: list[str] = []
    try:
        fields = proto_fields(data)
    except ValueError:
        return found
    for number_ in sorted(fields):
        for value in fields[number_]:
            if not isinstance(value, bytes) or not value:
                continue
            # A nested message can also decode as UTF-8: take it as a message
            # when its strings make up most of its bytes.
            nested = proto_texts(value, depth + 1) if depth < 6 else []
            if nested and sum(len(t.encode("utf-8")) for t in nested) >= 0.6 * len(value):
                found.extend(nested)
                continue
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and _looks_human(text):
                found.append(text)
            else:
                found.extend(nested)
    return found


def antigravity_request(payload) -> str:
    """The user's message from an Antigravity user-input step body."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return ""
    texts = proto_texts(bytes(payload))
    if not texts:
        return ""
    # Prefer prose over pasted code/context: the longest line that is not mostly symbols.
    prose = [t for t in texts if len(re.findall(r"[{}();=<>]", t)) < max(4, len(t) // 25)]
    return max(prose or texts, key=len)[:4000]


def antigravity_title(home: Path, conversation: str) -> str:
    """Antigravity desktop's task artifact for a conversation, if any."""
    for name in ("task.md", "implementation_plan.md"):
        path = home / ".gemini/antigravity/brain" / conversation / name
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle.read(4000).splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line and not line.startswith(("-", "*", "[", "<!--")):
                        return re.sub(r"^(?:task|任务)\s*[:：]\s*", "", line, flags=re.I)[:200]
        except OSError:
            continue
    return ""


def read_antigravity_db(path: Path) -> tuple[list, list]:
    """Antigravity may hold its conversation database locked while it
    streams; read a private copy (with its WAL) when a direct read fails."""
    try:
        db = readonly_db(path)
        try:
            return _antigravity_query(db)
        finally:
            db.close()
    except sqlite3.OperationalError:
        pass
    with tempfile.TemporaryDirectory(prefix="cqt-agy-") as folder:
        copy = Path(folder) / path.name
        shutil.copyfile(path, copy)
        for suffix in ("-wal", "-shm"):
            extra = path.with_name(path.name + suffix)
            if extra.exists():
                shutil.copyfile(extra, copy.with_name(copy.name + suffix))
        db = sqlite3.connect(copy)
        try:
            return _antigravity_query(db)
        finally:
            db.close()


def step_columns(path: Path) -> list[str]:
    try:
        db = readonly_db(path)
        try:
            return [row[1] for row in db.execute("PRAGMA table_info(steps)")]
        finally:
            db.close()
    except sqlite3.Error:
        return []


def _proto_summary(data: bytes, depth: int = 0) -> dict[str, Any]:
    """Field numbers and numeric values only; strings become their length."""
    summary: dict[str, Any] = {}
    try:
        fields = proto_fields(data)
    except ValueError:
        return {"undecodable_bytes": len(data)}
    for number_, values in fields.items():
        out = []
        for value in values[:3]:
            if isinstance(value, int):
                out.append(value)
                continue
            stamp_ = None
            try:
                stamp_ = proto_seconds(value, epoch=True) or proto_seconds(value)
            except ValueError:
                pass
            if stamp_ is not None and set(proto_fields(value)) <= {1, 2}:
                out.append({"time": round(stamp_, 3)})
            elif depth < 2 and len(value) < 4000:
                nested = _proto_summary(value, depth + 1)
                out.append(nested if "undecodable_bytes" not in nested else {"bytes": len(value)})
            else:
                out.append({"bytes": len(value)})
        summary[str(number_)] = out if len(out) > 1 else out[0]
    return summary


def antigravity_diagnostics(home: Path | None = None, databases: int = 3, generations: int = 5, steps: int = 20) -> dict[str, Any]:
    """Structure of the newest Antigravity databases (no prompt or reply text)."""
    monitor = TpsMonitor(home=home)
    result: dict[str, Any] = {"roots": []}
    for root, source in monitor._antigravity_roots():
        paths = sorted(root.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        entry: dict[str, Any] = {"source": source, "exists": root.is_dir(), "databases": len(paths), "newest": []}
        for path in paths[:databases]:
            item: dict[str, Any] = {"modified": path.stat().st_mtime}
            try:
                rows, step_rows = read_antigravity_db(path)
            except sqlite3.Error as error:
                item["error"] = f"{type(error).__name__}: {error}"
                entry["newest"].append(item)
                continue
            item["gen_metadata"] = []
            for index, blob in rows[:generations]:
                chat_model = proto_message(blob, 1)
                described = _proto_summary(chat_model)
                for text_field in ("19", "21"):
                    if text_field in described:
                        described[text_field] = proto_message(chat_model, int(text_field)).decode("utf-8", errors="replace")[:80]
                item["gen_metadata"].append({"idx": index, "chat_model": described})
            item["steps"] = [{"idx": idx, "status": status, "step_type": kind, "created": proto_seconds(proto_message(meta, 1), epoch=True),
                              "completed": proto_seconds(proto_message(meta, 7), epoch=True),
                              "generation_index": proto_int(proto_message(meta, 20), 3, -1) if proto_message(meta, 20) else None,
                              "has_response_id": bool(proto_message(proto_message(meta, 9), 11))}
                             for idx, status, meta, kind, _body in step_rows[:steps]]
            # Which step types carry the user's text, without showing the text itself.
            item["user_steps"] = [{"idx": idx, "step_type": kind, "request_chars": len(antigravity_request(body))}
                                  for idx, _status, _meta, kind, body in step_rows if body][:5]
            item["step_columns"] = step_columns(path)
            entry["newest"].append(item)
        result["roots"].append(entry)
    performance = monitor.poll()
    result["gemini"] = performance["providers"]["gemini"]
    result["health"] = {k: v for k, v in performance["source_health"].items() if "Antigravity" in k}
    return result


def running_software() -> list[dict[str, Any]]:
    names = {"chatgpt.exe": "ChatGPT / Codex 桌面", "codex.exe": "Codex 引擎", "grok.exe": "Grok CLI", "agy.exe": "Antigravity CLI", "antigravity.exe": "Antigravity 桌面", "gemini.exe": "Gemini CLI", "claude.exe": "Claude Code", "cursor.exe": "Cursor", "ollama.exe": "Ollama", "lm studio.exe": "LM Studio", "copilot.exe": "Copilot CLI", "opencode.exe": "OpenCode", "mimo.exe": "MiMo Code"}
    counts: dict[str, int] = {}
    for process in psutil.process_iter(["name", "exe"]):
        name = (process.info.get("name") or "").lower()
        executable = (process.info.get("exe") or "").lower().replace("\\", "/")
        if name in ("claude.exe", "claude"):
            app = "Claude 桌面" if "/windowsapps/" in executable or "/claude.app/" in executable else "Claude Code"
            counts[app] = counts.get(app, 0) + 1
        elif name in names:
            counts[names[name]] = counts.get(names[name], 0) + 1
        elif name in ("node.exe", "node"):
            try:
                command = " ".join(process.cmdline()).lower().replace("\\", "/")
                for marker, app in (("@google/gemini-cli", "Gemini CLI"), ("@anthropic-ai/claude-code", "Claude Code"), ("@qwen-code", "Qwen Code"), ("@github/copilot", "Copilot CLI"), ("@mimo-ai/", "MiMo Code")):
                    if marker in command:
                        counts[app] = counts.get(app, 0) + 1
                        break
            except (psutil.Error, OSError):
                pass
    supported = {"ChatGPT / Codex 桌面", "Codex 引擎", "Grok CLI", "Antigravity CLI", "Antigravity 桌面", "Claude Code", "MiMo Code"}
    return [{"name": name, "processes": count, "status": ("读取本地记录" if name in supported else
             "桌面客户端" if name == "Claude 桌面" else "暂无可核验的本地TPS信号")} for name, count in sorted(counts.items())]


def performance_signature(snapshot: dict[str, Any]) -> str:
    """Everything except the clock, so unchanged state is not rewritten."""
    return json.dumps({k: v for k, v in snapshot.items() if k != "generated_at"}, sort_keys=True, ensure_ascii=False, default=str)


def save_performance(snapshot: dict[str, Any]) -> None:
    PERFORMANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PERFORMANCE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PERFORMANCE_PATH)
