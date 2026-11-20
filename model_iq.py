"""Model "IQ" from AI Stupid Level (aistupidlevel.info).

AI Stupid Level re-benchmarks frontier models around the clock and publishes a
0-100 score per model.  The tray shows the 24 h coding score only.  Its data API needs
a free key (Authorization: Bearer asl_live_...), and the free tier allows 10
calls a day (quota resets 00:00 UTC).  This module therefore reads at most
AUTO_CALLS_PER_DAY times a day on its own (every READ_SECONDS), keeps the rest
for "立即读取" in the settings, counts every call on disk so restarts do not
spend extra calls, and stops for the day on HTTP 429.

Terms: anywhere the data is shown, cite aistupidlevel.info as the source.
The settings page links it; the panel line is short and opens that page.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import app_identity

API_URL = "https://aistupidlevel.info/api/v1/models?period=24h&sortBy=coding"   # the coding board only
SITE = "https://aistupidlevel.info/"
SIGNUP = "https://aistupidlevel.info/router/data-keys"
USER_AGENT = app_identity.USER_AGENT
DAILY_LIMIT = 10                 # free key: 10 calls per UTC day
AUTO_CALLS_PER_DAY = 7           # automatic reads; the other 3 stay for manual refresh
READ_SECONDS = 3 * 3600          # automatic cadence (7 reads span 21 h)
HOVER_MAX = 3                    # models on the hover panel line
ENV_KEY = "CIRCLOW_IQ_KEY"


@dataclass
class ModelScore:
    id: str
    name: str
    provider: str
    score: float | None
    trend: str = ""              # up | down | stable | ""
    status: str = ""             # e.g. good | warning | critical
    lower: float | None = None
    upper: float | None = None
    updated: str = ""

    @property
    def short(self) -> str:
        return short_name(self.name)


@dataclass
class IqState:
    models: list = field(default_factory=list)      # ModelScore dicts, best first
    fetched_at: float = 0.0                         # last successful read
    attempted_at: float = 0.0                       # last call, successful or not
    calls: dict = field(default_factory=dict)       # UTC day -> calls spent
    blocked_day: str = ""                           # 429 seen: no calls until the next UTC day
    error: str = ""
    board: str = "coding"                      # provenance of the cached score values
    synced_from: str = ""                      # another device's label when its read is the one kept

    def scores(self) -> list[ModelScore]:
        result = []
        for item in self.models:
            try:
                result.append(ModelScore(**item))
            except TypeError:
                continue
        return result


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def calls_today(state: IqState, now: float) -> int:
    return int(state.calls.get(utc_day(now), 0))


def remaining_today(state: IqState, now: float) -> int:
    if state.blocked_day == utc_day(now):
        return 0
    return max(0, DAILY_LIMIT - calls_today(state, now))


def due(state: IqState, now: float) -> bool:
    """An automatic read is due: old enough, within the automatic budget."""
    if state.blocked_day == utc_day(now):
        return False
    # A failed call also waits the full interval: a bad key must not spend the day's calls.
    # A fresh result synced from another device counts as a read here too.
    return now - state.attempted_at >= READ_SECONDS and now - state.fetched_at >= READ_SECONDS and \
        calls_today(state, now) < AUTO_CALLS_PER_DAY


# ----- names ---------------------------------------------------------------------------------

_PROVIDERS = {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "xai": "xAI", "deepseek": "DeepSeek",
              "kimi": "Kimi", "moonshot": "Kimi", "glm": "GLM", "zhipu": "GLM", "qwen": "Qwen", "alibaba": "Qwen",
              "mistral": "Mistral", "meta": "Meta", "xiaomi": "Xiaomi"}


def provider_label(provider: str) -> str:
    return _PROVIDERS.get((provider or "").lower(), provider or "")


def short_name(name: str) -> str:
    """Compact panel name: "claude-opus-4-7-20260101" -> "Opus 4.7",
    "gpt-5.6-sol" -> "GPT-5.6 Sol", "gemini-3-pro-preview" -> "Gemini 3 Pro"."""
    text = re.sub(r"[-_ ](20\d{6}|\d{4}-\d{2}-\d{2}|latest|preview|exp)\b", "", (name or "").strip(), flags=re.I)
    text = re.sub(r"^(anthropic|openai|google|x-ai|xai)[/:]", "", text, flags=re.I)
    text = re.sub(r"^claude[-_ ]", "", text, flags=re.I)
    text = re.sub(r"(?<=\d)-(?=\d\b)", ".", text)               # opus-4-7 -> opus-4.7
    words = [w for w in re.split(r"[-_ ]+", text) if w]
    if words and words[0].lower() == "gpt" and len(words) > 1:
        words = ["GPT-" + words[1]] + words[2:]
    pretty = []
    for word in words:
        low = word.lower()
        if low in ("gpt", "glm", "o1", "o3", "o4"):
            pretty.append(word.upper())
        elif word.startswith("GPT-") or any(ch.isdigit() for ch in word[:1]):
            pretty.append(word)
        else:
            pretty.append(word[:1].upper() + word[1:])
    return " ".join(pretty) or name


_MAKERS = (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"), ("claude", "anthropic"),
           ("opus", "anthropic"), ("sonnet", "anthropic"), ("haiku", "anthropic"), ("fable", "anthropic"),
           ("gemini", "google"), ("grok", "xai"), ("kimi", "kimi"), ("deepseek", "deepseek"), ("qwen", "qwen"),
           ("glm", "glm"), ("mimo", "xiaomi"), ("mistral", "mistral"), ("llama", "meta"))


def maker(score: ModelScore) -> str:
    """Who makes the model, for "one model per maker": the board's provider
    (aliases folded: moonshot is Kimi), else the name's family; a model of
    unknown family counts as a maker of its own."""
    label = provider_label(score.provider)
    if label:
        return label.lower()
    name = _key(score.name)
    return next((who for prefix, who in _MAKERS if name.startswith(prefix)), name)


def best_per_maker(scores: list[ModelScore], skip: set[str] | None = None) -> list[ModelScore]:
    """Each maker's highest coding score, best first; makers in `skip` left out.
    Ties keep the board's order."""
    taken = set(skip or ())
    result = []
    for score in sorted((s for s in scores if s.score is not None), key=lambda s: -s.score):
        who = maker(score)
        if who not in taken:
            result.append(score)
            taken.add(who)
    return result


