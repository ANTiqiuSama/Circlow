"""macOS agent panel: the Windows agent panel (agent_panel.Scene) shown in a
borderless, non-activating NSPanel under its menu bar item.

A click on a source's status item opens its panel; nothing opens on hover. A
second click on the item, a mouse press anywhere off the card (seen by
mouse-down monitors, in this app and in others), or a status menu opening
closes it at once, with no fade-out. While a panel is shown a timer on the
AppKit main thread polls the pointer and mouse buttons, steps the scene with
that input and presents a frame only when something moved; neither the timer
nor the monitors exist while no panel is shown. The panel never takes focus.

Geometry is kept in screen points with Cocoa's bottom-left origin; the scene
works in panel points with a top-left origin (its logical units), so the
conversions below only flip y and subtract the shadow margin.
"""
from __future__ import annotations

import logging
import time

import agent_panel
from detail_card import MARGIN

log = logging.getLogger("Circlow")

GAP = 6                     # menu bar bottom to the card's top edge, in points
CARD_SLACK = 2              # points around the visible card that still count as on it


def contains(frame, point, slack: float = 0.0) -> bool:
    """frame: ((x, y), (w, h)) in screen points; point: (x, y)."""
    (x, y), (w, h) = frame
    return x - slack <= point[0] < x + w + slack and y - slack <= point[1] < y + h + slack


def panel_origin(anchor, size, visible) -> tuple[float, float]:
    """Bottom-left of a panel of `size` (points, shadow margin included)
    hanging under the status item `anchor`, centred on it and kept inside the
    screen's visible frame. The transparent margin overlaps the gap."""
    (ax, ay), (aw, _ah) = anchor
    (vx, vy), (vw, _vh) = visible
    width, height = size
    x = ax + aw / 2 - width / 2
    x = max(vx, min(x, vx + vw - width))
    top = ay - GAP + MARGIN
    return x, max(vy, top - height)


def on_card(point, frame) -> bool:
    """On the visible card: the panel frame minus its transparent shadow
    margin. A mouse press anywhere else closes the panel."""
    if point is None or frame is None:
        return False
    (x, y), (w, h) = frame
    return contains(((x + MARGIN, y + MARGIN), (w - 2 * MARGIN, h - 2 * MARGIN)), point, CARD_SLACK)


def local_point(frame, point) -> tuple[float, float]:
    """Screen point -> scene coordinates (top-left origin, inside the margin)."""
    (x, y), (_w, h) = frame
    return point[0] - x - MARGIN, y + h - point[1] - MARGIN


def event_point(event) -> tuple[float, float]:
    """Where a mouse event happened, in screen points. An event with no
    window (one seen by a global monitor) is already in screen coordinates."""
    location, window = event.locationInWindow(), event.window()
    if window is not None:
        location = window.convertPointToScreen_(location)
    return float(location.x), float(location.y)


def to_nsimage(image, scale: float):
    """PIL RGBA -> NSImage at `scale` pixels per point (about 1 ms at 2x)."""
    from AppKit import NSBitmapImageRep, NSDeviceRGBColorSpace, NSImage
    width, height = image.size
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bitmapFormat_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, NSDeviceRGBColorSpace,
        2,                      # NSBitmapFormatAlphaNonpremultiplied: PIL's own layout
        width * 4, 32)
    memoryview(rep.bitmapData())[:] = image.convert("RGBA").tobytes()
    native = NSImage.alloc().initWithSize_((width / scale, height / scale))
    native.addRepresentation_(rep)
    return native


# Dark, see-through glass, the recipe of LumaCenter's menu bar panel: the
# clear style (the regular style's frosted material lifts the card to a mid
# grey, about 51/255 over a dark window, whatever the tint) in the dark
# appearance, with a black tint at 30 %, so the blurred backdrop shows
# through the card. The light figures read on any wallpaper; the drawn
# highlights (agent_panel) give the glass its edges. Corners: the softer
# 26 pt of macOS 26.
GLASS_TINT = (0, 0, 0, 0.30)
GLASS_STYLE = "clear"
GLASS_RADIUS = agent_panel.GLASS_RADIUS      # the content draws its highlights to the same corner


def glass_available() -> bool:
    import os
    if os.environ.get("CIRCLOW_GLASS", "1").strip().lower() in ("0", "false", "off", "no"):
        return False
    try:
        import objc
        objc.lookUpClass("NSGlassEffectView")
        return True
    except Exception:
        return False


