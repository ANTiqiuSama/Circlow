"""Community forecasts of OpenAI's early, global Codex usage resets.

OpenAI's Codex team occasionally resets everyone's Codex usage limits ahead of
schedule (announced by @thsottiaux on X).  Independent community trackers
publish a 24 h / 48 h probability for the next such reset.  This module reads
those public, key-free JSON endpoints; it sends no account data.  The figures
are community estimates, not an OpenAI schedule.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import app_identity

ENABLED = (os.environ.get("CIRCLOW_RESET_WATCH") or "1").strip().lower() not in ("0", "off", "false", "no")
try:
    ALERT_PERCENT = max(1, min(100, int(os.environ.get("CIRCLOW_RESET_ALERT") or 60)))
except ValueError:
    ALERT_PERCENT = 60
POLL_SECONDS = 30 * 60           # normal cadence
POLL_SECONDS_HIGH = 10 * 60      # while a reset looks likely
STALE_SECONDS = 3 * 3600         # older forecasts are not shown as current
USER_AGENT = app_identity.USER_AGENT


@dataclass
class ResetOutlook:
    source: str
    page: str
    prob_24: int | None
    prob_48: int | None
    last_reset_at: float | None = None
    last_reset_id: str | None = None
    last_reset_text: str = ""
    promise: str = ""
    fetched_at: float = 0.0
    signal_at: float | None = None          # an official post the source scores as a reset promise
    signal_percent: int | None = None       # how strongly the source reads that post
    readings: list = field(default_factory=list)   # per source: {"source", "prob_24", "prob_48", "signal"}

    def level(self, now: float | None = None) -> str:
        """"high" (24 h chance at or above the alert), "watch" (48 h chance at
        or above it), "low", or "unknown" when the forecast is missing/old."""
        now = now or time.time()
        if self.prob_24 is None or now - self.fetched_at > STALE_SECONDS:
            return "unknown"
        if self.prob_24 >= ALERT_PERCENT:
            return "high"
        if (self.prob_48 or 0) >= ALERT_PERCENT:
            return "watch"
        return "low"


def _when(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:32]).timestamp()
    except ValueError:
        # Nanosecond fractions ("…04:18:22.032775575Z") exceed fromisoformat.
        text = str(value)
        if "." in text:
            head, tail = text.split(".", 1)
            zone = "+00:00" if tail.endswith("Z") else ""
            try:
                return datetime.fromisoformat(head + "." + tail.rstrip("Z")[:6] + zone).timestamp()
            except ValueError:
                return None
        return None


def _percent(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(max(0.0, min(100.0, number))))


def parse_willreset(data: dict) -> ResetOutlook:
    promise = str(data.get("promise_quote") or "") if data.get("promise_active") else ""
    return ResetOutlook("willreset.com", "https://willreset.com/", _percent(data.get("prob_24")), _percent(data.get("prob_48")),
                        _when(data.get("last_reset_at")), str(data.get("last_reset_id") or "") or None,
                        str(data.get("last_reset_text") or "")[:80], promise[:80])


def parse_codex_reset(data: dict) -> ResetOutlook:
    probabilities = data.get("probabilities") or {}
    hint = data.get("tease_signal") or {}
    quote = ((hint.get("post") or {}).get("quote") or "") if hint else ""
    last = data.get("last_reset_at")
    outlook = ResetOutlook("codex-reset.com", "https://codex-reset.com/", _percent(probabilities.get("rounded_24h")),
                           _percent(probabilities.get("rounded_48h")), _when(last), str(last) if last else None, "", str(quote)[:80])
    # Its published probability is the statistical model only; an official post it
    # scores as a promise ("it's reset day today") is reported separately.
    signal = data.get("official_signal") or {}
    band = ((signal.get("score") or {}).get("band") or (data.get("signal_score") or {}).get("band") or "")
    if signal.get("at") and band == "promise":
        outlook.signal_at = _when(signal.get("at"))
        outlook.signal_percent = _percent(probabilities.get("signal_percent") or probabilities.get("commitment_floor_percent"))
        outlook.promise = str(signal.get("summary") or "").split("\n", 1)[0][:80] or outlook.promise
    return outlook


def parse_willcodexreset(data: dict) -> ResetOutlook:
    data = data.get("data") or {}
    resets = [event for event in data.get("events") or [] if event.get("kind") == "reset"]
    latest = max(resets, key=lambda event: str(event.get("occurredAt") or ""), default=None)
    return ResetOutlook("willcodexreset.com", "https://willcodexreset.com/", _percent(data.get("probability24h")),
                        _percent(data.get("probability48h")), _when(latest.get("occurredAt")) if latest else None,
                        str(latest.get("id")) if latest else None, str(latest.get("title") or "")[:80] if latest else "")


# Small documented payloads first; willcodexreset.com's radar is ~1 MB.
SOURCES: list[tuple[str, Callable[[dict], ResetOutlook]]] = [
    ("https://willreset.com/api/notify", parse_willreset),
    ("https://codex-reset.com/api/forecast", parse_codex_reset),
    ("https://willcodexreset.com/api/reset-radar", parse_willcodexreset),
]
SIGNAL_SECONDS = 24 * 3600       # an official promise counts for a day, until a reset follows it


def _get_json(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(4_000_000).decode("utf-8"))


def effective(outlook: ResetOutlook, last_reset: float | None, now: float) -> tuple[int | None, int | None, bool]:
    """(24 h, 48 h, promised) of one source, raised to its own reading of a
    fresh official promise that no reset has followed yet."""
    p24, p48 = outlook.prob_24, outlook.prob_48
    promised = bool(outlook.signal_at and outlook.signal_percent and now - outlook.signal_at <= SIGNAL_SECONDS
                    and not (last_reset and last_reset >= outlook.signal_at))
    if promised:
        p24 = max(p24 or 0, outlook.signal_percent)
        p48 = max(p48 or 0, outlook.signal_percent)
    return p24, p48, promised


def combine(outlooks: list[ResetOutlook], now: float) -> ResetOutlook:
    """One forecast from every source that answered: the mean of their 24 h /
    48 h figures (each raised by a fresh official promise it reports), the most
    recent reset any of them saw, and the per-source readings for display."""
    latest = max(outlooks, key=lambda o: o.last_reset_at or 0.0)
    last_reset = latest.last_reset_at
    readings = []
    for outlook in outlooks:
        p24, p48, promised = effective(outlook, last_reset, now)
        readings.append({"source": outlook.source, "prob_24": p24, "prob_48": p48, "signal": promised})
    mean = lambda values: round(sum(values) / len(values)) if values else None  # noqa: E731
    p24 = mean([r["prob_24"] for r in readings if r["prob_24"] is not None])
    p48 = mean([r["prob_48"] for r in readings if r["prob_48"] is not None])
    lead = max(outlooks, key=lambda o: o.prob_24 or 0)
    promise = next((o.promise for o in outlooks if o.promise), "")
    name = outlooks[0].source if len(outlooks) == 1 else f"{len(outlooks)} 个来源"
    return ResetOutlook(name, lead.page, p24, p48, last_reset, latest.last_reset_id, latest.last_reset_text, promise, now,
                        readings=readings)


def fetch_outlook(get: Callable[[str], dict] = _get_json, now: float | None = None) -> tuple[ResetOutlook | None, list[str]]:
    """Every source, read in parallel and combined; plus the errors seen.
    One site's estimate alone was misleading: one model ignored an official
    "reset day today" post that another raised to 89 %."""
    from concurrent.futures import ThreadPoolExecutor
    now = now or time.time()
    errors, answers = [], []

    def read(source):
        url, parse = source
        try:
            return url, parse(get(url)), None
        except Exception as error:   # network, JSON or layout change: the other sources still count
            return url, None, f"{url}: {type(error).__name__}: {error}"

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        for url, outlook, error in pool.map(read, SOURCES):
            if error:
                errors.append(error)
            elif outlook.prob_24 is None:
                errors.append(f"{url}: no probability")
            else:
                answers.append(outlook)
    if not answers:
        return None, errors
    return combine(answers, now), errors


def load(path: Path) -> ResetOutlook | None:
    try:
        return ResetOutlook(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save(path: Path, outlook: ResetOutlook) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(outlook), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def panel_line(outlook: ResetOutlook | None, now: float) -> str:
    """The banner's short left part: the combined 24 h chance only."""
    if outlook is None:
        return "Codex 重置 读取中"
    if outlook.level(now) == "unknown":
        return "Codex 重置 —"
    return f"Codex 重置 {outlook.prob_24}%"


def announced(outlook: ResetOutlook | None, now: float) -> bool:
    """A fresh official promise reported by any source (shown as a badge)."""
    return bool(outlook and outlook.level(now) != "unknown" and any(r.get("signal") for r in outlook.readings))
