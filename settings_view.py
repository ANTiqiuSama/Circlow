"""Pictures for the settings window, drawn with Pillow in the panel's style.

Tk only lays these out; everything visual lives here so it renders the same on
every Windows build (and can be previewed without a display)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image, ImageDraw

from detail_card import MUTED_TEXT, SECONDARY_TEXT, TEXT, ui_font, figure_font
from forecast import reset_distance
from icon_art import AMBER, CLAY, PAID, UNIFORM_LETTERS, accent, ring_color, window_shade

# Window palette (Tk wants hex).
# Three depths, each a step lighter: the window (BG), the sidebar and footer
# bar (SIDEBAR), and the cards on them (SURFACE); fields sink below their card.
BG = "#161513"
SIDEBAR = "#1c1b19"
SURFACE = "#2a2925"
SURFACE_HI = "#34332e"
BORDER = "#3b3934"
INK = "#f0eee6"
INK_2 = "#c4bfb4"
INK_3 = "#8e897f"
FIELD = "#1d1c1a"
GREEN = "#7ec9a6"               # primary actions and the selected page
WARN = "#e5a84b"

CARD_W, CARD_H = 240, 200      # three to a row; every card keeps the three-row height for an even grid
CARD_RGB = (42, 41, 37, 255)
CARD_EDGE = (58, 56, 51, 255)


def mix(top, amount, base=(42, 41, 37)):
    """Opaque blend onto the card: draw calls replace pixels, they do not composite."""
    return tuple(round(b + (t - b) * amount) for b, t in zip(base[:3], top[:3])) + (255,)


IVORY_RGB = (240, 238, 230)
TRACK_RGB = mix(IVORY_RGB, .11)
RULE = mix(IVORY_RGB, .07)
TONE_COLORS = {"danger": CLAY, "warn": AMBER, "ok": SECONDARY_TEXT, "idle": MUTED_TEXT, "unknown": MUTED_TEXT}
PILL = {"live": None, "idle": (mix(IVORY_RGB, .09), SECONDARY_TEXT), "stale": (mix(AMBER, .2), AMBER),
        "error": (mix(CLAY, .2), CLAY), "paid": (mix(PAID, .2), PAID), "blocked": (mix(CLAY, .2), CLAY)}
ROW_INK = {"paid": PAID, "blocked": CLAY}


def hex_color(rgba) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgba[:3])


def countdown(timestamp: float | None, now: float | None = None) -> str:
    if not timestamp:
        return ""
    delta = int(timestamp - (now or time.time()))
    if delta <= 0:
        return "已到重置时间"
    days, rest = divmod(delta, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} 天 {hours} 小时后重置"
    if hours:
        return f"{hours} 小时 {rest // 60} 分后重置"
    return f"{max(1, rest // 60)} 分钟后重置"


def window_name(minutes: int | None) -> str:
    if not minutes:
        return "额度"
    if minutes == 10080:
        return "每周"
    if 524_000 <= minutes <= 528_000:
        return "年度"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


@dataclass(frozen=True)
class SourceCard:
    provider: str
    letter: str
    name: str
    status: str
    tone: str                       # live | idle | stale | error | paid | blocked
    rows: tuple                     # (title, remaining %, caption, tone, figure): the panel's header columns
    tps: float | None
    rate60: float
    active: int
    forecast: str
    forecast_tone: str
    note: str = ""                  # e.g. the read error
    refresh: str = ""               # when this source is read next, e.g. "12 秒后刷新 · 悬停"


def quota_rows(snapshot, now: float | None = None) -> tuple:
    """Up to three (title, remaining, caption, tone, figure) rows, written like the
    panel's columns: the time left to the reset on top, the window or pool beside it."""
    if snapshot is None:
        return ()
    now = now or time.time()
    rows = []
    if snapshot.pools and len(snapshot.pools) > 1:
        windows = [(pool.get("resets_at"), pool.get("remaining_percent"), str(pool.get("label") or pool.get("key") or "额度"))
                   for pool in snapshot.pools[:3]]
    else:
        windows = [(snapshot.resets_at, snapshot.remaining_percent, window_name(snapshot.window_minutes))]
        if snapshot.secondary_window_minutes or snapshot.secondary_remaining_percent is not None:
            windows.append((snapshot.secondary_resets_at, snapshot.secondary_remaining_percent,
                            window_name(snapshot.secondary_window_minutes)))
    for stamp, value, name in windows:
        left = reset_distance(stamp, now)
        rows.append((left or name, value, name if left else "", "", None))
    return tuple(rows)


