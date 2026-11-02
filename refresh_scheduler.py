"""Event-driven quota refresh instead of a fixed 5-minute poll.

Triggers, per provider:
* startup / manual refresh / wake from sleep  -> now
* the pointer comes to its icon or panel        -> now, every time (a read
                                                  still running is followed by
                                                  one more), then every HOVER s
* a generation just finished                   -> after a short settle delay
* an agent of this source works                -> every ACTIVE seconds
* the reset time of any window passes          -> a few seconds after it
* read failed                                  -> 30 s, 1, 2, 5 min backoff
* otherwise idle                               -> IDLE seconds (long)
* quota used up (Grok)                         -> rarely, then every couple of
                                                  minutes shortly before the reset
* no such quota on this account (Claude without a subscription), or repeated
  failures                                     -> every GIVE_UP seconds; manual
                                                  refresh still reads at once
Hover < active < idle for every source. Grok's reader launches a whole CLI
session, so its intervals are the widest, and no source is read again sooner
than DURATION_GAP times its last read took.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Iterable

from providers import SPECS

# Per-provider intervals come from the registry (providers.py).
IDLE = {spec.key: spec.idle_seconds for spec in SPECS}
ACTIVE = {spec.key: spec.active_seconds for spec in SPECS}
HOVER = {spec.key: spec.hover_seconds for spec in SPECS}
SETTLE = {spec.key: spec.settle_seconds for spec in SPECS}
EXHAUSTED = {spec.key: spec.exhausted_seconds for spec in SPECS}
NEAR_RESET = {spec.key: (spec.near_reset_seconds, spec.near_reset_poll) for spec in SPECS}
GIVE_UP = {spec.key: spec.give_up_seconds for spec in SPECS}
RETRY = (30, 60, 120, 300)
GIVE_UP_AFTER = 3            # consecutive failures before GIVE_UP applies
DURATION_GAP = 2             # a read that took 20 s waits at least 40 s before the next
HOVER_REPEAT = 3             # pointer out and back within this many seconds: the same look
HELD_RECHECK = 10            # a reading set aside as doubtful is checked again this soon
# Errors meaning "this account has no such quota": retrying cannot help.
NO_PLAN_MARKERS = ("未找到订阅额度", "API Key 计费", "未找到 Claude Code", "未登录", "not logged in", "no subscription")


def no_plan(error: str | None) -> bool:
    text = (error or "").lower()
    return any(marker.lower() in text for marker in NO_PLAN_MARKERS)


def exhausted_reset(snapshot: Any) -> tuple[bool, float | None]:
    """(used up?, when the used-up window(s) reset).  Every exhausted window
    must reset before the quota is usable again, hence the latest reset."""
    windows = ((getattr(snapshot, "remaining_percent", None), getattr(snapshot, "resets_at", None)),
               (getattr(snapshot, "secondary_remaining_percent", None), getattr(snapshot, "secondary_resets_at", None)))
    empty = [reset for remaining, reset in windows if remaining is not None and remaining <= 0.5]
    if not empty:
        return False, None
    known = [reset for reset in empty if reset]
    return True, (max(known) if len(known) == len(empty) else None)


FINAL_LEAD = 5 * 60          # last minutes before a reset: check every FINAL_POLL
FINAL_POLL = 60
CONFIRM_FAST = 10 * 60       # after the reset time, until the numbers come back
CONFIRM_SLOW = 60 * 60


def exhausted_schedule(now: float, reset: float | None, window_minutes: int | None, idle: int,
                       exhausted: int | None, lead: int, poll: int) -> tuple[float, str] | None:
    """When to read a used-up quota next.  Far from the reset: rarely
    (`exhausted`, only for providers that opt in).  Closer: every `poll`
    seconds, every minute in the final minutes, and every minute right after
    the reset time until the server shows the quota back (it may lag)."""
    if reset is None or (window_minutes and reset > now + window_minutes * 60 + 86400):
        # No usable reset time (or an implausible one): never wait long.
        return (now + idle, "额度用尽（重置时间未知）") if exhausted else None
    left = reset - now
    if left <= -CONFIRM_SLOW:
        return now + idle, "等待重置生效"
    if left <= -CONFIRM_FAST:
        return now + min(poll, idle), "等待重置生效"
    if left <= FINAL_LEAD:
        return now + min(FINAL_POLL, idle), "临近重置" if left > 0 else "等待重置生效"
    if left <= lead:
        return min(now + min(poll, idle), reset - FINAL_LEAD), "临近重置"
    if exhausted:
        return min(now + exhausted, reset - lead), "额度用尽"
    return None


RESET_DELAY = 5
MAX_SLEEP = 30.0


class RefreshScheduler:
    def __init__(self, providers: Iterable[str], now: float):
        self.lock = threading.Lock()
        self.providers = list(providers)
        self.due = {p: now for p in self.providers}
        self.reason = {p: "启动" for p in self.providers}
        self.last_done = {p: 0.0 for p in self.providers}
        self.failures = {p: 0 for p in self.providers}
        self.active = {p: False for p in self.providers}
        self.hovered = {p: False for p in self.providers}
        self.duration = {p: 0.0 for p in self.providers}     # how long the last read took
        self.dormant = {p: False for p in self.providers}     # no quota on this account
        self.running: set[str] = set()
        self.started = {p: 0.0 for p in self.providers}       # when the running read began
        self.again: set[str] = set()                           # hovered during a read: read once more

    def _gap(self, provider: str, interval: float) -> float:
        return max(interval, DURATION_GAP * self.duration[provider])

    def _tier(self, provider: str) -> tuple[float, str]:
        """(interval, reason) for the current attention: hover < active < idle."""
        if self.hovered[provider]:
            return self._gap(provider, HOVER[provider]), "悬停"
        if self.active[provider]:
            return self._gap(provider, ACTIVE[provider]), "使用中"
        return self._gap(provider, IDLE[provider]), "空闲"

    def _forward(self, provider: str, when: float, reason: str) -> bool:
        if when < self.due[provider]:
            self.due[provider] = when
            self.reason[provider] = reason
            return True
        return False

    def request(self, providers: Iterable[str] | None, now: float, reason: str = "手动刷新") -> None:
        with self.lock:
            for provider in providers or self.providers:
                self._forward(provider, now, reason)

    def clock_jump(self, now: float) -> None:
        self.request(None, now, "唤醒后刷新")

    def take_due(self, now: float) -> list[str]:
        with self.lock:
            ready = [p for p in self.providers if p not in self.running and self.due[p] <= now]
            for provider in ready:
                self.running.add(provider)
                self.started[provider] = now
                self.due[provider] = math.inf
            return ready

    def completed(self, provider: str, snapshot: Any, now: float, duration: float | None = None) -> None:
        with self.lock:
            self.running.discard(provider)
            self.last_done[provider] = now
            if duration is not None and duration >= 0:
                self.duration[provider] = duration
            error = getattr(snapshot, "error", None)
            self.dormant[provider] = False
            if getattr(snapshot, "held", False):
                # A doubtful reading set aside is not a failure: no backoff and
                # never the long "no quota" pause; check again shortly.
                interval, reason = self._tier(provider)
                self.due[provider] = now + min(interval, HELD_RECHECK)
                self.reason[provider] = "核对读数"
            elif error:
                self.failures[provider] += 1
                delay = min(RETRY[min(self.failures[provider], len(RETRY)) - 1], IDLE[provider])
                reason = "重试"
                if GIVE_UP[provider] and (no_plan(error) or self.failures[provider] > GIVE_UP_AFTER):
                    delay, reason = GIVE_UP[provider], ("无可用额度" if no_plan(error) else "多次失败")
                    self.dormant[provider] = True
                self.due[provider] = now + delay
                self.reason[provider] = reason
            else:
                self.failures[provider] = 0
                interval, reason = self._tier(provider)
                self.due[provider] = now + interval
                self.reason[provider] = reason
                used_up, reset = exhausted_reset(snapshot)
                if used_up:
                    lead, poll = NEAR_RESET[provider]
                    windows = [getattr(snapshot, "window_minutes", None), getattr(snapshot, "secondary_window_minutes", None)]
                    longest = max([w for w in windows if w] or [0]) or None
                    plan = exhausted_schedule(now, reset, longest, IDLE[provider], EXHAUSTED[provider], lead, poll)
                    if plan is not None:
                        self.due[provider], self.reason[provider] = plan
                        if self.hovered[provider]:      # someone is looking: hover cadence wins
                            self._forward(provider, now + interval, reason)
            for reset in (getattr(snapshot, "resets_at", None), getattr(snapshot, "secondary_resets_at", None)):
                if reset and reset > now:
                    self._forward(provider, reset + RESET_DELAY, "额度重置")
            if provider in self.again:
                self.again.discard(provider)
                self._forward(provider, now, "悬停")

    def activity(self, provider: str, active: bool, finished: bool, now: float) -> bool:
        """Feed live generation state; returns True when a refresh moved earlier."""
        with self.lock:
            was = self.active[provider]
            self.active[provider] = active
            moved = False
            if self.dormant[provider]:
                return False   # e.g. Claude Code used without a subscription quota
            if active and not was:
                # Not at once (the read would still show the old figure), but
                # within one active interval rather than the idle one.
                gap = self._gap(provider, ACTIVE[provider])
                moved |= self._forward(provider, max(now + gap, self.last_done[provider] + gap), "使用中")
            if finished or (was and not active):
                moved |= self._forward(provider, now + SETTLE[provider], "生成结束")
            return moved

    def hover(self, provider: str, on: bool, now: float) -> bool:
        """Pointer comes to this source's icon or panel: every hover reads now.
        A read already under way that began before the pointer came is followed
        by one more; one finished HOVER_REPEAT seconds ago counts as this look.
        Returns True when a read moved earlier."""
        with self.lock:
            was = self.hovered.get(provider, False)
            self.hovered[provider] = on
            if not on or was or self.dormant[provider]:
                return False
            if provider in self.running:
                if now - self.started[provider] >= HOVER_REPEAT:
                    self.again.add(provider)
                return False
            return self._forward(provider, max(now, self.last_done[provider] + HOVER_REPEAT), "悬停")

    def next_wake(self, now: float) -> float:
        with self.lock:
            pending = [self.due[p] for p in self.providers if p not in self.running]
        if not pending:
            return MAX_SLEEP
        return max(0.2, min(MAX_SLEEP, min(pending) - now))

    def plan(self, provider: str, now: float) -> tuple[float | None, str]:
        """(seconds until the next read, reason); (0, "正在刷新") while reading,
        (None, "") when nothing is scheduled."""
        with self.lock:
            if provider not in self.due:
                return None, ""
            if provider in self.running:
                return 0.0, "正在刷新"
            due, reason = self.due[provider], self.reason[provider]
        return (None, "") if due == math.inf else (max(0.0, due - now), reason)

    def describe(self, provider: str, now: float) -> str:
        with self.lock:
            if provider in self.running:
                return "正在刷新"
            due, reason = self.due[provider], self.reason[provider]
        if due == math.inf:
            return "等待刷新"
        left = due - now
        if left <= 1:
            return f"即将刷新（{reason}）"
        minutes = left / 60
        when = f"{int(left)} 秒" if left < 60 else (f"{minutes:.0f} 分钟" if minutes < 90 else f"{minutes / 60:.1f} 小时")
        return f"下次刷新约 {when}后（{reason}）"
