"""Short, optional MiMo advice for the hover panel.

Only compact agent labels/actions, provider quota percentages and detected AI
client names leave the machine. No transcripts, paths, cookies or credentials
are placed in the prompt. Results stay in memory and refresh at a bounded pace.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import agent_tasks

log = logging.getLogger(__name__)
MIN_REQUEST_GAP = 120
RESULT_SECONDS = 120
FAILURE_GAP = 300

SYSTEM_PROMPT = (
    "你是本机编程工作台的灵感助手。参考当前工作状态以及各 agent 的历史话题，充分发散，"
    "可以提出与当前任务不同、但值得尝试的小项目、清理、验证或改进。历史话题是抽样线索，不代表仍在进行。"
    "只输出 JSON：{\"now\":\"正在做什么，不超过32字\",\"next\":\"下一步可做什么，不超过38字\"}。"
    "now 必须只依据当前任务，不编造工作或登录状态；客户端进程存在不代表正在生成。"
    "next 可跨话题发散，给一个具体、有趣、值得做的建议，不必延续当前任务。"
    "建议是可选的，不要执行操作。用中文，避免套话。"
)


def _label(value: object, length: int = 48) -> str:
    text = re.sub(r"https?://\S+|(?:[A-Za-z]:)?(?:[\\/][\w.\-~ ]+)+", "", str(value or ""))
    text = re.sub(r"[\w.+-]+@[\w.-]+|\b(?:\+?\d[\d -]{9,}\d)\b", "", text)
    text = re.sub(r"(?i)\b(?:api[_ -]?key|token|secret)\s*[:=]\s*\S+|\b(?:sk-|tp-)[\w-]{12,}\b|\b[A-Za-z0-9_+/]{32,}\b", "[隐藏]", text)
    return agent_tasks.shorten(re.sub(r"\s+", " ", text).strip(), length)


def context(provider: str, agents: list[dict], snapshots: dict, software: list[dict]) -> dict:
    active = [agent for agent in agents if agent.get("live") and not agent.get("schedule")]
    tasks = [{"provider": str(agent.get("provider") or ""),
              "title": _label(agent.get("title") or agent.get("thread_title")),
              "doing": _label(agent.get("doing"), 32)} for agent in active[:5]]
    quotas = {}
    for name, snapshot in snapshots.items():
        value = snapshot.display_remaining if snapshot is not None else None
        if snapshot is not None and not snapshot.stale and not snapshot.error and value is not None:
            quotas[name] = round(float(value) / 5) * 5
    clients = sorted({str(row.get("name")) for row in software if row.get("name") and row.get("processes", 0) > 0})[:8]
    return {"hovered": provider, "tasks": tasks, "quotas": quotas, "detected_clients": clients}


def local_hint(data: dict) -> tuple[str, str]:
    tasks = data["tasks"]
    if tasks:
        first = tasks[0]
        current = f"{first['provider']}：{first['title'] or first['doing'] or '正在工作'}"
        if len(tasks) > 1:
            current += f"；另有 {len(tasks) - 1} 个 agent 在工作"
    elif data["detected_clients"]:
        current = "检测到 AI 客户端，暂无可确认的生成任务"
    else:
        current = "当前未检测到生成中的 agent"
    quota = data["quotas"].get(data["hovered"])
    if quota is not None and quota <= 15:
        next_step = "当前额度较低，非紧急任务可留到重置后"
    elif tasks:
        next_step = "可先检查当前任务的结果，再决定下一步"
    else:
        next_step = "可准备下一项任务，或查看刚完成的结果"
    return _label(current, 64), next_step


def _signature(data: dict) -> str:
    # Tool actions change every few seconds. Keep one suggestion per task and
    # quota band, then ask MiMo again after its short cache expires.
    stable = {**data, "tasks": [{"provider": task["provider"], "title": task["title"]}
                                 for task in data["tasks"]]}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True)


def history_topics(monitor, now: float | None = None, limit: int = 18) -> list[dict]:
    """Sample task-level history across clients; never send raw conversations."""
    now = time.time() if now is None else now
    candidates: list[dict] = []

    def offer(provider: str, value: object) -> None:
        title = agent_tasks.clean(str(value or ""))
        title = _label(title, 56)
        if title and len(title) >= 4:
            candidates.append({"provider": provider, "topic": title})

    if monitor is not None:
        try:
            notes = list(monitor.notes.notes.values())
            contexts = dict(monitor.context)
        except (AttributeError, RuntimeError):
            notes, contexts = [], {}
        for note in notes:
            if note.active_at < now - 30 * 86400 or note.schedule_id:
                continue
            info = contexts.get(note.key) or {}
            provider = str(info.get("provider") or info.get("source") or "agent").lower()
            provider = ("codex" if provider in ("openai", "gpt", "codex") else
                        "gemini" if "antigravity" in provider or "gemini" in provider else
                        "claude" if "claude" in provider else provider)
            offer(provider, note.title or note.first or note.prompt)
            if note.prompt and note.prompt != note.first:
                offer(provider, note.prompt)

        # Codex keeps older thread titles in a local index. Read only short
        # title/first-request prefixes; the SQLite connection is read-only.
        database = Path(monitor.codex_root) / "state_5.sqlite"
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
                names = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                if "title" in names or "first_user_message" in names:
                    title = ("coalesce(nullif(title,''),first_user_message)" if {"title", "first_user_message"} <= names
                             else "title" if "title" in names else "first_user_message")
                    for (topic,) in db.execute(f"SELECT substr({title},1,180) FROM threads ORDER BY updated_at DESC LIMIT 160"):
                        offer("codex", topic)
        except (OSError, sqlite3.Error, AttributeError):
            pass

    # Claude session summaries or first user requests extend beyond the live
    # reader's two-day memory. Read a bounded prefix of recent files only.
    paths = []
    if monitor is not None:
        root = Path(monitor.claude_root) / "projects"
        try:
            paths = sorted((path for folder in root.iterdir() if folder.is_dir()
                            for path in folder.glob("*.jsonl") if path.stat().st_mtime >= now - 30 * 86400),
                           key=lambda path: path.stat().st_mtime, reverse=True)[:80]
        except OSError:
            pass
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as stream:
                for _ in range(160):
                    line = stream.readline(65536)
                    if not line:
                        break
                    if len(line) == 65536 and not line.endswith("\n"):
                        break
                    record = json.loads(line)
                    if record.get("type") == "summary" and record.get("summary"):
                        offer("claude", record["summary"])
                        break
                    if record.get("type") == "user" and not record.get("isMeta"):
                        content = (record.get("message") or {}).get("content")
                        text = content if isinstance(content, str) else " ".join(
                            str(item.get("text") or "") for item in content or []
                            if isinstance(item, dict) and item.get("type") == "text")
                        if agent_tasks.clean(text):
                            offer("claude", text[:300])
                            break
        except (OSError, ValueError, TypeError, AttributeError):
            continue

    if monitor is not None:
        try:
            grok_paths = sorted((path for path in (Path(monitor.grok_root) / "sessions").glob("*/*/updates.jsonl")
                                 if path.stat().st_mtime >= now - 30 * 86400),
                                key=lambda path: path.stat().st_mtime, reverse=True)[:60]
        except OSError:
            grok_paths = []
        for path in grok_paths:
            try:
                fragments = []
                with path.open("r", encoding="utf-8") as stream:
                    for _ in range(120):
                        line = stream.readline(65536)
                        if not line or len(line) == 65536 and not line.endswith("\n"):
                            break
                        record = json.loads(line)
                        update = (record.get("params") or {}).get("update") or {}
                        if update.get("sessionUpdate") == "user_message_chunk":
                            content = update.get("content") or {}
                            fragments.append(str(content.get("text") or "") if isinstance(content, dict) else "")
                            if sum(map(len, fragments)) >= 300:
                                break
                        elif fragments:
                            break
                offer("grok", "".join(fragments)[:300])
            except (OSError, ValueError, TypeError, AttributeError):
                continue

    unique = {}
    for item in candidates:
        unique.setdefault((item["provider"], item["topic"]), item)
    groups: dict[str, list[dict]] = {}
    for item in unique.values():
        groups.setdefault(item["provider"], []).append(item)
    rng = random.SystemRandom()
    chosen = [item for group in groups.values() for item in rng.sample(group, min(3, len(group)))]
    remaining = [item for item in unique.values() if item not in chosen]
    chosen += rng.sample(remaining, min(max(0, limit - len(chosen)), len(remaining)))
    rng.shuffle(chosen)
    return chosen[:limit]


class HintAdvisor:
    def __init__(self, config: dict | None, opener=None):
        self.config = config if config and agent_tasks.is_mimo_url(config.get("url", "")) else None
        self.opener = opener or urllib.request.urlopen
        self.lock = threading.Lock()
        self.signature = ""
        self.result: tuple[str, str] | None = None
        self.result_at = 0.0
        self.last_request_at = 0.0
        self.failed_until = 0.0
        self.pending = False

    def lookup(self, data: dict, monitor=None) -> tuple[str, str]:
        fallback = local_hint(data)
        if self.config is None:
            return fallback
        signature = _signature(data)
        now = time.time()
        with self.lock:
            if signature == self.signature and self.result and now - self.result_at < RESULT_SECONDS:
                return self.result
            if (not self.pending and now >= self.failed_until
                    and now - self.last_request_at >= MIN_REQUEST_GAP):
                self.pending = True
                self.last_request_at = now
                threading.Thread(target=self._fetch, args=(signature, data, monitor), name="mimo-hint", daemon=True).start()
        return fallback

    def _fetch(self, signature: str, data: dict, monitor=None) -> None:
        try:
            material = {**data, "past_topics": history_topics(monitor)}
            payload = {"model": self.config["model"], "temperature": 0.85,
                       "max_completion_tokens": 180, "thinking": {"type": "disabled"},
                       "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": json.dumps(material, ensure_ascii=False)}]}
            headers = {"Content-Type": "application/json", "api-key": self.config["key"]}
            request = urllib.request.Request(self.config["url"],
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
            with self.opener(request, timeout=10) as response:
                reply = json.loads(response.read().decode("utf-8"))
            content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            # Parse only the two user-visible fields; no raw model response is logged.
            match = re.search(r"\{.*\}", content, flags=re.S)
            values = json.loads(match.group(0)) if match else {}
            result = (_label(values.get("now"), 42), _label(values.get("next"), 50))
            if not all(result):
                raise ValueError("MiMo advice lacked now/next")
            with self.lock:
                self.signature, self.result, self.result_at = signature, result, time.time()
        except Exception as error:
            log.info("MiMo hint unavailable: %s", type(error).__name__)
            with self.lock:
                self.failed_until = time.time() + FAILURE_GAP
        finally:
            with self.lock:
                self.pending = False
