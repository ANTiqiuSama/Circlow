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

HISTORY_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Circlow" / "history.json"
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
# Windows a day long or more (weekly) are paced by the last day's average,
# idle hours included: their use comes in bursts with nights in between, and
# a three-hour burst carried across days ran out the week in hours.
DAY_PACE_MINUTES = 1440
DAY_LOOKBACK_SECONDS = 24 * 3600
DAY_MIN_SPAN_SECONDS = 6 * 3600


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


BLIP_POINTS = 5.0        # a reading this far off both neighbours, which agree, is a glitch
BLIP_RUN = 3             # at most this many such readings in a row


def _far(sample: Sample, reference: Sample) -> bool:
    return abs(sample.used - reference.used) >= BLIP_POINTS or not same_window(sample.resets_at, reference.resets_at)


def _backwards(sample: Sample, reference: Sample) -> bool:
    """Another window than `reference`, or BLIP_POINTS less used in the same
    one, which use within a window cannot do."""
    return not same_window(sample.resets_at, reference.resets_at) or sample.used < reference.used - BLIP_POINTS


def without_blips(samples: Iterable[Sample]) -> list[Sample]:
    """Readings with one-off glitches taken out. A glitch is up to BLIP_RUN
    readings in a row that went backwards from the reading before them (see
    _backwards) and are BLIP_POINTS off the reading after them, while those
    two agree (same window, use not going back); or a single reading that
    jumped BLIP_POINTS above both of its agreeing neighbours. A Claude status
    line from an idle session, or a /usage read that caught another window,
    shows up like this; a real reset does not, because the readings after it
    stay in the new window. The latest reading is not judged until the next."""
    out = sorted(samples, key=lambda s: s.at)
    index = 1
    while index < len(out) - 1:
        before = out[index - 1]
        end = index
        while end < len(out) and end - index < BLIP_RUN and _backwards(out[end], before):
            end += 1
        if index < end < len(out):
            after = out[end]
            if same_window(before.resets_at, after.resets_at) and after.used >= before.used - 1.0 \
                    and all(_far(item, after) for item in out[index:end]):
                del out[index:end]
                continue
        item, after = out[index], out[index + 1]
        if same_window(before.resets_at, after.resets_at) and same_window(item.resets_at, before.resets_at) \
                and after.used >= before.used - 1.0 and item.used >= max(before.used, after.used) + BLIP_POINTS:
            del out[index]
            continue
        index += 1
    return out


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
            elif same_window(last.resets_at, first.resets_at) and last.used - first.used >= CLAUDE_DROP_TOLERANCE \
                    and first.used <= 5:
                # Claude may report a genuine full recovery before it updates
                # the reset timestamp. Draw a step only at >=95% remaining.
                steps.update((len(points), len(points) + 1))
                points += [(first.at, 100.0 - last.used), (first.at, 100.0 - first.used)]
                after = first.at
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


def day_pace(window: list[Sample], now: float) -> float | None:
    """%/hour over the last day of a window's readings (from its start when
    younger), idle time included; None with under DAY_MIN_SPAN_SECONDS."""
    day = [s for s in window if s.at >= now - DAY_LOOKBACK_SECONDS]
    if len(day) < 2 or day[-1].at - day[0].at < DAY_MIN_SPAN_SECONDS:
        return None
    return max(0.0, day[-1].used - day[0].used) / ((day[-1].at - day[0].at) / 3600)


def forecast(samples: Iterable[Sample], now: float, remaining: float | None, resets_at: int | None,
             window_minutes: int | None = None) -> Forecast:
    """When `remaining` runs out at the window's pace, against its reset.
    The pace is the last three hours' (a sustained burst can raise it) or,
    for windows of a day or more, the last day's average."""
    if remaining is None:
        return Forecast("unknown", "额度未知，暂无法预测")
    if remaining <= 0.5:
        tail = f"，{duration_text(resets_at - now)}后重置" if resets_at and resets_at > now else ""
        return Forecast("danger", f"额度已用完{tail}", exhausts_at=now, before_reset=True)
    window = current_window(samples, resets_at)
    if window_minutes and window_minutes >= DAY_PACE_MINUTES:
        rate = day_pace(window, now)
        if rate is not None:
            return _project(rate, "按近一天的平均用量", now, remaining, resets_at)
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
    return _project(rate, "按近期较快用量估算，" if short_pace else "按当前速度", now, remaining, resets_at)


