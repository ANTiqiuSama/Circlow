"""Agent panel: compact quota header plus a field of task bubbles.

Each bubble is one task: a single agent, or several agents the user has
dropped onto each other (or that the client itself spawned) shown as one
larger bubble with orbiting member dots. Drag a bubble onto another to merge,
right-click a merged bubble to split it again (no on-screen instructions: the
halo and the gulp/burst motion explain themselves).

Scene holds the animation state and is pure Python + PIL so it can be tested
anywhere; hover_details feeds it the pointer and presents its frames.
"""
from __future__ import annotations

import math
import time
import random
from dataclasses import dataclass, field, replace
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from detail_card import MARGIN, MUTED_TEXT, SECONDARY_TEXT, SURFACE, TEXT, TRACK, _compose, _wrap, figure_font, ui_font
from icon_art import AMBER, CLAY, PAID, POOL_SHADES, UNIFORM_LETTERS, WINDOW_SHADES, accent, ring_color, window_shade

WIDTH = 420
PAD = 18
FIELD_TOP = 118
BASE_RADIUS = 30.0
GAP = 10.0                  # Clearance between neighbouring bubble boxes.
LABEL = 20.0                # Caption height under a bubble.
SUPERSAMPLE = 3
MAX_BUBBLES = 9
CAPTION_SPARE = 24.0        # Caption may be this much wider than its bubble.
LIVE_HOLD = 1.5             # A live/idle flip must persist this long before it shows.


@dataclass(frozen=True)
class BubbleModel:
    key: str
    title: str
    doing: str
    letter: str
    live: bool = True
    detail: str = ""
    members: tuple[tuple[str, bool], ...] = ()   # (letter, live) for grouped bubbles
    busy: bool = False                           # an optional summary is being made
    focus: bool = True                           # belongs to the hovered provider
    keys: tuple[str, ...] = ()                   # member agent ids, for continuity across regrouping

    @property
    def radius(self) -> float:
        count = len(self.members)
        return BASE_RADIUS if count < 2 else min(52.0, BASE_RADIUS + 9.0 * math.sqrt(count - 1))

    @property
    def half_width(self) -> float:
        """Half the footprint. Fixed per bubble size (never from the caption text),
        so a changing action line does not reflow the shelf and shove neighbours."""
        return self.radius + CAPTION_SPARE / 2 + GAP / 2


@dataclass(frozen=True)
class BurnSeries:
    """Remaining quota of one window over time. `slot` is the header column
    showing the same window, so both use one colour. `rate` is the pace in %
    per hour (the forecast's slope)."""
    label: str
    slot: int
    points: tuple[tuple[float, float], ...]     # (time, remaining %), oldest first
    rate: float | None = None
    reset_at: float | None = None
    letter: str = ""                         # source colour, independent of hovered icon
    pooled: bool = False
    idle: bool = False                       # not in use: a faint line, no fill or forecast


@dataclass(frozen=True)
class BurnChart:
    start: float
    end: float
    now: float
    series: tuple[BurnSeries, ...]
    axis: "BurnScale | None" = None          # set while a zoom eases between two views' scales


# Reset notices: a quiet steel blue that sits with the panel's muted palette
# (the bright blue bar with an amber badge clashed with it).
RESET_TINT = (150, 178, 214, 255)


# Header bars and chart lines share the tray ring's per-window colours.
quota_shade = window_shade


def pooled(model: "PanelModel") -> bool:
    return len(model.quota) > 1


@dataclass(frozen=True)
class PanelModel:
    provider: str
    letter: str
    title: str
    status: str
    quota: tuple[tuple, ...]                    # (label, remaining %, reserved)
    tps: float | None
    bubbles: tuple[BubbleModel, ...]
    status_tone: str = "live"                   # live | idle | stale | error
    banner: str = ""
    forecast: str = ""                          # pace forecast: will the quota last at the current rate
    forecast_tone: str = "unknown"              # danger | warn | ok | idle | unknown
    today_used: float | None = None              # MiMo Credits used today
    burn: BurnChart | None = None
    banner_alert: str = ""                      # e.g. "官方预告": the banner turns solid with this badge
    intel: tuple = ()                           # coding IQ on the banner's right: (name, score, tone, running)
    intel_note: str = ""                        # shown there instead when there are no scores (e.g. "未设置")
    chart_padding: float = 0.0                   # static-background copy of wrapped legend space
    quota_tones: tuple[str, ...] = ()            # per column: "" | "active" (pool in use) | "paid" | "blocked"
    quota_figures: tuple = ()                    # per column: (number, unit) shown instead of the %, e.g. Codex credits
    column_iq: tuple = ()                       # spare header slot, Grok only: (label, figure, caption, unit)
    hint_now: str = ""                          # MiMo or local summary of current agent work
    hint_next: str = ""                         # one optional next action
    local_tps: float | None = None              # with multi-device sync: this device's own TPS, under the shared figure

    def figure_of(self, slot: int) -> tuple[str, str] | None:
        return self.quota_figures[slot] if slot < len(self.quota_figures) else None

    def tone_of(self, slot: int) -> str:
        return self.quota_tones[slot] if slot < len(self.quota_tones) else ""

    @property
    def header(self) -> tuple:
        """Static part of the panel (cached background). Quota figures, bars,
        TPS and the burn chart are drawn per frame; the reset line is cached here."""
        return (self.letter, self.title, self.status, self.status_tone,
                tuple((item[0], item[2] if len(item) > 2 else "", self.tone_of(slot)) for slot, item in enumerate(self.quota[:3])),
                (self.banner, self.banner_alert, self.intel, self.intel_note),
                (self.forecast, self.forecast_tone), self.provider == "mimo", chart_extra_for(self),
                self.column_iq, self.tps_label)

    @property
    def tps_label(self) -> str:
        """The TPS column's title; empty when there is none (Gemini). With
        multi-device sync the figure is every device's total, "本机" its own."""
        if self.provider == "gemini":
            return ""
        return "总 TPS" if self.local_tps is not None else "TPS"


METER_TOP = PAD + 32        # Top of the quota / TPS row.
FOOTER = 40                 # Two forecast lines plus a separate settings button.
CHART_AXIS = 11             # Clock row under the plot.
CHART = 80 + CHART_AXIS     # Burn chart under the task field.
CHART_TOP = 16              # Field bottom to chart sprite; the two plates keep an 8 px gap.
CHART_H = 64 + CHART_AXIS   # Chart sprite: label row, plot and clock row.
CHART_LABEL = 15            # Label row inside the sprite.
HINT_H = 48                 # One short paragraph, up to two lines, above the forecast.
HINT_FONT, HINT_LINE = 13, 24   # px: LumaCenter's tray-panel notes beside it (size, row)
HINT_TEXT_X = 22                # text after the 12 px mark, as LumaCenter lays its rows out
GEAR_RADIUS = 7.5
GEAR_HIT = 17.0


def meter_columns(count: int, show_tps: bool = True) -> list[tuple[float, float]]:
    """(left, bar end) of each header column; the last column is TPS."""
    # At least three slots, so a single quota (Grok) does not stretch across half
    # the panel; TPS always takes the rightmost slot, as on every other source.
    slots = max(3, count + 1) if show_tps else max(1, count)
    column = (WIDTH - 2 * PAD) / slots
    indices = [*range(count), slots - 1] if show_tps else range(count)
    return [(PAD + index * column, PAD + (index + 1) * column - 16) for index in indices]


def spare_slot(count: int, show_tps: bool = True) -> tuple[float, float] | None:
    """The first header column `meter_columns` leaves empty, if any."""
    slots = max(3, count + 1) if show_tps else max(1, count)
    used = {*range(count), slots - 1} if show_tps else set(range(count))
    free = [index for index in range(slots) if index not in used]
    if not free:
        return None
    column = (WIDTH - 2 * PAD) / slots
    index = free[0]
    return PAD + index * column, PAD + (index + 1) * column - 16


def chart_top(model, field_h: float) -> float:
    return field_top(model) + field_h + CHART_TOP


def chart_extra_for(model: PanelModel) -> float:
    return max(model.chart_padding, chart_extra(model.burn))


def footer_middle(model, field_h: float) -> float:
    return field_top(model) + field_h + CHART + chart_extra_for(model) + HINT_H + 24


def gear_center(model, field_h: float) -> tuple[float, float]:
    return WIDTH - PAD - GEAR_RADIUS - 2, footer_middle(model, field_h)


def header_offset(model: PanelModel | None) -> float:
    return 32 if model and (model.banner or model.intel or model.intel_note) else 0


def banner_box() -> tuple[float, float, float, float]:
    """The banner bar (reset chance on the left, model IQ on the right)."""
    return PAD - 6, PAD - 2, WIDTH - PAD + 6, PAD + 18


def over_banner(model: PanelModel | None, pointer) -> bool:
    if pointer is None or not header_offset(model):
        return False
    x0, y0, x1, y1 = banner_box()
    return x0 <= pointer[0] <= x1 and y0 <= pointer[1] <= y1


def field_top(model: PanelModel | None) -> float:
    if model is None:
        return FIELD_TOP
    # Reserve the subtitle row (e.g. MiMo Credits) to keep every source the same height.
    return FIELD_TOP + 6 + header_offset(model)


def pack(bubbles, top: float = FIELD_TOP) -> tuple[dict[str, tuple[float, float]], float]:
    """Centred shelf layout: home position per bubble and the field height."""
    room = WIDTH - 2 * PAD
    rows: list[list] = [[]]
    for bubble in bubbles:
        used = sum(2 * b.half_width for b in rows[-1])
        if rows[-1] and used + 2 * bubble.half_width > room:
            rows.append([])
        rows[-1].append(bubble)
    homes, y = {}, top + GAP / 2
    for row in rows:
        if not row:
            continue
        height = max(2 * b.radius for b in row) + LABEL + GAP
        x = PAD + (room - sum(2 * b.half_width for b in row)) / 2
        for bubble in row:
            homes[bubble.key] = (x + bubble.half_width, y + height / 2 - LABEL / 2)
            x += 2 * bubble.half_width
        y += height
    return homes, float(max(96.0, y - top + GAP / 2))


def field_height(bubbles) -> float:
    return pack(bubbles)[1]


def panel_height(model: PanelModel, field: float) -> float:
    return field_top(model) + field + CHART + chart_extra_for(model) + HINT_H + FOOTER + 8


# ----- motion helpers ------------------------------------------------------------