def refresh_text(seconds: float | None, reason: str) -> str:
    """"12 秒后刷新 · 悬停" for the card footer."""
    if seconds is None:
        return ""
    if reason == "正在刷新":
        return "正在刷新"
    seconds = round(seconds)
    when = "即将刷新" if seconds <= 1 else (f"{seconds} 秒后刷新" if seconds < 90 else f"{round(seconds / 60)} 分钟后刷新")
    return f"{when} · {reason}" if reason else when


def _rounded(size, radius, fill, outline=None, scale=1.0):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=round(radius * scale), fill=fill,
                                            outline=outline, width=max(1, round(scale)))
    return image


def _fit(draw, text, face, limit):
    if draw.textlength(text, font=face) <= limit:
        return text
    while text and draw.textlength(text + "…", font=face) > limit:
        text = text[:-1]
    return text + "…"


ROW_H = 31


def card_height(slots: int) -> int:
    """Cards in one grid row share the height of the one with most quota rows."""
    return CARD_H - (3 - max(1, min(3, slots))) * ROW_H


def source_card(card: SourceCard, icon: Image.Image | None, scale: float = 1.0, hover: bool = False, slots: int = 3,
                width: float = CARD_W, height: float | None = None) -> Image.Image:
    """One source as the hover panel shows it: same columns, tones and colours.
    `width` and `height` are unscaled pixels; a taller card (to match the
    tiles beside it) keeps its footer on the bottom edge."""
    W = width
    H = max(card_height(slots), height or 0)
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    color = accent(card.letter)
    edge = tuple(round(c * .55 + e * .45) for c, e in zip(color[:3], CARD_EDGE[:3])) + (255,) if hover else CARD_EDGE
    image = _rounded((px(W), px(H)), 12, (47, 46, 41, 255) if hover else CARD_RGB, edge, s)
    draw = ImageDraw.Draw(image)
    if icon is not None:
        image.alpha_composite(icon.convert("RGBA").resize((px(28), px(28)), Image.LANCZOS), (px(14), px(14)))
    else:
        draw.ellipse((px(14), px(14), px(42), px(42)), fill=color)
    title, small, tiny = ui_font(px(13), "bold"), ui_font(px(10)), ui_font(px(9))
    pill_w = draw.textlength(card.status, font=tiny) / s + 14
    fill, ink = PILL.get(card.tone) or ((*color[:3], 255), (31, 30, 28, 255))
    draw.rounded_rectangle((px(W - 14 - pill_w), px(19), px(W - 14), px(37)), radius=px(9), fill=fill)
    draw.text((px(W - 14 - pill_w / 2), px(28)), card.status, font=tiny, fill=ink, anchor="mm")
    draw.text((px(50), px(28)), _fit(draw, card.name, title, px(W - 50 - pill_w - 22)), font=title, fill=TEXT, anchor="lm")

    rows = list(card.rows[:3]) or [("额度", None, card.note or "正在读取", "", None)]
    for index, row in enumerate(rows):
        label, value, caption, tone, figure = (tuple(row) + ("", "", None))[:5]
        y = 54 + index * ROW_H
        ink = ROW_INK.get(tone)
        draw.text((px(14), px(y)), _fit(draw, str(label), small, px(W * .52)), font=small, fill=SECONDARY_TEXT, anchor="lt")
        if caption:
            x = 14 + draw.textlength(_fit(draw, str(label), small, px(W * .52)), font=small) / s + 6
            draw.text((px(x), px(y + 1)), _fit(draw, str(caption), tiny, px(W - 76 - x)), font=tiny, fill=ink or MUTED_TEXT, anchor="lt")
        number = figure if figure else ("—" if value is None else f"{value:.0f}%")
        # Like the hover panel: the number warns, the bar keeps its window's colour.
        shade = ink or (MUTED_TEXT if value is None else TEXT if value >= 50 or card.letter in UNIFORM_LETTERS
                        else ring_color(value, card.letter))
        draw.text((px(W - 14), px(y - 2)), number, font=figure_font(px(13)), fill=shade, anchor="rt")
        bar = y + 17
        draw.rounded_rectangle((px(14), px(bar), px(W - 14), px(bar + 4)), radius=px(2), fill=TRACK_RGB)
        if tone == "paid":
            draw.rounded_rectangle((px(14), px(bar), px(W - 14), px(bar + 4)), radius=px(2), fill=PAID)
        elif value:
            right = 14 + (W - 28) * max(0.0, min(100.0, value)) / 100
            fill = ink or window_shade(card.letter, index, value, len(rows) > 1)
            draw.rounded_rectangle((px(14), px(bar), px(max(18, right)), px(bar + 4)), radius=px(2), fill=fill)

    y = H - 50
    draw.line((px(14), px(y), px(W - 14), px(y)), fill=RULE, width=max(1, round(s)))
    if card.provider != "gemini":
        tps = card.tps or 0.0
        big = figure_font(px(16))
        number = f"{tps:.0f}"
        draw.text((px(14), px(y + 22)), number, font=big, fill=color if card.tps else TEXT, anchor="ls")
        after = 14 + draw.textlength(number, font=big) / s + 3
        draw.text((px(after), px(y + 22)), "tok/s", font=tiny, fill=SECONDARY_TEXT, anchor="ls")
    meta = card.refresh or f"{card.active} 个会话"
    draw.text((px(W - 14), px(y + 21)), _fit(draw, meta, tiny, px(W * .62)), font=tiny, fill=MUTED_TEXT, anchor="rs")
    forecast = card.forecast or card.note or "正在积累用量，稍后给出预测"
    tint = TONE_COLORS.get(card.forecast_tone, MUTED_TEXT) if card.forecast else MUTED_TEXT
    draw.text((px(14), px(y + 37)), _fit(draw, forecast, tiny, px(W - 28)), font=tiny, fill=tint, anchor="lm")
    return image