def _project(rate: float, pace: str, now: float, remaining: float, resets_at: int | None) -> Forecast:
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


def left_at_reset(outlook: Forecast, now: float, remaining: float | None, resets_at: int | None) -> float | None:
    """% expected to be left when the window resets, at the outlook's pace;
    None when unknown or the window runs out first."""
    if remaining is None or not resets_at or resets_at <= now or outlook.tone == "unknown" or outlook.before_reset:
        return None
    rate = max(0.0, outlook.rate_per_hour or 0.0)
    return max(0.0, remaining - rate * (resets_at - now) / 3600)


SPARE_AHEAD = 36 * 3600         # a weekly reset this close with this much to spare: say "可多用"
SPARE_PERCENT = 20.0


def outlook(windows: Iterable[tuple], now: float) -> tuple[str, str]:
    """One forecast line for a source's quota windows, each (label, Forecast,
    remaining %, resets_at, minutes), and its tone:

    - a window on course to run out before its reset: the earliest of them,
      "5 小时额度：按当前速度约 49 分钟后用完，早于重置 26 分钟";
    - otherwise the window that resets next and what will be left of it,
      "每周额度 12 分钟后重置，预计剩 34%"; "，可多用" when it is a weekly
      window resetting within SPARE_AHEAD with at least SPARE_PERCENT to
      spare; and, after it, any other window expected to end nearly empty.

    Every window is judged by the same forecast, so the line, the chart's
    dashed lines and the usage tips never disagree."""
    rows = [row for row in windows if row[1] is not None]
    single = len(rows) == 1
    name = lambda label: f"{label}额度" if label else "额度"           # noqa: E731
    running_out = sorted((row for row in rows if row[1].before_reset or row[1].tone == "danger"),
                         key=lambda row: row[1].exhausts_at or now)
    if running_out:
        label, chosen = running_out[0][0], running_out[0][1]
        return (chosen.text if single else f"{name(label)}：{chosen.text}"), chosen.tone
    known = [(row, left_at_reset(row[1], now, row[2], row[3])) for row in rows]
    known = [(row, left) for row, left in known if left is not None]
    if not known:
        rest = [row[1] for row in rows]
        if not rest:
            return "", "unknown"
        chosen = next((item for item in rest if item.tone != "unknown"), rest[0])
        return chosen.text, chosen.tone
    (label, _chosen, _remaining, resets_at, minutes), left = min(known, key=lambda item: item[0][3])
    text = f"{name(label)} {duration_text(resets_at - now)}后重置，预计剩 {left:.0f}%"
    tone = "ok"
    if left <= LOW_SPARE_PERCENT:
        text, tone = f"{name(label)} {duration_text(resets_at - now)}后重置，到时可能接近用尽（约剩 {left:.0f}%）", "warn"
    elif (minutes or 0) >= DAY_PACE_MINUTES and resets_at - now <= SPARE_AHEAD and left >= SPARE_PERCENT:
        text += "，可多用"
    tight = [(other_label, other_left) for (other_label, _f, _r, other_reset, _m), other_left in known
             if other_reset != resets_at and other_left <= LOW_SPARE_PERCENT]
    for other_label, other_left in tight[:1]:
        text += f"；{name(other_label)}到重置时约剩 {other_left:.0f}%"
        tone = "warn"
    return text, tone


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
                # Glitches saved by earlier versions are dropped here, and stay
                # out of the file from the next save on.
                self.series[name] = without_blips(self._without_false_claude_recoveries(name, readings))
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
        """Saved readings without one-off glitches (see without_blips)."""
        with self.lock:
            return without_blips(self.series.get(name, []))

    def forecast_for(self, snapshot: Any, now: float, secondary: bool = False) -> Forecast:
        if secondary:
            return forecast(self.samples(f"{snapshot.provider}:secondary"), now,
                            snapshot.secondary_remaining_percent, snapshot.secondary_resets_at,
                            snapshot.secondary_window_minutes)
        return forecast(self.samples(snapshot.provider), now, snapshot.remaining_percent, snapshot.resets_at,
                        snapshot.window_minutes)
