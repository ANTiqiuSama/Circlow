"""Native macOS menu-bar frontend for the shared quota and TPS readers.

AppKit stays on the main thread. The data side is the Windows tray's own
QuotaTray run headless (quotas, TPS, agents, forecasts, reset watch), fed
stand-in icons (MacIcon) instead of pystray ones; a short timer copies their
pictures into NSStatusItems/NSMenus. Hovering an item opens the same agent
panel as on Windows (mac_panel); clicking it opens the native menu.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path


import app_identity  # first: adopts the old CODEX_QUOTA_* settings before other modules read them

APP_NAME = app_identity.APP_NAME
if sys.platform == "darwin":
    # Shared readers use LOCALAPPDATA as their configurable data root.
    os.environ.setdefault("LOCALAPPDATA", str(Path.home() / "Library/Application Support"))
    # Finder launches apps with a restricted PATH; resolve native CLI shims.
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"),
             str(Path.home() / ".grok/bin")]
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(os.environ.get("PATH", "").split(os.pathsep) + extra))
if __name__ == "__main__":
    app_identity.migrate_data_dir()   # ~/Library/Application Support/CodexQuotaTray -> Circlow, before anything reads it

import herdr_source
import launchers
import mimo_usage
import providers
from agent_panel import compact_usage
from forecast import reset_distance
from quota_sources import QuotaSnapshot, collect, load_cached_snapshots, mimo_snapshot, mimo_tracker, placeholder, save_cached_snapshots
from refresh_scheduler import RefreshScheduler
from tps_sources import TpsMonitor, save_performance
from tray_icons import make_display_icon, quota_exhausted, stale_note


HERDR_AGENTS = launchers.HERDR_AGENTS      # "打开来源" starts these in a new Herdr workspace
LEFT_UP_MASK, RIGHT_UP_MASK = 1 << 2, 1 << 4   # NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp
RIGHT_UP_TYPE = 4                              # NSEventTypeRightMouseUp
CONTROL_FLAG = 1 << 18                         # NSEventModifierFlagControl
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "Library/Application Support")) / APP_NAME
log = logging.getLogger(APP_NAME)


def window_name(minutes: int | None) -> str:
    if minutes == 10080:
        return "每周"
    if minutes and 524_000 <= minutes <= 528_000:
        return "年度"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟" if minutes else "当前"


def countdown(timestamp: int | None, now: float | None = None) -> str:
    """Same wording as the Windows panel's column titles ("4小时32分钟后重置")."""
    if not timestamp:
        return "重置时间未知"
    distance = reset_distance(timestamp, now if now is not None else time.time())
    return f"{distance}后重置" if distance else "等待重置生效"


def paid_text(snapshot: QuotaSnapshot) -> str:
    """Codex used up but running on purchased credits: show what is left."""
    balance = snapshot.credits_balance
    if balance == float("inf"):
        return "购买额度 无限"
    number, unit = compact_usage(balance)
    return f"购买额度 {number}{unit}"


def quota_rows(snapshot: QuotaSnapshot, now: float | None = None) -> list[str]:
    """Menu details: every known window/pool, without stale account data reuse."""
    paid = (snapshot.provider == "codex" and snapshot.credits_balance is not None and snapshot.credits_balance > 0)

    def row(label: str, remaining: float | None, reset: int | None) -> str:
        value = "未知" if remaining is None else f"剩余 {remaining:.0f}%"
        if paid and quota_exhausted(remaining, snapshot.provider):
            value = f"已用完 · {paid_text(snapshot)}"
        return f"{label} · {value} · {countdown(reset, now)}"

    if snapshot.pools:
        return [row(str(pool.get("label") or pool.get("key") or "额度"),
                    pool.get("remaining_percent"), pool.get("resets_at")) for pool in snapshot.pools]
    label = ("年度套餐额度" if snapshot.provider == "mimo" and window_name(snapshot.window_minutes) == "年度"
             else "套餐额度" if snapshot.provider == "mimo" else window_name(snapshot.window_minutes))
    rows = [row(label,
                snapshot.remaining_percent, snapshot.resets_at)]
    if snapshot.secondary_window_minutes or snapshot.secondary_remaining_percent is not None:
        rows.append(row(window_name(snapshot.secondary_window_minutes),
                        snapshot.secondary_remaining_percent, snapshot.secondary_resets_at))
    return rows


