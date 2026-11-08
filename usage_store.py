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

from forecast import duration_text, forecast, same_window

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
        # Other devices' hours from the sync hub (sync_hub), in memory only:
        # device -> (label, provider -> hour start -> [output tokens, replies]).
        self.remote: dict[str, tuple[str, dict[str, dict[int, list[int]]]]] = {}
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

    def _sources(self, everywhere: bool) -> list[dict[str, dict[int, list[int]]]]:
        return [self.hours] + ([hours for _label, hours in self.remote.values()] if everywhere else [])

    def hourly(self, providers: Iterable[str], since: float, everywhere: bool = True) -> dict[int, int]:
        """Output tokens per hour start for the given providers since `since`,
        on every synced device (everywhere) or this one only."""
        keys = set(providers)
        totals: dict[int, int] = {}
        with self.lock:
            for source in self._sources(everywhere):
                for provider, hours in source.items():
                    if provider in keys:
                        for hour, (tokens, _replies) in hours.items():
                            if hour >= since - 3600:
                                totals[hour] = totals.get(hour, 0) + tokens
        return totals

    def replies(self, providers: Iterable[str], since: float, everywhere: bool = True) -> int:
        keys = set(providers)
        with self.lock:
            return sum(replies for source in self._sources(everywhere) for provider, hours in source.items() if provider in keys
                       for hour, (_tokens, replies) in hours.items() if hour >= since)

    def export_hours(self, since: float) -> dict[str, dict[str, list[int]]]:
        """This device's own hours since `since`, for the sync hub (never the
        other devices' copies, so nothing is echoed back)."""
        with self.lock:
            return {provider: {str(hour): list(value) for hour, value in hours.items() if hour >= since}
                    for provider, hours in self.hours.items() if any(hour >= since for hour in hours)}

    def set_remote(self, device: str, label: str, hours: Any) -> None:
        """Another device's hours as it last sent them (export_hours)."""
        parsed: dict[str, dict[int, list[int]]] = {}
        if isinstance(hours, dict):
            for provider, rows in hours.items():
                if not isinstance(rows, dict):
                    continue
                try:
                    parsed[str(provider)] = {int(hour): [int(value[0]), int(value[1])] for hour, value in rows.items()}
                except (TypeError, ValueError, IndexError):
                    continue
        with self.lock:
            self.remote[device] = (label or device, parsed)

    def keep_remote(self, devices: Iterable[str]) -> None:
        """Forget synced devices the hub no longer lists."""
        keep = set(devices)
        with self.lock:
            for device in [device for device in self.remote if device not in keep]:
                del self.remote[device]

    def by_device(self, providers: Iterable[str], since: float) -> list[tuple[str, int]]:
        """(label, output tokens since `since`) for this device ("本机") and
        each synced one that did any work."""
        keys = set(providers)
        with self.lock:
            rows = [("本机", self.hours)] + [(label, hours) for label, hours in self.remote.values()]
            out = [(label, sum(tokens for provider, hours in source.items() if provider in keys
                               for hour, (tokens, _replies) in hours.items() if hour >= since)) for label, source in rows]
        return [(label, tokens) for index, (label, tokens) in enumerate(out) if tokens or index == 0]

    def hour_rows(self, providers: Iterable[str], since: float, now: float) -> list[Any]:
        """Hourly totals as chart events (one per provider and hour, at the
        hour's middle), for ranges longer than the replies kept one by one."""
        from types import SimpleNamespace
        keys = set(providers)
        with self.lock:
            return [SimpleNamespace(ended_at=min(hour + 1800, now), provider=provider, output_tokens=tokens, count_in_rate=True)
                    for provider, hours in self.hours.items() if provider in keys
                    for hour, (tokens, _replies) in hours.items() if since - 3600 <= hour <= now]


SOURCE_NAMES = {"codex": "Codex", "claude": "Claude", "grok": "Grok", "gemini": "Gemini", "mimo": "MiMo"}