def default_selection(scores: list[ModelScore]) -> list[str]:
    """What the panel line shows while nothing runs: the HOVER_MAX best
    makers' best models."""
    return [s.name for s in best_per_maker(scores)[:HOVER_MAX]]


def hover_models(state: IqState | None, running: list[str] | None = None) -> list[tuple[ModelScore, bool]]:
    """(model, running) shown on the panel line, HOVER_MAX in all: on the
    left the model the source is running right now (the TPS figure's), then
    the smartest models by coding score, one per maker and none of the
    running model's maker (never Astra next to Sol, or Opus next to Fable)."""
    if state is None:
        return []
    scores = state.scores()
    result, makers = [], set()
    for local in running or ():
        found = match(local, scores)
        if found and maker(found) not in makers:
            result.append((found, True))
            makers.add(maker(found))
    result += [(score, False) for score in best_per_maker(scores, makers)]
    return result[:HOVER_MAX]


def score_text(score: ModelScore) -> str:
    """The coding score, a bare number."""
    return "—" if score.score is None else str(round(score.score))


def _key(name: str) -> str:
    """Comparable model id: lower case, no date / preview suffix, one separator
    ("claude-opus-4.7-20260101" == "Claude Opus 4-7")."""
    text = re.sub(r"[-_ ](20\d{6}|\d{4}-\d{2}-\d{2}|latest|preview|exp)\b", "", (name or "").strip().lower())
    text = re.sub(r"^(anthropic|openai|google|x-ai|xai)[/:]", "", text)
    return re.sub(r"[-_. /]+", "-", text).strip("-")


def match(local: str, scores: list[ModelScore]) -> ModelScore | None:
    """The scored model a client reports (e.g. Codex's "gpt-6.1-sol"):
    the same id, else the longest scored id that the local one starts with."""
    wanted = _key(local)
    if not wanted:
        return None
    exact = next((s for s in scores if _key(s.name) == wanted), None)
    if exact:
        return exact
    prefixed = [s for s in scores if wanted.startswith(_key(s.name) + "-")]
    return max(prefixed, key=lambda s: len(_key(s.name)), default=None)


# ----- reading --------------------------------------------------------------------------------

def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _coding(row: dict) -> float | None:
    """The coding board's score: an explicit coding field when present, else
    the board score (the request asks for sortBy=coding)."""
    nested = row.get("scores") if isinstance(row.get("scores"), dict) else {}
    for value in (row.get("codingScore"), row.get("coding"), nested.get("coding"), row.get("currentScore"), row.get("score")):
        number = _number(value)
        if number is not None:
            return number
    return None