def menu_model(provider: str, snapshot: QuotaSnapshot, performance: dict, refresh: str,
               now: float | None = None) -> dict:
    now = now or time.time()
    quota = quota_rows(snapshot, now)
    tps = performance.get("display_tps")
    active = bool(performance.get("active_sessions", 0))
    speed = ("" if provider == "gemini" else
             f"生成 TPS {float(tps):.1f}" if active and tps is not None else
             f"最近 TPS {float(tps):.1f}" if tps is not None else "TPS 暂无样本")
    updated = (datetime.fromtimestamp(snapshot.updated_at).astimezone().strftime("%m-%d %H:%M")
               if snapshot.updated_at else "尚未读取")
    summary = f"{snapshot.label} · " + (f"剩余 {snapshot.display_remaining:.0f}%" if snapshot.display_remaining is not None else "额度未知")
    if snapshot.stale:
        summary += " · 旧数据"
    # A stale source says when its figures are from and why (the amber dot).
    error = stale_note(snapshot, now) if snapshot.stale else (snapshot.error or "")[:150]
    if provider == "mimo" and snapshot.today_used is not None:
        today = datetime.fromtimestamp(now).astimezone().date().isoformat()
        if snapshot.today_date == today:
            quota.append(f"今日用量 {mimo_usage.credits_text(snapshot.today_used)}")
    return {"summary": summary, "quota": quota, "speed": speed,
            "updated": f"更新 {updated} · {refresh}", "error": error,
            "live": provider != "gemini" and active and tps is not None,
            "tps": None if provider == "gemini" else tps}


def status_image(snapshot: QuotaSnapshot, letter: str, performance: dict):
    """The Windows tray icon: split ring, Gemini pool thirds, countdown or TPS."""
    return make_display_icon(snapshot, letter, performance)


def bar_order(keys: list[str]) -> list[str]:
    """Creation order for status items. macOS places each new item to the
    left of the existing ones, so create right-to-left to read Claude, Codex,
    Gemini, Grok, MiMo from left to right, like the Windows tray."""
    rank = {key: index for index, key in enumerate(providers.TRAY_POSITIONS)}
    return sorted(keys, key=lambda key: rank.get(key, len(rank)), reverse=True)


