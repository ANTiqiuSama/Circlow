"""The hover panel's quota curves and the window logic behind them (a
QuotaTray mixin): which windows a source has, which are in use, and the
burn-down chart of their history and forecast."""
from __future__ import annotations

import dataclasses
import time

import agent_panel
import forecast as forecast_module
import providers as provider_registry
from quota_sources import mimo_tracker
from tray_icons import quota_depleted, quota_exhausted


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


def weekly_series_name(snapshot) -> str | None:
    """The quota-history series of a source's weekly window: its only window
    (Grok) or its second one (Codex, Claude Code); None without one."""
    if snapshot is None:
        return None
    if snapshot.window_minutes == 10080:
        return snapshot.provider
    if snapshot.secondary_window_minutes == 10080:
        return f"{snapshot.provider}:secondary"
    return None


class ChartData:
    """Needs from the tray: snapshots, performance, agents, tps_monitor,
    history."""

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
        """Low-quota warnings versus genuinely depleted subscription windows."""
        snapshot = self.snapshots.get(provider)
        if snapshot is None or snapshot.stale or snapshot.error or provider == "mimo" or (snapshot.pools and len(snapshot.pools) > 1):
            return None, ()
        values = (snapshot.remaining_percent, snapshot.secondary_remaining_percent)
        empty = tuple(slot for slot, value in enumerate(values) if quota_depleted(value))
        if not empty:
            near = tuple(slot for slot, value in enumerate(values) if quota_exhausted(value, provider))
            return (("near_working" if self.source_working(provider) else "near"), near) if near else (None, ())
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
        snapshot = self.snapshots.get("codex")
        if snapshot is None or snapshot.stale or snapshot.error or not self.source_working("codex"):
            return False
        windows = self.quota_windows("codex")
        five = next((w for w in windows if w[3] == 300 and w[1] is not None), None)
        week = next((w for w in windows if w[3] == 10080 and w[1] is not None), None)
        # A countdown icon is only an early warning. Keep the real five-hour
        # curve until that window reaches zero and work continues.
        return bool(five and quota_depleted(five[1]) and week)

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
                outlook = forecast_module.forecast(samples, now, remaining, resets_at, minutes)   # the same pace as the headline
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

    def weekly_windows(self) -> dict:
        """provider -> (quota readings, remaining %, resets_at) of its weekly
        window: Grok's only window, Codex's and Claude Code's second one."""
        found = {}
        for provider, snapshot in self.snapshots.items():
            name = weekly_series_name(snapshot)
            if name is None:
                continue
            if snapshot.window_minutes == 10080:
                remaining, resets_at = snapshot.remaining_percent, snapshot.resets_at
            else:
                remaining, resets_at = snapshot.secondary_remaining_percent, snapshot.secondary_resets_at
            readings = self.history.samples(name) if self.history is not None else []
            found[provider] = (readings, None if snapshot.stale else remaining, resets_at)
        return found
