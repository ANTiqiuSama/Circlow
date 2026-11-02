from __future__ import annotations

import ctypes
import dataclasses
import itertools
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pystray
from PIL import Image

import agent_panel
import agent_tasks
import agent_hints
import forecast as forecast_module
from forecast import Forecast, QuotaHistory, duration_text, reset_distance
from hover_details import HoverDetails, HoverIcon
import quota_sources
from quota_sources import (
    mimo_snapshot,
    mimo_tracker,
    QuotaSnapshot,
    collect,
    dump_snapshot,
    get_claude_quota,
    get_codex_quota,
    get_grok_quota,
    parse_codex_session_limits,
    load_cached_snapshots,
    placeholder,
    save_cached_snapshots,
)
import herdr_source
import hover_details
import mimo_usage
import providers as provider_registry
import model_iq
import grok_iq
import reset_watch
from refresh_scheduler import RefreshScheduler
from tray_icons import (  # noqa: F401  (re-exported for callers and tests)
    EXHAUSTED_REMAINING, ERROR_PREFIX_RE, PACED_PROVIDERS, _rounded, countdown_value, exhaustion_text, exhaustion_threshold,
    icon_windows, make_display_icon, make_provider_icon, make_tps_icon, pace_marker, quota_exhausted,
    stale_note, usable_again_at)
from tps_sources import PERFORMANCE_PATH, TpsMonitor, performance_signature, save_performance
from usage_store import UsageStore, insights as usage_insights


APP_NAME = "CodexQuotaTray"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP_NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PERFORMANCE_SAVE_SECONDS = 60
# Shown providers (key -> icon letter), in registry order; see providers.py.
PROVIDERS = {key: provider_registry.get(key).letter for key in provider_registry.enabled_keys()}
LAUNCHERS = {spec.key: spec.launch for spec in provider_registry.SPECS}
# Sources whose click opens a new Herdr tab running that agent (herdr --kind).
HERDR_AGENTS = {"claude", "grok"}
log = logging.getLogger(APP_NAME)


def has_panel(provider: str, snapshot: QuotaSnapshot | None) -> bool:
    """Every quota source gets the bubble panel except Claude without a plan."""
    if provider != "claude" or snapshot is None:
        return True
    return not (snapshot.display_remaining is None and "订阅" in (snapshot.error or ""))


def reset_hint(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    countdown = reset_countdown(timestamp)
    return countdown if countdown == "已到重置时间" else f"{countdown}后重置"


def pool_lines(snapshot: QuotaSnapshot) -> list[str]:
    """One line per extra model pool (Antigravity), ring pool first."""
    lines = []
    for pool in snapshot.pools or []:
        left = pool.get("remaining_percent")
        value = "未知" if left is None else f"剩余 {left:.0f}%"
        reset = f" · {reset_text(pool['resets_at'])} 重置" if pool.get("resets_at") else ""
        lines.append(f"  {pool.get('label') or pool.get('key')}：{value}{reset}")
    return lines


def provider_letter(provider: str) -> str:
    try:
        return provider_registry.get(provider).letter
    except KeyError:
        return "?"


def window_label(minutes: int | None) -> str:
    if not minutes:
        return ""
    if minutes == 10080:
        return "每周"
    if 524_000 <= minutes <= 528_000:
        return "年度"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def compact_number(value: float) -> str:
    """59773 -> "59.8k", 1250000 -> "1.3M", 840 -> "840"."""
    for limit, suffix in ((1e6, "M"), (1e3, "k")):
        if value >= limit:
            return f"{value / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:.0f}"


def reset_text(timestamp: int | None) -> str:
    if not timestamp:
        return "重置时间未知"
    moment = datetime.fromtimestamp(timestamp).astimezone()
    offset = moment.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}"
    return moment.strftime("%Y-%m-%d %H:%M") + f"（{zone}）"


def window_idle(snapshot: QuotaSnapshot) -> bool:
    """A fresh window nobody has used yet: full quota, no reset scheduled."""
    return snapshot.resets_at is None and snapshot.used_percent == 0 and snapshot.error is None


def reset_countdown(timestamp: int | None) -> str:
    if not timestamp:
        return "未知"
    delta = int(timestamp - time.time())
    if delta <= 0:
        return "已到重置时间"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    return f"{hours} 小时 {minutes} 分"


def quota_line(snapshot: QuotaSnapshot) -> str:
    second = window_label(snapshot.secondary_window_minutes) or "第二窗口"
    if snapshot.remaining_percent is None and snapshot.secondary_remaining_percent is None:
        status = "未知/获取失败"
    elif snapshot.remaining_percent is None:
        # One window known is still worth showing (e.g. Claude weekly without its 5 h reading).
        first = window_label(snapshot.window_minutes) or "当前窗口"
        status = f"{first}未知 · {second}剩余 {snapshot.secondary_remaining_percent:.0f}%"
    else:
        used = 100.0 - snapshot.remaining_percent if snapshot.used_percent is None else snapshot.used_percent
        status = f"剩余 {snapshot.remaining_percent:.0f}% · 已用 {used:.0f}%"
        if snapshot.secondary_remaining_percent is not None:
            status += f" · {second}剩余 {snapshot.secondary_remaining_percent:.0f}%"
        for pool in (snapshot.pools or [])[1:]:
            if pool.get("remaining_percent") is not None:
                status += f" · {pool.get('label') or pool.get('key')} {pool['remaining_percent']:.0f}%"
    stale = " · 旧数据" if snapshot.stale else ""
    return f"{snapshot.label}: {status}{stale}"


# Tray order is fixed (providers.TRAY_POSITIONS): Claude Code, Codex (GPT),
# Grok, Gemini, MiMo.  It never changes with remaining quota or exhaustion.
REORDER_MIN_SECONDS = 2          # after a drag, the order is right again within a few seconds
ORDER_CHECK_SECONDS = 2          # five icon rectangles and one registry read: cheap
PREFERENCES_PATH = DATA_DIR / "settings.json"


def load_preferences() -> dict:
    try:
        value = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
RESET_PATH = DATA_DIR / "codex_reset.json"
IQ_STATE_PATH = DATA_DIR / "model_iq.json"
IQ_CONFIG_PATH = DATA_DIR / "model_iq_config.json"     # AI Stupid Level key + chosen models
GROK_IQ_PATH = DATA_DIR / "grok_iq.json"               # LiveBench overall for the Grok column
# Providers whose quota resets raise a Windows notification.
RESET_NOTIFY = {part.strip().lower() for part in (os.environ.get("CODEX_QUOTA_RESET_NOTIFY") or "codex,grok,claude").split(",") if part.strip()}
RESET_NOTIFY_MIN_GAIN = 10.0     # percentage points regained before it counts as a reset


def reset_changes(old: QuotaSnapshot | None, new: QuotaSnapshot | None, now: float) -> list[str]:
    """Windows whose quota came back between two reads, as short lines."""
    if old is None or new is None or new.error or new.stale:
        return []
    lines = []
    for minutes, before, after, old_reset, new_reset in (
            (new.window_minutes, old.remaining_percent, new.remaining_percent, old.resets_at, new.resets_at),
            (new.secondary_window_minutes, old.secondary_remaining_percent, new.secondary_remaining_percent,
             old.secondary_resets_at, new.secondary_resets_at)):
        if before is None or after is None or after - before < RESET_NOTIFY_MIN_GAIN:
            continue
        if new.provider == "claude" and after < 95:
            continue
        moved = old_reset is None or new_reset is None or abs(new_reset - old_reset) > 60
        if not moved and after < 95:
            continue
        early = bool(old_reset) and now < old_reset - 300
        lines.append(f"{window_label(minutes) or '额度'}窗口：{before:.0f}% → {after:.0f}%" + ("（提前重置）" if early else ""))
    return lines


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    runner = windowless if windowless.is_file() else interpreter
    return f'"{runner}" -X utf8 "{Path(__file__).resolve()}"'


def restart_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if getattr(sys, "frozen", False):
        # The new instance must own its extraction directory after the old exits.
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


