"""Output records kept on disk, so the output chart and usage insights survive
a restart.

Only numbers and names are stored: provider, source, session id, model, finish
time and token counts. The replies of the last RAW_SECONDS are kept one by one
(the chart's longest range is a day); older work survives as per-hour totals
per provider for KEEP_DAYS. Written gzip-compressed, at most once a minute."""
from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import astuple
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from forecast import same_window

USAGE_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray" / "usage.json.gz"
RAW_SECONDS = 26 * 3600         # a little over the chart's day, so a restart finds the whole day
KEEP_DAYS = 90
SAVE_EVERY = 60
VERSION = 1


def _hour(stamp: float) -> int:
    return int(stamp // 3600 * 3600)


def local_day(stamp: float) -> int:
    """Local midnight (epoch seconds) of the day holding stamp."""
    moment = time.localtime(stamp)
    return int(time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday, 0, 0, 0, 0, 0, -1)))


class UsageStore:
    def __init__(self, path: Path = USAGE_PATH, observation_type: Any = None):
        self.path = path
        self.lock = Lock()
        self.raw: list[tuple] = []                       # Observation fields, oldest first
        self.hours: dict[str, dict[int, list[int]]] = {}  # provider -> hour start -> [output tokens, replies]
        self.dirty = False
        self.saved_at = 0.0
        if observation_type is None:
            from tps_sources import Observation as observation_type
        self.observation_type = observation_type
        self._load()

    def _load(self) -> None:
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("version") != VERSION:
                return
            width = len(self.observation_type.__dataclass_fields__)
            self.raw = [tuple(row) for row in payload.get("raw", []) if len(row) == width]
            self.hours = {provider: {int(hour): [int(tokens), int(replies)] for hour, (tokens, replies) in hours.items()}
                          for provider, hours in payload.get("hours", {}).items()}
        except (OSError, ValueError, TypeError, KeyError, EOFError):
            self.raw, self.hours = [], {}

    def note(self, observation: Any) -> None:
        """A new observation from the monitor (each identity arrives once)."""
        with self.lock:
            self.raw.append(astuple(observation))
            if observation.count_in_rate and observation.output_tokens > 0:
                slot = self.hours.setdefault(observation.provider, {}).setdefault(_hour(observation.ended_at), [0, 0])
                slot[0] += int(observation.output_tokens)
                slot[1] += 1
            self.dirty = True

    def restore(self, now: float, seconds: float = 86400) -> list[Any]:
        """Observations of the last `seconds`, oldest first, for the monitor to start with."""
        make = self.observation_type
        with self.lock:
            rows = [row for row in self.raw if now - seconds <= row[5] <= now + 5]
        out = []
        for row in sorted(rows, key=lambda row: row[5]):
            try:
                out.append(make(*row))
            except TypeError:
                continue
        return out

    def save(self, now: float | None = None, force: bool = False) -> bool:
        now = time.time() if now is None else now
        if not self.dirty or (not force and now - self.saved_at < SAVE_EVERY):
            return False
        with self.lock:
            self.raw = [row for row in self.raw if row[5] >= now - RAW_SECONDS]
            oldest = now - KEEP_DAYS * 86400
            self.hours = {provider: {hour: value for hour, value in hours.items() if hour >= oldest}
                          for provider, hours in self.hours.items()}
            payload = {"version": VERSION, "raw": self.raw,
                       "hours": {provider: {str(hour): value for hour, value in sorted(hours.items())}
                                 for provider, hours in self.hours.items() if hours}}
            self.dirty = False
            self.saved_at = now
            try:                            # under the lock: the exit save and the reader thread's never interleave
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
                    json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
                temporary.replace(self.path)
                return True
            except OSError:
                self.dirty = True
                return False

    def hourly(self, providers: Iterable[str], since: float) -> dict[int, int]:
        """Output tokens per hour start for the given providers since `since`."""
        keys = set(providers)
        totals: dict[int, int] = {}
        with self.lock:
            for provider, hours in self.hours.items():
                if provider in keys:
                    for hour, (tokens, _replies) in hours.items():
                        if hour >= since - 3600:
                            totals[hour] = totals.get(hour, 0) + tokens
        return totals

    def replies(self, providers: Iterable[str], since: float) -> int:
        keys = set(providers)
        with self.lock:
            return sum(replies for provider, hours in self.hours.items() if provider in keys
                       for hour, (_tokens, replies) in hours.items() if hour >= since)

    def hour_rows(self, providers: Iterable[str], since: float, now: float) -> list[Any]:
        """Hourly totals as chart events (one per provider and hour, at the
        hour's middle), for ranges longer than the replies kept one by one."""
        from types import SimpleNamespace
        keys = set(providers)
        with self.lock:
            return [SimpleNamespace(ended_at=min(hour + 1800, now), provider=provider, output_tokens=tokens, count_in_rate=True)
                    for provider, hours in self.hours.items() if provider in keys
                    for hour, (tokens, _replies) in hours.items() if since - 3600 <= hour <= now]