def hint_line(store: UsageStore, providers: Iterable[str], now: float, weekly: dict | None = None) -> str:
    """One short usage insight for the hover panel's hint: a weekly quota
    about to reset with much of it unused (from `weekly`: provider ->
    (quota readings, remaining %, resets_at)), else which source did most of
    today's work, else the busiest hours of the week. No comparison with
    yesterday: that figure said nothing anyone could act on. Empty without
    records."""
    day = local_day(now)
    keys = list(providers)
    for key in keys:
        readings, remaining, resets_at = (weekly or {}).get(key, ((), None, None))
        spare = weekly_spare(readings, now, remaining, resets_at)
        if spare:
            return (f"{SOURCE_NAMES.get(key, key)} 周额度 {duration_text(spare['reset_at'] - now)}后重置，"
                    f"预计剩 {spare['spare']:.0f}%，可多用")
    by_source = {key: store.hourly([key], day - 7 * 86400) for key in keys}
    today = {key: sum(t for hour, t in hours.items() if hour >= day) for key, hours in by_source.items()}
    total = sum(today.values())
    working = sorted(((t, key) for key, t in today.items() if t), reverse=True)
    if len(working) >= 2:
        share, key = working[0][0] / total * 100, working[0][1]
        return f"今天输出 {_tokens(total)}，{SOURCE_NAMES.get(key, key)} 占 {share:.0f}%"
    by_hour = [0] * 24
    for hours in by_source.values():
        for hour, t in hours.items():
            by_hour[time.localtime(hour).tm_hour] += t
    if sum(by_hour):
        start = max(range(24), key=lambda h: (by_hour[h] + by_hour[(h + 1) % 24], by_hour[h]))
        lead = f"今天输出 {_tokens(total)}，" if total else ""
        return f"{lead}近 7 天 {start:02d}–{(start + 2) % 24:02d} 点最忙"
    return ""


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


WEEKLY_RECENT = 2 * 86400      # a weekly reset this recent is still news
WEEKLY_LEFTOVER = 20.0         # % of last week left unused before it is worth a word
WEEKLY_LAST_READ = 12 * 3600   # the old week's last reading must be this close to its reset
WEEKLY_AHEAD = 36 * 3600       # a weekly reset this close ahead: say what will be left over


def weekly_spare(samples: Iterable[Any], now: float, remaining: float | None, resets_at: int | None) -> dict | None:
    """The weekly window about to reset with quota that will go unused:
    {"reset_at", "spare", "remaining", "rate"} when the reset is within
    WEEKLY_AHEAD and, at the current pace (forecast's slope over this
    window's readings), at least WEEKLY_LEFTOVER will be left; else None."""
    if remaining is None or not resets_at or not 0 < resets_at - now <= WEEKLY_AHEAD:
        return None
    outlook = forecast(samples, now, remaining, resets_at)
    if outlook.tone == "unknown":
        return None                 # no readings over this window yet: the pace is anyone's guess
    rate = max(0.0, outlook.rate_per_hour or 0.0)
    spare = remaining - rate * (resets_at - now) / 3600
    if spare < WEEKLY_LEFTOVER:
        return None
    return {"reset_at": float(resets_at), "spare": spare, "remaining": float(remaining), "rate": rate}


def spare_line(store: UsageStore, provider: str, spare: dict, now: float, weeks: list[dict] = ()) -> str:
    """"周额度：…" for a weekly window that will reset with quota unused, what
    that is worth in this week's own output when known, and how the last
    weeks went (WeeklyLedger.recent)."""
    text = f"周额度：{duration_text(spare['reset_at'] - now)}后重置，按现在的用法到时还剩约 {spare['spare']:.0f}%"
    used = 100.0 - spare["remaining"]
    week = sum(tokens for hour, tokens in store.hourly([provider], spare["reset_at"] - 7 * 86400).items() if hour < now)
    if used >= 10 and week > 0:
        text += f"（约 {_tokens(week / used * spare['spare'])} 输出 tokens）"
    note = _weeks_note(list(weeks))
    return text + (f"；{note}" if note else "") + "，趁重置前多用"


