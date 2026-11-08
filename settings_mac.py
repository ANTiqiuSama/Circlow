"""macOS settings window: the Windows settings page (settings_window) in its
own process.

Tk needs the main thread, which the menu bar's AppKit loop already owns, so
the menu bar starts `Circlow --settings [page]` instead. That process
builds a headless QuotaTray from the same data files (no readers of its own
beyond a light refresh of cached figures and local TPS), so the pages show
what the menu bar shows. Actions that concern the running menu bar (refresh,
reload settings, a changed set of sources, quit) travel as small JSON files
in DATA_DIR/mac-commands/<target>/, read by the other process every second;
the menu bar uses the same channel to raise an open window on another page.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
import app_identity

APP_NAME = app_identity.APP_NAME
log = logging.getLogger(APP_NAME)

LAUNCH_AGENT_LABEL = app_identity.BUNDLE_ID
MENU, SETTINGS = "menu", "settings"


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "Library/Application Support")) / APP_NAME


# ----- commands between the two processes -------------------------------------------

def commands_dir(target: str) -> Path:
    return data_dir() / "mac-commands" / target


def send_command(target: str, name: str, **values) -> None:
    folder = commands_dir(target)
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"name": name, **values}, ensure_ascii=False)
    stem = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}"
    temporary = folder / f"{stem}.tmp"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, folder / f"{stem}.json")      # readers only pick up complete files


def take_commands(target: str) -> list[dict]:
    """The waiting commands for `target`, oldest first; each is read once."""
    folder = commands_dir(target)
    if not folder.is_dir():
        return []
    taken = []
    for path in sorted(folder.glob("*.json")):
        try:
            taken.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        path.unlink(missing_ok=True)
    return taken


# ----- launching ---------------------------------------------------------------------

def app_bundle() -> Path | None:
    """The running .app bundle (frozen builds only)."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent
    return None


def own_command(*args: str) -> list[str]:
    """Run this program again: the packaged executable, or mac_menu.py from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-X", "utf8", str(Path(__file__).with_name("mac_menu.py")), *args]


def settings_running() -> bool:
    import fcntl
    path = data_dir() / "mac-settings.lock"
    if not path.exists():
        return False
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def open_settings(page: str | None = None, popen=subprocess.Popen, running=settings_running) -> str:
    """From the menu bar: raise the open settings window (on `page`), or start one.
    Returns "raised" or "started"."""
    if running():
        send_command(SETTINGS, "show", page=page)
        return "raised"
    args = ["--settings"] + ([page] if page else [])
    popen(own_command(*args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
          start_new_session=True)
    return "started"


def relaunch_command() -> list[str]:
    """Start the menu bar again a moment after this one has quit."""
    bundle = app_bundle()
    if bundle is not None:
        return ["/bin/sh", "-c", f"sleep 1; /usr/bin/open -n {json.dumps(str(bundle))}"]
    quoted = " ".join(json.dumps(part) for part in own_command())
    return ["/bin/sh", "-c", f"sleep 1; exec {quoted}"]


# ----- open at login (LaunchAgent) ---------------------------------------------------

def launch_agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def login_item_enabled() -> bool:
    return launch_agent_path().is_file()


def set_login_item(enabled: bool) -> None:
    """A per-user LaunchAgent that opens the app at login; removed when off."""
    path = launch_agent_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    bundle = app_bundle()
    program = ["/usr/bin/open", "-a", str(bundle)] if bundle is not None else own_command()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump({"Label": LAUNCH_AGENT_LABEL, "ProgramArguments": program, "RunAtLoad": True}, handle)


def migrate_login_item() -> bool:
    """Before the rename the LaunchAgent was com.antiqiusama.codexquotatray
    and opened CodexQuotaTray.app: put this build's in its place."""
    old = launch_agent_path().with_name(f"{app_identity.LEGACY_BUNDLE_ID}.plist")
    if not old.is_file():
        return False
    old.unlink(missing_ok=True)
    set_login_item(True)
    return True


# ----- macOS stand-ins for Windows shell calls ---------------------------------------

