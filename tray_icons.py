"""Tray / menu-bar icon state shared by the Windows tray and the macOS menu bar:
which ring, countdown or TPS disc a source shows, and why its figures are old.
Pure functions of a QuotaSnapshot; no platform UI here."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime

from PIL import Image

from icon_art import live_icon, ring_icon, waiting_icon, window_shade
from quota_sources import QuotaSnapshot


PACED_PROVIDERS = {"mimo"}


def pace_marker(snapshot: QuotaSnapshot, now: float | None = None) -> float | None:
    """For pooled plans (MiMo): where the binding ring should end if Credits
    were spent evenly - the share of its window that is still ahead."""
    if snapshot.provider not in PACED_PROVIDERS or snapshot.display_remaining is None:
        return None
    now = now or time.time()
    use_secondary = (snapshot.secondary_remaining_percent is not None and
                     (snapshot.remaining_percent is None or snapshot.secondary_remaining_percent <= snapshot.remaining_percent))
    minutes = snapshot.secondary_window_minutes if use_secondary else snapshot.window_minutes
    ends = snapshot.secondary_resets_at if use_secondary else snapshot.resets_at
    if not minutes or not ends:
        return None
    return max(0.0, min(100.0, 100.0 * (ends - now) / (minutes * 60)))


def _rounded(value, digits=1):
    return None if value is None else round(float(value), digits)


def icon_windows(snapshot: QuotaSnapshot, letter: str) -> tuple[tuple, tuple | None]:
    """(values, colours) of the ring: left half the short window, right half
    the long one. Model pools (Gemini) show every pool in panel order, each in
    the colour of its panel column: three pools split the ring into equal
    120-degree thirds (Argon top-left, Claude/GPT bottom, Gemini top-right),
    two pools into halves."""
    if snapshot.provider == "mimo":
        return (snapshot.display_remaining,), None
    pools = snapshot.pools or []
    if len(pools) >= 3:
        shown = list(enumerate(pools[:3]))
        if all(pool.get("remaining_percent") is None for _, pool in shown):
            return (None,), None
        values = tuple(pool.get("remaining_percent") for _, pool in shown)
        return values, tuple(window_shade(letter, index, value, True) for index, value in enumerate(values))
    if len(pools) > 1:
        known = [(index, pool) for index, pool in enumerate(pools[:3]) if pool.get("remaining_percent") is not None]
        tight = sorted(sorted(known, key=lambda item: item[1]["remaining_percent"])[:2], key=lambda item: item[0])
        if not tight:
            return (None,), None
        values = tuple(pool["remaining_percent"] for _, pool in tight)
        if len(tight) == 1:
            return values, None
        return values, tuple(window_shade(letter, index, pool["remaining_percent"], True) for index, pool in tight)
    if snapshot.secondary_remaining_percent is not None:
        return (snapshot.remaining_percent, snapshot.secondary_remaining_percent), None
    return (snapshot.remaining_percent,), None


EXHAUSTED_REMAINING = 5.0  # User treats 95% used as exhausted for display.


def exhaustion_threshold(provider: str) -> float:
    return 10.0 if provider in ("codex", "grok") else EXHAUSTED_REMAINING


def quota_exhausted(value: float | None, provider: str = "") -> bool:
    return value is not None and value <= exhaustion_threshold(provider)


def countdown_value(seconds: float) -> tuple[int, str]:
    """A compact time unit, rounded up so it never suggests an early reset."""
    if seconds >= 86400:
        return math.ceil(seconds / 86400), "d"
    if seconds >= 3600:
        return math.ceil(seconds / 3600), "h"
    return max(1, math.ceil(seconds / 60)), "m"


ERROR_PREFIX_RE = re.compile(r"^(?:读取 /usage 失败：)?(?:\w+(?:Error|Exception): )*")


def stale_note(snapshot: QuotaSnapshot, now: float) -> str:
    """Why a source shows old figures (its amber corner dot), for the panel."""
    error = ERROR_PREFIX_RE.sub("", snapshot.error or "").strip()
    if not error:
        reason = "未能读取最新额度"
    elif "Timeout" in (snapshot.error or "") or "秒内未" in error:
        reason = "读取超时"
    else:
        reason = error if len(error) <= 24 else error[:23] + "…"
    if not snapshot.updated_at:
        return reason
    moment = datetime.fromtimestamp(snapshot.updated_at)
    when = moment.strftime("%H:%M" if moment.date() == datetime.fromtimestamp(now).date() else "%m-%d %H:%M")
    return f"显示 {when} 的读数；{reason}"


def usable_again_at(snapshot: QuotaSnapshot, now: float) -> float | None:
    """When a used-up source can run again: the last reset among its empty
    windows (all must recover), or the first pool to come back for model
    pools. None while it still has quota or the time is unknown."""
    if snapshot.pools and len(snapshot.pools) > 1:
        known = [(p.get("remaining_percent"), p.get("resets_at")) for p in snapshot.pools if p.get("remaining_percent") is not None]
        if not known or len(known) != len(snapshot.pools) or any(not quota_exhausted(value, snapshot.provider) for value, _ in known):
            return None
        stamps = [stamp for _, stamp in known if stamp and stamp > now]
        return float(min(stamps)) if stamps else None
    empty = [(value, stamp) for value, stamp in ((snapshot.remaining_percent, snapshot.resets_at),
                                                  (snapshot.secondary_remaining_percent, snapshot.secondary_resets_at))
             if quota_exhausted(value, snapshot.provider)]
    if not empty or any(not stamp or stamp <= now for _, stamp in empty):
        return None
    return float(max(stamp for _, stamp in empty))


def make_provider_icon(snapshot: QuotaSnapshot, letter: str, reset_hint: bool = False, now: float | None = None) -> Image.Image:
    # Icons are cached in icon_art and shared: never draw on them afterwards.
    now = now or time.time()
    until = usable_again_at(snapshot, now)
    if until is not None:
        amount, unit = countdown_value(until - now)
        return waiting_icon(amount, letter, bool(reset_hint), bool(snapshot.stale), unit)
    values, colors = icon_windows(snapshot, letter)
    return ring_icon(tuple(_rounded(value) for value in values), letter, bool(snapshot.stale),
                     _rounded(pace_marker(snapshot)), bool(reset_hint), colors)


def make_tps_icon(performance: dict, letter: str, reset_hint: bool = False) -> Image.Image:
    return live_icon(_rounded(performance.get("display_tps", 0) or 0), letter, bool(reset_hint))


def make_display_icon(snapshot: QuotaSnapshot, letter: str, performance: dict, reset_hint: bool = False) -> Image.Image:
    provider = getattr(snapshot, "provider", None)
    if provider in ("codex", "grok") and usable_again_at(snapshot, time.time()) is not None:
        return make_provider_icon(snapshot, letter, reset_hint)
    if provider == "gemini":
        return make_provider_icon(snapshot, letter, reset_hint)
    if performance.get("active_sessions", 0) and performance.get("display_tps") is not None:
        return make_tps_icon(performance, letter, reset_hint)
    return make_provider_icon(snapshot, letter, reset_hint)