class QuotaTray:
    # Class-level so test doubles built with __new__ share the same guarantees.
    ui_lock = threading.RLock()
    refresh_lock = threading.Lock()
    history: QuotaHistory | None = None
    scheduler: RefreshScheduler | None = None

    def __init__(self) -> None:
        # Show cached values immediately; reading the CLIs can take ~25 s.
        cached = load_cached_snapshots()
        self.snapshots = {provider: cached.get(provider) or placeholder(provider) for provider in PROVIDERS}
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.history = QuotaHistory()
        self.scheduler = RefreshScheduler(PROVIDERS, time.time())
        self.usage = UsageStore()
        self.tps_monitor = TpsMonitor(store=self.usage)
        self.apply_preferences(load_preferences())
        self.performance = self.tps_monitor.snapshot(time.time())
        self.agents: list[dict] = []
        self.agent_groups = agent_tasks.AgentGroups()
        self.summarizer = agent_tasks.Summarizer(agent_tasks.summary_config())
        self.hint_advisor = agent_hints.HintAdvisor(agent_tasks.summary_config())
        self.settings_requested = threading.Event()
        self.settings_ui = None
        self.clusters: dict[str, agent_tasks.Cluster] = {}
        self.last_samples: dict[str, float | None] = {}
        self.saved_signature: str | None = None
        self.saved_at = 0.0
        self.mimo_view_at = 0.0
        self.mimo_ingested = 0       # tps_monitor.added_total already handed to the MiMo ledger
        self.shown_order: list[str] = []
        self.reordered_at = 0.0
        self.reset_outlook = reset_watch.load(RESET_PATH) if reset_watch.ENABLED else None
        self.iq_state = model_iq.load_state(IQ_STATE_PATH)
        self.iq_config = model_iq.load_config(IQ_CONFIG_PATH)
        self.grok_iq = grok_iq.load_state(GROK_IQ_PATH)
        self.iq_lock = threading.Lock()
        self.iq_wake = threading.Event()
        # Cached figures may use a different alert threshold.  Notify once
        # after a fresh high forecast arrives, then suppress repeats this run.
        self.reset_alerted = "unknown"
        self.migrate_startup()
        self.icons = {
            provider: HoverIcon(
                f"{APP_NAME}-{provider}",
                make_display_icon(self.snapshots[provider], letter, self.performance["providers"][provider]),
                self.tooltip(provider),
                menu=self.build_menu(provider),
            )
            for provider, letter in PROVIDERS.items()
        }
        self.slots = list(self.icons.values())   # fixed tray icons; providers move between them
        for index, icon in enumerate(self.slots):
            icon.icon_id = index                  # one ID per slot, so Windows tracks each icon on its own
            icon.settings_action = self.open_settings

    # ----- text and card content -------------------------------------------------

    def forecasts(self, provider: str) -> list[tuple[str, Forecast]]:
        snapshot = self.snapshots[provider]
        if self.history is None:
            return []
        now = time.time()
        result = [(window_label(snapshot.window_minutes), self.history.forecast_for(snapshot, now))]
        if snapshot.secondary_remaining_percent is not None:
            result.append((window_label(snapshot.secondary_window_minutes) or "第二窗口",
                           self.history.forecast_for(snapshot, now, secondary=True)))
        return result

    def headline_forecast(self, provider: str) -> tuple[str, str]:
        """The most urgent forecast, prefixed with its window when there are two."""
        if provider == "mimo":
            return self.mimo_forecast()
        snapshot = self.snapshots.get(provider)
        if snapshot is not None and snapshot.pools and len(snapshot.pools) > 1:
            return self.pool_forecast(provider)
        state, empty = self.exhausted_state(provider)
        if state:
            now = time.time()
            minutes = (snapshot.window_minutes, snapshot.secondary_window_minutes)
            names = "、".join(window_label(minutes[slot]) or "当前" for slot in empty)
            until = usable_again_at(snapshot, now)
            back = f"{reset_distance(until, now)}后恢复" if until else "恢复时间未知"
            if state in ("near", "near_working"):
                return f"{names}剩余{exhaustion_text(provider)}，按耗尽处理；{back}", "warn"
            if state == "paid":
                using = "正在用购买的额度" if self.source_working(provider) else "可用购买的额度继续"
                return f"{names}额度已用完，{using}；{back}", "warn"
            if state == "inferred":
                return f"{names}额度已用完，仍有输出，可能使用额外额度；{back}", "warn"
            if state == "working":
                return f"{names}额度已用完，仍在工作；{back}", "warn"
            spare = f"；可用免费重置 {snapshot.reset_credits} 次" if snapshot.reset_credits else ""
            return f"{names}额度已用完，{back}{spare}", "danger"
        if snapshot is not None and snapshot.stale:
            return stale_note(snapshot, time.time()), "warn"
        items = self.forecasts(provider)
        if not items:
            return "", "unknown"
        if provider == "codex" and self.codex_quota_mode():
            weekly = next((outlook for label, outlook in items if label == "每周"), None)
            if weekly is not None:
                return f"每周额度：{weekly.text}", weekly.tone
        rank = {"danger": 0, "warn": 1, "ok": 2, "idle": 3, "unknown": 4}
        label, chosen = min(items, key=lambda item: (rank[item[1].tone], item[1].exhausts_at or float("inf")))
        prefix = f"{label}额度：" if len(items) > 1 and label else ""
        return prefix + chosen.text, chosen.tone

    POOL_FORECAST_FLOOR = 10.0   # % left: below this a pool's own run-out time is not announced

    def pool_forecast(self, provider: str, now: float | None = None) -> tuple[str, str]:
        """Independent model pools (Gemini): name only the pools on course to
        run out before their reset; otherwise one plain "enough" line."""
        if self.history is None:
            return "", "unknown"
        now = now or time.time()
        running_out = []
        for label, remaining, resets_at, _minutes, names in self.quota_windows(provider):
            if remaining is None or remaining < self.POOL_FORECAST_FLOOR:
                continue
            samples = max((forecast_module.current_window(self.history.samples(name), resets_at) for name in names), key=len)
            outlook = forecast_module.forecast(samples, now, remaining, resets_at)
            if outlook.exhausts_at is not None and outlook.before_reset is not False and outlook.tone in ("warn", "danger"):
                running_out.append((outlook.exhausts_at, label, outlook.tone))
        if not running_out:
            return "额度够用", "ok"
        running_out.sort()
        text = "；".join(f"{label} 预计 {duration_text(at - now)}后用完" for at, label, _ in running_out)
        return text, "danger" if any(tone == "danger" for *_, tone in running_out) else "warn"

    def forecast_line(self, provider: str) -> str:
        text, _ = self.headline_forecast(provider)
        return f"预测：{text}" if text else ""

    CHART_WEEKLY_BELOW = 10.0       # Codex 5-hour % left at which the chart shows the weekly window
    CHART_VIEW_MAX = 5 * 3600        # The solid line: the live window, at most its last 5 hours.
    CHART_POINTS = 160
    CHART_RECENT_SECONDS = 30 * 60  # keep completed work visible long enough to inspect its trend

    def quota_windows(self, provider: str) -> list[tuple]:
        """(label, remaining, resets_at, window minutes, history names) in the
        order of the panel's header columns, so a line and its bar share a slot."""
        snapshot = self.snapshots[provider]
        if provider == "mimo":
            return [("套餐额度", snapshot.display_remaining, snapshot.resets_at, snapshot.window_minutes, (provider,))]
        if snapshot.pools and len(snapshot.pools) > 1:
            return [(pool.get("label") or pool.get("key") or "额度", pool.get("remaining_percent"), pool.get("resets_at"),
                     pool.get("window_minutes"), (f"{provider}:pool:{pool.get('key')}",) + ((provider,) if index == 0 else ()))
                    for index, pool in enumerate(snapshot.pools[:3])]
        windows = [(window_label(snapshot.window_minutes) or "当前", snapshot.remaining_percent, snapshot.resets_at,
                    snapshot.window_minutes, (provider,))]
        if snapshot.secondary_window_minutes or snapshot.secondary_remaining_percent is not None:
            windows.append((window_label(snapshot.secondary_window_minutes) or "第二窗口", snapshot.secondary_remaining_percent,
                            snapshot.secondary_resets_at, snapshot.secondary_window_minutes, (provider + ":secondary",)))
        return windows

    def source_working(self, provider: str) -> bool:
        data = (getattr(self, "performance", None) or {}).get("providers", {}).get(provider, {})
        return bool(data.get("active_sessions", 0) or any(
            agent.get("live") and agent.get("provider") == provider for agent in getattr(self, "agents", [])))

    def exhausted_state(self, provider: str) -> tuple[str | None, tuple[int, ...]]:
        """Known credits, inferred Claude extra usage, working, or blocked."""
        snapshot = self.snapshots.get(provider)
        if snapshot is None or snapshot.stale or provider == "mimo" or (snapshot.pools and len(snapshot.pools) > 1):
            return None, ()
        values = (snapshot.remaining_percent, snapshot.secondary_remaining_percent)
        empty = tuple(slot for slot, value in enumerate(values) if quota_exhausted(value, provider))
        if not empty:
            return None, ()
        if all(values[slot] >= 0.5 for slot in empty):
            return ("near_working" if self.source_working(provider) else "near"), empty
        if provider == "codex" and snapshot.credits_balance is not None and snapshot.credits_balance > 0:
            return "paid", empty
        if self.source_working(provider):
            now = time.time()
            events = getattr(getattr(self, "tps_monitor", None), "events", ())
            if provider == "claude" and snapshot.updated_at and any(
                    event.provider == "claude" and event.output_tokens > 0 and
                    max(now-120, snapshot.updated_at-5) <= event.ended_at <= now+5 for event in events):
                return "inferred", empty
            return "working", empty
        return "blocked", empty

    def codex_quota_mode(self) -> bool:
        if "codex" not in self.snapshots or not self.source_working("codex"):
            return False
        windows = self.quota_windows("codex")
        five = next((w for w in windows if w[3] == 300 and w[1] is not None), None)
        week = next((w for w in windows if w[3] == 10080 and w[1] is not None), None)
        # The chart moves to the weekly curve only once the 5-hour window is
        # nearly empty, not as early as the icon's 15% countdown.
        return bool(five and five[1] <= self.CHART_WEEKLY_BELOW and week)

    def active_sources(self, now: float) -> dict[str, set[str]]:
        """Sources working now or with real output in the last half hour, and
        the (lower-cased) models they ran."""
        active: dict[str, set[str]] = {}
        for agent in getattr(self, "agents", []):
            if agent.get("live") and agent.get("provider") in self.snapshots:
                active.setdefault(agent["provider"], set()).add(str(agent.get("model") or "").lower())
        for key, data in self.performance.get("providers", {}).items():
            if data.get("active_sessions", 0) > 0 and key in self.snapshots:
                active.setdefault(key, set())
        monitor = getattr(self, "tps_monitor", None)
        for event in getattr(monitor, "events", ()):
            if (event.provider in self.snapshots and event.output_tokens > 0
                    and now - self.CHART_RECENT_SECONDS <= event.ended_at <= now + 5):
                active.setdefault(event.provider, set()).add((event.model or "").lower())
        desktop_use = getattr(monitor, "claude_desktop_last_use", 0.0)
        if "claude" in self.snapshots and desktop_use > 0 and now - self.CHART_RECENT_SECONDS <= desktop_use <= now + 5:
            active.setdefault("claude", set())
        return active

    def live_models(self, provider: str) -> set[str]:
        """Only models tied to current work; recent completed sessions do not mark a pool active."""
        return {str(agent.get("model") or "").lower() for agent in getattr(self, "agents", [])
                if agent.get("live") and agent.get("provider") == provider and agent.get("model")}

    @staticmethod
    def pool_keys(models) -> set[str]:
        """Gemini pools the given models draw from."""
        keys = set()
        for model in models:
            if "argon" in model:
                keys.add("argon")
            elif any(name in model for name in ("claude", "opus", "sonnet", "gpt")):
                keys.add("claude_gpt")
            elif "gemini" in model:
                keys.add("gemini")
        return keys

    def burn_chart(self, provider: str, now: float) -> "agent_panel.BurnChart | None":
        """Quota trends of every model with a reading, identical on every icon."""
        if self.history is None:
            return None
        candidates = []
        active = self.active_sources(now)
        for key in provider_registry.tray_positions(self.snapshots):
            snapshot = self.snapshots[key]
            pools_in_use = self.pool_keys(active.get(key, ())) if key == "gemini" else set()
            windows = self.quota_windows(key)
            selected = None
            quota_mode = key == "codex" and self.codex_quota_mode()
            if key in ("codex", "claude"):
                selected = next((index for index, window in enumerate(windows)
                                 if window[3] == 300 and window[1] is not None), None)
                if quota_mode:
                    selected = next(index for index, window in enumerate(windows) if window[3] == 10080)
            for slot, (label, remaining, resets_at, minutes, names) in enumerate(windows):
                if selected is not None and slot != selected:
                    continue
                if remaining is None:
                    continue
                recorded = max((self.history.samples(name) for name in names),
                               key=lambda items: len(forecast_module.current_window(items, resets_at)))
                samples = forecast_module.current_window(recorded, resets_at)
                outlook = forecast_module.forecast(samples, now, remaining, resets_at)
                rate = outlook.rate_per_hour
                if key == "mimo":
                    view = mimo_tracker().view(now)
                    rate = view.pace_daily / view.total * 100 / 24 if view.pace_daily and view.total else rate
                # The last hours across resets: the saved history before a reset
                # (or a restart) stays on screen, stepping back up at the reset.
                points, _steps = forecast_module.history_curve(recorded, now - self.CHART_VIEW_MAX, now, minutes)
                updated = snapshot.updated_at
                if not points or (updated and points[-1][0] + 1 < updated <= now):
                    points.append((float(min(now, updated or now)), float(remaining)))
                if key == "mimo":
                    chart_label = "MiMo"
                elif key == "grok":
                    chart_label = "Grok"
                else:
                    name = "Gemini" if key == "gemini" else provider_registry.get(key).label
                    chart_label = name if label == name else f"{name} · {label}"
                if quota_mode:
                    chart_label += "（5 小时已用完）"
                pooled_models = snapshot.pools and len(snapshot.pools) > 1
                idle = key not in active or bool(pooled_models and snapshot.pools[slot].get("key") not in pools_in_use)
                series = agent_panel.BurnSeries(chart_label, slot,
                    tuple((round(t, 1), round(r, 2)) for t, r in points),
                    None if rate is None else round(rate, 3), float(resets_at) if resets_at else None,
                    provider_letter(key), len(windows) > 1, idle=idle)
                candidates.append((series, outlook))
        if not candidates:
            return None
        # How far back: a close-up while quota drops fast, longer for slow use
        # (agent_panel.burn_span). The forecast gets a fixed share to the right
        # of now; a far exhaustion or reset stays in the text, not on the axis.
        minutes = agent_panel.burn_span([series.points for series, _ in candidates], now, getattr(self, "burn_minutes", None))
        self.burn_minutes = minutes
        back = minutes * 60
        # Quantised "now": the cached chart is redrawn every few seconds, not
        # every frame: every 5 s in a 5-minute view, at most every 30 s.
        step = max(5.0, min(30.0, back / 60))
        shown = float(round(now / step) * step)
        start, end = shown - back, shown + agent_panel.burn_future(back)
        # Readings for the furthest span a hover eases out to (30 min, 2 h, then 5 h).
        keep = shown - max(back, agent_panel.BURN_DWELL_BEATS[-1][0] * 60)
        series = tuple(self._fit_points(item[0], keep) for item in candidates)
        return agent_panel.BurnChart(float(start), float(end), shown, series)

    def _fit_points(self, series: "agent_panel.BurnSeries", start: float) -> "agent_panel.BurnSeries":
        """Readings from just before the view on, thinned to CHART_POINTS
        evenly over the view; reset steps and the latest reading stay."""
        points = list(series.points)
        before = [index for index, (t, _r) in enumerate(points) if t < start]
        points = points[before[-1]:] if before else points
        if len(points) > self.CHART_POINTS:
            steps = {i for i in range(1, len(points)) if points[i][1] > points[i - 1][1] + 1}
            steps |= {i - 1 for i in steps}
            stride = len(points) / self.CHART_POINTS
            keep = sorted(steps | {int(i * stride) for i in range(self.CHART_POINTS - 1 - len(steps))} | {len(points) - 1})
            points = [points[i] for i in keep]
        return dataclasses.replace(series, points=tuple(points))

    def hover_text(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        data = self.performance["providers"][provider]
        lines = [data["label"], quota_line(snapshot)]
        if provider == "mimo":
            view = mimo_tracker().view()
            lines.append(f"已用 {mimo_usage.credits_text(view.used)} / 共 {mimo_usage.credits_text(view.total)}"
                         f" · 剩余 {mimo_usage.credits_text(view.remaining)}")
            if view.error:
                lines.append(view.error)
        if self.forecast_line(provider):
            lines.append(self.forecast_line(provider))
        lines += (["", self.performance_line(provider), self.output_rate_line(provider)] if provider != "gemini" else [])
        lines += [f"活动会话：{data['active_sessions']}", "",
            self.menu_reset(provider), self.menu_countdown(provider), self.menu_updated(provider)]
        if snapshot.error:
            lines.extend(("", "读取状态：" + snapshot.error))
        return "\n".join(lines)

    def mimo_forecast(self) -> tuple[str, str]:
        view = mimo_tracker().view()
        if view.remaining is None:
            return "", "unknown"
        if not view.pace_daily:
            return "正在积累用量记录，稍后给出节奏预测", "unknown"
        if view.period_end and view.exhausts_at and view.exhausts_at < view.period_end:
            early = (view.period_end - view.exhausts_at) / 86400
            when = datetime.fromtimestamp(view.exhausts_at).strftime("%Y-%m-%d")
            return f"按近期速度约 {when} 用完，比到期早 {early:.0f} 天", "danger" if early > 30 else "warn"
        if view.period_end:
            left = view.remaining - view.pace_daily * max(0.0, (view.period_end - time.time()) / 86400)
            share = max(0.0, 100.0 * left / view.total) if view.total else 0.0
            return f"按近期速度可用到到期，预计剩余 {share:.0f}%", "ok"
        return f"日均消耗 {mimo_usage.credits_text(view.pace_daily)}", "ok"

    # ----- agent panel (hover thread) ---------------------------------------------

    def panel(self, provider: str) -> agent_panel.PanelModel | None:
        """The hover panel: quota header, task bubbles, forecast and settings.
        None for a source without a quota to show (Claude without a
        subscription), which keeps the native tooltip instead."""
        now = time.time()
        if not has_panel(provider, self.snapshots.get(provider)):
            return None
        agents = [agent for agent in self.agents if not agent.get("schedule")]
        clusters = self.agent_groups.compose(agents)
        working = sum(1 for agent in agents if agent["live"] and agent["provider"] == provider)
        self.clusters = {cluster.key: cluster for cluster in clusters}
        bubbles = []
        for cluster in clusters:
            summary, busy = self.summarizer.lookup(cluster)
            title, doing, detail = agent_tasks.describe(cluster, summary)
            kinds = [m["provider"] for m in cluster.members]
            # Fixed colour per group: following whichever member is live made it flicker.
            lead = provider if provider in kinds else kinds[0]
            members = tuple((provider_letter(m["provider"]), m["live"]) for m in cluster.members) if len(cluster.members) > 1 else ()
            bubbles.append(agent_panel.BubbleModel(cluster.key, agent_tasks.shorten(title, 16), doing, provider_letter(lead),
                                                   cluster.live, detail, members, busy, provider in kinds,
                                                   tuple(m["key"] for m in cluster.members)))
        # Same order for every icon so the panel keeps its size and layout when switching.
        snapshot = self.snapshots.get(provider)
        quota: list[tuple] = []
        tones: list[str] = []
        # Window columns are titled by the time left until their reset; the
        # window name stays as a fallback when no reset is scheduled. Model
        # pools follow suit and name the model under the bar.
        if snapshot is not None and provider == "mimo":
            quota.append((reset_distance(snapshot.resets_at, now) or "套餐额度", snapshot.display_remaining, ""))
        elif snapshot is not None and snapshot.pools and len(snapshot.pools) > 1:
            in_use = self.pool_keys(self.live_models(provider)) if self.source_working(provider) else set()
            for pool in snapshot.pools[:3]:
                name = pool.get("label") or pool.get("key") or "额度"
                countdown = reset_distance(pool.get("resets_at"), now)
                quota.append((countdown or name, pool.get("remaining_percent"), name if countdown else ""))
                tones.append("active" if countdown and pool.get("key") in in_use else "")
        elif snapshot is not None:
            quota.append((reset_distance(snapshot.resets_at, now) or window_label(snapshot.window_minutes) or "当前",
                          snapshot.remaining_percent, ""))
            if snapshot.secondary_window_minutes or snapshot.secondary_remaining_percent is not None:
                quota.append((reset_distance(snapshot.secondary_resets_at, now)
                              or window_label(snapshot.secondary_window_minutes) or "第二窗口",
                              snapshot.secondary_remaining_percent, ""))
        state, empty = self.exhausted_state(provider)
        figures: list = [None] * len(quota)
        if state:
            balance = snapshot.credits_balance
            paid = "购买额度" if balance is None else "购买额度 无限" if balance == float("inf") else f"购买额度 {compact_number(balance)}"
            tones = [""] * len(quota)
            for slot in empty:
                if slot < len(quota):
                    note = paid if state == "paid" else "可能额外额度" if state == "inferred" else "接近用尽" if state in ("near", "near_working") else "已用完"
                    if state == "paid" and balance is not None:
                        # The credits left replace the "0 %" figure itself.
                        figures[slot] = ("∞", "") if balance == float("inf") else agent_panel.compact_usage(balance)
                        note = "购买额度"
                    quota[slot] = quota[slot][:2] + (note,)
                    tones[slot] = "paid" if state in ("paid", "inferred") else "blocked"
        data = self.performance["providers"].get(provider, {})
        if state == "paid":
            status, tone = ("购买额度运行" if working or self.source_working(provider) else "可用购买额度"), "paid"
        elif state == "inferred":
            status, tone = "可能使用额外额度", "paid"
        elif state in ("working", "near_working"):
            status, tone = "额度模式工作", "live"
        elif state == "near":
            status, tone = "接近用尽", "blocked"
        elif state == "blocked":
            status, tone = "等待重置", "blocked"
        elif working:
            status, tone = f"{working} 个在工作", "live"
        elif snapshot is not None and snapshot.stale:
            status, tone = "旧数据", "stale"
        elif snapshot is not None and snapshot.error and snapshot.display_remaining is None:
            status, tone = "读取失败", "error"
        else:
            status, tone = "空闲", "idle"
        # The Codex reset notice is shown on every panel (same size everywhere).
        banner, alert, codex = "", "", self.snapshots.get("codex")
        if reset_watch.ENABLED and codex is not None and "codex" in self.snapshots:
            outlook = getattr(self, "reset_outlook", None)
            banner = reset_watch.panel_line(outlook, now)
            alert = "官方预告" if reset_watch.announced(outlook, now) else ""
        # The model behind the TPS figure leads the IQ line.
        running = [data["display_model"]] if data.get("display_tps") is not None and data.get("display_model") else []
        intel, intel_note = self.panel_intel(now, running)
        forecast, forecast_tone = self.headline_forecast(provider)
        hint_data = agent_hints.context(agents, self.snapshots, self.performance.get("software", []))
        advisor = getattr(self, "hint_advisor", None)
        hint_now, hint_next = advisor.lookup(hint_data, getattr(self, "tps_monitor", None)) if advisor is not None else agent_hints.local_hint(hint_data)
        today_used = None
        if provider == "mimo":
            today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            today_used = (snapshot.today_used if snapshot and snapshot.today_date == today
                          else mimo_tracker().view(now).today_used)
        column_iq = grok_iq.column(getattr(self, "grok_iq", None)) if provider == "grok" else ()
        return agent_panel.PanelModel(provider, PROVIDERS.get(provider, provider_letter(provider)), data.get("label", provider),
                                      status, tuple(quota), None if provider == "gemini" else data.get("display_tps") or 0.0, tuple(bubbles[:agent_panel.MAX_BUBBLES]),
                                      tone, banner, forecast, forecast_tone, today_used,
                                      self.burn_chart(provider, now), alert, intel, intel_note,
                                      quota_tones=tuple(tones), quota_figures=tuple(figures), column_iq=column_iq,
                                      hint_now=hint_now, hint_next=hint_next)

    def merge(self, first: str, second: str) -> str | None:
        a, b = self.clusters.get(first), self.clusters.get(second)
        if a is None or b is None or a.key == b.key:
            return None
        merged = self.agent_groups.merge(a, b)
        log.info("agent group merged: %s members=%s", merged, len(a.members) + len(b.members))
        return merged

    def split(self, key: str) -> list[str] | None:
        cluster = self.clusters.get(key)
        if cluster is None or len(cluster.members) < 2:
            return None
        members = self.agent_groups.split(cluster)
        log.info("agent group split: %s members=%s", key, len(members))
        return members

    def tooltip(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        performance = self.performance["providers"][provider]
        value = performance.get("generation_tps")
        generation = "暂无完整计数" if value is None else f"{value:.1f}"
        hint = f"可能即将全局重置（24h {self.reset_outlook.prob_24}%）\n" if provider == "codex" and self.reset_level() == "high" else ""
        if provider == "gemini":
            return f"{quota_line(snapshot)}\n{self.menu_reset(provider)}"[:127]
        return f"{hint}{quota_line(snapshot)}\n最近TPS {generation} · 60秒 {performance['output_tps_60']:.1f} tok/s\n{self.menu_reset(provider)}"[:127]

    def build_menu(self, provider: str) -> pystray.Menu:
        return pystray.Menu(
            # Each build_menu call owns its provider closure. pystray counts
            # defaulted positional parameters too and rejects more than two.
            pystray.MenuItem("打开对应程序", lambda _icon, _item: self.open_provider(provider), default=True),
            pystray.MenuItem(lambda _: "立即刷新", self.request_refresh),
            pystray.MenuItem("额度详情", lambda _icon, _item: self.open_details(provider)),
            pystray.MenuItem("本机全部 AI 速率", self.open_all_performance),
            pystray.MenuItem("设置…", self.open_settings),
            pystray.MenuItem(lambda _: self.menu_quota(provider), None, enabled=False),
            pystray.MenuItem(lambda _: self.forecast_line(provider) or "预测：暂无", None, enabled=False),
            pystray.MenuItem(lambda _: self.menu_reset(provider), None, enabled=False),
            pystray.MenuItem(lambda _: self.menu_countdown(provider), None, enabled=False),
            pystray.MenuItem(lambda _: self.menu_updated(provider), None, enabled=False),
            *((pystray.MenuItem("登录小米 MiMo…", self.mimo_login),
               pystray.MenuItem("退出 MiMo 登录", self.mimo_logout, visible=lambda _: mimo_usage.COOKIE_PATH.exists()))
              if provider == "mimo" else ()),
            *((pystray.MenuItem(lambda _: self.menu_reset_outlook(), self.open_reset_page, visible=lambda _: reset_watch.ENABLED),)
              if provider == "codex" else ()),
            pystray.MenuItem(lambda _: "打开诊断信息", self.open_diagnostics),
            pystray.MenuItem("Claude Code 额度与 TPS", self.toggle_claude,
                             checked=lambda _: "claude" in PROVIDERS),
            pystray.MenuItem(
                lambda _: "开机自启",
                self.toggle_startup,
                checked=lambda _: self.startup_enabled(),
            ),
            pystray.MenuItem(lambda _: "退出全部", self.exit_app),
        )

    def menu_quota(self, provider: str) -> str:
        return quota_line(self.snapshots[provider])

    def menu_reset(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        if window_idle(snapshot):
            return "重置时间：窗口未开始（下次使用时开始计时）"
        return f"重置时间：{reset_text(snapshot.resets_at)}"

    def menu_countdown(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        return f"距离重置：{'未开始' if window_idle(snapshot) else reset_countdown(snapshot.resets_at)}"

    def menu_updated(self, provider: str) -> str:
        updated = self.snapshots[provider].updated_at
        if not updated:
            return "更新时间：未知"
        return "更新时间：" + datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M:%S")

    def detail_sections(self, provider: str) -> list[tuple[str, list[str]]]:
        """The quota details as (group title, "标题：内容" lines): one tile
        each on the settings window's detail page, and the tray's 额度详情 box."""
        snapshot = self.snapshots[provider]
        quota = [quota_line(snapshot)] + (pool_lines(snapshot) if snapshot.pools and len(snapshot.pools) > 1 else [])
        for label, item in self.forecasts(provider):
            quota.append(f"预测{f'（{label}）' if label else ''}：{item.text}")
        sections = [("额度", quota)]
        if provider != "gemini":
            sections.append(("速度", [*self.performance_line(provider).split("\n"), self.output_rate_line(provider)]))
        resets = [self.menu_reset(provider), self.menu_countdown(provider)]
        if snapshot.secondary_resets_at:
            resets.append(f"{window_label(snapshot.secondary_window_minutes)}重置：{reset_text(snapshot.secondary_resets_at)}")
        sections.append(("重置", resets))
        usage = getattr(self, "usage", None)
        if usage is not None:
            samples = self.history.samples(provider) if self.history is not None else []
            window = window_label(snapshot.window_minutes) if snapshot.window_minutes else ""
            sections.append(("用量", usage_insights(usage, provider, time.time(), samples, f"{window}额度" if window else "")))
        reads = [self.menu_updated(provider)]
        if self.scheduler:
            reads.append("刷新计划：" + self.scheduler.describe(provider, time.time()))
        if snapshot.error:
            reads.append("读取状态：" + snapshot.error)
        sections.append(("读取", reads))
        return sections

    def open_details(self, provider: str) -> None:
        text = "\n\n".join("\n".join(lines) for _title, lines in self.detail_sections(provider))
        threading.Thread(
            target=ctypes.windll.user32.MessageBoxW,
            args=(None, text, f"{self.snapshots[provider].label} · 额度详情", 0),
            daemon=True,
        ).start()

    def preference(self, name: str, default=None):
        return load_preferences().get(name, default)

    def performance_line(self, provider: str) -> str:
        data = self.performance["providers"][provider]
        value = data.get("generation_tps")
        if value is None:
            return "TPS：等待完整流式计数" if provider == "codex" else "TPS：客户端未提供原生数值"
        basis = "、".join(data.get("basis") or [])
        sample = datetime.fromtimestamp(data["last_sample_at"]).strftime("%H:%M:%S") if data.get("last_sample_at") else "未知"
        return f"最近生成 TPS：{value:.1f} tok/s（{sample}）\n来源：{basis}"

    def output_rate_line(self, provider: str) -> str:
        data = self.performance["providers"][provider]
        return f"最近60秒输出速率：{data['output_tps_60']:.1f} tok/s（{data['output_tokens_60']} 输出tokens）"

    def open_all_performance(self, _icon=None, _item=None) -> None:
        sections = [f"本机合计 · 最近60秒：{self.performance['total_output_tps_60']:.1f} tok/s", ""]
        for provider, data in self.performance["providers"].items():
            if provider == "gemini":
                continue
            sections.extend((data["label"], self.performance_line(provider), self.output_rate_line(provider), ""))
        sections.append("检测到的软件：")
        sections.extend(f"{item['name']}：{item['status']}" for item in self.performance.get("software", []))
        sections.extend(("", self.performance["measurement_note"]))
        threading.Thread(target=ctypes.windll.user32.MessageBoxW, args=(None, "\n".join(sections), "本机 AI · TPS", 0), daemon=True).start()

    # ----- icon updates ----------------------------------------------------------

    def update_ui(self) -> None:
        save_cached_snapshots(self.snapshots)
        for provider, letter in PROVIDERS.items():
            self.update_icon(provider, letter)
            self.icons[provider].update_menu()
        self.maybe_reorder(time.time())

    def wanted_order(self) -> list[str]:
        return provider_registry.tray_positions(PROVIDERS)

    def slots_by_position(self) -> list:
        """The tray icons ("slots") as Windows currently shows them, left to
        right (top to bottom on a vertical taskbar). Icons that are not on
        the taskbar keep their creation order at the end, so the providers
        that come last are the ones left in the overflow flyout."""
        slots = getattr(self, "slots", None) or list(self.icons.values())
        shown = hover_details.tray_visibility()
        placed, unplaced = [], []
        for index, slot in enumerate(slots):
            try:
                rect = hover_details.icon_rect(slot)
            except Exception:
                rect = None
            # Windows' own record says hidden: the rectangle is the ^ button's.
            hidden = shown.get(getattr(slot, "icon_id", index)) is False
            (placed if rect and not hidden else unplaced).append((rect, index, slot))
        # Overflow icons all report the ^ button's rectangle; two of ours on
        # one spot are both hidden (the only check where Windows keeps no record).
        spots = [rect for rect, _, _ in placed]
        stacked = [item for item in placed if spots.count(item[0]) > 1]
        if stacked:
            placed = [item for item in placed if item not in stacked]
            unplaced = sorted(unplaced + stacked, key=lambda item: item[1])
        if len(unplaced) != getattr(self, "hidden_slots", 0):
            self.hidden_slots = len(unplaced)
            if unplaced:
                log.warning("%d of %d tray icons are not on the taskbar; turn them on in Settings > Personalization > "
                            "Taskbar > Other system tray icons (or drag them out of ^)", len(unplaced), len(slots))
        if placed:
            xs = [rect[0] for rect, _, _ in placed]
            ys = [rect[1] for rect, _, _ in placed]
            axis = 0 if max(xs) - min(xs) >= max(ys) - min(ys) else 1
            placed.sort(key=lambda item: (item[0][axis], item[1]))
        return [slot for _, _, slot in placed] + [slot for _, _, slot in unplaced]

    def ensure_icons_registered(self) -> None:
        with self.ui_lock:
            for index, slot in enumerate(getattr(self, "slots", ())):
                ensure = getattr(slot, "ensure_registered", None)
                if ensure is not None and ensure():
                    log.warning("tray icon %d was missing from the notification area; added again", index)
                elif getattr(slot, "resend_icon", None) is not None and not getattr(slot, "details_visible", False):
                    # Every 30 s each slot's picture is sent again: a change the
                    # Shell dropped can never leave a source showing another's icon.
                    slot.resend_icon()

    def maybe_reorder(self, now: float, force: bool = False) -> bool:
        """Show providers in wanted_order() by assigning them to the existing
        tray icons by on-screen position.  Icons are never deleted or re-added:
        Explorer restores its own remembered layout for re-added icons, so
        moving them does not stick; swapping what each icon shows does.
        The wanted order is fixed, so this only acts when Explorer's own
        layout (startup, reflow, a manual drag) differs from it."""
        icons = getattr(self, "icons", None)
        if not icons:
            return False
        if not force and now - self.reordered_at < REORDER_MIN_SECONDS:
            return False
        if any(getattr(icon, "details_visible", False) or getattr(icon, "hover_open", False) for icon in icons.values()):
            return False   # never change an icon under the pointer
        wanted = self.wanted_order()
        mapping = dict(zip(wanted, self.slots_by_position()))
        if len(mapping) != len(icons) or all(icons[p] is mapping[p] for p in mapping):
            self.shown_order = wanted
            return False
        with self.ui_lock:
            icons.update(mapping)          # same keys: safe for the hover thread
            for provider in wanted:
                icon = icons[provider]
                icon.details_visible = False
                icon.menu = self.build_menu(provider)
                self.update_icon(provider, PROVIDERS[provider])
        self.shown_order, self.reordered_at = wanted, now
        log.info("tray order: %s", ", ".join(wanted))
        return True

    def update_icon(self, provider: str, letter: str) -> None:
        # The refresh worker and the 1 s TPS loop both land here. pystray
        # replaces the native HICON on assignment, so serialise the updates
        # (this also keeps the cached FreeType faces single-threaded).
        with self.ui_lock:
            icon = self.icons[provider]
            if getattr(icon, "details_visible", False):
                # Keep the Shell's hover target stable. The independent detail
                # window still reads live values from self.performance.
                return
            image = make_display_icon(self.snapshots[provider], letter, self.performance["providers"][provider],
                                      reset_hint=provider == "codex" and self.reset_level() == "high")
            # Icon assignment always sends NIM_MODIFY, even for identical pixels.
            # Avoid needless Shell redraws; hover detail updates use a separate HWND.
            # A picture the Shell refused is sent again even when unchanged.
            if (icon.icon is not image and icon.icon != image) or getattr(icon, "icon_failed", False):
                icon.icon = image
            icon.title = self.tooltip(provider)

    # ----- refreshing ------------------------------------------------------------

    def refresh_providers(self, providers) -> None:
        providers = list(providers)
        if not providers:
            return
        # Read outside the lock: a slow source (a Grok CLI session) must not
        # hold up the others. Only merging the results is serialised.
        started = time.monotonic()
        results = collect(providers, self.snapshots)
        duration = time.monotonic() - started
        with self.refresh_lock:
            now = time.time()
            for provider, snapshot in results.items():
                old = self.snapshots.get(provider)
                if provider == "gemini" and old is not None and not snapshot.error:
                    scope = lambda item: tuple((p.get("key"), p.get("window_minutes")) for p in item.pools or [])
                    if scope(old) != scope(snapshot):
                        if self.history is not None:
                            self.history.clear(provider)
                        log.info("gemini quota pools changed: %s", [p.get("key") for p in snapshot.pools or []])
                self.announce_resets(provider, self.snapshots.get(provider), snapshot, now)
            snapshots = dict(self.snapshots)
            snapshots.update(results)
            self.snapshots = snapshots
            if self.history is not None:
                if any([self.history.record(snapshot) for snapshot in results.values()]):
                    self.history.save()
            if self.scheduler is not None:
                for provider, snapshot in results.items():
                    self.scheduler.completed(provider, snapshot, now, duration)
            for provider, snapshot in results.items():
                log.info("quota %s: left=%s second=%s reset=%s/%s stale=%s error=%s (%.1fs)", provider,
                         snapshot.remaining_percent, snapshot.secondary_remaining_percent, snapshot.resets_at,
                         snapshot.secondary_resets_at, snapshot.stale, snapshot.error, duration)
            self.update_ui()

    def refresh(self) -> None:
        """Synchronous refresh of every provider (used by checks and tests)."""
        self.refresh_providers(PROVIDERS)

    def request_refresh(self, _icon=None, _item=None) -> None:
        if self.scheduler is None:
            threading.Thread(target=self.refresh, daemon=True).start()
            return
        self.scheduler.request(None, time.time())
        self.wake.set()

    def refresh_loop(self) -> None:
        """Each due source is read on its own thread, so a hovered Codex is
        not kept waiting behind a Grok CLI session."""
        while not self.stop_event.is_set():
            try:
                for provider in self.scheduler.take_due(time.time()):
                    threading.Thread(target=self.refresh_one, args=(provider,), name=f"quota-{provider}", daemon=True).start()
            except Exception:
                log.exception("quota refresh scheduling failed")
            self.wake.wait(self.scheduler.next_wake(time.time()))
            self.wake.clear()

    def refresh_one(self, provider: str) -> None:
        try:
            self.refresh_providers([provider])
        except Exception:
            log.exception("quota refresh of %s failed", provider)
        finally:
            if provider in self.scheduler.running:    # never leave a provider stuck "running"
                self.scheduler.completed(provider, placeholder(provider, "刷新异常"), time.time())
            self.wake.set()                          # its next due time may be the earliest now

    def feed_scheduler(self, performance: dict, now: float) -> None:
        """Live state for the refresh cadence: generating or a live agent of
        the source (active), and the pointer on its icon or panel (hover)."""
        moved = False
        icons = getattr(self, "icons", None) or {}
        pointed = {provider for provider, icon in icons.items()
                   if getattr(icon, "details_visible", False) or getattr(icon, "hover_open", False)}
        if pointed:
            # The panel charts every source in use, so those are being looked at too.
            pointed |= set(self.active_sources(now))
        for provider in pointed - quota_sources.WATCHED:
            quota_sources.HOVER_STARTED[provider] = now          # a new look: its first read is a real check
        quota_sources.WATCHED = frozenset(pointed)
        quota_sources.ACTIVE = frozenset(p for p in self.scheduler.providers if self.source_working(p))
        for provider in icons:
            if provider in self.scheduler.providers:
                moved |= self.scheduler.hover(provider, provider in pointed, now)
        for provider in self.scheduler.providers:
            data = performance["providers"].get(provider)
            if data is None:
                continue
            sample = data.get("last_sample_at")
            previous = self.last_samples.get(provider)
            finished = sample is not None and previous is not None and sample > previous
            if sample is not None:
                self.last_samples[provider] = sample
            elif provider not in self.last_samples:
                self.last_samples[provider] = None
            moved |= self.scheduler.activity(provider, self.source_working(provider), finished, now)
        if moved:
            self.wake.set()

    def save_performance_if_changed(self, performance: dict, now: float) -> None:
        signature = performance_signature(performance)
        if signature == self.saved_signature and now - self.saved_at < PERFORMANCE_SAVE_SECONDS:
            return
        save_performance(performance)
        self.saved_signature, self.saved_at = signature, now

    def tps_loop(self) -> None:
        last_wall = time.time()
        failures = 0
        while not self.stop_event.wait(1):
            now = time.time()
            try:
                if now - last_wall > 120:
                    # Sleep/hibernate: quotas may have reset meanwhile.
                    self.scheduler.clock_jump(now)
                    self.wake.set()
                last_wall = now
                self.performance = self.tps_monitor.poll(now)
                self.agents = self.tps_monitor.agents(now)
                self.save_performance_if_changed(self.performance, now)
                self.feed_scheduler(self.performance, now)
                if "mimo" in PROVIDERS:
                    self.update_mimo_locally(now)
                if "codex" in PROVIDERS:
                    self.update_codex_locally(now)
                if "claude" in PROVIDERS:
                    self.update_claude_locally(now)
                if now - getattr(self, "registered_at", 0.0) >= 30:
                    self.registered_at = now
                    self.ensure_icons_registered()
                if now - getattr(self, "order_checked_at", 0.0) >= ORDER_CHECK_SECONDS:
                    self.order_checked_at = now
                    self.maybe_reorder(now)   # a drag or Explorer reflow is undone within seconds
                for provider, letter in PROVIDERS.items():
                    if self.icons[provider].visible:
                        self.update_icon(provider, letter)
                failures = 0
            except Exception:
                # Any unexpected error used to end this thread silently and
                # freeze the TPS display; log (rate-limited) and keep going.
                failures += 1
                if failures in (1, 10) or failures % 300 == 0:
                    log.exception("TPS loop error (%d in a row)", failures)

    # ----- actions ---------------------------------------------------------------

    def open_provider(self, provider: str) -> None:
        if provider in HERDR_AGENTS and herdr_source.find_exe():
            # A new focused Herdr tab running the agent. Starting waits for the
            # agent to be ready, so it runs off the tray thread; clicks that
            # arrive meanwhile would only start more agents.
            opening = self.__dict__.setdefault("herdr_opening", set())
            if provider in opening:
                return
            opening.add(provider)
            # Raise the (often minimised) Herdr window now, while the click
            # still lets the tray take the foreground.
            herdr_source.raise_client()
            threading.Thread(target=self.open_in_herdr, args=(provider,), name="herdr-open", daemon=True).start()
            return
        kind, resolve = LAUNCHERS[provider]
        label = provider_registry.get(provider).label
        try:
            target = resolve()
            if kind == "url":
                os.startfile(str(target))  # type: ignore[attr-defined]
                return
            if kind == "aumid":
                launched = ctypes.windll.shell32.ShellExecuteW(None, "open", f"shell:AppsFolder\\{target}", None, None, 1)
                if launched <= 32:
                    raise OSError(f"无法启动 {label}（错误 {launched}）")
                return
            if target is None or not Path(target).is_file():
                raise FileNotFoundError(f"未找到 {label} 客户端：{target or '未安装'}")
            path = Path(target)
            flags = subprocess.CREATE_NEW_CONSOLE if kind == "cli" else 0
            # A CLI opened from its install folder would work on that folder;
            # Claude Code and similar start in the user's home instead.
            cwd = path.parent if provider == "grok" else Path.home()
            command = ["cmd.exe", "/k", str(path)] if path.suffix.lower() in (".cmd", ".bat") else [str(path)]
            subprocess.Popen(command, cwd=str(cwd), creationflags=flags, close_fds=True)
        except OSError as error:
            ctypes.windll.user32.MessageBoxW(None, str(error), f"{label} · 无法打开", 0x10)

    def open_in_herdr(self, provider: str) -> None:
        label = provider_registry.get(provider).label
        try:
            name = herdr_source.start_agent(provider)
            log.info("opened %s in herdr as %s", provider, name)
        except herdr_source.AgentSlow as slow:
            log.info("opened %s in herdr: %s", provider, slow)
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("could not open %s in herdr: %s", provider, error)
            ctypes.windll.user32.MessageBoxW(None, str(error), f"{label} · 无法在 herdr 中打开", 0x10)
        finally:
            self.__dict__.setdefault("herdr_opening", set()).discard(provider)

    def mimo_login(self, _icon=None, _item=None) -> None:
        if getattr(self, "mimo_login_running", False):
            return
        self.mimo_login_running, self.mimo_login_result = True, "已打开登录窗口，请在浏览器中登录小米账号…"

        def run():
            try:
                plan = mimo_usage.login(interactive=True)
                self.mimo_login_result = "登录成功，已读取套餐"
                if "mimo" in PROVIDERS:
                    tracker = mimo_tracker()
                    tracker.refresh(lambda: plan)
                    with self.ui_lock:
                        self.snapshots["mimo"] = mimo_snapshot(tracker.view())
                    self.wake.set()
                else:
                    self.mimo_login_result = "登录成功；在“额度来源”中开启小米 MiMo 并保存后显示 M 图标"
            except Exception as error:
                self.mimo_login_result = f"登录未完成：{error}"
                ctypes.windll.user32.MessageBoxW(None, str(error), "Xiaomi MiMo · 登录未完成", 0x30)
            finally:
                self.mimo_login_running = False
        threading.Thread(target=run, name="mimo-login", daemon=True).start()

    def mimo_logout(self, _icon=None, _item=None) -> None:
        mimo_usage.logout()
        self.scheduler.request(["mimo"], time.time(), "退出登录")
        self.wake.set()

    # ----- community forecast of global Codex resets ---------------------------

    def announce_resets(self, provider: str, old: QuotaSnapshot | None, new: QuotaSnapshot | None, now: float) -> None:
        if provider not in RESET_NOTIFY:
            return
        lines = reset_changes(old, new, now)
        if not lines:
            return
        outlook = getattr(self, "reset_outlook", None)
        if provider == "codex" and outlook and outlook.last_reset_at and outlook.last_reset_at > now - 6 * 3600:
            lines.append(f"社区已确认全局重置（{outlook.source}）")
        label = provider_registry.get(provider).label.split("（")[0]
        log.info("quota reset noticed for %s: %s", provider, "; ".join(lines))
        self.notify(provider, f"{label} 额度已重置", "\n".join(lines))

    def reset_level(self, now: float | None = None) -> str:
        outlook = getattr(self, "reset_outlook", None)
        return outlook.level(now) if outlook and reset_watch.ENABLED else "unknown"

    def menu_reset_outlook(self) -> str:
        outlook = getattr(self, "reset_outlook", None)
        if outlook is None or outlook.level() == "unknown":
            return "全局重置预测：暂无（点击打开网站）"
        mark = "可能即将重置 · " if outlook.level() == "high" else ""
        return f"全局重置预测：{mark}24h {outlook.prob_24}% · 48h {outlook.prob_48}%（{outlook.source}）"

    # ----- model IQ (AI Stupid Level) -------------------------------------------------
    def panel_intel(self, now: float, running: list[str] | None = None) -> tuple[tuple, str]:
        """(models on the banner's right, note when there are none): coding
        scores, the source's running model first, then the highest scores."""
        state, config = getattr(self, "iq_state", None), getattr(self, "iq_config", None) or {}
        if not model_iq.effective_key(config):
            return (), "未设置"
        models = model_iq.hover_models(state, config.get("shown"), running)
        if not models:                 # nothing ever read: no quota or error wording on the panel
            error = state.error if state else ""
            return (), ("Key 无效" if "Key" in error else "读取中")
        # Always the last result as it is, however old or whatever the last call returned.
        return tuple((m.short, model_iq.score_text(m), "ok", live) for m, live in models), ""

    def refresh_grok_iq(self, now: float | None = None) -> str:
        """Read LiveBench for the Grok column now (the IQ loop, or the settings button)."""
        now = now or time.time()
        with self.iq_lock:
            grok_iq.read(self.grok_iq, now)
            grok_iq.save_state(GROK_IQ_PATH, self.grok_iq)
        log.info("grok iq: %s release %s%s", grok_iq.score_text(self.grok_iq.score),
                 self.grok_iq.release or "-", f" ({self.grok_iq.error})" if self.grok_iq.error else "")
        self.update_ui()
        return self.grok_iq.error or "已读取"

    def model_iq_status(self) -> dict:
        now = time.time()
        state, config = self.iq_state, self.iq_config
        return {"key": config.get("key", ""), "env_key": bool(os.environ.get(model_iq.ENV_KEY)),
                "shown": config.get("shown"), "models": state.scores(), "fetched": model_iq.age_text(state, now),
                "fetched_at": state.fetched_at, "remaining": model_iq.remaining_today(state, now),
                "calls": model_iq.calls_today(state, now), "error": state.error,
                "default": model_iq.default_selection(state.scores())}

    def save_model_iq(self, key: str, shown: list[str] | None) -> None:
        model_iq.save_config(IQ_CONFIG_PATH, key, shown)
        changed = (key or "").strip() != self.iq_config.get("key")
        self.iq_config = model_iq.load_config(IQ_CONFIG_PATH)
        if changed:
            self.iq_state.attempted_at = 0.0  # try the new key on the next pass (still within the daily budget)
            self.iq_wake.set()             # a new key: read now if a read is due (it is, before the first one)
        self.update_ui()

    def refresh_model_iq(self) -> str:
        """Manual read from the settings page; spends one of the day's calls."""
        key = model_iq.effective_key(self.iq_config)
        if not key:
            return "请先填写 API Key"
        now = time.time()
        with self.iq_lock:
            if model_iq.remaining_today(self.iq_state, now) <= 0:
                return "今日 10 次已用完（00:00 UTC 重置）"
            model_iq.read(self.iq_state, key, now)
            model_iq.save_state(IQ_STATE_PATH, self.iq_state)
        self.update_ui()
        return self.iq_state.error or f"✓ 已读取 {len(self.iq_state.models)} 个模型"

    def iq_loop(self) -> None:
        """Automatic reads: every 3 h, at most 7 a day; the count is kept on disk
        so restarts spend no extra calls."""
        while not self.stop_event.is_set():
            key = model_iq.effective_key(self.iq_config)
            now = time.time()
            if key:
                with self.iq_lock:
                    if model_iq.due(self.iq_state, now):
                        model_iq.read(self.iq_state, key, now)
                        model_iq.save_state(IQ_STATE_PATH, self.iq_state)
                        log.info("model iq read: %d models%s", len(self.iq_state.models),
                                 f" ({self.iq_state.error})" if self.iq_state.error else "")
                        self.update_ui()
            if grok_iq.due(getattr(self, "grok_iq", None), now):
                self.refresh_grok_iq(now)
            self.iq_wake.clear()
            self.iq_wake.wait(600)
            if self.stop_event.is_set():
                break

    def open_iq_page(self, _icon=None, _item=None) -> None:
        os.startfile(model_iq.SITE)  # type: ignore[attr-defined]

    def open_reset_page(self, _icon=None, _item=None) -> None:
        outlook = getattr(self, "reset_outlook", None)
        os.startfile(outlook.page if outlook else "https://willreset.com/")  # type: ignore[attr-defined]

    def notify(self, provider: str, title: str, message: str) -> None:
        try:
            self.icons[provider].notify(message[:250], title[:60])
        except Exception:
            log.exception("notification failed")

    def apply_reset_outlook(self, outlook: "reset_watch.ResetOutlook", now: float) -> None:
        previous = getattr(self, "reset_outlook", None)
        self.reset_outlook = outlook
        reset_watch.save(RESET_PATH, outlook)
        level = outlook.level(now)
        log.info("codex reset outlook %s: 24h %s%% 48h %s%% level %s last reset %s", outlook.source, outlook.prob_24,
                 outlook.prob_48, level, outlook.last_reset_at)
        remaining = self.snapshots["codex"].display_remaining if "codex" in self.snapshots else None
        if previous and previous.last_reset_at and outlook.last_reset_at and \
                outlook.last_reset_at > previous.last_reset_at + 600 and outlook.last_reset_at > now - 6 * 3600:
            # A new global reset was confirmed: re-read our own figures now;
            # the read itself announces the reset when the numbers came back.
            log.info("community confirmed a global Codex reset; refreshing")
            if self.scheduler is not None:
                self.scheduler.request(["codex"], now, "全局重置")
                self.wake.set()
        elif level == "high" and getattr(self, "reset_alerted", "unknown") != "high":
            message = f"社区预测 24 小时内全局重置概率 {outlook.prob_24}%（{outlook.source}）。"
            if remaining is not None and remaining < 30:
                message += f"当前仅剩 {remaining:.0f}%，可考虑等重置后再大量使用。"
            self.notify("codex", "Codex 可能即将全局重置", message + "社区估计，非官方时间表。")
        self.reset_alerted = level
        self.update_ui()

    def reset_loop(self) -> None:
        while not self.stop_event.is_set():
            now = time.time()
            outlook, errors = reset_watch.fetch_outlook()
            if errors:
                log.info("codex reset outlook source errors: %s", " | ".join(errors)[:600])
            if outlook is not None:
                try:
                    self.apply_reset_outlook(outlook, now)
                except Exception:
                    log.exception("codex reset outlook update failed")
            level = outlook.level(now) if outlook else "unknown"
            self.stop_event.wait(reset_watch.POLL_SECONDS_HIGH if level in ("high", "watch") or outlook is None else reset_watch.POLL_SECONDS)

    def update_claude_locally(self, now: float) -> None:
        import claude_status
        record = claude_status.latest(now)
        old = self.snapshots.get("claude")
        seen = record.get("observed_at", record["updated_at"]) if record else 0
        if record and old is not None and seen > max(old.updated_at or 0, getattr(self, "claude_rejected_seen", 0)):
            # The status line only, never /usage: this runs on the 1 s TPS loop.
            # A reading behind what is shown (another session's older figure,
            # or less than the last /usage check) is not taken.
            native = quota_sources.native_claude_quota(record)
            if not old.stale and quota_sources.claude_newer(native, old) is old:
                return
            fresh = quota_sources.keep_last_good(native, old)
            if fresh.error:
                self.claude_rejected_seen = seen
                self.snapshots["claude"] = fresh
                log.info("claude local quota read held: %s", fresh.error)
                return
            self.announce_resets("claude", old, fresh, now)
            self.snapshots["claude"] = fresh
            if self.history is not None and self.history.record(fresh):
                self.history.save()
            log.info("quota claude from native status: left=%s second=%s reset=%s/%s", fresh.remaining_percent,
                     fresh.secondary_remaining_percent, fresh.resets_at, fresh.secondary_resets_at)

    def update_codex_locally(self, now: float) -> None:
        """Codex writes fresh rate limits into its session record after every
        response; use them at once instead of waiting for the next App Server
        read.  A recorded "usage limit" error triggers an immediate read."""
        monitor = self.tps_monitor
        record = getattr(monitor, "codex_rate_limits", None)
        current = self.snapshots.get("codex")
        if record and current is not None and record[0] > (current.updated_at or 0) and record[0] > now - 6 * 3600:
            fresh = parse_codex_session_limits(record[1], record[0])
            if fresh is not None:
                # Session records carry no free-reset count (and older ones no balance).
                if not current.stale:
                    fresh.reset_credits = current.reset_credits
                    if fresh.credits_balance is None:
                        fresh.credits_balance = current.credits_balance
                self.announce_resets("codex", current, fresh, now)
                self.snapshots["codex"] = fresh
                if self.history is not None and self.history.record(fresh):
                    self.history.save()
                log.info("codex quota from session record: %.0f%% left, reset %s", fresh.remaining_percent, fresh.resets_at)
                self.update_ui()
        hit = getattr(monitor, "codex_limit_hit_at", 0.0)
        if hit and hit > getattr(self, "codex_limit_seen", 0.0):
            self.codex_limit_seen = hit
            if self.scheduler is not None:
                self.scheduler.request(["codex"], now + 3, "额度用尽")
                self.wake.set()
            log.info("codex usage-limit error recorded at %s; refreshing", hit)

    def update_mimo_locally(self, now: float) -> None:
        """Fold new local MiMo token records into the Credits view (no network)."""
        tracker = mimo_tracker()
        # Only the observations added since the last second; re-reading the
        # whole day of events every second was a full pass for nothing.
        events = self.tps_monitor.events
        total = getattr(self.tps_monitor, "added_total", None)
        if total is None:
            fresh = events
        else:
            count = min(len(events), max(0, total - self.mimo_ingested))
            fresh = list(itertools.islice(reversed(events), count))[::-1] if count else []
            self.mimo_ingested = total
        added = tracker.ledger.ingest(fresh) if fresh else 0
        tracker.ledger.save()
        if added or now - self.mimo_view_at >= 60:
            self.mimo_view_at = now
            fresh = mimo_snapshot(tracker.view(now))
            if fresh.remaining_percent is not None or self.snapshots["mimo"].remaining_percent is None:
                self.snapshots["mimo"] = fresh

    def diagnostic_text(self) -> str:
        sections = [dump_snapshot(self.snapshots), "", "数据源边界："]
        sections += [f"- {provider_registry.get(p).label}：{provider_registry.get(p).source_note}" for p in PROVIDERS]
        for item in self.snapshots.values():
            if item.error:
                sections.append(f"- {item.label}：{item.error}")
        if self.scheduler:
            now = time.time()
            sections += ["", "刷新计划："] + [f"- {p}：{self.scheduler.describe(p, now)}" for p in PROVIDERS]
        sections += ["", "额度预测："] + [f"- {p}：{self.forecast_line(p) or '暂无'}" for p in PROVIDERS]
        return "\n".join(sections)

    def open_diagnostics(self, _icon=None, _item=None) -> None:
        path = DATA_DIR / "diagnostics.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.diagnostic_text(), encoding="utf-8")
        os.startfile(path)  # type: ignore[attr-defined]

    # Start with Windows through HKCU\\...\\Run: no console flash, and from
    # source it uses pythonw -X utf8 instead of the .py file association.
    @staticmethod
    def startup_path() -> Path:
        """Legacy Startup-folder script written by earlier versions."""
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.cmd"

    @staticmethod
    def _run_value() -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, APP_NAME)[0]
        except OSError:
            return None

    @staticmethod
    def _set_run_value(command: str | None) -> None:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if command is None:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)

    def startup_enabled(self) -> bool:
        return self._run_value() is not None or self.startup_path().is_file()

    def migrate_startup(self) -> None:
        try:
            legacy = self.startup_path()
            if legacy.is_file():
                self._set_run_value(startup_command())
                legacy.unlink()
        except (OSError, KeyError, ImportError):
            log.warning("startup migration skipped", exc_info=True)

    def toggle_startup(self, _icon=None, _item=None) -> None:
        enabled = self.startup_enabled()
        self._set_run_value(None if enabled else startup_command())
        self.startup_path().unlink(missing_ok=True)

    def toggle_claude(self, _icon=None, _item=None) -> None:
        enabled = "claude" not in PROVIDERS
        provider_registry.set_claude_enabled(enabled)
        log.info("Claude quota polling manually %s", "enabled" if enabled else "disabled")
        self.restart_requested = True
        self.exit_app()

    def set_providers(self, changes: dict) -> None:
        """Settings-page source switches; the tray restarts to show the new set."""
        changes = {key: value for key, value in changes.items() if (key in PROVIDERS) != bool(value)}
        if not changes:
            return
        provider_registry.set_provider_choices(changes)
        log.info("source switches changed: %s", changes)
        self.restart_requested = True
        self.exit_app()

    def mimo_status(self) -> dict:
        """What the MiMo settings page shows: login, manual plan, latest error."""
        view = mimo_tracker().view() if "mimo" in PROVIDERS else None
        manual = mimo_usage.manual_plan()
        return {
            "enabled": "mimo" in PROVIDERS,
            "logged_in": mimo_usage.COOKIE_PATH.exists(),
            "browser": mimo_usage.edge_path(),
            "manual": manual,
            "plan": getattr(view, "plan_name", "") if view else "",
            "source": getattr(view, "source", "") if view else "",
            "total": getattr(view, "total", None) if view else None,
            "used": getattr(view, "used", None) if view else None,
            "error": getattr(view, "error", "") if view else "",
            "logging_in": bool(getattr(self, "mimo_login_running", False)),
            "login_result": getattr(self, "mimo_login_result", ""),
        }

    def save_mimo_manual(self, total: str, period_end: str, used: str = "", name: str = "") -> None:
        mimo_usage.save_manual_plan(total, period_end, used, name)
        if "mimo" in PROVIDERS:
            if not mimo_usage.COOKIE_PATH.exists():      # the console, when logged in, is read by the scheduler
                tracker = mimo_tracker()
                tracker.refresh(mimo_usage.manual_plan)
                with self.ui_lock:
                    self.snapshots["mimo"] = mimo_snapshot(tracker.view())
            self.scheduler.request(["mimo"], time.time(), "手动配置")
            self.wake.set()

    def open_settings(self, _icon=None, _item=None) -> None:
        self.open_settings_page(None)

    def open_settings_page(self, page: str | None) -> None:
        """Settings window, optionally on one page (the panel banner opens "iq")."""
        from settings_window import SettingsController
        self.settings_requested.set()
        for icon in self.icons.values():
            icon.hover_open = False
            icon.suppress_hover = True
        with self.ui_lock:
            if self.settings_ui is None:
                self.settings_ui = SettingsController(self)
            self.settings_ui.show(page)
        log.info("settings window requested%s", f" ({page})" if page else "")

    def apply_preferences(self, prefs: dict) -> None:
        """Settings-page choices; an explicitly set environment variable still wins."""
        if not os.environ.get("CODEX_QUOTA_HERDR") and "herdr" in prefs:
            herdr = self.tps_monitor.herdr
            herdr.enabled = bool(prefs["herdr"])
            herdr.status = herdr.status if herdr.enabled else "未启用"
            if not herdr.enabled:
                herdr.rows = []

    def save_preferences(self, prefs: dict) -> None:
        merged = {**load_preferences(), **prefs}
        merged.pop("tray_order", None)     # retired: tray positions are fixed
        PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = PREFERENCES_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, PREFERENCES_PATH)
        self.apply_preferences(merged)

    def apply_summary_settings(self, config: dict, enabled: bool) -> None:
        from settings_window import save_summary
        save_summary(config, enabled)
        self.summarizer = agent_tasks.Summarizer(agent_tasks.summary_config())
        self.hint_advisor = agent_hints.HintAdvisor(agent_tasks.summary_config())
        log.info("agent summary settings applied: enabled=%s", self.summarizer.enabled)

    def exit_app(self, _icon=None, _item=None) -> None:
        self.stop_event.set()
        self.wake.set()
        if getattr(self, "usage", None) is not None:
            self.usage.save(force=True)
        if getattr(self, "settings_ui", None):
            self.settings_ui.close()
        for icon in self.icons.values():
            icon.stop()

    def run(self) -> None:
        if "claude" in PROVIDERS:
            import claude_status
            if claude_status.refresh_receiver():
                log.info("Claude status-line receiver updated")
        threading.Thread(target=self.refresh_loop, name="quota-refresh", daemon=True).start()
        threading.Thread(target=self.tps_loop, name="tps-reader", daemon=True).start()
        if reset_watch.ENABLED and "codex" in PROVIDERS:
            threading.Thread(target=self.reset_loop, name="codex-reset-watch", daemon=True).start()
        threading.Thread(target=self.iq_loop, name="model-iq", daemon=True).start()
        hover = HoverDetails(self.icons, self.hover_text, self.stop_event, agents=self)
        threading.Thread(target=hover.run, name="hover-details", daemon=True).start()
        ready = [threading.Event() for _ in self.slots]

        def show(icon, done):
            icon.visible = True
            done.set()
        threads = [
            threading.Thread(target=icon.run, kwargs={"setup": lambda icon, done=done: show(icon, done)},
                             name=f"tray-{index}", daemon=True)
            for index, (icon, done) in enumerate(zip(self.slots, ready))
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 15
        for event in ready:
            event.wait(max(0.0, deadline - time.monotonic()))
        time.sleep(1.0)   # let Explorer lay the icons out before reading positions
        self.maybe_reorder(time.time(), force=True)
        for thread in threads:
            thread.join()


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}-36d9a73f")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


def setup_logging() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(DATA_DIR / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    except OSError:
        pass


def check_mimo(destination: Path, raw: bool = False) -> int:
    """Read the MiMo console once. `--raw` also saves both responses with
    identities redacted, to check the parser against the real format."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    responses: dict = {}
    result: dict = {}
    try:
        plan = mimo_usage.fetch_with_renewal(raw=responses)
        result = {"via": responses.get("via"), "total": plan.total, "used": plan.used, "plan": plan.plan_name, "period_start": plan.period_start,
                  "period_end": plan.period_end, "settlement": plan.settlement, "expired": plan.expired, "items": plan.items}
        code = 0
    except Exception as error:
        result, code = {"error": f"{type(error).__name__}: {error}"}, 1
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if raw:
        destination.with_suffix(".raw.json").write_text(
            json.dumps(mimo_usage.redact(responses), ensure_ascii=False, indent=2), encoding="utf-8")
    return code


def check_claude(destination: Path, raw: bool = False) -> int:
    """Read Claude Code /usage once; `--raw` also saves the cleaned screen
    (emails redacted) so the parser can be checked against a new CLI version."""
    import re
    from cli_usage import CLAUDE_ARGS, CLAUDE_PROBE, run_temporary_usage
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot = get_claude_quota()
    destination.write_text(dump_snapshot({"claude": snapshot}), encoding="utf-8")
    if raw:
        import claude_status
        report = claude_status.latest()
        if report or claude_status.bridge_installed():
            destination.with_suffix(".raw.json").write_text(
                json.dumps(report or {"status": "waiting_for_native_data"}, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1 if snapshot.error else 0
        executable = provider_registry.find_claude()
        try:
            text = run_temporary_usage(str(executable), CLAUDE_ARGS, timeout=30.0, probe=CLAUDE_PROBE) if executable else "未找到 Claude Code"
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
        destination.with_suffix(".raw.txt").write_text(text, encoding="utf-8")
    return 1 if snapshot.error else 0


def check_agents(destination: Path) -> int:
    """What the bubble panel would show now: JSON report plus a PNG preview."""
    tray = QuotaTray.__new__(QuotaTray)
    cached = load_cached_snapshots()
    tray.snapshots = {provider: cached.get(provider) or placeholder(provider) for provider in PROVIDERS}
    tray.history = QuotaHistory()
    monitor = TpsMonitor()
    monitor.poll()
    time.sleep(1)
    tray.performance = monitor.poll()
    tray.tps_monitor = monitor
    monitor.last_slow_discovery = 0
    monitor.discover(time.time())          # schedules are read with the slow discovery
    tray.agents = monitor.agents(time.time())
    tray.agent_groups = agent_tasks.AgentGroups()
    tray.summarizer = agent_tasks.Summarizer(agent_tasks.summary_config())
    tray.clusters = {}
    clusters = tray.agent_groups.compose([agent for agent in tray.agents if not agent.get("schedule")])
    for cluster in clusters:
        tray.summarizer.lookup(cluster)
    deadline = time.time() + 15
    while tray.summarizer.pending and time.time() < deadline:
        time.sleep(0.2)
    report = {"summarizer": bool(tray.summarizer.enabled), "schedules": monitor.schedules,
              "schedule_chips": agent_tasks.schedule_chips(monitor.schedules, tray.agents, time.time()), "agents": [
        {key: value for key, value in agent.items() if key != "request"} for agent in tray.agents], "bubbles": []}
    for cluster in clusters:
        title, doing, detail = agent_tasks.describe(cluster, tray.summarizer.lookup(cluster)[0])
        report["bubbles"].append({"key": cluster.key, "title": title, "doing": doing, "detail": detail,
                                  "members": [m["key"] for m in cluster.members], "manual": cluster.manual})
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    provider = next((m["provider"] for m in tray.agents if m["live"] and m["provider"] in PROVIDERS), next(iter(PROVIDERS)))
    model = tray.panel(provider)
    scene = agent_panel.Scene()
    scene.set_model(model, 0.0)
    for index in range(120):
        scene.step(index / 60, None, False, False)
    scene.render(1.5).save(destination.with_suffix(".png"))
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--check-runtime":
        time.sleep(3)  # let the previous onefile parent exit and clean its own files
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        report = {"frozen": bool(getattr(sys, "frozen", False)), "runtime_dir": str(root),
                  "base_library_exists": (root / "base_library.zip").is_file(),
                  "utf8_ok": "Claude 额度".encode("utf-8").decode("utf-8") == "Claude 额度"}
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report["utf8_ok"] and (report["base_library_exists"] or not report["frozen"]) else 1
    if len(sys.argv) == 3 and sys.argv[1] == "--check-restart":
        command = ([sys.executable] if getattr(sys, "frozen", False) else
                   [sys.executable, "-X", "utf8", str(Path(__file__).resolve())])
        subprocess.Popen([*command, "--check-runtime", str(Path(sys.argv[2]).resolve())],
                         env=restart_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--install-claude-statusline":
        import claude_status
        claude_status.install()
        return 0
    if len(sys.argv) == 4 and sys.argv[1] in ("--claude-quota-worker", "--grok-quota-worker"):
        import cli_quota
        provider = "grok" if sys.argv[1] == "--grok-quota-worker" else "claude"
        return cli_quota.worker(sys.argv[2], sys.argv[3], provider)
    if len(sys.argv) == 3 and sys.argv[1] == "--check-hover":
        from hover_check import check_hover_runtime
        tray = QuotaTray.__new__(QuotaTray)
        cached = load_cached_snapshots()
        tray.snapshots = {provider: cached.get(provider) or placeholder(provider) for provider in PROVIDERS}
        tray.performance = json.loads(PERFORMANCE_PATH.read_text(encoding="utf-8"))
        tray.history = QuotaHistory()
        tray.agents, tray.agent_groups, tray.clusters = [], agent_tasks.AgentGroups(), {}
        tray.summarizer = agent_tasks.Summarizer(None)
        tray.tps_monitor = SimpleNamespace(schedules=[])
        return check_hover_runtime(tray.panel(next(iter(PROVIDERS))), Path(sys.argv[2]).resolve())
    if len(sys.argv) == 3 and sys.argv[1] == "--check-agents":
        return check_agents(Path(sys.argv[2]).resolve())
    if len(sys.argv) == 3 and sys.argv[1] == "--check-tps":
        monitor = TpsMonitor()
        snapshot = monitor.poll()
        time.sleep(1)
        snapshot = monitor.poll()
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--check-reset":
        outlook, errors = reset_watch.fetch_outlook()
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = {"outlook": None if outlook is None else {**outlook.__dict__, "level": outlook.level()},
                  "alert_percent": reset_watch.ALERT_PERCENT, "errors": errors}
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if outlook else 1
    if len(sys.argv) == 3 and sys.argv[1] == "--check-gemini-tps":
        from tps_sources import antigravity_diagnostics
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(antigravity_diagnostics(), ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--check-grok":
        raw: dict = {}
        snapshot = get_grok_quota(raw)
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(dump_snapshot({"grok": snapshot}), encoding="utf-8")
        if len(sys.argv) == 4 and sys.argv[3] == "--raw":
            destination.with_suffix(".raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1 if snapshot.error else 0
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--check-codex":
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw: dict = {}
        snapshot = get_codex_quota(raw)
        destination.write_text(dump_snapshot({"codex": snapshot}), encoding="utf-8")
        if len(sys.argv) == 4 and sys.argv[3] == "--raw":
            monitor = TpsMonitor()
            monitor.poll()
            record = monitor.codex_rate_limits
            raw["session_record"] = {"at": record[0], "rate_limits": record[1]} if record else None
            raw["limit_error_at"] = monitor.codex_limit_hit_at or None
            destination.with_suffix(".raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1 if snapshot.error else 0
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--check-mimo":
        return check_mimo(Path(sys.argv[2]).resolve(), raw=len(sys.argv) == 4 and sys.argv[3] == "--raw")
    if len(sys.argv) == 2 and sys.argv[1] == "--mimo-login":
        # The EXE has no console: report with a message box as well.
        try:
            plan = mimo_usage.login(interactive=True)
            text, code, flags = (f"MiMo 登录成功：{plan.plan_name or '套餐'}，剩余 "
                                 f"{mimo_usage.credits_text((plan.total or 0) - (plan.used or 0))}"), 0, 0x40
        except Exception as error:
            text, code, flags = f"MiMo 登录失败：{error}", 1, 0x30
        print(text)
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, text, "Xiaomi MiMo", flags)
        return code
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--check-claude":
        return check_claude(Path(sys.argv[2]).resolve(), raw=len(sys.argv) == 4 and sys.argv[3] == "--raw")
    handle = acquire_single_instance()
    if handle is None:
        return 0
    setup_logging()
    tray = QuotaTray()
    tray.run()
    if getattr(tray, "restart_requested", False):
        ctypes.windll.kernel32.CloseHandle(handle)
        command = ([sys.executable] if getattr(sys, "frozen", False)
                   else [sys.executable, "-X", "utf8", str(Path(__file__).resolve())])
        subprocess.Popen(command, env=restart_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