def make_glass():
    """macOS 26 Liquid Glass for the panel card (NSGlassEffectView): the
    GLASS_STYLE, GLASS_TINT and GLASS_RADIUS above; None where the system
    has no glass, and the panel draws its own card instead."""
    if not glass_available():
        return None
    import objc
    import AppKit
    from AppKit import NSColor
    glass = objc.lookUpClass("NSGlassEffectView").alloc().initWithFrame_(((MARGIN, MARGIN), (10, 10)))
    glass.setCornerRadius_(GLASS_RADIUS)
    glass.setStyle_(getattr(AppKit, "NSGlassEffectViewStyleClear", 1) if GLASS_STYLE == "clear"
                    else getattr(AppKit, "NSGlassEffectViewStyleRegular", 0))
    r, g, b, alpha = GLASS_TINT
    glass.setTintColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, alpha))
    return glass


_PANEL_CLASS = None


def panel_class():
    """NSPanel that keeps the frame it is given. AppKit otherwise pushes a
    window below the menu bar, and the panel's transparent shadow margin
    (which overlaps the bar by design) would move the card down 8 points."""
    global _PANEL_CLASS
    if _PANEL_CLASS is None:
        from AppKit import NSPanel

        class CirclowPanelWindow(NSPanel):
            def constrainFrameRect_toScreen_(self, rect, screen):
                return rect

            def canBecomeKeyWindow(self):
                return False

        _PANEL_CLASS = CirclowPanelWindow
    return _PANEL_CLASS


class PanelWindow:
    """One NSPanel reused for every source; the scene is rebuilt on open."""

    OPEN_SECONDS = 0.2
    AMBIENT_SECONDS = 1 / 30 - 0.004

    def __init__(self):
        from AppKit import (NSBackingStoreBuffered, NSColor, NSImageView,
                            NSWindowCollectionBehaviorCanJoinAllSpaces,
                            NSWindowCollectionBehaviorFullScreenAuxiliary,
                            NSWindowCollectionBehaviorIgnoresCycle,
                            NSWindowCollectionBehaviorStationary,
                            NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel)
        panel = panel_class().alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (10, 10)), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setLevel_(25)                      # NSStatusWindowLevel: above normal and floating windows
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setIgnoresMouseEvents_(False)      # clicks land here, not on the window below
        panel.setReleasedWhenClosed_(False)
        panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorStationary
                                     | NSWindowCollectionBehaviorIgnoresCycle
                                     | NSWindowCollectionBehaviorFullScreenAuxiliary)
        view = NSImageView.alloc().initWithFrame_(((0, 0), (10, 10)))
        view.setImageScaling_(1)                 # NSImageScaleAxesIndependently: fill the frame exactly
        view.setEditable_(False)
        self.glass = make_glass()
        if self.glass is not None:
            # Liquid Glass card: the glass draws the card (and the window its
            # shadow); the image above it holds only the content.
            from AppKit import NSAppearance, NSView
            container = NSView.alloc().initWithFrame_(((0, 0), (10, 10)))
            container.addSubview_(self.glass)
            container.addSubview_(view)
            panel.setContentView_(container)
            panel.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
            panel.setHasShadow_(True)
        else:
            panel.setContentView_(view)
            panel.setHasShadow_(False)           # the image carries its own soft shadow
        agent_panel.set_glass(self.glass is not None)
        self.panel, self.view = panel, view
        self.scene = agent_panel.Scene()
        self.provider = None
        self.anchor = None
        self.visible_frame = None
        self.scale = 2.0
        self.opened_at = None
        self.frame = None            # ((x, y), (w, h)) as last placed
        self.size = None             # current image size in points
        self.drawn_at = 0.0
        self.has_image = False

    @property
    def active(self) -> bool:
        return self.provider is not None

    def open(self, provider, model, anchor, screen, now):
        if self.provider is None:
            self.scene = agent_panel.Scene()
            self.opened_at = now
            self.has_image = False
        scale = float(screen.backingScaleFactor()) if screen is not None else 2.0
        if scale != self.scale or anchor != self.anchor:
            self.has_image = False
        self.scale = scale
        self.visible_frame = screen.visibleFrame() if screen is not None else anchor
        self.provider, self.anchor = provider, anchor
        self.scene.set_model(model, now)

    def refresh(self, model, now):
        self.scene.set_model(model, now)

    def contains(self, point, slack: float = 6) -> bool:
        return self.frame is not None and contains(self.frame, point, slack)

    def on_card(self, point) -> bool:
        return on_card(point, self.frame)

    def step(self, point, left, right, now):
        """Advance the scene and present a frame if one is due; returns its events."""
        inside = point is not None and self.contains(point, 0)
        scene = self.scene
        local = local_point(self.frame, point) if self.frame is not None and (inside or scene.dragging) else None
        events = scene.step(now, local, left and (inside or bool(scene.press)), right and inside)
        progress = agent_panel.ease_out_cubic((now - self.opened_at) / self.OPEN_SECONDS)
        alpha, lift = progress, 10 * (1 - progress)
        if (not self.has_image or scene.dirty or scene.animating
                or scene.ambient and now - self.drawn_at >= self.AMBIENT_SECONDS):
            image = scene.render(self.scale)
            self.view.setImage_(to_nsimage(image, self.scale))
            self.size = (image.size[0] / self.scale, image.size[1] / self.scale)
            self.has_image, self.drawn_at = True, now
        x, y = panel_origin(self.anchor, self.size, self.visible_frame)
        frame = ((x, y - lift), self.size)
        if frame != self.frame:
            if self.glass is not None and (self.frame is None or self.frame[1] != self.size):
                width, height = self.size
                self.view.setFrame_(((0, 0), (width, height)))
                self.glass.setFrame_(((MARGIN, MARGIN), (max(1, width - 2 * MARGIN), max(1, height - 2 * MARGIN))))
            self.panel.setFrame_display_(frame, True)
            self.frame = frame
        self.panel.setAlphaValue_(alpha)
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()
            log.info("agent panel shown: provider=%s bubbles=%s frame=%s", self.provider, len(scene.bodies), frame)
        return events

    def hide(self):
        self.panel.orderOut_(None)
        self.provider = self.frame = None
        self.has_image = False