def shell_open(target) -> None:
    """os.startfile for macOS: files, folders and URLs through `open`.
    Windows-only targets (ms-settings:) are ignored."""
    text = str(target)
    if text.startswith("ms-settings:"):
        return
    subprocess.Popen(["/usr/bin/open", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def show_text(title: str, text: str) -> None:
    """A plain macOS dialog (the Windows MessageBox)."""
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    subprocess.Popen(["osascript", "-e", f"display dialog {quoted(text[:2000])} with title {quoted(title)} "
                      'buttons {"好"} default button 1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----- the settings process ----------------------------------------------------------

def make_settings_tray():
    """A QuotaTray for the settings window: the menu bar's data files, its own
    light refresh, and actions forwarded to the running menu bar."""
    import mac_menu
    import mimo_usage
    import tray_app
    from quota_sources import load_cached_snapshots

    class SettingsTray(tray_app.QuotaTray):
        def __init__(self):
            super().__init__(icons={key: mac_menu.MacIcon(key) for key in tray_app.PROVIDERS})
            self.mimo_login_result = ""

        # Live figures for the open window: cached quotas from the menu bar,
        # local TPS from this process' own monitor.
        def follow(self):
            while not self.stop_event.wait(2.0):
                try:
                    cached = load_cached_snapshots()
                    for key in self.snapshots:
                        if cached.get(key) is not None:
                            self.snapshots[key] = cached[key]
                    now = time.time()
                    self.performance = self.tps_monitor.poll(now)
                    self.agents = self.tps_monitor.agents(now)
                    for key, letter in self.letters.items():
                        self.update_icon(key, letter)
                except Exception:
                    log.exception("settings window refresh failed")

        # Actions for the running menu bar.
        def request_refresh(self, _icon=None, _item=None) -> None:
            send_command(MENU, "refresh")

        def save_preferences(self, prefs: dict) -> None:
            super().save_preferences(prefs)
            send_command(MENU, "reload")

        def apply_summary_settings(self, config: dict, enabled: bool) -> None:
            super().apply_summary_settings(config, enabled)
            send_command(MENU, "reload")

        def save_model_iq(self, *args, **kwargs):
            result = super().save_model_iq(*args, **kwargs)
            send_command(MENU, "reload")
            return result

        def refresh_model_iq(self, *args, **kwargs):
            result = super().refresh_model_iq(*args, **kwargs)
            send_command(MENU, "reload")
            return result

        def refresh_grok_iq(self, *args, **kwargs):
            result = super().refresh_grok_iq(*args, **kwargs)
            send_command(MENU, "reload")
            return result

        def save_mimo_manual(self, *args, **kwargs):
            result = super().save_mimo_manual(*args, **kwargs)
            send_command(MENU, "refresh", providers=["mimo"])
            return result

        def mimo_login(self, _icon=None, _item=None) -> None:
            """The console login in an app-owned Chrome/Edge window; the session
            goes to the keychain and the menu bar reads the plan again."""
            if getattr(self, "mimo_login_running", False):
                return
            if not mimo_usage.edge_path():
                self.mimo_login_result = "未找到 Chrome / Edge / Chromium / Brave，无法打开登录窗口；可在下方手动填写套餐"
                show_text("Xiaomi MiMo", self.mimo_login_result)
                return
            self.mimo_login_running = True
            self.mimo_login_result = "已打开登录窗口，请在弹出的浏览器窗口中登录小米账号…"

            def run():
                try:
                    mimo_usage.login(interactive=True)
                    shown = "mimo" in tray_app.provider_registry.enabled_keys()
                    self.mimo_login_result = ("登录成功，已读取套餐" if shown else
                                              "登录成功；在“来源”中打开小米 MiMo 后显示 M 图标")
                    send_command(MENU, "refresh", providers=["mimo"])
                except Exception as error:
                    self.mimo_login_result = f"登录未完成：{error}"
                    show_text("Xiaomi MiMo · 登录未完成", str(error))
                finally:
                    self.mimo_login_running = False
            threading.Thread(target=run, name="mimo-login", daemon=True).start()

        def mimo_logout(self, _icon=None, _item=None) -> None:
            mimo_usage.logout()
            send_command(MENU, "refresh", providers=["mimo"])

        def set_providers(self, changes: dict) -> None:
            # Against the switches as saved now, not the set this process
            # started with: off and back on in one sitting are two changes.
            shown = set(tray_app.provider_registry.enabled_keys())
            changes = {key: value for key, value in changes.items() if (key in shown) != bool(value)}
            if not changes:
                return
            tray_app.provider_registry.set_provider_choices(changes)
            log.info("source switches changed from settings: %s", changes)
            send_command(MENU, "restart")     # the menu bar comes back with the new set; this window stays open

        def exit_app(self, _icon=None, _item=None) -> None:
            send_command(MENU, "quit")
            self.stop_event.set()

        def test_notification(self, _icon=None, _item=None) -> None:
            send_command(MENU, "test-notify")    # posted by the menu bar, as a real notice would be

        # Windows registry / message boxes -> macOS.
        def startup_enabled(self) -> bool:
            return login_item_enabled()

        def toggle_startup(self, _icon=None, _item=None) -> None:
            set_login_item(not login_item_enabled())

        def open_all_performance(self, _icon=None, _item=None) -> None:
            performance = self.performance
            sections = [f"本机合计 · 最近60秒：{performance.get('total_output_tps_60', 0.0):.1f} tok/s", ""]
            for provider, data in performance.get("providers", {}).items():
                if provider == "gemini":
                    continue
                sections.extend((data.get("label", provider), self.performance_line(provider), self.output_rate_line(provider), ""))
            sections.append("检测到的软件：")
            sections.extend(f"{item['name']}：{item['status']}" for item in performance.get("software", []))
            show_text("本机 AI · TPS", "\n".join(sections))

    return SettingsTray()


_VIEW_CLASSES = None


def _view_classes():
    """The overlay's NSViews: click-through clip and image views (pictures
    only draw; Tk keeps every click), a clip view that hands clicks to its
    native control, and the target the native switches report to."""
    global _VIEW_CLASSES
    if _VIEW_CLASSES is None:
        import objc
        from AppKit import NSImageView, NSObject, NSView

        class PassThroughClip(NSView):
            def hitTest_(self, _point):
                return None

            def isFlipped(self):
                return True                     # top-left origin, like Tk

        class PassThroughImage(NSImageView):
            def hitTest_(self, _point):
                return None

        class ControlClip(NSView):
            def hitTest_(self, point):
                hit = objc.super(ControlClip, self).hitTest_(point)
                return None if hit is self else hit     # the control takes clicks; the empty margin does not

            def isFlipped(self):
                return True

        class SwitchTarget(NSObject):
            def toggled_(self, sender):
                callback = getattr(self, "callback", None)
                if callback is not None:
                    callback(int(sender.tag()), int(sender.state()) == 1)

        _VIEW_CLASSES = (PassThroughClip, PassThroughImage, ControlClip, SwitchTarget)
    return _VIEW_CLASSES


_SWITCH_SIZE = None


def native_switch_size() -> tuple[int, int]:
    """The system switch's own size in points (54 x 24 with Liquid Glass)."""
    global _SWITCH_SIZE
    if _SWITCH_SIZE is None:
        try:
            from AppKit import NSSwitch
            size = NSSwitch.alloc().initWithFrame_(((0, 0), (60, 30))).fittingSize()
            _SWITCH_SIZE = (max(1, round(size.width)), max(1, round(size.height)))
        except Exception:
            _SWITCH_SIZE = (40, 22)
    return _SWITCH_SIZE


def intersect(a, b):
    """(x, y, w, h) rectangles, top-left origin; None when they do not overlap."""
    if a is None or b is None:
        return None
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


class NativeOverlay:
    """Native AppKit views laid over a Tk window, kept in place every frame.

    Sharp pictures (2x screens): Tk on macOS shows one image pixel per point,
    so the settings window's Pillow pictures (cards, buttons, charts) are
    stretched and soft. The window draws them at f times their size
    (SettingsWindow.f); this lays a native image view with that original
    exactly over each picture label, clipped to the label's ancestors
    (scrolling pages, slides), and passes every click through to Tk. The
    sidebar's selection bar, a plain frame above the menu pictures, is kept
    above them as a coloured view.

    Native switches (all screens): each settings switch is a blank Tk canvas
    of the system switch's size with an NSSwitch over it (Liquid Glass when
    the executable is built against the macOS 26 SDK). The NSSwitch takes the
    clicks, writes the Tk variable and runs the switch's command; a variable
    changed from Tk (loading saved settings) moves the switch."""

    INTERVAL_MS = 16
    RESCAN_SECONDS = 1.0

    def __init__(self, window):
        self.window = window
        self.root = window.root
        self.f = window.f
        self.tracked: dict[str, object] = {}
        self.views: dict[str, list] = {}          # path -> [clip, image view, picture shown]
        self.switches: dict[str, list] = {}       # path -> [canvas, variable, command, tag, clip, NSSwitch]
        self.by_tag: dict[int, str] = {}
        self.target = None
        self.bar = None
        self.content = None
        self.scanned_at = 0.0
        if self.f > 1:
            window.on_picture = self.picture_changed

    # ----- native switches -------------------------------------------------------
    def add_switch(self, canvas, variable, command=None):
        tag = len(self.by_tag) + 1
        self.by_tag[tag] = str(canvas)
        self.switches[str(canvas)] = [canvas, variable, command, tag, None, None]

    def switch_toggled(self, tag: int, on: bool):
        """The user flipped an NSSwitch: the same as clicking the Tk switch."""
        entry = self.switches.get(self.by_tag.get(tag, ""))
        if entry is None:
            return
        _canvas, variable, command = entry[:3]
        if bool(variable.get()) != on:
            variable.set(on)
        if command:
            command()

    def make_switch(self, entry):
        from AppKit import NSAppearance, NSSwitch
        _, _, control_clip, target_class = _view_classes()
        if self.target is None:
            self.target = target_class.alloc().init()
            self.target.callback = self.switch_toggled
        width, height = native_switch_size()
        clip = control_clip.alloc().initWithFrame_(((0, 0), (width, height)))
        clip.setWantsLayer_(True)
        clip.layer().setMasksToBounds_(True)
        control = NSSwitch.alloc().initWithFrame_(((0, 0), (width, height)))
        control.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))   # the window is always dark
        control.setTag_(entry[3])
        control.setTarget_(self.target)
        control.setAction_("toggled:")
        clip.addSubview_(control)
        self.content.addSubview_(clip)
        entry[4], entry[5] = clip, control

    def place_switch(self, path, entry, origin, cache, height):
        canvas, variable = entry[0], entry[1]
        if not canvas.winfo_exists():
            if entry[4] is not None:
                entry[4].removeFromSuperview()
            self.switches.pop(path, None)
            return
        if entry[4] is None:
            self.make_switch(entry)
        clip, control = entry[4], entry[5]
        rect = self.rect_of(canvas, origin, cache) if canvas.winfo_viewable() else None
        visible = intersect(rect, self.clip_of(canvas, origin, cache)) if rect else None
        if visible is None:
            clip.setHidden_(True)
            return
        on = 1 if variable.get() else 0
        if int(control.state()) != on:
            control.setState_(on)
        width, height_ = native_switch_size()
        clip.setFrame_(self.to_content(visible, height))
        control.setFrame_(((rect[0] - visible[0] + (rect[2] - width) / 2, rect[1] - visible[1] + (rect[3] - height_) / 2),
                           (width, height_)))
        clip.setHidden_(False)

    # ----- bookkeeping -----------------------------------------------------------
    def picture_changed(self, widget):
        self.tracked[str(widget)] = widget

    def rescan(self):
        """Pick up picture labels made before the hook was set (window build)."""
        hires = self.window.hires
        stack = [self.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "Label":
                try:
                    if str(widget.cget("image")) in hires:
                        self.tracked[str(widget)] = widget
                except Exception:
                    pass
        self.scanned_at = time.monotonic()

    def attach(self) -> bool:
        from AppKit import NSApplication
        title = str(self.root.title())
        for candidate in NSApplication.sharedApplication().windows():
            if str(candidate.title()) == title:
                self.content = candidate.contentView()
                return True
        return False

    def start(self):
        self.root.after(self.INTERVAL_MS, self.tick)

    def tick(self):
        try:
            self.sync()
        except Exception:
            log.exception("retina overlay sync failed")
        finally:
            try:
                self.root.after(self.INTERVAL_MS, self.tick)
            except Exception:
                pass                              # the window is gone

    # ----- geometry --------------------------------------------------------------
    def to_content(self, rect, height):
        """Top-left rect in window points -> the content view's own coordinates."""
        x, y, w, h = rect
        if self.content.isFlipped():
            return ((x, y), (w, h))
        return ((x, height - y - h), (w, h))

    def rect_of(self, widget, origin, cache):
        path = str(widget)
        if path not in cache:
            cache[path] = (widget.winfo_rootx() - origin[0], widget.winfo_rooty() - origin[1],
                           widget.winfo_width(), widget.winfo_height())
        return cache[path]

    def clip_of(self, widget, origin, cache):
        """The part of the window where `widget` can be seen: its ancestors' intersection."""
        key = ("clip", str(widget))
        if key in cache:
            return cache[key]
        parent = widget.master
        if parent is None:
            result = self.rect_of(widget, origin, cache)
        else:
            result = intersect(self.rect_of(parent, origin, cache), self.clip_of(parent, origin, cache))
        cache[key] = result
        return result

    # ----- one frame -------------------------------------------------------------
    def sync(self):
        if self.content is None and not self.attach():
            return
        if str(self.root.state()) == "withdrawn":
            return
        if self.f > 1 and time.monotonic() - self.scanned_at >= self.RESCAN_SECONDS:
            self.rescan()
        height = float(self.content.frame().size.height)
        origin = (self.root.winfo_rootx(), self.root.winfo_rooty())
        cache: dict = {}
        for path, widget in list(self.tracked.items()):
            try:
                if not widget.winfo_exists():
                    self.drop(path)
                    continue
                self.place(path, widget, origin, cache, height)
            except Exception:
                self.drop(path)
        if self.f > 1:
            self.place_bar(origin, cache, height)
        for path, entry in list(self.switches.items()):
            try:
                self.place_switch(path, entry, origin, cache, height)
            except Exception:
                log.exception("native switch %s failed", path)
                if entry[4] is not None:
                    entry[4].setHidden_(True)

    def place(self, path, widget, origin, cache, height):
        picture = self.window.hires.get(str(widget.cget("image")))
        if picture is None or not widget.winfo_viewable():
            self.hide(path)
            return
        x, y, w, h = self.rect_of(widget, origin, cache)
        iw, ih = picture.width / self.f, picture.height / self.f
        image_rect = (x + (w - iw) / 2, y + (h - ih) / 2, iw, ih)   # a label centres its image
        visible = intersect(image_rect, intersect((x, y, w, h), self.clip_of(widget, origin, cache)))
        if visible is None:
            self.hide(path)
            return
        clip, view, shown = self.views.get(path) or self.make(path)
        if shown is not picture:
            from mac_panel import to_nsimage
            view.setImage_(to_nsimage(picture, self.f))
            self.views[path][2] = picture
        clip.setFrame_(self.to_content(visible, height))
        view.setFrame_(((image_rect[0] - visible[0], image_rect[1] - visible[1]), (iw, ih)))
        clip.setHidden_(False)

    def make(self, path):
        clip_class, image_class, *_ = _view_classes()
        clip = clip_class.alloc().initWithFrame_(((0, 0), (1, 1)))
        clip.setWantsLayer_(True)
        clip.layer().setMasksToBounds_(True)
        view = image_class.alloc().initWithFrame_(((0, 0), (1, 1)))
        view.setImageScaling_(1)                  # NSImageScaleAxesIndependently: fill exactly
        clip.addSubview_(view)
        self.content.addSubview_(clip)
        self.views[path] = [clip, view, None]
        if self.bar is not None:                  # keep the selection bar on top
            self.content.addSubview_(self.bar[0])
        return self.views[path]

    def place_bar(self, origin, cache, height):
        widget = getattr(self.window, "nav_bar", None)
        if widget is None or not widget.winfo_exists():
            return
        if self.bar is None:
            _, image_class, *_ = _view_classes()
            view = image_class.alloc().initWithFrame_(((0, 0), (1, 1)))
            view.setImageScaling_(1)              # a one-colour image stretched to the bar
            self.bar = [view, None]
            self.content.addSubview_(view)
        view, color = self.bar
        rect = intersect(self.rect_of(widget, origin, cache), self.clip_of(widget, origin, cache)) \
            if widget.winfo_viewable() else None
        if rect is None:
            view.setHidden_(True)
            return
        fill = str(widget.cget("bg"))
        if fill != color:
            from PIL import Image
            from mac_panel import to_nsimage
            rgb = tuple(value // 257 for value in widget.winfo_rgb(fill))
            view.setImage_(to_nsimage(Image.new("RGBA", (2, 2), rgb + (255,)), 1))
            self.bar[1] = fill
        view.setFrame_(self.to_content(rect, height))
        view.setHidden_(False)

    def hide(self, path):
        entry = self.views.get(path)
        if entry is not None:
            entry[0].setHidden_(True)

    def drop(self, path):
        self.tracked.pop(path, None)
        entry = self.views.pop(path, None)
        if entry is not None:
            entry[0].removeFromSuperview()


def make_window(tray, commands):
    import settings_window

    class MacSettingsWindow(settings_window.SettingsWindow):
        """Closing ends the process (the Windows window only hides, for its tray
        thread); native switches and, on 2x screens, sharp pictures are laid
        over the Tk window (NativeOverlay)."""

        def __init__(self, *args, **kwargs):
            self.overlay = None
            self.early_switches = []          # switches made while the window builds its pages
            super().__init__(*args, **kwargs)
            self.overlay = NativeOverlay(self)
            for canvas, variable, command in self.early_switches:
                self.overlay.add_switch(canvas, variable, command)
            self.overlay.start()

        def switch(self, parent, variable, command=None):
            """A blank canvas the size of the system switch; the NSSwitch over it does the rest."""
            width, height = native_switch_size()
            canvas = self.tk.Canvas(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0, bd=0)
            if self.overlay is not None:
                self.overlay.add_switch(canvas, variable, command)
            else:
                self.early_switches.append((canvas, variable, command))
            return canvas

        def close(self):
            super().close()
            if self.root.state() == "withdrawn":
                self.root.destroy()

        def appear(self, page=None):
            super().appear(page)
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)   # an LSUIElement app: come forward
            except Exception:
                pass

    return MacSettingsWindow(tray, commands)


def run_settings(page: str | None = None) -> int:
    """`Circlow --settings [page]`: one settings window per user."""
    import fcntl
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    lock = (folder / "mac-settings.lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        send_command(SETTINGS, "show", page=page)    # already open: raise it
        lock.close()
        return 0
    os.startfile = shell_open                         # type: ignore[attr-defined]  # settings / tray links
    take_commands(SETTINGS)                           # stale requests from an earlier window
    tray = make_settings_tray()
    commands: queue.Queue = queue.Queue()
    commands.put(("show", page))

    def relay():
        while not tray.stop_event.wait(0.3):
            for command in take_commands(SETTINGS):
                if command.get("name") == "show":
                    commands.put(("show", command.get("page")))

    threading.Thread(target=tray.follow, name="settings-follow", daemon=True).start()
    threading.Thread(target=relay, name="settings-relay", daemon=True).start()
    try:
        make_window(tray, commands).run()
    finally:
        tray.stop_event.set()
        lock.close()
    return 0


# ----- the menu bar side -------------------------------------------------------------

def handle_menu_command(tray, command: dict, now: float | None = None) -> str | None:
    """Apply one command from the settings window to the running menu bar.
    Returns "restart" or "quit" when the menu bar should end; None otherwise."""
    import agent_hints
    import agent_tasks
    import grok_iq
    import model_iq
    import tray_app
    name = command.get("name")
    now = now or time.time()
    if name == "refresh":
        tray.scheduler.request(command.get("providers"), now, "设置页刷新")
        tray.wake.set()
    elif name == "reload":
        tray.apply_preferences(tray_app.load_preferences())
        tray.summarizer = agent_tasks.Summarizer(agent_tasks.summary_config())
        tray.hint_advisor = agent_hints.HintAdvisor(agent_tasks.summary_config())
        with tray.iq_lock:
            tray.iq_config = model_iq.load_config(tray_app.IQ_CONFIG_PATH)
            tray.iq_state = model_iq.load_state(tray_app.IQ_STATE_PATH)
            tray.grok_iq = grok_iq.load_state(tray_app.GROK_IQ_PATH)
        tray.iq_wake.set()
    elif name == "test-notify":
        tray.test_notification()                  # from the menu bar process: the path a real notice takes
    elif name in ("restart", "quit"):
        return name
    else:
        log.warning("unknown settings command: %s", command)
    return None