FACT_TOP, FACT_LABEL, FACT_LINE, FACT_GAP, FACT_FOOT = 44, 15, 18, 9, 12


def _fact_layout(draw, rows, width: float, s: float, title: str = ""):
    """[(label, [value lines], warn)] wrapped to the tile, and its height."""
    px = lambda value: round(value * s)  # noqa: E731
    face = ui_font(px(12))
    laid, height = [], FACT_TOP if title else 16
    for label, value, warn in rows:
        lines = [line for line in _wrap_text(draw, str(value), face, px(width - 28)) if line] or ["—"]
        laid.append((label, lines, warn))
        height += (FACT_LABEL if label else 0) + len(lines) * FACT_LINE + FACT_GAP
    return laid, height - FACT_GAP + FACT_FOOT


NO_LINE_START = "）)」』】》，。、；：！？,.;:!?%"     # closing marks hang on the line before
NO_LINE_END = "（(「『【《"                          # opening marks go to the next line


def _wrap_text(draw, text: str, face, limit: float) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if not line or draw.textlength(line + char, font=face) <= limit or char in NO_LINE_START:
                line += char           # a closing mark may run a little past the edge
            else:
                carry = ""
                while line and line[-1] in NO_LINE_END:
                    line, carry = line[:-1], line[-1] + carry
                lines.append(line.rstrip())
                line = (carry + char).lstrip()
        lines.append(line)
    return lines


def fact_height(rows, scale: float = 1.0, width: float = CARD_W, title: str = "") -> float:
    """Unscaled height a fact tile needs for its rows at this width."""
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return _fact_layout(draw, rows, width, scale, title)[1]


