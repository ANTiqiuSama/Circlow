"""The tray's icon slots: which source each of the fixed tray icons shows,
and keeping their pictures and tooltips current (a QuotaTray mixin).

Windows remembers each icon's place by (program, id), so the icons are never
removed or re-added; the sources move between them instead."""
from __future__ import annotations

import logging
import time

try:
    import hover_details
except ImportError:   # macOS: the menu bar places its own items; nothing to reorder
    hover_details = None
import providers as provider_registry
from quota_sources import save_cached_snapshots
from tray_icons import make_display_icon

log = logging.getLogger("Circlow")
# Tray order is fixed (providers.TRAY_POSITIONS): Claude Code, Codex (GPT),
# Grok, Gemini, MiMo.  It never changes with remaining quota or exhaustion.
REORDER_MIN_SECONDS = 2          # after a drag, the order is right again within a few seconds


class IconSlots:
    """Needs from the tray: icons, slots, snapshots, performance, ui_lock,
    letters, shown_order, reordered_at, hidden_slots, tooltip(), build_menu(),
    reset_level()."""

    def update_ui(self) -> None:
        save_cached_snapshots(self.snapshots)
        for provider, letter in self.letters.items():
            self.update_icon(provider, letter)
            self.icons[provider].update_menu()
        self.maybe_reorder(time.time())

    def wanted_order(self) -> list[str]:
        return provider_registry.tray_positions(self.letters)

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
        if not icons or hover_details is None:
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
                self.update_icon(provider, self.letters[provider])
        self.shown_order, self.reordered_at = wanted, now
        log.info("tray order: %s", ", ".join(wanted))
        return True

    def update_icon(self, provider: str, letter: str) -> None:
        # The refresh worker and the 1 s TPS loop both land here. pystray
        # replaces the native HICON on assignment, so serialise the updates
        # (this also keeps the cached FreeType faces single-threaded).
        with self.ui_lock:
            icon = self.icons[provider]
            # Hovered icons update too: the panel stays open through a Shell
            # POPUPCLOSE (HoverDetails.target_at), and freezing the icon while
            # its panel was open left it on a ring or an old countdown while
            # the panel showed live TPS.
            image = make_display_icon(self.snapshots[provider], letter, self.performance["providers"][provider],
                                      reset_hint=provider == "codex" and self.reset_level() == "high")
            # Icon assignment always sends NIM_MODIFY, even for identical pixels.
            # Avoid needless Shell redraws; hover detail updates use a separate HWND.
            # A picture the Shell refused is sent again even when unchanged.
            if (icon.icon is not image and icon.icon != image) or getattr(icon, "icon_failed", False):
                icon.icon = image
            icon.title = self.tooltip(provider)