class MacPanel:
    """Shows PanelWindow for a clicked status item.

    A click on a source's item opens its panel under it; a second click on
    the item, a mouse press off the card, or a status menu opening closes it.
    Nothing happens on hover. The frame timer and the mouse-down monitors
    exist only while a panel is shown.

    tray: QuotaTray (panel(), merge(), split()); items: source -> NSStatusItem;
    icons: source -> stand-in icon whose details_visible marks the shown
    source for the refresh scheduler; on_settings(provider, page) handles the
    panel's settings gear and right-click."""

    PANEL_REFRESH = 0.5
    RETRY_SECONDS = 2.0
    FRAME_SECONDS = 1 / 60
    MOUSE_DOWN = (1 << 1) | (1 << 3) | (1 << 25)     # NSEventMask: left, right and other mouse down

    def __init__(self, tray, items: dict, icons: dict, on_settings):
        self.tray, self.items, self.icons, self.on_settings = tray, items, icons, on_settings
        self.window: PanelWindow | None = None
        self.panel_built = 0.0
        self.retry_at = 0.0
        self.target = None
        self.timer = None
        self.monitors: list = []             # mouse-down monitors, while a panel is shown

    @property
    def shown(self) -> str | None:
        """The source whose panel is on screen."""
        return self.window.provider if self.window is not None and self.window.active else None

    # ----- timer ----------------------------------------------------------------

    def start(self, target):
        """`target` is the NSObject whose `panelTick:` calls tick()."""
        self.target = target

    def _run(self, on: bool):
        """The frame timer and the mouse-down monitors: there while a panel
        is shown, gone otherwise."""
        if on == (self.timer is not None):
            return
        from AppKit import NSEvent
        if not on:
            self.timer.invalidate()
            self.timer = None
            for monitor in self.monitors:
                NSEvent.removeMonitor_(monitor)
            self.monitors = []
            return
        from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            self.FRAME_SECONDS, self.target, "panelTick:", None, True)
        # Common modes: keeps stepping while a menu or a drag runs its own loop.
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

        # Monitors rather than the timer's button polling: a trackpad tap is
        # shorter than a frame and would be missed. The global one sees
        # presses in other apps, the local one those in this app (the panel's
        # shadow margin, another of its status items). Each press is placed by
        # its own event, not by the pointer: an accessibility press arrives
        # with the pointer somewhere else.
        def elsewhere(event):
            self.mouse_down(event_point(event))

        def here(event):
            if not self.on_own_item(event.window()):
                self.mouse_down(event_point(event))
            return event
        self.monitors = [monitor for monitor in (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(self.MOUSE_DOWN, elsewhere),
            NSEvent.addLocalMonitorForEventsMatchingMask_handler_(self.MOUSE_DOWN, here)) if monitor is not None]

    def stop(self):
        self.close()

    # ----- open and close -------------------------------------------------------

    @staticmethod
    def pointer():
        from AppKit import NSEvent
        location = NSEvent.mouseLocation()
        buttons = int(NSEvent.pressedMouseButtons())
        return (float(location.x), float(location.y)), bool(buttons & 1), bool(buttons & 2)

    def anchor_of(self, provider):
        """(frame, screen) of the source's status item; (None, None) while it is not in the bar."""
        item = self.items.get(provider)
        button = item.button() if item is not None else None
        window = button.window() if button is not None else None
        if window is None:
            return None, None
        frame = window.frame()
        return (((float(frame.origin.x), float(frame.origin.y)), (float(frame.size.width), float(frame.size.height))),
                window.screen())

    def toggle(self, provider: str) -> bool:
        """A click on the source's status item: its panel opens, or closes if
        it was the one shown. False when there is nothing to show (a source
        without a panel, or the window could not be built), so the caller can
        show the source's menu instead."""
        if self.shown == provider:
            self.close("its item clicked again")
            return True
        now = time.monotonic()
        try:
            model = self.tray.panel(provider)
            anchor, screen = self.anchor_of(provider)
            if model is None or anchor is None or now < self.retry_at:
                self.close()
                return False
            if self.window is None:
                self.window = PanelWindow()
            window = self.window
            if window.active:
                window.hide()                # another source's panel: this one opens afresh
            window.open(provider, model, anchor, screen, now)
            self.panel_built = now
            window.step(self.pointer()[0], False, False, now)
        except Exception:
            self._failed(now)
            return False
        self._mark(provider)
        self._run(True)
        return True

    def close(self, why: str = ""):
        if self.window is not None and self.window.active:
            log.info("agent panel closed: provider=%s%s", self.window.provider, f" ({why})" if why else "")
            self.window.hide()
        self._mark(None)
        self._run(False)

    def on_own_item(self, window) -> bool:
        """`window` is the status item of the source whose panel is shown."""
        item = self.items.get(self.shown)
        button = item.button() if item is not None else None
        return window is not None and button is not None and window == button.window()

    def mouse_down(self, point):
        """A mouse button went down at `point` (screen): off the card, the
        panel closes, as a menu would. A press on the panel's own item is
        left to the item's click, which toggles it."""
        window = self.window
        if window is None or not window.active:
            return
        if window.on_card(point) or contains(window.anchor, point):
            return
        self.close(f"press at {point[0]:.0f}, {point[1]:.0f}")

    def _mark(self, shown):
        for key, icon in self.icons.items():
            icon.details_visible = key == shown

    def _failed(self, now):
        # A failed frame must not end panels for the rest of the run.
        log.exception("mac agent panel failed")
        if self.window is not None:
            try:
                self.window.hide()
            except Exception:
                pass
        self.window = None
        self.retry_at = now + self.RETRY_SECONDS
        self._mark(None)
        self._run(False)

    # ----- one tick -------------------------------------------------------------

    def tick(self):
        window = self.window
        if window is None or not window.active:
            self._run(False)
            return
        now = time.monotonic()
        try:
            point, left, right = self.pointer()
            self.drive(window, point, left, right, now)
        except Exception:
            self._failed(now)

    def drive(self, window: PanelWindow, point, left, right, now):
        if (now - self.panel_built >= self.PANEL_REFRESH
                and not window.scene.press and not window.scene.dragging):
            model = self.tray.panel(window.provider)
            if model is None:
                self.close("the source has no panel now")
                return
            window.refresh(model, now)
            self.panel_built = now
        for event in window.step(point, left, right, now):
            if event[0] == "settings":
                provider = window.provider
                self.close("settings opened")
                self.on_settings(provider, event[1] if len(event) > 1 else None)
                return
            if event[0] == "merge":
                merged = self.tray.merge(event[1], event[2])
                if merged:
                    window.scene.absorb(event[1], event[2], merged, now)
            elif event[0] == "split":
                members = self.tray.split(event[1])
                if members:
                    window.scene.burst(event[1], members, now)
            model = self.tray.panel(window.provider)
            if model is not None:
                window.refresh(model, now)