def fact_tile(title: str, rows, scale: float = 1.0, width: float = CARD_W, height: float | None = None) -> Image.Image:
    """A detail tile in the card style: a title, then label / value rows
    ((label, value, warn)); as tall as the tiles beside it."""
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    laid, natural = _fact_layout(probe, rows, width, s, title)
    H = max(natural, height or 0)
    image = _rounded((px(width), px(H)), 12, CARD_RGB, CARD_EDGE, s)
    draw = ImageDraw.Draw(image)
    draw.text((px(14), px(24)), title, font=ui_font(px(13), "bold"), fill=TEXT, anchor="lm")
    label_face, value_face = ui_font(px(10)), ui_font(px(12))
    y = FACT_TOP if title else 16
    for label, lines, warn in laid:
        if label:
            draw.text((px(14), px(y)), _fit(draw, label, label_face, px(width - 28)), font=label_face, fill=MUTED_TEXT, anchor="lt")
            y += FACT_LABEL
        for line in lines:
            draw.text((px(14), px(y)), line, font=value_face, fill=AMBER if warn else TEXT, anchor="lt")
            y += FACT_LINE
        y += FACT_GAP
    return image


# Output chart: how far back it looks, and how long each bar is. Bars start
# on round clock times so a hovered bar reads "14:20–14:25", not 14:17:43.
CHART_RANGES = {30: 60, 120: 300, 360: 900, 1440: 1800, 10080: 21600}     # 7 days from the hourly totals (usage_store)
ZOOM_STEPS = {10: 15}               # windows only auto-zoom uses, shorter than the picker's
RANGE_NAMES = {10: "10 分钟", 30: "30 分钟", 120: "2 小时", 360: "6 小时", 1440: "24 小时", 10080: "7 天"}
TICK_MINUTES = {10: 2, 30: 5, 120: 20, 360: 60, 1440: 180, 10080: 1440}
SHORT_NAMES = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini", "claude": "Claude", "mimo": "MiMo"}
CHART_PAD, CHART_GUTTER, CHART_TOP, CHART_FOOT = 16, 34, 40, 30

# Auto-zoom: a clock minute with HIGH_TPS tok/s of output, within the last
# BURST_HOLD minutes, is fast output. The run reaches back to the last
# BURST_QUIET minutes in a row under a quarter of that.
HIGH_TPS, BURST_HOLD, BURST_QUIET = 20.0, 3, 3
ZOOM_CHOICES = (10, 30, 120, 360)


@dataclass(frozen=True)
class Activity:
    """Output tokens per bar, one column per provider, oldest bar first."""
    start: float
    step: int
    minutes: int
    providers: tuple
    buckets: tuple                  # tuple per bar of tokens per provider
    now: float

    @property
    def totals(self) -> tuple:
        return tuple(sum(bar[index] for bar in self.buckets) for index in range(len(self.providers)))

    @property
    def total(self) -> int:
        return sum(self.totals)

    def bar_span(self, index: int) -> tuple[float, float]:
        begin = self.start + index * self.step
        return begin, min(begin + self.step, self.now)