def ease_out_cubic(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


# (stiffness, damping) of the header figures, all critically damped (no overshoot).
METER_SPRING = (90.0, 19.0)       # quota bars and Credits count up when the panel opens (~0.6 s)
RATE_SPRING = (120.0, 22.0)       # TPS eases between readings
SWITCH_SPRING = (600.0, 49.0)     # hovering another source: roll to its figures (~0.25 s)


class Spring:
    """Critically under-damped spring: quick, with a small elegant overshoot."""

    def __init__(self, value: float, stiffness=190.0, damping=15.0):
        self.value, self.target, self.velocity = value, value, 0.0
        self.stiffness, self.damping = stiffness, damping

    def step(self, dt: float) -> float:
        for _ in range(max(1, math.ceil(dt / 0.008))):
            h = dt / max(1, math.ceil(dt / 0.008))
            force = self.stiffness * (self.target - self.value) - self.damping * self.velocity
            self.velocity += force * h
            self.value += self.velocity * h
        return self.value

    @property
    def settled(self) -> bool:
        return abs(self.target - self.value) < 0.002 and abs(self.velocity) < 0.01


class Follow:
    """Critically damped follower, solved exactly: no overshoot, stable for any
    frame time, and it snaps onto the target once it is closer than `epsilon`."""

    def __init__(self, value: float, omega=13.0, epsilon=0.05):
        self.value, self.target, self.velocity = value, value, 0.0
        self.omega, self.epsilon = omega, epsilon

    def step(self, dt: float) -> float:
        w, d = self.omega, self.value - self.target
        decay = math.exp(-w * dt)
        drift = self.velocity + w * d
        self.value = self.target + (d + drift * dt) * decay
        self.velocity = (self.velocity - w * drift * dt) * decay
        if abs(self.value - self.target) < self.epsilon and abs(self.velocity) < 10 * self.epsilon:
            self.value, self.velocity = self.target, 0.0
        return self.value

    @property
    def settled(self) -> bool:
        return self.value == self.target and self.velocity == 0.0


@dataclass
class Body:
    model: BubbleModel
    px: Follow
    py: Follow
    born: float
    phase: float = field(default_factory=lambda: random.uniform(0, math.tau))
    scale: Spring = field(default_factory=lambda: Spring(0.0))
    radius: Spring = field(default_factory=lambda: Spring(BASE_RADIUS, 150.0, 13.0))
    hover: Spring = field(default_factory=lambda: Spring(0.0, 260.0, 26.0))
    glow: Spring = field(default_factory=lambda: Spring(0.0, 260.0, 26.0))
    lift: Spring = field(default_factory=lambda: Spring(0.0, 260.0, 26.0))    # 1 while dragged

    @classmethod
    def at(cls, model: BubbleModel, x: float, y: float, born: float) -> "Body":
        return cls(model, Follow(x), Follow(y), born)

    @property
    def x(self) -> float:
        return self.px.value

    @property
    def y(self) -> float:
        return self.py.value

    def place(self, x: float, y: float):
        """Move without animation (dragging): the follower forgets its momentum."""
        self.px.value, self.py.value = x, y
        self.px.velocity = self.py.velocity = 0.0

    @property
    def springs(self) -> tuple:
        return self.px, self.py, self.scale, self.radius, self.hover, self.glow, self.lift

    @property
    def size(self) -> float:
        # Picked up: slightly larger, closer to the viewer.
        return self.radius.value * max(0.0, self.scale.value) * (1 + 0.05 * self.hover.value + 0.1 * self.glow.value
                                                                  + 0.07 * self.lift.value)


@dataclass
class Ghost:
    """A bubble leaving the scene: absorbed into another, popped, or faded."""
    model: BubbleModel
    x: float
    y: float
    r: float
    start: float
    kind: str
    tx: float = 0.0
    ty: float = 0.0
    duration: float = 0.3


@dataclass
class Ripple:
    x: float
    y: float
    r: float
    start: float
    letter: str
    duration: float = 0.6
    key: str = ""               # follow this bubble while it settles


class Scene:
    """Bubbles sit on shelf slots (pack) and glide to them on critically damped
    followers: no collision pass, no idle drift, so a steady panel is pixel-still
    and every change is one smooth move. New bubbles pop in on their own slot."""

    def __init__(self):
        self.model: PanelModel | None = None
        self.bodies: dict[str, Body] = {}
        self.ghosts: list[Ghost] = []
        self.ripples: list[Ripple] = []
        self.field = Follow(field_height(()), 11.0, 0.1)
        self.time = 0.0
        self.last = None
        self.press = None           # (key, dx, dy, x, y)
        self.dragging: str | None = None
        self.target: str | None = None
        self.hovered: str | None = None
        self.left = self.right = False
        self.origins: dict[str, tuple[float, float, float]] = {}
        self.homes: dict[str, tuple[float, float]] = {}
        self.meters: list[Spring] = []          # quota bars count up from zero when the panel opens
        self.rate = Spring(0.0, *RATE_SPRING)   # TPS figure eases between readings
        self.today_usage = Spring(0.0, *METER_SPRING)
        self.opened = 0.0
        self.gear = Spring(0.0, 260.0, 26.0)    # settings gear highlight under the pointer
        self.gear_angle = 0.0
        self.order: list[str] = []              # first-seen order: bubbles keep their slots
        self.live_since: dict[str, tuple[bool, float]] = {}
        self.gear_press = False
        self.banner_press = False
        self.dirty = True                       # something changed that the next frame must show
        self._source: tuple[str, str] | None = None   # (provider, letter) of the figures shown

    def over_gear(self, pointer) -> bool:
        if pointer is None or self.model is None:
            return False
        cx, cy = gear_center(self.model, self.field.value)
        return math.hypot(pointer[0] - cx, pointer[1] - cy) <= GEAR_HIT

    # ----- geometry --------------------------------------------------------
    @property
    def bounds(self) -> tuple[float, float, float, float]:
        top = field_top(self.model)
        return PAD, top, WIDTH - PAD, top + self.field.value

    @property
    def height(self) -> float:
        return panel_height(self.model, self.field.value) if self.model else field_top(None) + self.field.value

    def body_at(self, x: float, y: float) -> str | None:
        order = sorted(self.bodies.items(), key=lambda item: item[0] == self.dragging, reverse=True)
        for key, body in order:
            if math.hypot(x - body.x, y - body.y) <= max(14.0, body.size):
                return key
        return None

    # ----- model -----------------------------------------------------------
    def set_model(self, model: PanelModel, now: float):
        first = self.model is None
        if model != self.model:
            self.dirty = True
        self.model = model
        if first:
            self.opened = now
        previous = self._source
        self._source = (model.provider, model.letter)
        switched = not first and previous != self._source
        values = [item[1] for item in model.quota[:3]]
        if len(self.meters) != len(values):
            # Keep the figures already shown so a switch rolls from them, not from zero.
            old = [spring.value for spring in self.meters]
            self.meters = [Spring(old[i] if i < len(old) else 0.0, *METER_SPRING) for i in range(len(values))]
        for spring, value in zip(self.meters, values):
            spring.target = 0.0 if value is None else float(value)
        self.rate.target = float(model.tps or 0.0)
        self.today_usage.target = float(model.today_used or 0.0)
        if switched or first:
            # Opening counts up at an easy pace; hopping to another source's
            # figures rolls about 2.5x faster so the numbers keep up with the pointer.
            meter, rate = (SWITCH_SPRING, SWITCH_SPRING) if switched else (METER_SPRING, RATE_SPRING)
            for spring in (*self.meters, self.today_usage):
                spring.stiffness, spring.damping = meter
            self.rate.stiffness, self.rate.damping = rate
        bubbles = self._stable(model.bubbles[:MAX_BUBBLES], now, first)
        self.homes, self.field.target = pack(bubbles, field_top(model))
        if first:
            self.field.value = self.field.target
        keys = {bubble.key for bubble in bubbles}
        removed = {key: self.bodies.pop(key) for key in [key for key in self.bodies if key not in keys]}
        if self.dragging in removed:
            self.dragging = self.target = self.press = None
        for index, bubble in enumerate(bubbles):
            home = self.homes[bubble.key]
            body = self.bodies.get(bubble.key)
            if body is None and bubble.key not in self.origins:
                # The same agents regrouped by the client (e.g. a sub-agent spawned): keep the bubble.
                mine = set(bubble.keys or (bubble.key,))
                heir = next((key for key, old in removed.items() if mine & set(old.model.keys or (key,))), None)
                if heir:
                    body = self.bodies[bubble.key] = removed.pop(heir)
                    if len(bubble.keys) != len(body.model.keys):    # really regrouped, not just renamed
                        body.scale.velocity += 3.0
            if body is None:
                origin = self.origins.pop(bubble.key, None)
                if origin:
                    # Split: members leave the burst point outward, then settle on their slots.
                    x, y, angle = origin
                    body = Body.at(bubble, x, y, now)
                    body.px.velocity, body.py.velocity = math.cos(angle) * 220, math.sin(angle) * 220
                    body.scale.value = 0.35
                else:
                    # Pop in on the bubble's own slot; neighbours glide aside meanwhile.
                    body = Body.at(bubble, *home, now + (index * 0.04 if first else 0.0))
                body.radius.value = body.radius.target = bubble.radius
                self.bodies[bubble.key] = body
            if body.model.live != bubble.live and not first and bubble.live:
                # picked work up again (after LIVE_HOLD): a ring announces it
                self.ripples.append(Ripple(body.x, body.y, body.size, now, bubble.letter, 0.7, bubble.key))
            body.model = bubble
            body.radius.target = bubble.radius
            body.px.target, body.py.target = home
        for body in removed.values():
            self.ghosts.append(Ghost(body.model, body.x, body.y, body.size, now, "fade", duration=0.28))

    def _stable(self, bubbles, now: float, first: bool) -> list:
        """Keep each bubble in the slot it first appeared in (the client's
        activity order changes every few seconds) and debounce live/idle
        flips between tool calls, which otherwise make bubbles twitch."""
        known = {key: index for index, key in enumerate(self.order)}
        ordered = sorted(bubbles, key=lambda b: known.get(b.key, len(known) + bubbles.index(b)))
        self.order = [b.key for b in ordered]
        result = []
        for bubble in ordered:
            body = self.bodies.get(bubble.key)
            shown = body.model.live if body is not None and not first else bubble.live
            if bubble.live != shown:
                state, since = self.live_since.get(bubble.key, (bubble.live, now))
                if state != bubble.live:
                    since = now
                self.live_since[bubble.key] = (bubble.live, since)
                if now - since < LIVE_HOLD:
                    bubble = replace(bubble, live=shown)
                else:
                    self.live_since.pop(bubble.key, None)
            else:
                self.live_since.pop(bubble.key, None)
            result.append(bubble)
        return result

    def absorb(self, source: str, destination: str, merged: str, now: float):
        """Animate `source` flowing into `destination`, which becomes `merged`."""
        moving, staying = self.bodies.pop(source, None), self.bodies.pop(destination, None)
        if staying is None:
            return
        if moving is not None:
            self.ghosts.append(Ghost(moving.model, moving.x, moving.y, moving.size, now, "absorb", staying.x, staying.y, 0.26))
        staying.scale.velocity += 4.5            # a soft "gulp" as it swallows the other
        self.bodies[merged] = staying
        self.ripples.append(Ripple(staying.x, staying.y, staying.size, now, staying.model.letter, 0.6, merged))
        self.dragging = self.target = self.press = None

    def burst(self, key: str, members: list[str], now: float):
        body = self.bodies.pop(key, None)
        if body is None:
            return
        self.ghosts.append(Ghost(body.model, body.x, body.y, body.size, now, "pop", duration=0.32))
        self.ripples.append(Ripple(body.x, body.y, body.size, now, body.model.letter, 0.7))
        offset = random.uniform(0, math.tau)
        for index, member in enumerate(members):
            angle = offset + index * math.tau / max(1, len(members))
            self.origins[member] = (body.x + math.cos(angle) * 6, body.y + math.sin(angle) * 6, angle)

    # ----- simulation ------------------------------------------------------
    def step(self, now: float, pointer: tuple[float, float] | None, left: bool, right: bool) -> list[tuple]:
        dt = 1 / 60 if self.last is None else min(1 / 20, max(0.0, now - self.last))
        self.last, self.time = now, self.time + dt
        events: list[tuple] = []
        under = self.body_at(*pointer) if pointer else None
        gear = not under and self.over_gear(pointer)
        banner = not under and over_banner(self.model, pointer)
        if left and not self.left and gear:
            self.gear_press = True
        if left and not self.left and banner:
            self.banner_press = True
        if not left and self.left and self.banner_press:
            if banner and not self.dragging:
                events.append(("settings", "iq"))    # the banner opens the model IQ / reset details
            self.banner_press = False
        if not left and self.left and self.gear_press:
            if gear and not self.dragging:
                events.append(("settings",))
            self.gear_press = False
        if left and not self.left and under:
            body = self.bodies[under]
            self.press = (under, pointer[0] - body.x, pointer[1] - body.y, *pointer)
        if left and self.press and pointer and not self.dragging and self.press[0] in self.bodies:
            if math.hypot(pointer[0] - self.press[3], pointer[1] - self.press[4]) > 4:
                self.dragging = self.press[0]
        if self.dragging and self.dragging in self.bodies and pointer:
            body = self.bodies[self.dragging]
            follow = 1 - math.exp(-dt * 32)
            x = body.x + (pointer[0] - self.press[1] - body.x) * follow
            y = body.y + (pointer[1] - self.press[2] - body.y) * follow
            self.target = None
            best = 1e9
            for key, other in self.bodies.items():
                if key == self.dragging:
                    continue
                distance = math.hypot(other.x - x, other.y - y)
                if distance < other.size * 0.95 + body.size * 0.4 and distance < best:
                    self.target, best = key, distance
            if self.target:         # gentle magnet toward the bubble it would join
                other = self.bodies[self.target]
                pull = min(1.0, dt * 6) * 0.35
                x, y = x + (other.x - x) * pull, y + (other.y - y) * pull
            body.place(x, y)
        if not left and self.left:
            if self.dragging and self.target:
                events.append(("merge", self.dragging, self.target))
            self.dragging = self.target = self.press = None
        if right and not self.right and under and len(self.bodies[under].model.members) > 1:
            events.append(("split", under))
        elif right and not self.right and pointer is not None and not self.dragging:
            events.append(("settings",))      # right-click anywhere else on the panel: settings
        self.left, self.right = left, right
        self.hovered = None if self.dragging else under

        self.gear.target = 1.0 if gear and not self.dragging else 0.0
        self.gear.step(dt)
        self.gear_angle = 24 * self.gear.value
        self.field.step(dt)
        if now - self.opened > 0.08:            # let the window fade in before the bars run
            for spring in self.meters:
                spring.step(dt)
        self.rate.step(dt)
        self.today_usage.step(dt)
        x0, y0, x1, y1 = self.bounds
        for key, body in self.bodies.items():
            # A new bubble on a new row waits until the growing field reaches its
            # slot; otherwise it pops in over the footer below the field.
            fits = body.py.target + body.model.radius + LABEL <= y1 + 2 or body.scale.value > 0.05
            body.scale.target = 1.0 if now >= body.born and fits else 0.0
            body.hover.target = 1.0 if key == self.hovered else 0.0
            body.glow.target = 1.0 if key == self.target else 0.0
            body.lift.target = 1.0 if key == self.dragging else 0.0
            for spring in (body.scale, body.radius, body.hover, body.glow, body.lift):
                spring.step(dt)
            if key == self.dragging:
                # Keep the picked-up bubble inside the field so it never leaves the window.
                r = body.radius.value
                body.place(min(max(body.x, x0 + r), x1 - r), min(max(body.y, y0 + r), max(y0 + r, y1 - r)))
                continue
            body.px.step(dt)
            body.py.step(dt)
        self.ghosts = [g for g in self.ghosts if now - g.start < g.duration]
        self.ripples = [r for r in self.ripples if now - r.start < r.duration]
        return events

    @property
    def animating(self) -> bool:
        """Something is moving: draw every frame until it rests."""
        return bool(self.dragging or self.ghosts or self.ripples or not self.field.settled or not self.rate.settled
                    or not self.gear.settled or not self.today_usage.settled or self.chart_progress < 1 or self.chart_zooming
                    or any(not spring.settled for spring in self.meters)
                    or any(not spring.settled for body in self.bodies.values() for spring in body.springs))

    @property
    def ambient(self) -> bool:
        """Slow looping decoration (pulse rings, orbiting members, spinners):
        worth a lower frame rate while nothing else moves."""
        model = self.model
        if model is None:
            return False
        return (model.status_tone == "live" or not model.bubbles
                or any(b.model.live or b.model.members or b.model.busy for b in self.bodies.values()))

    # ----- drawing ---------------------------------------------------------
    def render(self, scale: float) -> Image.Image:
        model = self.model
        self.dirty = False
        field_h = self.field.value
        frame = background(model.header, round(field_h * scale), scale).copy()
        origin = round(MARGIN * scale)

        def at(value):
            return origin + value * scale

        self._header(frame, model, field_h, scale, at)
        self._chart(frame, model, field_h, scale, at)
        self._hint(frame, model, field_h, scale, at)
        if not model.bubbles:
            blit(frame, empty_note(scale), at(WIDTH / 2), at(field_top(model) + field_h / 2))
        cx, cy = gear_center(model, field_h)
        level = self.gear.value
        tint = tuple(round(a + (b - a) * level) for a, b in zip(MUTED_TEXT[:3], accent(model.letter)[:3])) + (255,)
        # Just the glyph: no button plate over the footer; it lights up and turns a little under the pointer.
        sprite = gear(round(GEAR_RADIUS * scale), tint)
        if level > 0.02:
            sprite = sprite.rotate(-self.gear_angle, resample=Image.BICUBIC, expand=True)
        blit(frame, sprite, at(cx), at(cy))

        for ripple in self.ripples:
            p = (self.time_since(ripple.start)) / ripple.duration
            radius = ripple.r * (1 + 0.9 * ease_out_cubic(p))
            body = self.bodies.get(ripple.key)
            cx, cy = (body.x, body.y) if body else (ripple.x, ripple.y)
            blit(frame, faded(ring(round(radius * scale), max(1, round(2 * scale * (1 - p) + 1)), ripple.letter), 0.7 * (1 - p) ** 1.5),
                 at(cx), at(cy))
        for ghost in self.ghosts:
            p = min(1.0, self.time_since(ghost.start) / ghost.duration)
            e = ease_out_cubic(p)
            if ghost.kind == "absorb":
                x, y = ghost.x + (ghost.tx - ghost.x) * e, ghost.y + (ghost.ty - ghost.y) * e
                r, alpha = ghost.r * (1 - 0.7 * e), 1 - p ** 2
            elif ghost.kind == "pop":
                x, y, r, alpha = ghost.x, ghost.y, ghost.r * (1 + 0.35 * e), (1 - e) ** 1.4
            else:
                x, y, r, alpha = ghost.x, ghost.y, ghost.r * (1 - 0.4 * e), 1 - e
            self._bubble(frame, ghost.model, at(x), at(y), r, scale, alpha, label=False)
        order = sorted(self.bodies.items(), key=lambda item: (item[0] == self.dragging, item[0] == self.hovered))
        for key, body in order:
            x, y, r = at(body.x), at(body.y), body.size
            if r <= 0.5:
                continue
            m = body.model
            if m.live and key != self.dragging:
                period = 2.4
                p = ((self.time + body.phase) % period) / period
                blit(frame, faded(ring(round(r * (1 + 0.32 * ease_out_cubic(p)) * scale), max(1, round(1.4 * scale)), m.letter),
                                  0.5 * (1 - p) ** 2 * (1 if m.focus else 0.5)), x, y)
            if body.glow.value > 0.02:
                blit(frame, faded(halo(round((r + 4) * scale), m.letter), min(1.0, body.glow.value)), x, y)
            resting = body.scale.settled and body.hover.settled and body.glow.settled and body.lift.settled
            self._bubble(frame, m, x, y, r, scale, min(1.0, max(0.0, body.scale.value)), label=key != self.dragging,
                         expanded=body.hover.value > 0.5, crisp=resting)
            if m.members:
                count = min(6, len(m.members))
                for index, (letter, live) in enumerate(m.members[:count]):
                    angle = self.time * 0.55 + body.phase + index * math.tau / count
                    pulse = 1 + (0.25 * math.sin(self.time * 4 + index) if live else 0)
                    blit(frame, faded(dot(round(3.4 * scale * pulse), letter), 1.0 if live else 0.5),
                         x + math.cos(angle) * (r + 1) * scale, y + math.sin(angle) * (r + 1) * scale)
            if m.busy:
                spinner = arc(round((r + 3) * scale), m.letter)
                blit(frame, spinner.rotate(-self.time * 300 % 360, resample=Image.BICUBIC), x, y)
        return frame

    def _header(self, frame, model, field_h, scale, at):
        """Animated header: counting quota figures and bars, easing TPS, a sonar
        ping on the title dot while agents work, drifting shells when idle."""
        draw = ImageDraw.Draw(frame)
        meter_top = METER_TOP + header_offset(model)
        columns = meter_columns(len(self.meters) + (model.provider == "mimo"), model.provider != "gemini")
        for slot, ((x, end), spring, item) in enumerate(zip(columns, self.meters, model.quota[:3])):
            value = item[1]
            paid = model.tone_of(slot) == "paid"     # used up but covered by purchased credits: a full gold bar
            shade = PAID if paid else TEXT if value is None or value >= 50 or model.letter in UNIFORM_LETTERS else ring_color(value, model.letter)
            shown = None if value is None else spring.value
            override = model.figure_of(slot)
            if override:     # e.g. Codex at 0 %: the purchased credits left instead of "0 %"
                blit_at(frame, figure(override[0], override[1], shade, shade, scale), at(x), at(meter_top + 38))
            else:
                blit_at(frame, figure("—" if shown is None else f"{max(0.0, shown):.0f}", "" if shown is None else "%",
                                      shade, shade, scale), at(x), at(meter_top + 38))
            if paid:
                draw.rounded_rectangle((round(at(x)), round(at(meter_top + 45)), round(at(end)), round(at(meter_top + 49))),
                                       radius=max(1, round(2 * scale)), fill=PAID)
            elif shown and shown > 0.3:
                fill = quota_shade(model.letter, slot, value, pooled(model))
                draw.rounded_rectangle((round(at(x)), round(at(meter_top + 45)),
                                        round(at(x + max(4.0, (end - x) * min(100.0, shown) / 100))), round(at(meter_top + 49))),
                                       radius=max(1, round(2 * scale)), fill=fill)
        if model.provider == "mimo":
            number, unit = compact_usage(None if model.today_used is None else self.today_usage.value)
            blit_at(frame, figure(number, unit, accent(model.letter), SECONDARY_TEXT, scale),
                    at(columns[-2][0]), at(meter_top + 38))
        if model.provider != "gemini":
            x = columns[-1][0]
            color = accent(model.letter)
            rate = max(0.0, self.rate.value)
            blit_at(frame, figure(f"{rate:.0f}", "tok/s", color if model.tps and rate >= 0.5 else TEXT, SECONDARY_TEXT, scale),
                    at(x), at(meter_top + 38))
            if model.local_tps is not None:
                # The figure is the same on every synced device; this one's own share below.
                draw.text((at(x), at(meter_top + 55)), f"本机 {model.local_tps:.0f}", font=ui_font(max(1, round(10 * scale))),
                          fill=MUTED_TEXT, anchor="lt")
        if model.status_tone == "live":
            p = (self.time % 1.8) / 1.8
            blit(frame, faded(ring(round((4 + 7 * ease_out_cubic(p)) * scale), max(1, round(1.2 * scale)), model.letter),
                              0.8 * (1 - p) ** 1.6), at(PAD + 4), at(PAD + 9 + header_offset(model)))
        if not model.bubbles:
            top = field_top(model)
            for index, (dx, dy, radius) in enumerate(((-96, -6, 16), (92, 4, 12), (-58, 18, 8), (64, -20, 7))):
                t = self.time * (0.35 + 0.07 * index) + index * 1.9
                cx = WIDTH / 2 + dx + 5 * math.sin(t)
                cy = top + field_h / 2 + dy + 4 * math.cos(t * 1.3)
                blit(frame, faded(ring(round(radius * scale), max(1, round(scale)), model.letter), 0.11 + 0.05 * math.sin(t * 2)),
                     at(cx), at(cy))

    def time_since(self, moment: float) -> float:
        return max(0.0, (self.last or moment) - moment)

    CHART_DELAY, CHART_REVEAL = 0.12, 0.5

    @property
    def chart_progress(self) -> float:
        return ease_out_cubic((self.time_since(self.opened) - self.CHART_DELAY) / self.CHART_REVEAL)

    @property
    def chart_zooming(self) -> bool:
        """A dwell zoom is easing right now."""
        chart = self.model.burn if self.model is not None else None
        if chart is None:
            return False
        return dwell_easing((chart.now - chart.start) / 60, self.time_since(self.opened))

    def _chart(self, frame, model, field_h, scale, at):
        """Burn chart below the task field; the plot draws itself left to right
        on opening. The next wider beat starts in the same moment, whatever
        span opened (30 minutes, then 2 hours, then 5 hours). Each later beat
        waits out its short rest first."""
        chart = burn_dwell(model.burn, self.time_since(self.opened))
        colors = tuple(quota_shade(s.letter or model.letter, s.slot, s.points[-1][1] if s.points else None,
                                  s.pooled if s.letter else pooled(model))
                       for s in chart.series) if chart else ()
        sprite = burn_sprite(chart, colors, scale)
        left, top = round(at(PAD - 6)), round(at(chart_top(model, field_h)))
        label = round((CHART_LABEL + chart_extra(chart)) * scale)
        progress = self.chart_progress
        if progress >= 1:
            frame.alpha_composite(sprite, (left, top))
            return
        frame.alpha_composite(sprite.crop((0, 0, sprite.width, label)), (left, top))
        width = round(sprite.width * progress)
        if width > 0:
            frame.alpha_composite(sprite.crop((0, label, width, sprite.height)), (left, top + label))

    def _hint(self, frame, model, field_h, scale, at):
        """The shared MiMo/local hint as two marked rows, like LumaCenter's
        notes beside it: what is going on now (a dot in the source's colour,
        bright text) and an idea for next (a MiMo spark, dimmer text). Each
        row is one line, cut with an ellipsis."""
        draw = ImageDraw.Draw(frame)
        text_font = ui_font(round(HINT_FONT * scale))
        top = chart_top(model, field_h) + CHART_H + chart_extra_for(model) + 15
        now = model.hint_now.strip("。；！ ") or "正在整理可做的事项"
        idea = model.hint_next.strip("。；！ ")
        rows = [("now", now, TEXT, accent(model.letter))] + ([("next", idea, SECONDARY_TEXT, accent("M"))] if idea else [])
        limit = at(WIDTH - PAD) - at(PAD + HINT_TEXT_X)
        for index, (kind, text, ink, tone) in enumerate(rows):
            y = at(top + index * HINT_LINE)
            mark = hint_mark(kind, tone, scale)
            frame.alpha_composite(mark, (round(at(PAD + 2)), round(y - mark.height / 2)))
            draw.text((at(PAD + HINT_TEXT_X), y), forecast_text(text, limit, text_font, draw), font=text_font, fill=ink,
                      anchor="lm")

    def _bubble(self, frame, m: BubbleModel, x, y, r, scale, alpha, label=True, expanded=False, crisp=False):
        layout = round(m.radius * scale)
        drawn = max(1, round(r * scale))
        if crisp:
            # At rest (also hovered/picked up) draw the exact size, so the title stays sharp.
            base = bubble(m.title, m.letter, m.live, m.focus, layout, scale, drawn)
        else:
            # Mid-animation: resample the resting sprite (a fresh one per size would cost ms
            # per frame). Within a pixel of it, keep the sprite as is: alternating between
            # sharp and resampled frames while a spring settles reads as flicker.
            base = bubble(m.title, m.letter, m.live, m.focus, layout, scale, layout)
            if abs(drawn - layout) > 1:
                size = max(2, round(base.width * r * scale / layout))
                base = base.resize((size, size), Image.BILINEAR)
        blit(frame, faded(base, alpha), x, y)
        # The caption follows only the end of a pop-in, so a tiny bubble never carries a full-size label.
        text_alpha = min(1.0, max(0.0, (alpha - 0.6) / 0.35))
        if label and text_alpha > 0.02:
            text = m.detail if expanded and m.detail and m.detail != m.title else m.doing
            tag = caption(text, m.letter if m.live else "", round(m.radius * 2 + CAPTION_SPARE), expanded, scale)
            blit(frame, faded(tag, text_alpha * (1 if m.focus or expanded else 0.75)), x, y + (r + 4) * scale + tag.height / 2)


# ----- sprites -----------------------------------------------------------------

def blit(frame: Image.Image, sprite: Image.Image, cx: float, cy: float):
    """Alpha-composite `sprite` centred at (cx, cy), clipped to the frame."""
    left, top = round(cx - sprite.width / 2), round(cy - sprite.height / 2)
    crop = (max(0, -left), max(0, -top), min(sprite.width, frame.width - left), min(sprite.height, frame.height - top))
    if crop[2] <= crop[0] or crop[3] <= crop[1]:
        return
    piece = sprite if crop == (0, 0, sprite.width, sprite.height) else sprite.crop(crop)
    frame.alpha_composite(piece, (left + crop[0], top + crop[1]))


def blit_at(frame: Image.Image, sprite: tuple[Image.Image, int], left: float, baseline: float):
    """Composite a (sprite, baseline offset) pair with its left baseline at the point."""
    image, offset = sprite
    frame.alpha_composite(image, (max(0, round(left)), max(0, round(baseline) - offset)))


@lru_cache(maxsize=512)
def figure(number: str, unit: str, shade, unit_shade, scale: float) -> tuple[Image.Image, int]:
    """Big header figure with its unit, e.g. "72 %" or "85 tok/s"."""
    px = lambda value: round(value * scale)  # noqa: E731
    value_font, unit_font = figure_font(px(21)), ui_font(px(11), "bold")
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    width = probe.textlength(number, font=value_font) + (px(3) + probe.textlength(unit, font=unit_font) if unit else 0)
    baseline = px(24)
    image = Image.new("RGBA", (round(width) + px(4), px(30)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.text((0, baseline), number, font=value_font, fill=shade, anchor="ls")
    if unit:
        draw.text((probe.textlength(number, font=value_font) + px(3), baseline - px(1)), unit, font=unit_font, fill=unit_shade, anchor="ls")
    return image, baseline


@lru_cache(maxsize=64)
def _alpha_table(level: int):
    return [value * level // 255 for value in range(256)]


def faded(sprite: Image.Image, alpha: float) -> Image.Image:
    level = round(255 * min(1.0, max(0.0, alpha)))
    if level >= 255:
        return sprite
    out = sprite.copy()
    out.putalpha(sprite.getchannel("A").point(_alpha_table(level)))
    return out


def _with_alpha(color, alpha: int):
    return tuple(color[:3]) + (max(0, min(255, round(alpha))),)


@lru_cache(maxsize=1)
def _radial():
    return Image.radial_gradient("L")      # 0 at the centre, 181 at the inscribed circle


@lru_cache(maxsize=160)
def bubble(title: str, letter: str, live: bool, focus: bool, radius: int, scale: float, drawn: int | None = None) -> Image.Image:
    """Glass bubble: tinted rim-heavy fill, hairline rim, specular highlight, title.
    `radius` lays out the title; the glass is drawn at `drawn` (hover/lift
    enlarge the glass without reflowing the text)."""
    text_radius, radius = radius, drawn or radius
    pad = max(4, round(9 * scale))
    size = 2 * (radius + pad)
    color = accent(letter)
    strength = (1.0 if live else 0.62) * (1.0 if focus else 0.7)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((pad, pad + round(3 * scale), size - pad, size - pad + round(3 * scale)), fill=(0, 0, 0, round(95 * strength)))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, round(4 * scale)))))

    big = 2 * radius * SUPERSAMPLE
    lut = [round(255 * min(1.0, (0.16 + 0.66 * (v / 181) ** 3.0) * strength)) if v <= 181 else 0 for v in range(256)]
    fill_alpha = _radial().resize((big, big), Image.BILINEAR).point(lut)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    body = Image.new("RGBA", (big, big), tuple(color[:3]) + (0,))
    body.putalpha(ImageChops.multiply(fill_alpha, mask))
    draw = ImageDraw.Draw(body)
    rim = max(2, round(1.3 * scale * SUPERSAMPLE))
    draw.ellipse((rim // 2, rim // 2, big - 1 - rim // 2, big - 1 - rim // 2), outline=_with_alpha(color, 215 * strength), width=rim)
    gloss = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse((big * 0.2, big * 0.12, big * 0.56, big * 0.34), fill=(255, 255, 255, round(70 * strength)))
    body.alpha_composite(gloss.filter(ImageFilter.GaussianBlur(big * 0.035)))
    image.alpha_composite(body.resize((2 * radius, 2 * radius), Image.LANCZOS), (pad, pad))

    weight = "bold"
    face = ui_font(max(6, round((12.5 if text_radius > 34 * scale else 11.5) * scale)), weight)
    draw = ImageDraw.Draw(image)
    lines = _wrap(draw, title, face, text_radius * 1.52)
    if len(lines) > 2:
        lines = lines[:2]
        while lines[1] and draw.textlength(lines[1] + "…", font=face) > text_radius * 1.45:
            lines[1] = lines[1][:-1]
        lines[1] += "…"
    top, bottom = face.getbbox("国Ag")[1::2]
    step = (bottom - top) * 1.18
    start = size / 2 - step * (len(lines) - 1) / 2
    ink = _with_alpha(TEXT, 255 * (1.0 if live else 0.78) * (1.0 if focus else 0.85))
    for index, line in enumerate(lines):
        y = start + index * step
        draw.text((size / 2, y + max(1, round(scale))), line, font=face, fill=(0, 0, 0, 110), anchor="mm")
        draw.text((size / 2, y), line, font=face, fill=ink, anchor="mm")
    return image


@lru_cache(maxsize=128)
def caption(text: str, letter: str, max_width: int, expanded: bool, scale: float) -> Image.Image:
    face = ui_font(max(6, round(10.5 * scale)))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    limit = max_width * scale if not expanded else max(max_width, 120) * scale
    lines = _wrap(probe, text, face, limit)
    keep = 2 if expanded else 1
    if len(lines) > keep:
        lines = lines[:keep]
        while lines[-1] and probe.textlength(lines[-1] + "…", font=face) > limit:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    top, bottom = face.getbbox("国Ag")[1::2]
    step = round((bottom - top) * 1.35)
    width = max(2, round(max(probe.textlength(line, font=face) for line in lines)) + 4 + (round(10 * scale) if letter else 0))
    image = Image.new("RGBA", (width, step * len(lines) + 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if expanded:
        # Hovered: a wider detail line drawn above its neighbours on the panel colour.
        plate = Image.new("RGBA", (width + 2 * round(6 * scale), image.height + 2 * round(3 * scale)), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle((0, 0, plate.width - 1, plate.height - 1), radius=round(6 * scale),
                                                fill=tuple(c + 8 for c in SURFACE[:3]) + (245,), outline=(240, 238, 230, 24))
        plate.alpha_composite(image, (round(6 * scale), round(3 * scale)))
        image, draw = plate, ImageDraw.Draw(plate)
        offset = (round(6 * scale), round(3 * scale))
    else:
        offset = (0, 0)
    x = 2 + offset[0]
    if letter:
        d = round(5 * scale)
        draw.ellipse((x, offset[1] + step / 2 - d / 2, x + d, offset[1] + step / 2 + d / 2), fill=accent(letter))
        x += round(10 * scale)
    for index, line in enumerate(lines):
        draw.text((x, offset[1] + step * index + step / 2), line, font=face, fill=TEXT if expanded else SECONDARY_TEXT, anchor="lm")
    return image


@lru_cache(maxsize=512)
def ring(radius: int, stroke: int, letter: str) -> Image.Image:
    big = (radius + stroke + 1) * 2 * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    inset = SUPERSAMPLE * 1
    ImageDraw.Draw(image).ellipse((inset, inset, big - inset - 1, big - inset - 1), outline=accent(letter), width=stroke * SUPERSAMPLE)
    return image.resize((big // SUPERSAMPLE, big // SUPERSAMPLE), Image.LANCZOS)


@lru_cache(maxsize=64)
def halo(radius: int, letter: str) -> Image.Image:
    pad = max(4, radius // 3)
    size = 2 * (radius + pad)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((pad, pad, size - pad, size - pad), outline=_with_alpha(accent(letter), 255), width=max(3, radius // 6))
    glow = image.filter(ImageFilter.GaussianBlur(max(2, radius // 7)))
    glow.alpha_composite(glow)
    glow.alpha_composite(ring(radius, max(1, radius // 24), letter), ((size - ring(radius, max(1, radius // 24), letter).width) // 2,) * 2)
    return glow


@lru_cache(maxsize=64)
def dot(radius: int, letter: str) -> Image.Image:
    big = (radius + 2) * 2 * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    edge = 2 * SUPERSAMPLE
    draw.ellipse((edge - SUPERSAMPLE, edge - SUPERSAMPLE, big - edge + SUPERSAMPLE, big - edge + SUPERSAMPLE), fill=tuple(SURFACE[:3]) + (255,))
    draw.ellipse((edge, edge, big - edge, big - edge), fill=accent(letter))
    return image.resize((big // SUPERSAMPLE, big // SUPERSAMPLE), Image.LANCZOS)


@lru_cache(maxsize=32)
def arc(radius: int, letter: str) -> Image.Image:
    big = (radius + 2) * 2 * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = max(2, round(radius / 14)) * SUPERSAMPLE
    color = accent(letter)
    for step in range(24):                 # tapering tail
        start = -90 + step * 4
        draw.arc((stroke, stroke, big - stroke, big - stroke), start, start + 5, fill=_with_alpha(color, 30 + step * 9), width=stroke)
    return image.resize((big // SUPERSAMPLE, big // SUPERSAMPLE), Image.LANCZOS)


@lru_cache(maxsize=48)
def gear(radius: int, color: tuple) -> Image.Image:
    """Small solid cog: six rounded teeth around a body with a punched hub."""
    size = (radius + 2) * 2
    big = size * SUPERSAMPLE
    r = radius * SUPERSAMPLE
    c = big / 2
    shape = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(shape)
    body = r * .74
    draw.ellipse((c - body, c - body, c + body, c + body), fill=255)
    for index in range(6):
        angle = index * math.tau / 6
        tooth = []
        for offset, factor in ((-17, .7), (-11, 1), (11, 1), (17, .7)):
            a = angle + math.radians(offset)
            tooth.append((c + math.cos(a) * r * factor, c + math.sin(a) * r * factor))
        draw.polygon(tooth, fill=255)
    hole = r * .3
    draw.ellipse((c - hole, c - hole, c + hole, c + hole), fill=0)
    image = Image.new("RGBA", (big, big), tuple(color[:3]) + (0,))
    image.putalpha(shape.point(lambda value: value * (color[3] if len(color) > 3 else 255) // 255))
    return image.resize((size, size), Image.LANCZOS)


FORECAST_TONES = {"danger": CLAY, "warn": AMBER, "ok": SECONDARY_TEXT}
CHART_WIDTH = WIDTH - 2 * PAD + 12          # Same outer edges as the task field.


def _clock(moment: float, now: float) -> str:
    """"14:30" within the next/last day, otherwise "10-27 14:30"."""
    from datetime import datetime
    stamp = datetime.fromtimestamp(moment)
    same_day = abs(moment - now) < 20 * 3600 and stamp.date() == datetime.fromtimestamp(now).date()
    return stamp.strftime("%H:%M" if same_day else "%m-%d %H:%M")


BURN_SPANS = (10, 30, 60, 120, 300)      # minutes of history the chart may show
BURN_CHANGE = 3.0         # % of a quota a span must see used to be worth showing
BURN_HOLD = .6            # the span on screen stays while it still sees this share of that
BURN_FUTURE = .10         # the forecast strip takes about a tenth of the chart
BURN_MIN_FUTURE = 60      # retain one minute of visible dashed trend in close-up
BURN_TICKS_EVERY = {10: 2, 30: 5, 60: 15, 120: 30, 300: 60}   # span -> minutes between clock labels
# Hover zoom, three beats of the same motion: (target minutes, rest, ease).
# A beat already behind the opened span is skipped. The first beat still
# ahead starts at once — its rest is not waited — then each later beat
# keeps its rest: 30 min, 2 h, then 5 h.
BURN_DWELL_BEATS = ((30, 0.0, 1.2), (120, 2.5, 1.2), (300, 2.5, 1.2))


def smootherstep(t: float) -> float:
    """0..1 with zero speed and zero acceleration at both ends (Perlin's
    quintic), so a motion neither jolts into start nor snaps to rest."""
    t = min(1.0, max(0.0, t))
    return t * t * t * (t * (6 * t - 15) + 10)


def burn_view(chart: BurnChart, minutes: float) -> BurnChart:
    """The same chart showing `minutes` of history and room for the forecast."""
    back = minutes * 60
    return replace(chart, start=chart.now - back, end=chart.now + burn_future(back))


def burn_future(back: float) -> float:
    """Give a close-up enough future space without shrinking its history away."""
    return max(BURN_FUTURE * back, min(back, BURN_MIN_FUTURE))


IDLE_ALPHA = 110          # an idle source's line and legend swatch
LONE_TAG_ROOM = 112       # legend space kept for the tag of the one source in use


def _axis_values(pairs) -> list[list[float]]:
    """Values the %-axis is fitted to, from (series, history) pairs: the
    sources in use, or every source when none is."""
    live = [history for series, history in pairs if not series.idle]
    return [[value for _, value in history] for history in (live or [history for _, history in pairs])]


@lru_cache(maxsize=8)
def burn_axis(chart: BurnChart) -> "BurnScale":
    """The %-axis the chart would pick for itself (see burn_sprite)."""
    return burn_scale(_axis_values([(series, burn_points(series, chart)[0]) for series in chart.series]))


def _dwell_plan(base: float) -> list[tuple[float, float, float]]:
    """Beats still wider than `base` minutes. The first of them starts at
    once, whichever target that is; each later beat keeps its own rest."""
    plan = []
    for target, delay, ease in BURN_DWELL_BEATS:
        if base >= target - 1e-6:
            continue
        plan.append((float(target), 0.0 if not plan else float(delay), float(ease)))
    return plan


def dwell_easing(base: float, rested: float, slack: float = 0.05) -> bool:
    """True while a dwell beat is moving, plus a few frames after it lands
    so the settled view is drawn. `base` is the chart's own span in minutes.
    The first beat still ahead of `base` is already moving at rested 0."""
    for _target, delay, ease in _dwell_plan(base):
        end = delay + ease
        if delay <= rested <= end + slack:
            return True
        if rested < delay:
            return False
        rested -= end
    return False


def _eased_span(chart: BurnChart, origin: float, target: float, p: float) -> BurnChart:
    """Widen from `origin` minutes to `target` by progress `p`. The span grows
    by a constant ratio per step of the smootherstep curve, so the zoom looks
    even rather than rushing at first; the %-axis eases between the two
    views' own scales on the same curve, so it never jumps mid-zoom."""
    if p <= 0:
        return chart
    view = burn_view(chart, origin * (target / origin) ** p)
    if p >= 1:
        return view
    a, b = burn_axis(chart), burn_axis(burn_view(chart, target))
    axis = BurnScale(a.top + (b.top - a.top) * p, a.width * (b.width / a.width) ** p)
    return replace(view, axis=axis)


def burn_dwell(chart: BurnChart | None, rested: float) -> BurnChart | None:
    """The view after the pointer rested `rested` seconds. Each beat still
    wider than the opened span eases out to its target. The first of those
    starts at once; a chart already at 5 hours is unchanged."""
    if chart is None:
        return None
    base = (chart.now - chart.start) / 60
    for target, delay, ease in _dwell_plan(base):
        p = smootherstep((rested - delay) / ease) if ease else 1.0
        if p < 1:
            return _eased_span(chart, base, float(target), p)
        chart = burn_view(chart, target)
        base = target
        rested -= delay + ease
    return chart


def burn_time_ticks(chart: BurnChart) -> list[float]:
    """Round clock times inside the view, up to "now" (which has its own label)."""
    minutes = (chart.now - chart.start) / 60
    # Mid-zoom spans use the steps of the next span up, so labels do not reshuffle every frame.
    every = BURN_TICKS_EVERY[next((span for span in BURN_SPANS if span >= minutes - 1e-6), BURN_SPANS[-1])] * 60
    offset = time.localtime(chart.now).tm_gmtoff or 0
    stamp = math.ceil((chart.start + offset) / every) * every - offset
    ticks = []
    while stamp < chart.now - max(every * .3, 60):       # never the minute "now" already names
        ticks.append(float(stamp))
        stamp += every
    return ticks


def burn_used(points, start: float, end: float) -> float:
    """Percentage points used between start and end: the drops only (a reset
    stepping back up to 100% is not use), each prorated by how much of the
    time between its two readings falls inside the span."""
    used = 0.0
    for (t0, r0), (t1, r1) in zip(points, points[1:]):
        if r1 >= r0 or t1 < start or t0 > end:
            continue
        if t1 <= t0:
            used += r0 - r1 if start <= t1 <= end else 0.0
        else:
            used += (r0 - r1) * max(0.0, min(end, t1) - max(start, t0)) / (t1 - t0)
    return used


def burn_span(curves, now: float, previous: int | None = None) -> int:
    """Minutes of history to show: the shortest span in which some quota
    dropped by BURN_CHANGE, so heavy use gets a close-up of its slope (5 or 10
    minutes) and slow use a longer view; with no clear use, everything
    recorded, up to 5 hours. The span on screen is kept while it still shows
    most of that change, so the axis does not flick between neighbours."""
    def shows(minutes, need):
        return any(burn_used(points, now - minutes * 60, now) >= need for points in curves)
    best = next((minutes for minutes in BURN_SPANS if shows(minutes, BURN_CHANGE)), None)
    if best is None:
        first = min((points[0][0] for points in curves if points), default=now)
        best = next((minutes for minutes in BURN_SPANS if minutes * 60 >= now - first), BURN_SPANS[-1])
    if (previous in BURN_SPANS and previous != best and abs(BURN_SPANS.index(previous) - BURN_SPANS.index(best)) == 1
            and shows(previous, BURN_CHANGE * BURN_HOLD)):
        return previous
    return best


def burn_points(series: BurnSeries, chart: BurnChart) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float | None]:
    """(history, projection, exhausts_at) of one series in data space. The
    history enters at the left edge at the level it had then; the projection
    runs from the last reading at `rate` until the quota reaches zero, the
    window resets, or the view ends."""
    history = [(t, max(0.0, min(100.0, r))) for t, r in series.points if chart.start <= t <= chart.end]
    earlier = [(t, max(0.0, min(100.0, r))) for t, r in series.points if t < chart.start]
    if earlier and (not history or history[0][0] > chart.start):
        t0, r0 = earlier[-1]
        if history:
            t1, r1 = history[0]
            r0 = r0 + (r1 - r0) * (chart.start - t0) / max(1e-9, t1 - t0)
        history.insert(0, (chart.start, r0))
    if not history or not series.rate or series.rate <= 0:
        return history, [], None
    t0, r0 = history[-1]            # from the last reading, so no gap opens before "now"
    exhausts = t0 + r0 / series.rate * 3600
    limit = min(chart.end, series.reset_at or chart.end)
    stop = min(limit, exhausts)
    seen = exhausts if series.reset_at is None or exhausts <= series.reset_at else None
    if stop <= t0:
        return history, [], seen
    return history, [(t0, r0), (stop, max(0.0, r0 - series.rate * (stop - t0) / 3600))], seen


BURN_TOPS = (1, 2, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 98, 99, 100)
BURN_TICKS = (99.5, 99, 98, 95, 90, 85, 80, 75, 70, 60, 50, 40, 30, 20, 10, 5, 2, 1)
BURN_MIN_SPREAD = 1.0     # % points: flatter series are readings, not trends to magnify
BURN_BAND = .6            # share of the plot a trend may take; the rest keeps 0% in sight
BURN_TICK_GAP = .28       # of plot height, so 8 px labels never touch


@dataclass(frozen=True)
class BurnScale:
    """Remaining-% axis: `top` at the upper edge, 0% always at the baseline.

    Depth below the top maps like matplotlib's asinh/symlog scales: about
    linear within `width` points of the top and logarithmic beyond, so a few
    points of consumption stay readable while the exhaustion line stays in view.
    """
    top: float = 100.0
    width: float = 100.0

    def position(self, remaining: float) -> float:
        """0 at the top edge, 1 at 0% left."""
        depth = self.top - max(0.0, min(self.top, remaining))
        return math.log1p(depth / self.width) / math.log1p(self.top / self.width)


def burn_scale(series_values) -> BurnScale:
    """Fit the axis to the readings in view, one list of values per series.

    The top is the next round level above the highest reading. The width is
    chosen so that the series with the least room still gets as much height as
    possible (up to BURN_BAND); among equal fits the most linear one wins,
    and no series is bent harder than its own spread warrants.
    """
    series_values = [list(values) for values in series_values if values]
    if not series_values:
        return BurnScale()
    highest = max(max(values) for values in series_values)
    top = float(next((level for level in BURN_TOPS if level >= highest - 1e-9), 100))
    trends = [(min(values), max(values)) for values in series_values if max(values) - min(values) >= BURN_MIN_SPREAD]
    if not trends:
        return BurnScale(top, top)
    floor = max(.05, min(high - low for low, high in trends) / 3)
    best, chosen = -1.0, top * 50
    for step in range(64):
        width = top * 50 * (floor / (top * 50)) ** (step / 63)    # most linear first
        scale = BurnScale(top, width)
        fit = min(BURN_BAND, min(scale.position(low) - scale.position(high) for low, high in trends))
        if fit > best + 1e-3:
            best, chosen = fit, width
    return BurnScale(top, chosen)


def burn_ticks(scale: BurnScale, values=()) -> tuple[float, ...]:
    """Top and 0% always; round levels between them, nearest the data first."""
    ticks = [scale.top, 0.0]
    values = list(values)
    middle = (sum(scale.position(value) for value in values) / len(values)) if values else .5
    candidates = sorted((level for level in BURN_TICKS if 0 < level < scale.top),
                        key=lambda level: abs(scale.position(level) - middle))
    for level in candidates:
        if len(ticks) >= 4:
            break
        if all(abs(scale.position(level) - scale.position(tick)) >= BURN_TICK_GAP for tick in ticks):
            ticks.append(level)
    return tuple(sorted(ticks, reverse=True))


def burn_projection_curve(projection: list, steps: int = 32) -> list:
    if len(projection) < 2:
        return projection
    (t0, r0), (t1, r1) = projection[0], projection[-1]
    return [(t0 + (t1-t0)*index/steps, r0 + (r1-r0)*index/steps) for index in range(steps+1)]


def _dashed_curve(pen, points, color, width, dash, gap):
    """Continuous dash phase along a sampled nonlinear projection."""
    travelled, period = 0.0, dash + gap
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1-x0, y1-y0)
        if length <= 0:
            continue
        position = math.floor(travelled / period) * period
        while position < travelled + length:
            start, end = max(0.0, position-travelled), min(length, position+dash-travelled)
            if end > start:
                a, b = start/length, end/length
                pen.line((x0+(x1-x0)*a, y0+(y1-y0)*a, x0+(x1-x0)*b, y0+(y1-y0)*b), fill=color, width=width)
            position += period
        travelled += length


@lru_cache(maxsize=16)
def burn_legend(chart: BurnChart | None) -> tuple:
    """Wrap source/window labels without shrinking the plot or clipping legends.
    Labels run from the fullest current quota to the lowest; a tie keeps
    the chart's own order. Each entry still carries its series index so the
    swatch matches that line."""
    if chart is None:
        return ()
    face = ui_font(10)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x, row, result = 10.0, 0, []
    single = len(chart.series) == 1
    # Sources in use first, fullest first; idle ones after them under a short
    # name, only while they fit on the row in hand: their faint lines need no
    # second legend row.
    ranked = sorted(enumerate(chart.series), key=lambda item: (item[1].idle, -(item[1].points[-1][1] if item[1].points else float("-inf"))))
    # With one source in use, its "runs out at" / reset tag sits at the row's end.
    room = CHART_WIDTH - 38 - (LONE_TAG_ROOM if sum(not s.idle for s in chart.series) == 1 else 0)
    for index, series in ranked:
        text = f"{series.label} · 剩余额度" if single else (short_label(series.label) if series.idle else series.label)
        width = probe.textlength(text, font=face) + (0 if single else 8)
        if series.idle and x + width > room:
            continue
        if x > 10 and x + width > CHART_WIDTH - 38:
            x, row = 10.0, row + 1
        result.append((index, x, row, text))
        x += width + 12
    return tuple(result)


def short_label(label: str) -> str:
    """"Codex · 5 小时" -> "Codex"; "Gemini · Argon" -> "Argon"."""
    parts = label.split(" · ")
    return parts[1] if parts[0] == "Gemini" and len(parts) > 1 else parts[0]


def chart_extra(chart: BurnChart | None) -> float:
    legend = burn_legend(chart)
    return legend[-1][2] * CHART_LABEL if legend else 0


@lru_cache(maxsize=8)
def burn_sprite(chart: BurnChart | None, colors: tuple, scale: float) -> Image.Image:
    """Burn-down chart: remaining % of every model, each in the colour of its
    header bar. The forecast is dashed, resets are faint markers."""
    px = lambda value: round(value * scale)  # noqa: E731
    extra = chart_extra(chart)
    width, height = px(CHART_WIDTH), px(CHART_H + extra)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    small = ui_font(px(10))
    if chart is None or not chart.series:
        draw.text((width / 2, height / 2), "暂无额度读数", font=small, fill=MUTED_TEXT, anchor="mm")
        return image
    span = max(1.0, chart.end - chart.start)
    # Label row: a legend (one entry per line) and the next reset in view.
    single = len(chart.series) == 1
    last_x = 0
    for index, start_x, legend_row, text in burn_legend(chart):
        idle = chart.series[index].idle
        color = _with_alpha(colors[index], IDLE_ALPHA) if idle else colors[index]
        x, center_y = px(start_x), px(CHART_LABEL / 2 + 1 + legend_row * CHART_LABEL)
        if not single:
            draw.ellipse((x, center_y - px(2.5), x + px(5), center_y + px(2.5)), fill=color)
            x += px(8)
        draw.text((x, center_y), text, font=small, fill=MUTED_TEXT if single or idle else color, anchor="lm")
        last_x = x + draw.textlength(text, font=small) + px(12)
    # Idle sources are context only: no reset marker, forecast or exhaustion tag.
    resets = sorted(s.reset_at for s in chart.series if s.reset_at and not s.idle)
    drawn = [(series, color, *burn_points(series, chart)) for series, color in zip(chart.series, colors)]
    axis = chart.axis or burn_scale(_axis_values([(series, history) for series, _, history, _, _ in drawn]))
    ticks = burn_ticks(axis, (value for values in _axis_values([(series, history) for series, _, history, _, _ in drawn])
                              for value in values))
    exhausting = [(exhausts, series, color) for series, color, _, _, exhausts in drawn
                  if exhausts is not None and not series.idle]
    first = min(exhausting, key=lambda item: item[0]) if exhausting else None
    row = px(CHART_LABEL / 2 + 1)
    tag_right = width - px(10)
    lone = sum(not s.idle for s in chart.series) == 1     # idle lines are context, not rivals for the tag
    if first is not None:
        # The earliest exhaustion is named in the label row, where no line runs;
        # the reset stays visible as its blue marker.
        tag = f"约 {_clock(first[0], chart.now)} 用完"
        if lone and tag_right - draw.textlength(tag, font=small) > last_x:
            draw.text((tag_right, row), tag, font=small, fill=first[2], anchor="rm")
    elif resets:
        tag = f"重置 {_clock(resets[0], chart.now)}"
        if lone and tag_right - draw.textlength(tag, font=small) > last_x:
            draw.text((tag_right, row), tag, font=small, fill=MUTED_TEXT, anchor="rm")

    k = SUPERSAMPLE
    left, top, right, bottom = 34, CHART_LABEL + extra + 4, CHART_WIDTH - 10, CHART_H + extra - 6 - CHART_AXIS
    big = Image.new("RGBA", (width * k, height * k), (0, 0, 0, 0))
    pen = ImageDraw.Draw(big)
    X = lambda t: (left + (right - left) * (t - chart.start) / span) * scale * k  # noqa: E731
    Y = lambda r: (top + (bottom - top) * axis.position(r)) * scale * k  # noqa: E731
    hair = max(1, round(scale * k))
    for level in ticks:
        pen.line((X(chart.start), Y(level), X(chart.end), Y(level)), fill=(240, 238, 230, 34 if level == 0 else 16), width=hair)
    for reset in set(resets):
        if chart.start <= reset <= chart.end:
            for y in range(round(top*scale*k), round(bottom*scale*k), 4*k):
                pen.line((X(reset),y,X(reset),min(bottom*scale*k,y+2*k)), fill=(240,238,230,70), width=hair)
    # Fills first, only under sources in use (lighter when several overlap),
    # then every line above all fills; idle lines thin and faint.
    filled = sum(1 for series, *_ in drawn if not series.idle)
    for series, color, history, _, _ in drawn:
        if len(history) >= 2 and not series.idle:
            line = [(X(t), Y(r)) for t, r in history]
            area = Image.new("L", big.size, 0)
            baseline = bottom*scale*k
            ImageDraw.Draw(area).polygon(line + [(line[-1][0], baseline), (line[0][0], baseline)], fill=46 if filled == 1 else 22)
            fill = Image.new("RGBA", big.size, tuple(color[:3]) + (0,))
            fill.putalpha(area)
            big.alpha_composite(fill)
    for series, color, history, projection, _ in sorted(drawn, key=lambda item: not item[0].idle):
        if len(history) >= 2:
            if series.idle:
                pen.line([(X(t), Y(r)) for t, r in history], fill=_with_alpha(color, IDLE_ALPHA),
                         width=max(1, round(scale * k)), joint="curve")
            else:
                pen.line([(X(t), Y(r)) for t, r in history], fill=color, width=max(2, round(1.6 * scale * k)), joint="curve")
        if projection and not series.idle:
            _dashed_curve(pen, [(X(t), Y(r)) for t, r in burn_projection_curve(projection)],
                          _with_alpha(color, 200), max(2, round(1.3 * scale * k)), 5 * scale * k, 4 * scale * k)
    image.alpha_composite(big.resize((width, height), Image.LANCZOS))
    draw = ImageDraw.Draw(image)
    for level in ticks:
        # Mid-zoom the top edge sits between two round levels: name it to the whole percent.
        text = f"{level:g}%" if level in BURN_TICKS or level in BURN_TOPS or not level else f"{round(level)}%"
        draw.text((px(left-5), Y(level)/k), text, font=ui_font(px(8)), fill=MUTED_TEXT, anchor="rm")
    if all(len(history) < 2 for _, _, history, _, _ in drawn):
        draw.text((width / 2, px((top + bottom) / 2)), "正在积累用量记录", font=small, fill=MUTED_TEXT, anchor="mm")
    _burn_axis(draw, chart, X, k, px, left, right, bottom, scale)
    base, ceiling = px(bottom), px(top)
    if first is not None and chart.start <= first[0] <= chart.end:
        ex, color = X(first[0]) / k, first[2]
        for y in range(round(ceiling), round(base), max(2, px(4))):
            draw.line((ex, y, ex, min(base, y + px(2))), fill=_with_alpha(color, 140), width=max(1, round(scale)))
    return image


def _burn_axis(draw, chart: BurnChart, X, k, px, left, right, bottom, scale) -> None:
    """Clock row: round times at even steps (every minute in a 5-minute view,
    every hour in a 5-hour one) and "now" at its mark, brighter."""
    face = ui_font(px(8))
    y = px(bottom + 3 + CHART_AXIS / 2)
    hair = max(1, round(scale))
    now_x = X(min(chart.now, chart.end)) / k
    now_text = _clock(chart.now, chart.now)
    now_w = draw.textlength(now_text, font=face)
    now_left = min(now_x - now_w / 2, px(right) - now_w)
    draw.line((now_x, px(bottom), now_x, px(bottom + 2.5)), fill=_with_alpha(SECONDARY_TEXT, 160), width=hair)
    draw.text((now_left, y), now_text, font=face, fill=SECONDARY_TEXT, anchor="lm")
    edge = px(left) - px(10)                 # labels may reach a little under the % column
    for stamp in burn_time_ticks(chart):
        x = X(stamp) / k
        text = _clock(stamp, chart.now)
        half = draw.textlength(text, font=face) / 2
        if x - half < edge or x + half > now_left - px(6):
            continue
        draw.line((x, px(bottom), x, px(bottom + 2)), fill=_with_alpha(MUTED_TEXT, 120), width=hair)
        draw.text((x, y), text, font=face, fill=MUTED_TEXT, anchor="mm")
        edge = x + half + px(6)


@lru_cache(maxsize=16)
def hint_mark(kind: str, color: tuple, scale: float) -> Image.Image:
    """The 12 px mark in front of a hint row: "now" a filled dot, "next" a
    four-point spark; drawn large and scaled down so both stay smooth."""
    size = max(1, round(12 * scale))
    k = SUPERSAMPLE
    image = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    c = size * k / 2
    if kind == "now":
        r = 4 * scale * k
        draw.ellipse((c - r, c - r, c + r, c + r), fill=color)
    else:
        outer, inner = 5.6 * scale * k, 1.5 * scale * k
        draw.polygon([(c + (outer if i % 2 == 0 else inner) * math.cos(-math.pi / 2 + i * math.pi / 4),
                       c + (outer if i % 2 == 0 else inner) * math.sin(-math.pi / 2 + i * math.pi / 4)) for i in range(8)],
                     fill=color)
    return image.resize((size, size), Image.LANCZOS)


def forecast_text(text: str, limit: float, face, probe) -> str:
    """Fit the forecast line before the gear, ending with an ellipsis if cut."""
    if probe.textlength(text, font=face) <= limit:
        return text
    while text and probe.textlength(text + "…", font=face) > limit:
        text = text[:-1]
    return text + "…"


FORECAST_BREAKS = "；，。、 "


def forecast_lines(text: str, limit: float, face, probe) -> list[str]:
    lines = _wrap(probe, text, face, limit)
    if len(lines) > 1:
        # Break after punctuation or a space rather than inside a name ("Claude/GP" + "T").
        cut = max(lines[0].rfind(mark) for mark in FORECAST_BREAKS)
        if cut >= len(lines[0]) // 2:
            lines = [lines[0][:cut + 1].rstrip()] + _wrap(probe, text[cut + 1:].lstrip(), face, limit)
    if len(lines) > 2:
        lines = [lines[0], forecast_text("".join(lines[1:]), limit, face, probe)]
    return lines


def _trend(draw, x, cy, s, color, stroke):
    """Small rising-line glyph in front of the forecast."""
    points = [(x, cy + 4 * s), (x + 4 * s, cy), (x + 7 * s, cy + 2.5 * s), (x + 11 * s, cy - 3 * s)]
    draw.line(points, fill=color, width=stroke, joint="curve")
    draw.polygon([(x + 11 * s, cy - 5 * s), (x + 12.5 * s, cy - 1.2 * s), (x + 8.6 * s, cy - 2.6 * s)], fill=color)


PILL_TONES = {"stale": ((229, 168, 75, 46), (229, 168, 75, 255)), "error": ((226, 92, 92, 46), (226, 92, 92, 255)),
              "paid": (PAID[:3] + (46,), PAID), "blocked": ((226, 92, 92, 46), (226, 92, 92, 255)),
              "idle": ((240, 238, 230, 20), SECONDARY_TEXT)}


def compact_usage(value: float | None) -> tuple[str, str]:
    """Three significant digits, with rounding promoted at K/M/B boundaries."""
    if value is None or not math.isfinite(value):
        return "—", ""
    value = max(0.0, value)
    units = ("", "K", "M", "B")
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    places = 2 if index and value < 10 else 1 if index and value < 100 else 0
    if round(value, places) >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
        places = 2
    number = f"{value:.{places}f}"
    if places:
        number = number.rstrip("0").rstrip(".")
    return number, units[index]


REFERENCE_FIELD = 160       # Field height the background slices are cut from.
SLICE_INSET = 20            # Cut this far inside the field: past every rounded corner.

# macOS draws the card itself as Liquid Glass (mac_panel); the panel then
# renders only its content, on a transparent canvas of the same size so every
# position, hit test and margin stays as it is.
GLASS = False


def set_glass(on: bool) -> None:
    """Card drawn by the platform (True) or by us: shadow and dark surface (False)."""
    global GLASS
    if GLASS != bool(on):
        GLASS = bool(on)
        for cached in (_background, _slices, background):
            cached.cache_clear()


GLASS_RADIUS = 26.0          # the platform card's corner radius (mac_panel sets it on the glass view)
# The specular edge of the glass, drawn as LumaCenter's menu bar panel draws
# its GlassRim: a one-point hairline, white at 22 % along the top edge, 6 % at
# 15 % of the height, 3 % at the middle, 5 % at 85 % and 12 % along the bottom,
# light added to whatever is beneath; no sheen. The platform's own rim is
# about a pixel wide and a sixth brighter than the glass: too faint to read
# on a dark backdrop, so the card draws this one on top, at its final height
# (background), so the stops sit where they do on LumaCenter's card.
RIM_STOPS = ((0.0, 0.22), (0.15, 0.06), (0.5, 0.03), (0.85, 0.05), (1.0, 0.12))   # (fraction of the height, white)
RIM_WIDTH = 1.0                                                                     # points


def _rim_alpha(fraction: float) -> float:
    """The rim's white, 0-255, at `fraction` of the card's height: linear between RIM_STOPS."""
    for (start, low), (end, high) in zip(RIM_STOPS, RIM_STOPS[1:]):
        if fraction <= end:
            return 255 * (low + (high - low) * (fraction - start) / (end - start))
    return 255 * RIM_STOPS[-1][1]


def _column(height: int, value) -> Image.Image:
    """A vertical profile as an "L" column: value(row) -> 0-255."""
    return Image.frombytes("L", (1, height), bytes(max(0, min(255, round(value(y)))) for y in range(height)))


@lru_cache(maxsize=16)
def _rim(size: tuple[int, int], scale: float) -> Image.Image:
    """The hairline for a card of `size` pixels: white, alpha from RIM_STOPS."""
    width, height = size
    radius, stroke = round(GLASS_RADIUS * scale), max(1, round(RIM_WIDTH * scale))
    ring = Image.new("L", size, 0)
    ImageDraw.Draw(ring).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, outline=255, width=stroke)
    profile = _column(height, lambda y: _rim_alpha(y / max(1, height - 1)))
    rim = Image.new("RGBA", size, (255, 255, 255, 0))
    rim.putalpha(ImageChops.multiply(ring, profile.resize(size, Image.NEAREST)))
    return rim


def _card(content: Image.Image, scale: float) -> Image.Image:
    """The content on a transparent canvas with the card's margins; the
    platform draws the glass beneath and `background` adds the rim."""
    if not GLASS:
        return _compose(content, scale)
    margin = round(MARGIN * scale)
    image = Image.new("RGBA", (content.width + 2 * margin, content.height + 2 * margin), (0, 0, 0, 0))
    image.alpha_composite(content, (margin, margin))
    return image


@lru_cache(maxsize=12)
def _slices(header: tuple, scale: float) -> tuple[Image.Image, Image.Image, Image.Image, int]:
    """Top (header, resets, field top), one uniform field row, bottom (field
    bottom, forecast, shadow), and the rows between them at the reference
    height. Any field height is top + row stretched + bottom, so a resizing
    panel never re-renders or re-blurs its card."""
    full = _background(header, REFERENCE_FIELD, scale)
    model = _header_model(header)
    origin = round(MARGIN * scale)
    cut_top = origin + round((field_top(model) + SLICE_INSET) * scale)
    cut_bottom = origin + round((field_top(model) + REFERENCE_FIELD - SLICE_INSET) * scale)
    return (full.crop((0, 0, full.width, cut_top)), full.crop((0, cut_top, full.width, cut_top + 1)),
            full.crop((0, cut_bottom, full.width, full.height)), cut_bottom - cut_top)


@lru_cache(maxsize=4)
def background(header: tuple, field_px: int, scale: float) -> Image.Image:
    """The static card for a field `field_px` device pixels tall."""
    top, row, bottom, rows = _slices(header, scale)
    middle = max(1, rows + field_px - round(REFERENCE_FIELD * scale))
    image = Image.new("RGBA", (top.width, top.height + middle + bottom.height), (0, 0, 0, 0))
    image.paste(top, (0, 0))
    image.paste(row.resize((row.width, middle), Image.NEAREST), (0, top.height))
    image.paste(bottom, (0, top.height + middle))
    if GLASS:
        # The rim at the card's final height, so its stops are not stretched with the field.
        margin = round(MARGIN * scale)
        image.alpha_composite(_rim((image.width - 2 * margin, image.height - 2 * margin), scale), (margin, margin))
    return image


def _header_model(header: tuple) -> PanelModel:
    letter, title, status, tone, meters, (banner, alert, intel, note), forecast, daily, chart_padding, _iq, show_tps = header
    return PanelModel("mimo" if daily else "" if show_tps else "gemini", letter, title, status, tuple((label, None, reset) for label, reset, _ in meters), None, (),
                      tone, banner, forecast[0], forecast[1], banner_alert=alert,
                      intel=intel, intel_note=note, chart_padding=chart_padding,
                      quota_tones=tuple(column_tone for _, _, column_tone in meters))


@lru_cache(maxsize=8)
def empty_note(scale: float) -> Image.Image:
    face = ui_font(round(11 * scale))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    text = "暂无工作中的 agent"
    image = Image.new("RGBA", (round(probe.textlength(text, font=face)) + 4, round(18 * scale)), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((image.width / 2, image.height / 2), text, font=face, fill=MUTED_TEXT, anchor="mm")
    return image


INTEL_TONES = {"idle": MUTED_TEXT}         # stale scores fade


def _banner(draw, px, s, banner: str, alert: str, intel: tuple, note: str, letter: str = "") -> None:
    """Two separate pills on one row: the Codex reset chance (short) on the
    left, the models' coding scores on the right (the running model first, in
    the source's colour). An official announcement tints only the reset pill
    and adds a badge inside it."""
    right, mid = WIDTH - PAD, PAD + 8
    x0, y0, x1, y1 = banner_box()
    face, small, bold = ui_font(px(11), "bold"), ui_font(px(10)), ui_font(px(10), "bold")
    calm_fill, calm_edge, edge = (240, 238, 230, 8), (240, 238, 230, 22), max(1, round(s))
    x = PAD
    if banner:
        x += 10 + draw.textlength(banner, font=face) / s
        badge = ui_font(px(10), "bold")
        badge_w = draw.textlength(alert, font=badge) / s + 12 if alert else 0
        end = x + (6 + badge_w if alert else 0) + 2
        if alert:
            draw.rounded_rectangle((px(x0), px(y0), px(end + 6), px(y1)), radius=px(7), fill=_with_alpha(RESET_TINT, 64),
                                   outline=_with_alpha(RESET_TINT, 170), width=edge)
        else:
            draw.rounded_rectangle((px(x0), px(y0), px(end + 6), px(y1)), radius=px(7), fill=calm_fill, outline=calm_edge, width=edge)
        draw.ellipse((px(PAD), px(mid - 2.5), px(PAD + 5), px(mid + 2.5)), fill=RESET_TINT)
        draw.text((px(PAD + 10), px(mid)), banner, font=face, fill=TEXT if alert else SECONDARY_TEXT, anchor="lm")
        if alert:
            bx = x + 6
            draw.rounded_rectangle((px(bx), px(mid - 7), px(bx + badge_w), px(mid + 7)), radius=px(5), fill=RESET_TINT)
            draw.text((px(bx + badge_w / 2), px(mid)), alert, font=badge, fill=(28, 30, 36, 255), anchor="mm")
        x = end + 6 + 8                                  # pill edge, then the gap between the two pills
    # Right pill, right-aligned in order; when space is short the "智商" label
    # goes first, then models are dropped from the end.
    gap = 9

    def fit(room):
        chosen, width = [], 0.0
        for name, value, tone, *live in intel:
            need = (draw.textlength(name + " ", font=small) + draw.textlength(value, font=bold)) / s
            if width + need + (gap if chosen else 0) > room:
                break
            width += need + (gap if chosen else 0)
            chosen.append((name, value, tone, bool(live and live[0])))
        return chosen, width

    space = right + 1 - (x + 6)                          # inside the right pill's padding
    label_w = draw.textlength("智商", font=small) / s + 7
    pieces, total = fit(space - label_w)
    labelled = True
    if len(pieces) < len(intel):
        bare = fit(space)
        if len(bare[0]) > len(pieces):
            (pieces, total), labelled = bare, False
    if not pieces:
        if not note or draw.textlength(note, font=small) / s > space - label_w:
            return
        total = draw.textlength(note, font=small) / s
    start = right + 1 - total
    left_edge = start - (label_w if labelled or not pieces else 0) - 6
    draw.rounded_rectangle((px(left_edge), px(y0), px(x1), px(y1)), radius=px(7), fill=calm_fill, outline=calm_edge, width=edge)
    if labelled or not pieces:
        draw.text((px(start - 7), px(mid)), "智商", font=small, fill=MUTED_TEXT, anchor="rm")
    if not pieces:
        draw.text((px(start), px(mid)), note, font=small, fill=MUTED_TEXT, anchor="lm")
        return
    for name, value, tone, live in pieces:
        draw.text((px(start), px(mid)), name, font=small, fill=accent(letter) if live and letter else SECONDARY_TEXT, anchor="lm")
        start += draw.textlength(name + " ", font=small) / s
        draw.text((px(start), px(mid)), value, font=bold, fill=INTEL_TONES.get(tone, TEXT), anchor="lm")
        start += draw.textlength(value, font=bold) / s + gap


@lru_cache(maxsize=12)
def _background(header: tuple, field_h: int, scale: float) -> Image.Image:
    model = _header_model(header)
    letter, title, status, tone, meters, (banner, alert, intel, note), _, daily, _padding, column_iq, show_tps = header
    top = field_top(model)
    height = panel_height(model, field_h)
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    content = Image.new("RGBA", (px(WIDTH), px(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(content)
    color = accent(letter)
    right = WIDTH - PAD
    y = PAD + header_offset(model)
    draw.ellipse((px(PAD), px(y + 5), px(PAD + 8), px(y + 13)), fill=color)
    draw.text((px(PAD + 16), px(y + 9)), title, font=ui_font(px(14), "bold"), fill=TEXT, anchor="lm")
    pill = ui_font(px(11))
    width = draw.textlength(status, font=pill) / s + 18
    fill, ink = PILL_TONES.get(tone, (color, (31, 30, 28, 255)))
    draw.rounded_rectangle((px(right - width), px(y), px(right), px(y + 18)), radius=px(9), fill=fill)
    draw.text((px(right - width / 2), px(y + 9)), status, font=pill, fill=ink, anchor="mm")

    y = METER_TOP + header_offset(model)
    label_font, small = ui_font(px(11)), ui_font(px(10))
    columns = meter_columns(len(meters) + daily, show_tps)
    labels = tuple(meters) + ((("今日用量", "", ""),) if daily else ()) + (((show_tps, "", "tps"),) if show_tps else ())
    for (x, end), (label, reset, column_tone) in zip(columns, labels):
        draw.text((px(x), px(y)), label, font=label_font, fill=MUTED_TEXT, anchor="lt")
        if daily and label == "今日用量":
            if reset:
                draw.text((px(x), px(y + 51)), reset, font=small, fill=MUTED_TEXT, anchor="lt")
            continue
        if column_tone != "tps":
            draw.rounded_rectangle((px(x), px(y + 45), px(end), px(y + 49)), radius=px(2), fill=TRACK)
            if reset:
                ink = {"active": accent(letter), "paid": PAID, "blocked": CLAY}.get(column_tone, MUTED_TEXT)
                face = ui_font(px(10), "bold") if column_tone == "active" else small
                draw.text((px(x), px(y + 55)), reset, font=face, fill=ink, anchor="lt")
    if column_iq:
        slot = spare_slot(len(meters) + daily, show_tps)
        if slot:
            # Same label and figure as the other columns, and no bar: the slot
            # was the gap left so a single quota would not stretch.
            x, _end = slot
            label, value, caption, unit = (tuple(column_iq) + ("",))[:4]
            draw.text((px(x), px(y)), label, font=label_font, fill=MUTED_TEXT, anchor="lt")
            if caption:
                draw.text((px(x), px(y + 51)), caption, font=small, fill=MUTED_TEXT, anchor="lt")
            blit_at(content, figure(value or "—", unit, TEXT, TEXT, s), px(x), px(y + 38))
    if header_offset(model):
        _banner(draw, px, s, banner, alert, intel, note, letter)
    # On glass the sections are a faint lift with no outline (LumaCenter's
    # .primary.opacity(0.05)); on the drawn card, a hairline plate.
    plate = {"fill": (255, 255, 255, 13)} if GLASS else {"outline": (240, 238, 230, 14), "width": max(1, round(s))}
    draw.rounded_rectangle((px(PAD - 6), px(top - 6), px(right + 6), px(top + field_h + 4)), radius=px(12),
                           **({"fill": (240, 238, 230, 7)} | plate))
    chart_y = chart_top(model, field_h)
    plate = {"fill": (255, 255, 255, 13)} if GLASS else {"outline": (240, 238, 230, 12), "width": max(1, round(s))}
    draw.rounded_rectangle((px(PAD - 6), px(chart_y - 4), px(right + 6), px(chart_y + CHART_H + chart_extra_for(model) + 2)), radius=px(12 if GLASS else 10),
                           **({"fill": (240, 238, 230, 6)} | plate))
    middle = footer_middle(model, field_h)
    tint = FORECAST_TONES.get(model.forecast_tone, MUTED_TEXT)
    _trend(draw, px(PAD), px(middle), s, tint, max(1, round(s)))
    face = ui_font(px(11))
    lines = forecast_lines(model.forecast or "正在积累用量，稍后给出预测", px(WIDTH - 2 * PAD - 48), face, draw)
    for index, line in enumerate(lines):
        draw.text((px(PAD + 18), px(middle + (index - (len(lines) - 1) / 2) * 17)), line, font=face, fill=tint, anchor="lm")
    return _card(content, scale)
