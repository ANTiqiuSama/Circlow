"""Quota exhaustion forecast from the app's own refresh history.

Each successful refresh stores (time, used %, reset time) per window. The burn
rate is a least-squares slope over the current reset window only, so a reset
never looks like negative usage. Nothing beyond these numbers is stored.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

HISTORY_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray" / "history.json"
KEEP_SECONDS = 8 * 86400        # Longer than the longest (weekly) window.
KEEP_SAMPLES = 2000             # Per window key; repeated readings are folded (see add), so this spans the week.
LOOKBACK_SECONDS = 3 * 3600     # Recent pace matters more than the whole window.
MIN_SPAN_SECONDS = 10 * 60      # Do not extrapolate from a few seconds.
FAST_LOOKBACK_SECONDS = 20 * 60 # A sustained burst can justify an earlier, conditional forecast.
FAST_MIN_SPAN_SECONDS = 60
FAST_MIN_SPEND = 3.0             # percentage points, across at least three readings
FAST_MAX_AGE_SECONDS = 120
LOW_SPARE_PERCENT = 5.0
CLAUDE_DROP_TOLERANCE = 5.0   # same reset: reject partial recovery, retain >=95% full resets
RESET_TOLERANCE = 180           # Reset timestamps jitter a little between reads.
IDLE_RATE = 0.05                # %/hour treated as "no measurable use".


@dataclass(frozen=True)
class Sample:
    at: float
    used: float
    resets_at: int | None


@dataclass(frozen=True)
class Forecast:
    tone: str                       # "ok" | "warn" | "danger" | "idle" | "unknown"
    text: str
    rate_per_hour: float | None = None
    exhausts_at: float | None = None
    before_reset: bool | None = None


def duration_text(seconds: float) -> str:
    """Compact, one unit only: whole days (rounded) from a day up, whole hours
    from an hour up, minutes below an hour, e.g. "2 天", "3 小时", "35 分钟"."""
    seconds = max(0, int(seconds))
    if seconds < 3570:
        return f"{max(1, round(seconds / 60))} 分钟"
    hours = round(seconds / 3600)
    if hours >= 24:
        return f"{max(1, round(seconds / 86400))} 天"
    return f"{hours} 小时"


def reset_distance(timestamp: float | None, now: float) -> str | None:
    """Time left until a reset as a column label: "4小时32分钟", "3天7小时",
    "323天". None when the reset is unknown or already due."""
    if not timestamp or timestamp <= now:
        return None
    minutes = max(1, int((timestamp - now + 59) // 60))   # round up: never "0分钟" before a reset
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days >= 30:
        return f"{days}天"
    if days:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if hours:
        return f"{hours}小时{minutes}分钟" if minutes else f"{hours}小时"
    return f"{minutes}分钟"


def same_window(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= RESET_TOLERANCE


def current_window(samples: Iterable[Sample], resets_at: int | None) -> list[Sample]:
    """Samples belonging to the live window, cut at the last visible reset."""
    ordered = sorted(samples, key=lambda s: s.at)
    if resets_at is not None:
        ordered = [s for s in ordered if same_window(s.resets_at, resets_at)]
    window: list[Sample] = []
    for sample in ordered:
        if window and sample.used < window[-1].used - 1.0:
            window = []  # Usage dropped: a reset happened inside the series.
        window.append(sample)
    return window


def history_curve(samples: Iterable[Sample], floor: float, now: float,
                  minutes: int | None = None) -> tuple[list[tuple[float, float]], set[int]]:
    """Remaining-% points from `floor` to `now` across windows, and the
    indices of the reset steps (to keep when thinning). An earlier window runs
    to its reset, where the line steps back up to 100%; a window of known
    length also starts at 100% from the moment it began."""
    ordered = sorted((s for s in samples if floor <= s.at <= now), key=lambda s: s.at)
    segments: list[list[Sample]] = []
    for sample in ordered:
        last = segments[-1][-1] if segments else None
        if last is None or not same_window(last.resets_at, sample.resets_at) or sample.used < last.used - 1.0:
            segments.append([sample])
        else:
            segments[-1].append(sample)
    points: list[tuple[float, float]] = []
    steps: set[int] = set()
    for index, segment in enumerate(segments):
        first = segment[0]
        after = floor
        if index:
            last = segments[index - 1][-1]
            after = last.at
            if last.resets_at and last.at <= last.resets_at <= first.at:
                steps.update((len(points), len(points) + 1))
                points += [(float(last.resets_at), 100.0 - last.used), (float(last.resets_at), 100.0)]
                after = last.resets_at
        begun = first.resets_at - minutes * 60 if first.resets_at and minutes else None
        if begun and after < begun < first.at:
            steps.add(len(points))
            points.append((float(begun), 100.0))    # a window starts unused: draw from there
        points += [(s.at, 100.0 - s.used) for s in segment]
    return points, steps


def slope_per_hour(samples: list[Sample]) -> float:
    n = len(samples)
    mean_t = sum(s.at for s in samples) / n
    mean_u = sum(s.used for s in samples) / n
    var = sum((s.at - mean_t) ** 2 for s in samples)
    if var <= 0:
        return 0.0
    cov = sum((s.at - mean_t) * (s.used - mean_u) for s in samples)
    return cov / var * 3600


def forecast(samples: Iterable[Sample], now: float, remaining: float | None, resets_at: int | None) -> Forecast:
    if remaining is None:
        return Forecast("unknown", "额度未知，暂无法预测")
    if remaining <= 0.5:
        tail = f"，{duration_text(resets_at - now)}后重置" if resets_at and resets_at > now else ""
        return Forecast("danger", f"额度已用完{tail}", exhausts_at=now, before_reset=True)
    window = current_window(samples, resets_at)
    recent = [s for s in window if s.at >= now - LOOKBACK_SECONDS]
    if len(recent) < 2 or recent[-1].at - recent[0].at < MIN_SPAN_SECONDS:
        recent = window  # Fall back to the whole window when recent data is thin.
    regular = len(recent) >= 2 and recent[-1].at - recent[0].at >= MIN_SPAN_SECONDS
    fast = [s for s in window if s.at >= now - FAST_LOOKBACK_SECONDS]
    fast_ready = (len(fast) >= 3 and fast[-1].at - fast[0].at >= FAST_MIN_SPAN_SECONDS
                  and fast[-1].used - fast[0].used >= FAST_MIN_SPEND
                  and now - fast[-1].at <= FAST_MAX_AGE_SECONDS
                  and all(right.used >= left.used - 0.1 for left, right in zip(fast, fast[1:])))
    if not regular and not fast_ready:
        return Forecast("unknown", "正在积累用量记录，稍后给出预测")
    regular_rate = slope_per_hour(recent) if regular else 0.0
    fast_rate = slope_per_hour(fast) if fast_ready else 0.0
    short_pace = fast_ready and fast_rate > regular_rate * 1.2
    rate = fast_rate if short_pace else regular_rate
    pace = "按近期较快用量估算，" if short_pace else "按当前速度"
    if rate < IDLE_RATE:
        return Forecast("idle", "近期几乎没有消耗，重置前够用", rate)
    exhausts_at = now + remaining / rate * 3600
    if resets_at and resets_at > now and exhausts_at >= resets_at:
        spare = remaining - rate * (resets_at - now) / 3600
        if spare <= LOW_SPARE_PERCENT:
            return Forecast("warn", f"{pace}重置前可能接近用尽，预计剩余 {max(0, spare):.0f}%", rate, exhausts_at, False)
        return Forecast("ok", f"{pace}重置前够用，约剩 {spare:.0f}%", rate, exhausts_at, False)
    left = exhausts_at - now
    if resets_at and resets_at > now:
        gap = resets_at - exhausts_at
        tone = "danger" if left < 3600 else "warn"
        return Forecast(tone, f"{pace}约 {duration_text(left)}后用完，早于重置 {duration_text(gap)}", rate, exhausts_at, True)
    return Forecast("warn" if left < 3 * 3600 else "ok", f"{pace}约 {duration_text(left)}后用完", rate, exhausts_at, None)


class QuotaHistory:
    """Small persisted series keyed by "provider" and "provider:secondary"."""

    def __init__(self, path: Path = HISTORY_PATH):
        self.path = path
        self.lock = Lock()
        self.series: dict[str, list[Sample]] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for name, items in payload.get("series", {}).items():
                readings = [Sample(float(i[0]), float(i[1]), None if i[2] is None else int(i[2])) for i in items]
                self.series[name] = self._without_false_claude_recoveries(name, readings)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            self.series = {}

    @staticmethod
    def _without_false_claude_recoveries(name: str, readings: list[Sample]) -> list[Sample]:
        """Repair old saved spikes without erasing real new reset windows."""
        if name not in ("claude", "claude:secondary"):
            return readings
        clean: list[Sample] = []
        for item in sorted(readings, key=lambda row: row.at):
            if clean and same_window(clean[-1].resets_at, item.resets_at) and \
                    clean[-1].used - item.used >= CLAUDE_DROP_TOLERANCE and item.used > 5:
                continue
            clean.append(item)
        return clean

    def save(self) -> None:
        with self.lock:
            payload = {"series": {k: [[s.at, s.used, s.resets_at] for s in v] for k, v in self.series.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass

    def add(self, name: str, at: float | None, used: float | None, resets_at: int | None) -> bool:
        if at is None or used is None:
            return False
        with self.lock:
            series = self.series.setdefault(name, [])
            if series and abs(series[-1].at - at) < 1:
                return False  # Same reading (e.g. stale value carried forward).
            sample = Sample(float(at), float(used), resets_at)
            if name in ("claude", "claude:secondary") and series and \
                    same_window(series[-1].resets_at, resets_at) and \
                    series[-1].used - sample.used >= CLAUDE_DROP_TOLERANCE and sample.used > 5:
                return False
            if len(series) >= 2 and series[-1].at < at and all(
                    s.used == sample.used and same_window(s.resets_at, resets_at) for s in series[-2:]):
                # A run of equal readings keeps its first and latest only: hovering
                # reads every few seconds, and those repeats used to push the
                # older curve out of the kept samples.
                series[-1] = sample
            else:
                series.append(sample)
                series.sort(key=lambda s: s.at)
            cutoff = at - KEEP_SECONDS
            self.series[name] = [s for s in series if s.at >= cutoff][-KEEP_SAMPLES:]
        return True

    def clear(self, provider: str) -> None:
        """Discard an incompatible provider/pool history before recording a new scope."""
        with self.lock:
            for name in list(self.series):
                if name == provider or name.startswith(provider + ":"):
                    self.series.pop(name)

    def record(self, snapshot: Any) -> bool:
        """Store a fresh snapshot (stale or failed reads are ignored)."""
        if getattr(snapshot, "stale", False) or getattr(snapshot, "error", None):
            return False
        changed = self.add(snapshot.provider, snapshot.updated_at, snapshot.used_percent, snapshot.resets_at)
        if getattr(snapshot, "secondary_used_percent", None) is not None:
            changed |= self.add(f"{snapshot.provider}:secondary", snapshot.updated_at,
                                snapshot.secondary_used_percent, snapshot.secondary_resets_at)
        for pool in getattr(snapshot, "pools", None) or []:
            # Independent model pools (Antigravity) each get their own series for the chart.
            if pool.get("key") and pool.get("used_percent") is not None:
                changed |= self.add(f"{snapshot.provider}:pool:{pool['key']}", snapshot.updated_at,
                                    pool["used_percent"], pool.get("resets_at"))
        return changed

    def samples(self, name: str) -> list[Sample]:
        with self.lock:
            return list(self.series.get(name, []))

    def forecast_for(self, snapshot: Any, now: float, secondary: bool = False) -> Forecast:
        if secondary:
            return forecast(self.samples(f"{snapshot.provider}:secondary"), now,
                            snapshot.secondary_remaining_percent, snapshot.secondary_resets_at)
        return forecast(self.samples(snapshot.provider), now, snapshot.remaining_percent, snapshot.resets_at)