def open_source(key: str, state, popen=subprocess.Popen, run=subprocess.run) -> str | None:
    """A click on the source's icon (or "打开来源"): the first of
    launchers.ways that works here. Codex, Gemini and Claude open their
    desktop clients; Grok starts in a new Herdr workspace (Herdr in Ghostty).
    Returns the kind that was used, None when nothing could be opened."""
    for way in launchers.ways(key, "darwin"):
        if way.kind == "url":
            webbrowser.open(str(way.target))
        elif way.kind == "mac-app":
            opened = run(["open", "-b", way.target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if opened.returncode != 0:
                log.warning("could not open %s (%s); trying the next way", way.target, opened.returncode)
                continue
        elif way.kind == "herdr":
            state.open_in_herdr(key)
        elif not open_cli(key, popen):
            continue
        return way.kind
    log.warning("nothing could open %s", key)
    return None


def open_cli(key: str, popen=subprocess.Popen) -> bool:
    """Claude Code / Grok without Herdr: the CLI itself in a new Ghostty
    (else Terminal) window."""
    path = providers.find_claude() if key == "claude" else shutil.which("grok")
    if not path:
        return False
    popen(herdr_source.mac_terminal([str(path)], str(Path.home())), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def notify(title: str, message: str, popen=subprocess.Popen) -> None:
    """A Notification Center banner (the Windows balloon)."""
    def quoted(text: str) -> str:
        return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'
    popen(["osascript", "-e", f"display notification {quoted(message)} with title {quoted(title or APP_NAME)}"],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class MacIcon:
    """Stand-in for a pystray icon in a headless QuotaTray: the tray writes
    `icon` (PIL image) and `title`; the AppKit timer reads them. The panel
    sets `details_visible` and an open menu `hover_open`, which the tray's
    scheduler reads as "pointer on this source" (fastest refresh tier)."""

    def __init__(self, key: str):
        self.key = key
        self.icon = None
        self.title = ""
        self.visible = True
        self.details_visible = False
        self.hover_open = False
        self.suppress_hover = False

    def update_menu(self) -> None:
        pass                                   # the AppKit timer rebuilds menu titles itself

    def notify(self, message: str, title: str = "") -> None:
        notify(title, message)

    def stop(self) -> None:
        pass


class MenuState:
    """The menu bar's original standalone reader. run_mac now uses QuotaTray;
    this stays for open_source's Herdr launcher (open_in_herdr)."""
    def __init__(self, keys: list[str] | None = None):
        self.keys = keys if keys is not None else providers.enabled_keys()
        cached = load_cached_snapshots()
        self.snapshots = {key: cached.get(key) or placeholder(key) for key in self.keys}
        self.performance: dict = {"providers": {}}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.scheduler = RefreshScheduler(self.keys, time.time())
        self.monitor: TpsMonitor | None = None
        self.last_samples: dict[str, float | None] = {}
        self.mimo_ingested = 0
        self.herdr_opening: set[str] = set()

    def hover(self, provider: str | None, on: bool):
        """An open status menu reads its source at the hover rate, like the
        pointer resting on a Windows tray icon."""
        if provider in self.keys and self.scheduler.hover(provider, on, time.time()):
            self.wake.set()

    def open_in_herdr(self, provider: str, start=None) -> bool:
        """Claude Code / Grok in a new focused Herdr workspace; returns False
        when a click for this source is already being handled."""
        with self.lock:
            if provider in self.herdr_opening:
                return False
            self.herdr_opening.add(provider)

        def run():
            try:
                name = (start or herdr_source.start_agent)(provider)
                log.info("opened %s in herdr as %s", provider, name)
            except herdr_source.AgentSlow as slow:
                log.info("opened %s in herdr: %s", provider, slow)
            except (OSError, subprocess.SubprocessError) as error:
                log.warning("could not open %s in herdr: %s", provider, error)
            finally:
                with self.lock:
                    self.herdr_opening.discard(provider)
                herdr_source.raise_client()
        threading.Thread(target=run, name="herdr-open", daemon=True).start()
        return True

    def request(self, provider: str | None = None):
        self.scheduler.request([provider] if provider else None, time.time())
        self.wake.set()

    def view(self, provider: str) -> tuple[QuotaSnapshot, dict, str]:
        with self.lock:
            return (self.snapshots[provider], dict(self.performance.get("providers", {}).get(provider, {})),
                    self.scheduler.describe(provider, time.time()))

    def quota_loop(self):
        while not self.stop.is_set():
            due = self.scheduler.take_due(time.time())
            if due:
                try:
                    with self.lock:
                        previous = dict(self.snapshots)
                    result = collect(due, previous)
                except Exception:
                    log.exception("mac quota refresh failed")
                    result = {key: placeholder(key, "刷新异常") for key in due}
                now = time.time()
                with self.lock:
                    self.snapshots.update(result)
                    snapshots = dict(self.snapshots)
                for key, snapshot in result.items():
                    self.scheduler.completed(key, snapshot, now)
                    log.info("quota %s: left=%s reset=%s stale=%s error=%s", key,
                             snapshot.display_remaining, snapshot.resets_at, snapshot.stale, snapshot.error)
                save_cached_snapshots(snapshots)
            self.wake.wait(min(5.0, self.scheduler.next_wake(time.time())))
            self.wake.clear()

    def tps_loop(self):
        try:
            from usage_store import UsageStore
            self.monitor = TpsMonitor(store=UsageStore())
        except Exception:
            log.exception("mac TPS monitor could not start")
            return
        last_saved = 0.0
        while not self.stop.is_set():
            now = time.time()
            try:
                performance = self.monitor.poll(now)
                with self.lock:
                    self.performance = performance
                for key in self.keys:
                    data = performance.get("providers", {}).get(key, {})
                    sample = data.get("last_sample_at")
                    previous = self.last_samples.get(key)
                    finished = sample is not None and previous is not None and sample > previous
                    self.last_samples[key] = sample
                    if self.scheduler.activity(key, bool(data.get("active_sessions")), finished, now):
                        self.wake.set()
                if "mimo" in self.keys:
                    self.update_mimo(now)
                if now - last_saved >= 60:
                    save_performance(performance)
                    last_saved = now
            except Exception:
                log.exception("mac TPS read failed")
            self.stop.wait(2.0)

    def update_mimo(self, now: float):
        assert self.monitor is not None
        total = self.monitor.added_total
        count = min(len(self.monitor.events), max(0, total - self.mimo_ingested))
        self.mimo_ingested = total
        if count:
            fresh = list(self.monitor.events)[-count:]
            tracker = mimo_tracker()
            tracker.ledger.ingest(fresh)
            tracker.ledger.save()
        snapshot = mimo_snapshot(mimo_tracker().view(now))
        with self.lock:
            if snapshot.remaining_percent is not None or self.snapshots["mimo"].remaining_percent is None:
                self.snapshots["mimo"] = snapshot

    def start(self):
        threading.Thread(target=self.quota_loop, name="mac-quota", daemon=True).start()
        threading.Thread(target=self.tps_loop, name="mac-tps", daemon=True).start()


def _logger():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(DATA_DIR / "app-macos.log", maxBytes=1_000_000,
                                                   backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _lock_instance():
    import fcntl
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_DIR / "mac-instance.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def run_mac() -> int:
    if sys.platform != "darwin":
        raise RuntimeError("macOS 菜单栏入口只能在 macOS 运行")
    handle = _lock_instance()
    if handle is None:
        return 0
    _logger()
    from Cocoa import (NSApplication, NSApplicationActivationPolicyAccessory, NSMenu,
                       NSMenuItem, NSObject, NSStatusBar, NSVariableStatusItemLength, NSTimer)
    import objc
    import settings_mac
    import tray_app
    from mac_panel import MacHoverPanel, to_nsimage

    settings_mac.take_commands(settings_mac.MENU)      # requests left over from an earlier run
    try:
        if settings_mac.migrate_login_item():
            log.info("login item moved to %s", settings_mac.LAUNCH_AGENT_LABEL)
    except OSError:
        log.warning("login item migration skipped", exc_info=True)

    keys = list(tray_app.PROVIDERS)
    icons = {key: MacIcon(key) for key in keys}
    tray = tray_app.QuotaTray(icons=icons)
    launcher = MenuState(keys)        # Herdr launcher for "打开来源"; its readers are not started

    class Delegate(NSObject):
        def applicationDidFinishLaunching_(self, _notification):
            self.items = {}
            self.menus = {}
            self.menu_objs = {}
            self.order = []
            self.pictures = {}
            bar = NSStatusBar.systemStatusBar()
            for key in bar_order(keys):
                item = bar.statusItemWithLength_(NSVariableStatusItemLength)
                menu = NSMenu.alloc().initWithTitle_(key)   # the title names the source for the delegate
                menu.setAutoenablesItems_(False)
                menu.setDelegate_(self)          # open menu = hover: fastest refresh tier
                parts = {}
                for name, title in (("summary", key), ("quota0", ""), ("quota1", ""), ("quota2", ""),
                                    ("speed", ""), ("updated", ""), ("error", ""), ("sync", "")):
                    line = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
                    line.setEnabled_(False)
                    menu.addItem_(line)
                    parts[name] = line
                menu.addItem_(NSMenuItem.separatorItem())
                for title, action in (("立即刷新", "refreshOne:"), ("打开来源", "openSource:"), ("设置…", "openSettings:"),
                                      ("退出 环流 (Circlow)", "quitApp:")):
                    line = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
                    line.setTarget_(self)
                    line.setRepresentedObject_(key)
                    menu.addItem_(line)
                # Like the Windows tray: left-click opens the source, right-click
                # (or Control-click) shows the menu. A menu attached to the item
                # would take every click, so it is attached only while shown.
                button = item.button()
                button.setTag_(len(self.order))
                button.setTarget_(self)
                button.setAction_("statusClicked:")
                button.sendActionOn_(LEFT_UP_MASK | RIGHT_UP_MASK)
                self.order.append(key)
                self.items[key] = item
                self.menus[key] = parts
                self.menu_objs[key] = menu
            self.hover = MacHoverPanel(tray, self.items, icons, self.panelSettings)
            self.updateUI_(None)
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "updateUI:", None, True)
            tray.start_workers()
            self.hover.start(self)

        def updateUI_(self, _timer):
            now = time.time()
            for command in settings_mac.take_commands(settings_mac.MENU):
                outcome = settings_mac.handle_menu_command(tray, command, now)
                log.info("settings command %s -> %s", command.get("name"), outcome or "applied")
                if outcome == "restart":
                    subprocess.Popen(settings_mac.relaunch_command(), start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if outcome in ("restart", "quit"):
                    self.quitApp_(None)
                    return
            for key in keys:
                snapshot = tray.snapshots[key]
                performance = dict(tray.performance.get("providers", {}).get(key, {}))
                model = menu_model(key, snapshot, performance, tray.scheduler.describe(key, now), now)
                # The tray draws the icon each second (TPS loop); until then draw it here.
                image = icons[key].icon or status_image(snapshot, providers.get(key).letter, performance)
                button = self.items[key].button()
                if self.pictures.get(key) is not image:
                    button.setImage_(to_nsimage(image, image.size[0] / 20))
                    self.pictures[key] = image
                # Sources with a hover panel skip the native tooltip, which would cover it.
                tip = "" if tray_app.has_panel(key, snapshot) else "\n".join(
                    part for part in (model["summary"], model["speed"]) if part)
                button.setToolTip_(tip)
                rows = model["quota"][:3]
                for name, value in (("summary", model["summary"]), ("speed", model["speed"]),
                                    ("updated", model["updated"]), ("error", model["error"])):
                    self.menus[key][name].setTitle_(value)
                    self.menus[key][name].setHidden_(not bool(value))
                sync = f"同步：{tray.sync_status()}" if getattr(tray, "sync", None) is not None else ""
                self.menus[key]["sync"].setTitle_(sync)
                self.menus[key]["sync"].setHidden_(not sync)
                for index in range(3):
                    value = rows[index] if index < len(rows) else ""
                    line = self.menus[key][f"quota{index}"]
                    line.setTitle_(value)
                    line.setHidden_(not bool(value))

        def menuWillOpen_(self, menu):
            key = str(menu.title())
            if key in icons:
                icons[key].hover_open = True     # open menu = hover: fastest refresh tier
                tray.wake.set()
            self.hover.menu_will_open(key)

        def menuDidClose_(self, menu):
            key = str(menu.title())
            if key in icons:
                icons[key].hover_open = False
            self.hover.menu_did_close(key)

        def panelTick_(self, _timer):
            self.hover.tick()

        def statusClicked_(self, sender):
            index = int(sender.tag())
            if not 0 <= index < len(self.order):
                return
            key = self.order[index]
            event = NSApplication.sharedApplication().currentEvent()
            secondary = event is not None and (int(event.type()) == RIGHT_UP_TYPE
                                               or int(event.modifierFlags()) & CONTROL_FLAG)
            if secondary:
                self.showMenu(key)
                return
            self.hover.clicked(key)
            kind = open_source(key, launcher)
            log.info("clicked %s: opened via %s", key, kind)

        @objc.python_method
        def showMenu(self, key):
            """The source's menu under its item; returns once it has closed."""
            item = self.items[key]
            item.setMenu_(self.menu_objs[key])
            try:
                item.button().performClick_(None)
            finally:
                item.setMenu_(None)

        @objc.python_method
        def panelSettings(self, provider, page):
            # The panel's gear, banner and right-click: the settings window
            # (its own process, see settings_mac), on the page asked for.
            how = settings_mac.open_settings(page)
            log.info("settings window %s from the %s panel (page %s)", how, provider, page)

        def openSettings_(self, _sender):
            how = settings_mac.open_settings(None)
            log.info("settings window %s from the menu", how)

        def refreshOne_(self, sender):
            tray.scheduler.request([str(sender.representedObject())], time.time())
            tray.wake.set()

        def openSource_(self, sender):
            open_source(str(sender.representedObject()), launcher)

        def quitApp_(self, _sender):
            self.hover.stop()
            tray.exit_app()
            NSApplication.sharedApplication().terminate_(None)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    app.setDelegate_(delegate)
    try:
        app.run()
    finally:
        tray.stop_event.set()
        tray.wake.set()
        handle.close()
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--smoke-menu":
        return smoke_menu()
    if len(sys.argv) == 4 and sys.argv[1] in ("--grok-quota-worker", "--claude-quota-worker"):
        import cli_quota
        return cli_quota.worker(sys.argv[2], sys.argv[3], "grok" if sys.argv[1] == "--grok-quota-worker" else "claude")
    if len(sys.argv) in (2, 3) and sys.argv[1] == "--settings":
        import settings_mac
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(DATA_DIR / "settings-macos.log", maxBytes=500_000,
                                                       backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        return settings_mac.run_settings(sys.argv[2] if len(sys.argv) == 3 else None)
    return run_mac()


def smoke_menu() -> int:
    """Exercise the packaged AppKit status item without starting quota readers."""
    if sys.platform != "darwin":
        raise RuntimeError("macOS 菜单栏检查只能在 macOS 运行")
    from Cocoa import NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSStatusBar, NSVariableStatusItemLength

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    bar = NSStatusBar.systemStatusBar()
    item = bar.statusItemWithLength_(NSVariableStatusItemLength)
    try:
        menu = NSMenu.alloc().initWithTitle_("Circlow smoke")
        item.setMenu_(menu)
        assert item.button() is not None and str(item.menu().title()) == "Circlow smoke"
    finally:
        bar.removeStatusItem_(item)
    print("macOS menu bar AppKit smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