def last_completed_week(samples: Iterable[Any]) -> dict | None:
    """The latest weekly reset in a window's quota readings (forecast.Sample,
    glitches already out): {"reset_at", "used", "left", "since"}, with
    "used" the old week's last reading. None when no reset is recorded, the
    latest window change is not a reset (the window went back, or use did
    not start over), or the old week's last reading is more than
    WEEKLY_LAST_READ before the reset, so its end is unknown."""
    ordered = sorted(samples, key=lambda s: s.at)
    for before, after in reversed(list(zip(ordered, ordered[1:]))):
        if same_window(before.resets_at, after.resets_at):
            continue
        if (after.resets_at or 0) <= (before.resets_at or 0) or after.used >= before.used:
            return None
        # An early reset (Codex can reset before schedule) lands at the first new reading.
        reset_at = min(before.resets_at, after.at) if before.resets_at else after.at
        if reset_at - before.at > WEEKLY_LAST_READ:
            return None
        return {"reset_at": float(reset_at), "used": before.used, "left": 100.0 - before.used, "since": float(before.at)}
    return None


def weekly_leftover(samples: Iterable[Any], now: float) -> dict | None:
    """The weekly window that reset lately with plenty left (see
    last_completed_week): within WEEKLY_RECENT, at least WEEKLY_LEFTOVER left."""
    week = last_completed_week(samples)
    if week is None or now - week["reset_at"] > WEEKLY_RECENT or week["left"] < WEEKLY_LEFTOVER:
        return None
    return week


WEEKLY_PATH = USAGE_PATH.with_name("weekly.json")
WEEKLY_KEEP = 26               # completed weeks kept per window


class WeeklyLedger:
    """One line per completed week of each weekly quota window: when it
    reset, how much of it was used, and the output tokens it paid for. The
    quota readings themselves are kept for days; this is what lets the
    weekly tips look back over the last weeks."""

    def __init__(self, path: Path = WEEKLY_PATH):
        self.path = path
        self.lock = Lock()
        self.weeks: dict[str, list[dict]] = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            for name, rows in payload.get("windows", {}).items():
                self.weeks[name] = [{"reset_at": float(row["reset_at"]), "used": float(row["used"]), "tokens": int(row.get("tokens") or 0)}
                                    for row in rows]
        except (OSError, ValueError, TypeError, KeyError):
            self.weeks = {}

    def note(self, name: str, provider: str, samples: Iterable[Any], store: UsageStore | None) -> bool:
        """Record window `name`'s latest completed week (from its readings) if
        it is not recorded yet; the tokens are the provider's output over
        that week. Returns True when a week was added."""
        week = last_completed_week(samples)
        if week is None:
            return False
        with self.lock:
            rows = self.weeks.setdefault(name, [])
            if any(abs(row["reset_at"] - week["reset_at"]) <= 3600 for row in rows):
                return False
            tokens = 0
            if store is not None:
                tokens = sum(t for hour, t in store.hourly([provider], week["reset_at"] - 7 * 86400).items() if hour < week["reset_at"])
            rows.append({"reset_at": week["reset_at"], "used": float(week["used"]), "tokens": int(tokens)})
            rows.sort(key=lambda row: row["reset_at"])
            del rows[:-WEEKLY_KEEP]
            payload = {"windows": self.weeks}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                pass
        return True

    def recent(self, name: str, limit: int = 4) -> list[dict]:
        """The last `limit` completed weeks, newest first."""
        with self.lock:
            return list(reversed(self.weeks.get(name, [])[-limit:]))

    def export(self) -> dict[str, list[dict]]:
        with self.lock:
            return {name: [dict(row) for row in rows] for name, rows in self.weeks.items()}

    def merge(self, windows: Any) -> bool:
        """Weeks another device recorded (export): a week this one lacks is
        added; for one both have, the higher share used and token count win
        (each device saw part of the week's work). Returns True on a change."""
        if not isinstance(windows, dict):
            return False
        changed = False
        with self.lock:
            for name, rows in windows.items():
                if not isinstance(rows, list):
                    continue
                mine = self.weeks.setdefault(str(name), [])
                for row in rows:
                    try:
                        reset_at, used, tokens = float(row["reset_at"]), float(row["used"]), int(row.get("tokens") or 0)
                    except (TypeError, ValueError, KeyError):
                        continue
                    same = next((week for week in mine if abs(week["reset_at"] - reset_at) <= 3600), None)
                    if same is None:
                        mine.append({"reset_at": reset_at, "used": used, "tokens": tokens})
                        changed = True
                    elif used > same["used"] or tokens > same["tokens"]:
                        same["used"], same["tokens"] = max(used, same["used"]), max(tokens, same["tokens"])
                        changed = True
                mine.sort(key=lambda week: week["reset_at"])
                del mine[:-WEEKLY_KEEP]
            payload = {"windows": self.weeks}
        if changed:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                pass
        return changed