def activity(events: Iterable[Any], now: float, minutes: int, providers: Iterable[str]) -> Activity:
    """Bucket the monitor's output events for the chart."""
    step = CHART_RANGES.get(minutes) or ZOOM_STEPS.get(minutes) or max(60, minutes * 2)
    count = max(1, round(minutes * 60 / step))
    offset = _utc_offset(now)
    end = ((now + offset) // step + 1) * step - offset     # the bar holding "now" ends on the next boundary
    start = end - count * step
    keys = tuple(providers)
    column = {key: index for index, key in enumerate(keys)}
    sums = [[0] * len(keys) for _ in range(count)]
    for event in events:
        ended = getattr(event, "ended_at", 0)
        if not (start <= ended <= now) or not getattr(event, "count_in_rate", True):
            continue
        slot = column.get(getattr(event, "provider", None))
        if slot is not None:
            sums[min(count - 1, int((ended - start) // step))][slot] += int(getattr(event, "output_tokens", 0) or 0)
    return Activity(start, step, minutes, keys, tuple(tuple(bar) for bar in sums), now)


@dataclass(frozen=True)
class Burst:
    """A run of fast output that is still going on."""
    start: float                    # the first busy clock minute of the run
    rate: float                     # tok/s in its busiest recent minute


def burst(events: Iterable[Any], now: float, providers: Iterable[str], look_back: int = 6 * 3600) -> Burst | None:
    """The fast-output run under way now, from clock-minute output totals, or None."""
    keys = set(providers)
    offset = _utc_offset(now)
    current = int((now + offset) // 60)
    per_minute: dict[int, int] = {}
    for event in events:
        ended = getattr(event, "ended_at", 0)
        if not (now - look_back <= ended <= now) or not getattr(event, "count_in_rate", True):
            continue
        if getattr(event, "provider", None) in keys:
            minute = int((ended + offset) // 60)
            per_minute[minute] = per_minute.get(minute, 0) + int(getattr(event, "output_tokens", 0) or 0)
    rate = max(per_minute.get(current - back, 0) for back in range(BURST_HOLD)) / 60
    if rate < HIGH_TPS:
        return None
    busy, first, quiet = HIGH_TPS * 60 / 4, current, 0
    for minute in range(current, current - look_back // 60, -1):
        if per_minute.get(minute, 0) >= busy:
            first, quiet = minute, 0
        else:
            quiet += 1
            if quiet >= BURST_QUIET:
                break
    return Burst(first * 60 - offset, rate)


def zoom_minutes(run: Burst | None, chosen: int, now: float) -> int | None:
    """The shortest window, shorter than the chosen range, that holds the
    whole run with some quiet lead-in for the ramp; None to keep the range."""
    if run is None:
        return None
    length = now - run.start
    for minutes in ZOOM_CHOICES:
        if minutes < chosen and minutes * 60 >= length + max(120, length * .25):
            return minutes
    return None


def _utc_offset(stamp: float) -> int:
    return time.localtime(stamp).tm_gmtoff or 0


def tokens_text(value: float) -> str:
    if value < 1000:
        return f"{value:.0f}"
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            text = f"{value / size:.1f}".rstrip("0").rstrip(".")
            return text + unit
    return f"{value:.0f}"


def nice_ceiling(value: float) -> float:
    """The next 1 / 2 / 5 × 10ⁿ at or above value, so gridlines get round labels."""
    if value <= 0:
        return 1.0
    power = 10 ** (len(str(int(value))) - 1)
    for factor in (1, 2, 2.5, 5, 10):
        if factor * power >= value:
            return factor * power
    return 10 * power


def time_ticks(chart: Activity) -> list[tuple[float, str]]:
    """Round clock times inside the range: every 5 min for half an hour up to
    every 3 h for a day. Midnight shows the date instead."""
    every = TICK_MINUTES.get(chart.minutes, max(5, chart.minutes // 6)) * 60
    offset = _utc_offset(chart.now)
    first = ((chart.start + offset) // every + 1) * every - offset
    ticks = []
    stamp = first
    while stamp < chart.start + len(chart.buckets) * chart.step:
        local = time.localtime(stamp)
        label = f"{local.tm_mon}/{local.tm_mday}" if (local.tm_hour, local.tm_min) == (0, 0) else time.strftime("%H:%M", local)
        ticks.append((stamp, label))
        stamp += every
    return ticks


def chart_frame(width: float, height: float, count: int) -> tuple[float, float, float, float, float]:
    """(left, right, top, bottom, slot) of the plot, in unscaled pixels."""
    left, right = CHART_PAD + CHART_GUTTER, width - CHART_PAD
    return left, right, CHART_TOP, height - CHART_FOOT, (right - left) / max(1, count)


def chart_index(x: float, width: float, height: float, count: int) -> int | None:
    """Which bar an unscaled x falls on, or None outside the plot."""
    left, right, _top, _bottom, slot = chart_frame(width, height, count)
    if not left <= x < right:
        return None
    return min(count - 1, int((x - left) // slot))


def _span_text(start: float, end: float, step: int) -> str:
    begin = time.localtime(start)
    clock = "%H:%M:%S" if step < 60 else "%H:%M"
    text = time.strftime(clock, begin) + "–" + time.strftime(clock, time.localtime(end))
    if step >= 3600 or begin.tm_yday != time.localtime().tm_yday:
        text = f"{begin.tm_mon}/{begin.tm_mday} " + text
    return text


def activity_chart(chart: Activity, letters: dict[str, str], scale: float = 1.0, width: int = 652, height: int = 150,
                   hover: int | None = None, grow: float = 1.0, trend: bool = False) -> Image.Image:
    """Stacked output per bar with a clock-time axis underneath. `hover`
    highlights one bar and shows its exact figures; `grow` (0–1) lets the bars
    rise in after the range changes; `trend` runs a line over the bar tops so
    the climb and fall of a fast run read as a slope."""
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    image = _rounded((px(width), px(height)), 12, CARD_RGB, CARD_EDGE, s)
    draw = ImageDraw.Draw(image)
    small, tiny = ui_font(px(11)), ui_font(px(10))
    line = max(1, round(s))
    count = len(chart.buckets)
    left, right, top, bottom, slot = chart_frame(width, height, count)
    totals = chart.totals
    total = sum(totals)

    # Legend: each source with its total over the range, then the sum.
    x = right
    summary = f"共 {tokens_text(total)} tokens"
    draw.text((px(x), px(16)), summary, font=small, fill=SECONDARY_TEXT, anchor="rm")
    x -= draw.textlength(summary, font=small) / s + 18
    for index in reversed(range(len(chart.providers))):
        if not totals[index]:
            continue
        provider = chart.providers[index]
        text = f"{SHORT_NAMES.get(provider, provider)} {tokens_text(totals[index])}"
        w = draw.textlength(text, font=tiny) / s
        if x - w - 12 < CHART_PAD:
            break
        draw.text((px(x), px(16)), text, font=tiny, fill=SECONDARY_TEXT, anchor="rm")
        draw.ellipse((px(x - w - 12), px(12), px(x - w - 4), px(20)), fill=accent(letters.get(provider, "?")))
        x -= w + 26

    peak = max([sum(bar) for bar in chart.buckets] + [0])
    ceiling = nice_ceiling(peak)
    for fraction in (0.0, 0.5, 1.0):
        y = bottom - (bottom - top) * fraction
        draw.line((px(left), px(y), px(right), px(y)), fill=RULE if fraction else TRACK_RGB, width=line)
        if fraction:
            draw.text((px(left - 8), px(y)), tokens_text(ceiling * fraction), font=tiny, fill=MUTED_TEXT, anchor="rm")

    if hover is not None and 0 <= hover < count:
        draw.rectangle((px(left + hover * slot), px(top - 6), px(left + (hover + 1) * slot), px(bottom)), fill=mix(IVORY_RGB, .06))

    gap = min(3.0, slot * .25)
    for index, bar in enumerate(chart.buckets):
        x0, x1 = left + index * slot + gap / 2, left + (index + 1) * slot - gap / 2
        base = bottom
        if not any(bar):
            draw.rectangle((px(x0), px(bottom - 2), px(x1), px(bottom)), fill=TRACK_RGB)
            continue
        delay = .35 * index / max(1, count - 1)
        rise = ease(max(0.0, min(1.0, (grow - delay) / .65))) if grow < 1 else 1.0
        dim = hover is not None and hover != index
        for slot_index, amount in enumerate(bar):
            if not amount:
                continue
            h = max(2.0, (bottom - top) * amount / ceiling) * rise
            if h < .5:
                continue
            color = accent(letters.get(chart.providers[slot_index], "?"))
            draw.rectangle((px(x0), px(base - h), px(x1), px(base)), fill=mix(color, .55) if dim else color)
            base -= h
    if trend and total:
        _trend_line(image, chart, (left, right, top, bottom, slot), ceiling, grow, s)

    # Clock times under the axis; "现在" closes it on the right.
    now_x = left + (chart.now - chart.start) / chart.step * slot
    draw.text((px(right), px(height - 14)), "现在", font=tiny, fill=SECONDARY_TEXT, anchor="rm")
    reserved = right - draw.textlength("现在", font=tiny) / s - 8
    for stamp, label in time_ticks(chart):
        tx = left + (stamp - chart.start) / chart.step * slot
        half = draw.textlength(label, font=tiny) / s / 2
        if tx - half < CHART_PAD or tx + half > reserved:
            continue
        draw.line((px(tx), px(bottom), px(tx), px(bottom + 4)), fill=RULE, width=line)
        draw.text((px(tx), px(height - 14)), label, font=tiny, fill=MUTED_TEXT, anchor="mm")
    draw.line((px(now_x), px(bottom), px(now_x), px(bottom + 4)), fill=SECONDARY_TEXT, width=line)

    if not total:
        draw.text((px((left + right) / 2), px((top + bottom) / 2)), "这段时间没有输出", font=small, fill=MUTED_TEXT, anchor="mm")
    elif hover is not None and 0 <= hover < count:
        _readout(draw, chart, letters, hover, (left, right, top, bottom, slot), s)
    return image


def ease(t: float) -> float:
    return 1 - (1 - t) ** 3


def trend_points(chart: Activity, frame, ceiling: float, grow: float = 1.0) -> list[tuple[float, float]]:
    """The output rate as a line (unscaled points), from the first bar with
    output to "now". A reply's tokens all land when it finishes, so short bars
    alone zigzag; each point averages the bars of the last minute or so."""
    left, _right, top, bottom, slot = frame
    sums = [sum(bar) for bar in chart.buckets]
    first = next((index for index, value in enumerate(sums) if value), None)
    if first is None:
        return []
    width = max(3, round(60 / chart.step))
    rise = ease(max(0.0, min(1.0, grow))) if grow < 1 else 1.0
    points = []
    for index in range(first, len(sums)):
        window = sums[max(0, index - width + 1):index + 1]
        level = sum(window) / width
        points.append((left + (index + .5) * slot, bottom - (bottom - top) * min(1.0, level / ceiling) * rise))
    return points


def _trend_line(image: Image.Image, chart: Activity, frame, ceiling: float, grow: float, s: float) -> None:
    """The trend over the bars, drawn at 3x and scaled down so it stays smooth."""
    points = trend_points(chart, frame, ceiling, grow)
    if len(points) < 2:
        return
    k = 3 * s
    layer = Image.new("RGBA", (image.width * 3, image.height * 3), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    scaled = [(x * k, y * k) for x, y in points]
    draw.line(scaled, fill=(*CARD_RGB[:3], 170), width=round(5 * k), joint="curve")      # dark edge off the bars
    draw.line(scaled, fill=(*IVORY_RGB, 255), width=round(1.6 * k), joint="curve")
    x, y = scaled[-1]
    draw.ellipse((x - 3.2 * k, y - 3.2 * k, x + 3.2 * k, y + 3.2 * k), fill=(*IVORY_RGB, 255))
    image.alpha_composite(layer.resize(image.size, Image.LANCZOS))


def _readout(draw, chart: Activity, letters, index, frame, s) -> None:
    """The hovered bar's time span and figures in a small box beside it."""
    px = lambda value: round(value * s)  # noqa: E731
    left, right, top, bottom, slot = frame
    bold, tiny = ui_font(px(11), "bold"), ui_font(px(10))
    bar = chart.buckets[index]
    lines = [(chart.providers[i], amount) for i, amount in enumerate(bar) if amount]
    title = _span_text(*chart.bar_span(index), chart.step)
    rows = [f"{SHORT_NAMES.get(p, p)}  {amount:,}" for p, amount in lines]
    if len(lines) > 1:
        rows.append(f"合计  {sum(bar):,}")
    if not rows:
        rows = ["没有输出"]
    width = max([draw.textlength(title, font=bold) / s] + [draw.textlength(row, font=tiny) / s + 14 for row in rows]) + 20
    height = 26 + len(rows) * 16
    center = left + (index + .5) * slot
    x0 = center + slot / 2 + 8
    if x0 + width > right:
        x0 = center - slot / 2 - 8 - width
    x0 = max(CHART_PAD, x0)
    y0 = top - 4
    draw.rounded_rectangle((px(x0), px(y0), px(x0 + width), px(y0 + height)), radius=px(7),
                           fill=(52, 51, 46, 255), outline=CARD_EDGE, width=max(1, round(s)))
    draw.text((px(x0 + 10), px(y0 + 13)), title, font=bold, fill=TEXT, anchor="lm")
    for row_index, row in enumerate(rows):
        y = y0 + 31 + row_index * 16
        provider = lines[row_index][0] if row_index < len(lines) else None
        if provider:
            draw.ellipse((px(x0 + 10), px(y - 3), px(x0 + 16), px(y + 3)), fill=accent(letters.get(provider, "?")))
        draw.text((px(x0 + 24 if provider else x0 + 10), px(y)), row, font=tiny, fill=SECONDARY_TEXT if provider else TEXT, anchor="lm")


def agent_rows(agents: list[dict], letters: dict[str, str], scale: float = 1.0, width: int = 652, limit: int = 8) -> Image.Image:
    """The agents the tray currently sees, one line each."""
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    shown = agents[:limit]
    height = 46 + max(1, len(shown)) * 38 + 10
    image = _rounded((px(width), px(height)), 12, CARD_RGB, CARD_EDGE, s)
    draw = ImageDraw.Draw(image)
    small, tiny = ui_font(px(12)), ui_font(px(10))
    draw.text((px(16), px(14)), "当前任务", font=ui_font(px(13), "bold"), fill=TEXT, anchor="lt")
    live = sum(1 for agent in agents if agent.get("live"))
    draw.text((px(width - 16), px(15)), f"{live} 个工作中 · 共 {len(agents)} 个", font=ui_font(px(11)), fill=SECONDARY_TEXT, anchor="rt")
    if not shown:
        draw.text((px(width / 2), px(46 + 19)), "暂无工作中的 agent", font=small, fill=MUTED_TEXT, anchor="mm")
    for index, agent in enumerate(shown):
        y = 46 + index * 38
        if index:
            draw.line((px(16), px(y), px(width - 16), px(y)), fill=RULE, width=max(1, round(s)))
        color = accent(letters.get(agent.get("provider"), "?"))
        cy = y + 19
        if agent.get("live"):
            draw.ellipse((px(16), px(cy - 6), px(28), px(cy + 6)), fill=mix(color, .25))
        draw.ellipse((px(19), px(cy - 3), px(25), px(cy + 3)), fill=color if agent.get("live") else mix(color, .45))
        title = str(agent.get("title") or agent.get("thread_title") or agent.get("folder") or agent.get("source") or "会话")
        draw.text((px(38), px(cy - 7)), _fit(draw, title, small, px(width * .5)), font=small, fill=TEXT, anchor="lm")
        where = " · ".join(part for part in (agent.get("source"), agent.get("folder"), "herdr " + agent["pane"] if agent.get("pane") else "") if part)
        draw.text((px(38), px(cy + 9)), _fit(draw, where, tiny, px(width * .5)), font=tiny, fill=MUTED_TEXT, anchor="lm")
        doing = str(agent.get("doing") or ("工作中" if agent.get("live") else "刚完成"))
        tps = agent.get("tps")
        if agent.get("provider") != "gemini":
            draw.text((px(width - 16), px(cy - 7)), f"{tps:.0f} tok/s" if tps else "—", font=ui_font(px(11)), fill=color if tps else MUTED_TEXT, anchor="rm")
        draw.text((px(width - 16), px(cy + 9)), _fit(draw, doing, tiny, px(width * .35)), font=tiny,
                  fill=SECONDARY_TEXT if agent.get("live") else MUTED_TEXT, anchor="rm")
    return image


def health_tone(status: str) -> str:
    if status in ("已接入",):
        return "ok"
    if any(word in status for word in ("失败", "不可", "未运行")):
        return "error"
    return "idle"