def _tokens(value: float) -> str:
    if value < 1000:
        return f"{value:.0f}"
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{value:.0f}"


def _between(hours: dict[int, int], start: float, end: float) -> float:
    """Tokens of the hours overlapping [start, end], prorated by overlap."""
    total = 0.0
    for hour, tokens in hours.items():
        overlap = min(end, hour + 3600) - max(start, hour)
        if overlap > 0:
            total += tokens * overlap / 3600
    return total


def insights(store: UsageStore, provider: str, now: float, samples: Iterable[Any] = (), window: str = "") -> list[str]:
    """"标题：内容" lines about one provider's own output: today against
    yesterday, the week's daily average and busiest hours, and how many output
    tokens a percent of quota has gone for. `samples` are that window's quota
    readings (forecast.Sample) over the same week."""
    day = local_day(now)
    week_start = local_day(day - 7 * 86400 + 3600)
    hours = store.hourly([provider], week_start)
    if not any(hours.values()):
        return ["记录：暂无输出；开始使用后在此统计（保存在本机，重启后保留）"]
    today = sum(tokens for hour, tokens in hours.items() if hour >= day)
    yesterday_start = local_day(day - 3600)
    yesterday = sum(tokens for hour, tokens in hours.items() if yesterday_start <= hour < day)
    so_far = sum(tokens for hour, tokens in hours.items() if yesterday_start <= hour < now - 86400)
    lines = [f"今天：{_tokens(today)} tokens · {store.replies([provider], day)} 次回复"]
    if so_far > 0:
        change = (today - so_far) / so_far * 100
        lines.append(f"比昨天此时：{'+' if change >= 0 else ''}{change:.0f}%（昨天全天 {_tokens(yesterday)}）")
    elif yesterday:
        lines.append(f"昨天：{_tokens(yesterday)} tokens")
    days = [sum(tokens for hour, tokens in hours.items() if start <= hour < local_day(start + 30 * 3600))
            for start in (local_day(day - back * 86400 + 3600) for back in range(7, 0, -1))]
    active = sum(1 for value in days if value)
    if active:
        lines.append(f"近 7 天日均：{_tokens(sum(days) / 7)} tokens（{active} 天有输出）")
    by_hour = [0] * 24
    for hour, tokens in hours.items():
        by_hour[time.localtime(hour).tm_hour] += tokens
    if sum(by_hour):
        start = max(range(24), key=lambda h: by_hour[h] + by_hour[(h + 1) % 24])
        lines.append(f"最忙时段：{start:02d}:00–{(start + 2) % 24:02d}:00")
    spent = tokens = 0.0
    ordered = sorted((s for s in samples if s.at >= week_start), key=lambda s: s.at)
    for before, after in zip(ordered, ordered[1:]):
        if same_window(before.resets_at, after.resets_at) and after.used > before.used and after.at - before.at <= 6 * 3600:
            spent += after.used - before.used
            tokens += _between(hours, before.at, after.at)
    if spent >= 3 and tokens > 0:
        lines.append(f"额度换算：{window or '额度'}每 1% 约 {_tokens(tokens / spent)} 输出 tokens")
    return lines