def weeks_line(weeks: list[dict]) -> str:
    """"近几周：周额度用掉 62% · 71% · 55%（最新在前）" from WeeklyLedger.recent."""
    if not weeks:
        return ""
    used = " · ".join(f"{row['used']:.0f}%" for row in weeks)
    return f"近几周：周额度用掉 {used}" + ("（最新在前）" if len(weeks) > 1 else "")


def _weeks_note(weeks: list[dict]) -> str:
    """How the last weeks went, for the weekly tips: "近 4 周有 3 周没用完" or
    "近 4 周都用完了"; empty with fewer than two weeks."""
    if len(weeks) < 2:
        return ""
    spare = sum(1 for row in weeks if 100.0 - row["used"] >= WEEKLY_LEFTOVER)
    if spare == 0:
        return f"近 {len(weeks)} 周都用完了"
    return f"近 {len(weeks)} 周有 {spare} 周没用完"


def weekly_line(store: UsageStore, provider: str, leftover: dict, now: float, weeks: list[dict] = ()) -> str:
    """"周额度：…" for a weekly reset with quota left over, what that leftover
    was worth in last week's own output when known, and how the weeks before
    went (WeeklyLedger.recent, the week just ended included)."""
    moment = time.localtime(leftover["reset_at"])
    when = time.strftime("%H:%M" if local_day(leftover["reset_at"]) == local_day(now) else "%m-%d %H:%M", moment)
    text = f"周额度：{when} 刚重置；上周还剩 {leftover['left']:.0f}% 没用完"
    week = sum(tokens for hour, tokens in store.hourly([provider], leftover["reset_at"] - 7 * 86400).items()
               if hour < leftover["reset_at"])
    if leftover["used"] >= 10 and week > 0:
        text += f"（约 {_tokens(week / leftover['used'] * leftover['left'])} 输出 tokens）"
    note = _weeks_note(list(weeks))
    return text + (f"；{note}" if note else "") + "，本周可多用"


def insights(store: UsageStore, provider: str, now: float, samples: Iterable[Any] = (), window: str = "",
             weekly: Iterable[Any] = (), weekly_quota: tuple | None = None, weeks: list[dict] = ()) -> list[str]:
    """"标题：内容" lines about one provider's own output: a weekly window
    about to reset with quota to spare, or one that just reset with much of
    it unused; today against yesterday; the week's daily average and busiest
    hours; and how many output tokens a percent of quota has gone for.
    `samples` are that window's quota readings (forecast.Sample) over the same
    week; `weekly` the weekly window's and `weekly_quota` its (remaining %,
    resets_at) (none for 5-hour windows: those reset too often to be worth
    the note)."""
    day = local_day(now)
    week_start = local_day(day - 7 * 86400 + 3600)
    hours = store.hourly([provider], week_start)
    weeks = list(weeks)
    spare = weekly_spare(weekly, now, *(weekly_quota or (None, None)))
    leftover = None if spare else weekly_leftover(weekly, now)
    head = ([spare_line(store, provider, spare, now, weeks)] if spare
            else [weekly_line(store, provider, leftover, now, weeks)] if leftover else [])
    if weeks:
        head.append(weeks_line(weeks))
    if not any(hours.values()):
        return head + ["记录：暂无输出；开始使用后在此统计（保存在本机，重启后保留）"]
    today = sum(tokens for hour, tokens in hours.items() if hour >= day)
    yesterday_start = local_day(day - 3600)
    yesterday = sum(tokens for hour, tokens in hours.items() if yesterday_start <= hour < day)
    so_far = sum(tokens for hour, tokens in hours.items() if yesterday_start <= hour < now - 86400)
    lines = head + [f"今天：{_tokens(today)} tokens · {store.replies([provider], day)} 次回复"]
    split = store.by_device([provider], day)
    if len(split) > 1:                              # work on other synced devices today
        lines.append("设备：" + " · ".join(f"{label} {_tokens(tokens)}" for label, tokens in split))
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
        start = max(range(24), key=lambda h: (by_hour[h] + by_hour[(h + 1) % 24], by_hour[h]))
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