def parse(payload: dict) -> list[ModelScore]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected response: no data list")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        result.append(ModelScore(str(row.get("id") or row["name"]), str(row["name"]), str(row.get("provider") or ""),
                                 _coding(row), str(row.get("trend") or ""),
                                 str(row.get("status") or ""), _number(row.get("confidenceLower")),
                                 _number(row.get("confidenceUpper")), str(row.get("lastUpdated") or "")))
    result.sort(key=lambda s: -(s.score if s.score is not None else -1))
    return result


def _request(key: str, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                       "Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(4_000_000).decode("utf-8"))


def read(state: IqState, key: str, now: float, get: Callable[[str], dict] | None = None) -> IqState:
    """One API call (counted before it is sent); updates and returns `state`."""
    get = get or _request
    day = utc_day(now)
    state.calls = {d: n for d, n in state.calls.items() if d >= utc_day(now - 3 * 86400)}
    state.calls[day] = calls_today(state, now) + 1
    state.attempted_at = now
    try:
        models = parse(get(key))
    except urllib.error.HTTPError as error:
        if error.code == 429:
            state.blocked_day = day
            state.error = "今日免费额度已用完（00:00 UTC 重置）"
        elif error.code in (401, 403):
            state.error = "API Key 无效或已撤销"
        else:
            state.error = f"读取失败（HTTP {error.code}）"
        return state
    except Exception as error:   # network or layout change: keep the last scores
        state.error = f"读取失败：{type(error).__name__}"
        return state
    if not models:                 # an empty answer never wipes the last result
        state.error = "读取失败：返回为空"
        return state
    state.models = [asdict(m) for m in models]
    state.fetched_at, state.error = now, ""
    state.board, state.synced_from = "coding", ""
    return state


# ----- other devices --------------------------------------------------------------------------

def export(state: IqState | None) -> dict | None:
    """The last coding-board result for the other devices (sync_hub): the
    scores and when they were read; never the key or the call count."""
    if state is None or not state.fetched_at or state.board != "coding":
        return None
    models = [asdict(score) for score in state.scores()]
    return {"models": models, "fetched_at": state.fetched_at} if models else None


def adopt(state: IqState, shared: Any, source: str) -> bool:
    """Take another device's result (see export) when it was read later
    than the one here; True when taken. A device without a key shows the
    scores read elsewhere, and one with a key waits READ_SECONDS from them
    before spending a call of its own. Its own last error stays."""
    if not isinstance(shared, dict) or not isinstance(shared.get("models"), list):
        return False
    fetched = _number(shared.get("fetched_at"))
    if fetched is None or fetched <= state.fetched_at:
        return False
    models = []
    for item in shared["models"]:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        models.append(ModelScore(str(item.get("id") or item["name"]), str(item["name"]), str(item.get("provider") or ""),
                                 _number(item.get("score")), str(item.get("trend") or ""), str(item.get("status") or ""),
                                 _number(item.get("lower")), _number(item.get("upper")), str(item.get("updated") or "")))
    if not models:
        return False
    models.sort(key=lambda s: -(s.score if s.score is not None else -1))
    state.models = [asdict(m) for m in models]
    state.fetched_at, state.board, state.synced_from = fetched, "coding", source
    return True


# ----- storage --------------------------------------------------------------------------------

def load_state(path: Path) -> IqState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = IqState(**{k: v for k, v in data.items() if k in IqState.__dataclass_fields__})
        if data.get("board") != "coding":
            # Older releases cached the combined board. Keep showing those last
            # figures (the panel always shows the last result) until the next
            # coding-board read (the normal 3-hourly one) replaces them.
            state.board = "legacy"
        return state
    except (OSError, ValueError, TypeError, AttributeError):
        return IqState()


def save_state(path: Path, state: IqState) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def load_config(path: Path) -> dict:
    """{"key": str, "shown": [model names] or None for the default}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    shown = data.get("shown")
    return {"key": str(data.get("key") or "").strip(),
            "shown": [str(n) for n in shown][:HOVER_MAX] if isinstance(shown, list) else None}


def save_config(path: Path, key: str, shown: list[str] | None) -> None:
    key = (key or "").strip()
    if key and not re.fullmatch(r"[A-Za-z0-9_\-.]{8,200}", key):
        raise ValueError("API Key 格式不正确（应形如 asl_live_…）")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"key": key, "shown": None if shown is None else list(shown)[:HOVER_MAX]},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def effective_key(config: dict) -> str:
    return (os.environ.get(ENV_KEY) or config.get("key") or "").strip()


def age_text(state: IqState, now: float) -> str:
    if not state.fetched_at:
        return "尚未读取"
    minutes = max(0, int(now - state.fetched_at) // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    return f"{minutes // 60} 小时前"
