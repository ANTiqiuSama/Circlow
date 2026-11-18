"""The hover panel's hint: at most one line from MiMo.

Sessions waiting for the user are not hints: they show on their own task
bubbles (agent_tasks.with_waiting), read locally, with no model involved.

The hint is MiMo's: it reads the sessions of the last WORK_SECONDS in full
(the user's requests, the agent's last message, its steps and failures) and
writes at most one line, a working session that is stuck or drifting, or
one concrete idea about the work itself, or nothing. Secrets and e-mail
addresses are removed before anything is sent; quota and usage figures
never are.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request

import agent_tasks

log = logging.getLogger(__name__)
MIN_REQUEST_GAP = 90        # seconds between two MiMo requests
REFRESH_SECONDS = 600       # while something works, look again this often even if nothing changed
STALE_SECONDS = 1800        # a MiMo line is never shown older than this
FAILURE_GAP = 300
MAX_SESSIONS = 6            # sessions MiMo reads
HINT_MODEL = "mimo-v2.6-pro"
TIMEOUT = 90

NAMES = {"codex": "Codex", "claude": "Claude", "grok": "Grok", "gemini": "Gemini", "mimo": "MiMo"}
KIND_MARK = {"stuck": "留意"}

SYSTEM_PROMPT = (
    "你坐在一个人旁边，他同时开着好几个 AI agent（编程、科研、文档）。面板已经列出了哪些会话在等他回复，"
    "你只负责在下面写最多一行提醒。"
    "材料 sessions 是最近 45 分钟内他用过的每个会话：state（working 正在跑；blocked 等他批准；asked 停下来在问他；"
    "failed 出错或被中断；done 做完了），他提的要求（first 是最初的要求，asks 是最近几条），agent 最后对他说的话（reply），"
    "最近的动作（steps），本轮的报错（errors，older_errors 是更早的报错条数），minutes 是距今分钟数。"
    "只在下面两种情况写一条："
    "stuck：正在跑（working）的会话卡住或跑偏：同类报错反复出现、反复改同一处没有进展、做的事偏离了他的要求、"
    "要做有风险或不可逆的操作；说清卡在哪，以及该换的方向。"
    "idea：针对某个会话所做事情的内容本身，一条具体、有依据、懂行的建议：实验设计或结论证据的漏洞、被忽略的关键情况、"
    "明显更好的方法；必须引用材料里的具体内容，读的人能立刻判断对不对。stuck 优先于 idea。"
    "不要复述哪个会话在等他、问了什么（面板已经列出）。没有真正值得说的就输出 {\"kind\":\"none\"}，宁缺毋滥。"
    "他已经用 herdr 让多个 agent 互相同步和交接，也已经有可复用的文档和记忆："
    "绝不要谈多 agent 的分配、协作、同步、交接，也不要谈沉淀、复用、知识库、文档规范或工作习惯（收尾、休息、专注、节奏）。"
    "不要谈额度、token、用量或花费。不要编造材料里没有的事实，不确定就不写。"
    "只输出 JSON：{\"kind\":\"stuck|idea|none\",\"session\":\"会话 id\",\"name\":\"会话简称\",\"text\":\"一句话\"}。"
    "name 是这个会话在做的事的简称，4 到 10 个字，让他一眼认出是哪件事（例如“VANE 论文故事”“Lumen 屏幕图标”），"
    "不要用他原话的开头。text 用中文，12 到 30 个字，直接说事，不要重复 name。"
)
# Advice about the user's way of working, which herdr and his notes already cover.
META = re.compile(r"多\s*agent|多个\s*agent|分配|协作|同步|交接|沉淀|复用|可复用|知识库|规范|herdr|"
                  r"收尾|休息|节奏|专注|梳理|整理一下|循序渐进|一步一步")
USAGE_ADVICE = re.compile(
    r"用量|占比|消耗|额度|限额|使用高峰|(?:今天|今日|本日|累计|总计)输出|输出(?:量|总量)|"
    r"剩余?\s*\d+(?:\.\d+)?\s*[%％]|\b\d+(?:\.\d+)?\s*[KMB]?\s*(?:tokens?|tok/s)\b", re.I)
SECRET = re.compile(r"(?i)\b(?:api[_ -]?key|token|secret|password|passwd)\s*[:=]\s*\S+|\b(?:sk-|tp-|ghp_|gho_)[\w-]{12,}\b|"
                    r"\b[A-Za-z0-9_+/]{40,}={0,2}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _scrub(value: object, length: int) -> str:
    text = EMAIL.sub("[邮箱]", SECRET.sub("[隐藏]", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text if len(text) <= length else text[:length] + "…"


def session_name(session: dict) -> str:
    """The session's own name: the client's or herdr's title, else the
    request that says what it is about (agent_tasks.AgentNotes.work)."""
    return agent_tasks.clean(str(session.get("title") or "")) or str(session.get("project") or "")


def who(session: dict, names: dict | None = None) -> str:
    """"Claude·空气质量文献调研": the source and a short name for the session."""
    label = NAMES.get(str(session.get("provider") or ""), str(session.get("provider") or "agent").title())
    name = (names or {}).get(session.get("key")) or agent_tasks.shorten(session_name(session), 9)
    return f"{label}·{name}" if name else label


def context(work: list[dict], software: list[dict] | None = None) -> dict:
    """The sessions of TpsMonitor.recent_work, most pressing first; each gets
    a short id ("s1") for MiMo to answer with."""
    sessions = []
    for index, item in enumerate(list(work or []), 1):
        sessions.append({
            "id": f"s{index}", "key": item.get("key"), "provider": item.get("provider"),
            "state": item.get("state"), "project": item.get("project") or "", "title": _scrub(item.get("title"), 80),
            "detail": _scrub(item.get("detail"), 120),
            "minutes": item.get("minutes"), "running_minutes": item.get("running_minutes"),
            "first": _scrub(item.get("first"), 600),
            "asks": [_scrub(text, 500) for text in item.get("asks") or []][-3:],
            "reply": _scrub(item.get("reply"), 1400),
            "steps": [_scrub(step, 80) for step in item.get("steps") or []][-8:],
            "errors": [_scrub(text, 300) for text in item.get("errors") or []][-3:],
            "older_errors": item.get("older_errors") or 0,
        })
    return {"sessions": sessions, "time": time.strftime("%H:%M")}


def local_hint(data: dict) -> tuple:
    """Without MiMo there is no hint: an empty tuple."""
    return ()


def _signature(data: dict) -> str:
    # What changes the answer: which sessions, their state, a new request, a
    # new failure, a finished turn. Not the steps of a working agent (those
    # move every second; REFRESH_SECONDS covers them).
    return json.dumps([(s.get("key"), s.get("state"), len(s.get("asks") or []), (s.get("asks") or [""])[-1][:80],
                        len(s.get("errors") or []), s.get("reply", "")[-80:] if s.get("state") != "working" else "")
                       for s in data.get("sessions") or []], ensure_ascii=False)


def _material(data: dict) -> dict:
    sessions = [{key: value for key, value in session.items() if key not in ("key", "detail") and value not in ("", [], None)}
                for session in (data.get("sessions") or [])[:MAX_SESSIONS]]
    return {"time": data.get("time"), "sessions": sessions}


class HintAdvisor:
    def __init__(self, config: dict | None, opener=None):
        self.config = config if config and agent_tasks.is_mimo_url(config.get("url", "")) else None
        self.model = os.environ.get("CIRCLOW_HINT_MODEL") or HINT_MODEL
        self.opener = opener or urllib.request.urlopen
        self.lock = threading.Lock()
        self.signature = ""
        self.result: tuple | None = None     # (kind, session key, text, short name); kind "none" for nothing
        self.result_at = 0.0
        self.last_request_at = 0.0
        self.failed_until = 0.0
        self.pending = False

    def lookup(self, data: dict, monitor=None) -> tuple:
        """The hint rows, (kind, provider, text): MiMo's line while it still
        describes the sessions as they are; an empty tuple otherwise."""
        if not data.get("sessions") or self.config is None:
            return ()
        signature = _signature(data)
        now = time.time()
        working = any(s.get("state") == "working" for s in data["sessions"])
        with self.lock:
            current = self.result is not None and signature == self.signature and now - self.result_at < STALE_SECONDS
            due = not current or working and now - self.result_at >= REFRESH_SECONDS
            if due and not self.pending and now >= self.failed_until and now - self.last_request_at >= MIN_REQUEST_GAP:
                self.pending = True
                self.last_request_at = now
                threading.Thread(target=self._fetch, args=(signature, data), name="mimo-hint", daemon=True).start()
            result = self.result if current else None
        return self.render(result, data) if result else ()

    @staticmethod
    def render(result, data: dict) -> tuple:
        kind, key, text, name = result
        session = next((s for s in data.get("sessions") or [] if s.get("key") == key), None)
        if kind not in ("stuck", "idea") or session is None:
            return ()
        label = who(session, {key: name} if name else None)
        mark = KIND_MARK.get(kind)
        return ((kind, str(session.get("provider") or ""), f"{mark} {label}：{text}" if mark else f"{label}：{text}"),)

    def _fetch(self, signature: str, data: dict) -> None:
        try:
            payload = {"model": self.model, "temperature": 0.3, "max_completion_tokens": 4000,
                       "thinking": {"type": "enabled"},
                       "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                    {"role": "user", "content": json.dumps(_material(data), ensure_ascii=False)}]}
            headers = {"Content-Type": "application/json", "api-key": self.config["key"]}
            request = urllib.request.Request(self.config["url"],
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
            with self.opener(request, timeout=TIMEOUT) as response:
                reply = json.loads(response.read().decode("utf-8"))
            content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            result = self.parse(content, data)
            with self.lock:
                self.signature, self.result, self.result_at = signature, result, time.time()
            log.info("MiMo hint: %s", result[0])          # the kind only; never the text or the material
        except Exception as error:
            log.info("MiMo hint unavailable: %s", type(error).__name__)
            with self.lock:
                self.failed_until = time.time() + FAILURE_GAP
        finally:
            with self.lock:
                self.pending = False

    @staticmethod
    def parse(content: str, data: dict) -> tuple:
        """(kind, session key, text, name); ("none", "", "", "") for anything
        that does not hold up: an unknown session, a "stuck" for a session
        that is not working, workflow or usage advice, an empty or overlong line."""
        match = re.search(r"\{.*\}", content, flags=re.S)
        values = json.loads(match.group(0)) if match else {}
        if isinstance(values.get("items"), list):          # tolerate the list form
            values = next((item for item in values["items"] if isinstance(item, dict)
                           and str(item.get("kind") or "").lower() in ("stuck", "idea")), {})
        kind = str(values.get("kind") or "none").strip().lower()
        sessions = {s["id"]: s for s in data.get("sessions") or []}
        session = sessions.get(str(values.get("session") or "").strip())
        text = re.sub(r"\s+", " ", str(values.get("text") or "")).strip().strip("。；！ ")
        name = re.sub(r"\s+", " ", str(values.get("name") or "")).strip().strip("。；！“”\"' ")
        name = agent_tasks.shorten(name, 10) if 2 <= len(name) <= 16 else ""
        none = ("none", "", "", "")
        if kind not in ("stuck", "idea") or session is None:
            return none
        if kind == "stuck" and session.get("state") != "working":
            kind = "idea"
        if not 4 <= len(text) <= 40 or META.search(text) or USAGE_ADVICE.search(text):
            return none
        return kind, session["key"], text, name
