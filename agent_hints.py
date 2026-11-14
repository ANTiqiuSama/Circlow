"""Short, optional MiMo advice for the hover panel.

Only compact agent labels/actions, sampled history topics and detected AI
client names leave the machine. No usage figures, transcripts, paths, cookies or credentials
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
RESULT_SECONDS = 600        # an idea stays this long before a new one is asked for
KEEP_SECONDS = 3600         # while a new one is on its way, the last idea stays up to this old
FAILURE_GAP = 300

SYSTEM_PROMPT = (
    "你是一位眼界开阔的产品与技术顾问，旁观一个人同时用多个 AI agent 推进的几条工作线。"
    "材料里有当前任务和过去一个月的话题抽样（只是线索，不代表仍在进行）。"
    "读出这些话题背后的大方向，给出一条宏观、有新意、能启发人的建议："
    "把两条看似无关的线索连成一个新想法；指出一个被忽略却值得押注的方向；"
    "把手头的东西提升一个层次（变成产品、平台、方法论或可以分享的成果）；或提出一个反直觉的问题。"
    "只输出 JSON：{\"idea\":\"一句建议，18 到 40 字\"}。"
    "要求具体到这些话题本身，读的人能立刻联想到自己的工作；不要放之四海皆准的空话。"
    "绝对不要：收尾、准备、整理、梳理、休息、节奏、专注、先做完再开新线、查看结果这类工作习惯提醒；"
    "具体的操作或改动（某个文件、函数、命令、测试、修复，或“去改/加/检查某某”）；"
    "用量、token、占比、消耗、额度、限额或剩余多少；与过去的比较。用中文，不要套话。"
)
FILLER = re.compile(r"收尾|准备|整理|梳理|休息|节奏|专注|新线|查看.{0,4}结果|稳一点|更稳|一步一步|循序渐进")
USAGE_ADVICE = re.compile(
    r"用量|占比|消耗|使用高峰|(?:今天|今日|本日|累计|总计)输出|输出(?:量|总量)|"
    r"\d+(?:\.\d+)?\s*[%％]|\b\d+(?:\.\d+)?\s*[KMB]?\s*(?:tokens?|tok/s)\b", re.I)


def _label(value: object, length: int = 48) -> str:
    text = re.sub(r"https?://\S+|(?:[A-Za-z]:)?(?:[\\/][\w.\-~ ]+)+", "", str(value or ""))
    text = re.sub(r"[\w.+-]+@[\w.-]+|\b(?:\+?\d[\d -]{9,}\d)\b", "", text)
    text = re.sub(r"(?i)\b(?:api[_ -]?key|token|secret)\s*[:=]\s*\S+|\b(?:sk-|tp-)[\w-]{12,}\b|\b[A-Za-z0-9_+/]{32,}\b", "[隐藏]", text)
    return agent_tasks.shorten(re.sub(r"\s+", " ", text).strip(), length)


def context(agents: list[dict], software: list[dict]) -> dict:
    """Task labels and running clients, without usage statistics or quota."""
    active = [agent for agent in agents if agent.get("live") and not agent.get("schedule")]
    tasks = [{"provider": str(agent.get("provider") or ""),
              "title": _label(agent.get("title") or agent.get("thread_title")),
              "doing": _label(agent.get("doing"), 32)} for agent in active]
    tasks = sorted(tasks, key=lambda item: (item["provider"], item["title"], item["doing"]))[:5]
    clients = sorted({str(row.get("name")) for row in software if row.get("name") and row.get("processes", 0) > 0})[:8]
    return {"tasks": tasks, "detected_clients": clients}


def local_hint(data: dict) -> tuple[str, str]:
    """Without MiMo there is no idea to show: the panel leaves the line empty
    rather than fill it with stock advice ("收尾后再开新线"). The first field
    is kept for callers and is always empty too: the bubbles above already
    show what is going on."""
    return "", ""


def _signature(data: dict) -> str:
    # Tool actions change every few seconds. Keep one suggestion per set of tasks,
    # then ask MiMo again after
    # its short cache expires.
    stable = {"tasks": [{"provider": task["provider"], "title": task["title"]} for task in data["tasks"]],
              "detected_clients": data.get("detected_clients", [])}
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
        """("", idea): MiMo's latest idea, kept RESULT_SECONDS before another
        is asked for (sooner when the set of tasks changes), and shown until
        the next arrives (up to KEEP_SECONDS) so the line never flickers."""
        if self.config is None:
            return local_hint(data)
        signature = _signature(data)
        now = time.time()
        with self.lock:
            fresh = self.result and now - self.result_at < RESULT_SECONDS
            if not (fresh and signature == self.signature) and (
                    not self.pending and now >= self.failed_until and now - self.last_request_at >= MIN_REQUEST_GAP):
                self.pending = True
                self.last_request_at = now
                threading.Thread(target=self._fetch, args=(signature, data, monitor), name="mimo-hint", daemon=True).start()
            if self.result and now - self.result_at < KEEP_SECONDS:
                return self.result
        return local_hint(data)

    def _fetch(self, signature: str, data: dict, monitor=None) -> None:
        try:
            # Do not forward legacy callers' usage fields to the model either.
            material = {"tasks": data["tasks"], "detected_clients": data.get("detected_clients", []),
                        "past_topics": history_topics(monitor)}
            payload = {"model": self.config["model"], "temperature": 0.95,
                       "max_completion_tokens": 200, "thinking": {"type": "disabled"},
                       "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": json.dumps(material, ensure_ascii=False)}]}
            headers = {"Content-Type": "application/json", "api-key": self.config["key"]}
            request = urllib.request.Request(self.config["url"],
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
            with self.opener(request, timeout=10) as response:
                reply = json.loads(response.read().decode("utf-8"))
            content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            # Parse only the user-visible field; no raw model response is logged.
            match = re.search(r"\{.*\}", content, flags=re.S)
            values = json.loads(match.group(0)) if match else {}
            idea = _label(values.get("idea") or values.get("next"), 64).strip("。；！ ")
            if len(idea) < 8:
                raise ValueError("MiMo advice lacked an idea")
            if USAGE_ADVICE.search(idea):
                raise ValueError("MiMo returned usage advice")
            if FILLER.search(idea):
                raise ValueError("MiMo returned stock advice")
            result = ("", idea)
            with self.lock:
                self.signature, self.result, self.result_at = signature, result, time.time()
        except Exception as error:
            log.info("MiMo hint unavailable: %s", type(error).__name__)
            with self.lock:
                self.failed_until = time.time() + FAILURE_GAP
        finally:
            with self.lock:
                self.pending = False
