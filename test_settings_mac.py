"""macOS settings window bridge: commands between the processes, launching,
the login item and the actions a settings window sends to the menu bar."""
import os
import plistlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import settings_mac


class TempData(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = patch.dict(os.environ, {"LOCALAPPDATA": self.folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class CommandChannelTest(TempData):
    def test_commands_arrive_once_in_order_and_partial_files_are_ignored(self):
        settings_mac.send_command(settings_mac.MENU, "refresh")
        settings_mac.send_command(settings_mac.MENU, "refresh", providers=["mimo"])
        (settings_mac.commands_dir(settings_mac.MENU) / "9999-partial.tmp").write_text("{", encoding="utf-8")
        taken = settings_mac.take_commands(settings_mac.MENU)
        self.assertEqual(taken, [{"name": "refresh"}, {"name": "refresh", "providers": ["mimo"]}])
        self.assertEqual(settings_mac.take_commands(settings_mac.MENU), [])
        self.assertEqual(settings_mac.take_commands(settings_mac.SETTINGS), [])

    def test_open_settings_raises_a_running_window_or_starts_one(self):
        popen = Mock()
        self.assertEqual(settings_mac.open_settings("iq", popen=popen, running=lambda: True), "raised")
        popen.assert_not_called()
        self.assertEqual(settings_mac.take_commands(settings_mac.SETTINGS), [{"name": "show", "page": "iq"}])
        self.assertEqual(settings_mac.open_settings("iq", popen=popen, running=lambda: False), "started")
        self.assertEqual(popen.call_args.args[0][-2:], ["--settings", "iq"])
        settings_mac.open_settings(None, popen=popen, running=lambda: False)
        self.assertEqual(popen.call_args.args[0][-1], "--settings")

    def test_settings_running_follows_the_window_lock(self):
        import fcntl
        self.assertFalse(settings_mac.settings_running())
        path = settings_mac.data_dir() / "mac-settings.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen = []
            # flock is per open file description: check from another thread's own handle.
            thread = threading.Thread(target=lambda: seen.append(settings_mac.settings_running()))
            thread.start()
            thread.join()
            self.assertEqual(seen, [True])
        self.assertFalse(settings_mac.settings_running())


class LaunchTest(TempData):
    def test_login_item_is_a_launch_agent_that_opens_the_bundle(self):
        agent = Path(self.folder.name) / "LaunchAgents" / "x.plist"
        bundle = Path("/Applications/Circlow.app")
        with patch.object(settings_mac, "launch_agent_path", return_value=agent), \
                patch.object(settings_mac, "app_bundle", return_value=bundle):
            self.assertFalse(settings_mac.login_item_enabled())
            settings_mac.set_login_item(True)
            self.assertTrue(settings_mac.login_item_enabled())
            with agent.open("rb") as handle:
                plist = plistlib.load(handle)
            self.assertEqual(plist["ProgramArguments"], ["/usr/bin/open", "-a", str(bundle)])
            self.assertTrue(plist["RunAtLoad"])
            settings_mac.set_login_item(False)
            self.assertFalse(agent.exists())

    def test_relaunch_reopens_the_bundle_after_a_pause(self):
        with patch.object(settings_mac, "app_bundle", return_value=Path("/Applications/Circlow Beta.app")):
            command = settings_mac.relaunch_command()
        self.assertEqual(command[:2], ["/bin/sh", "-c"])
        self.assertIn('sleep 1; /usr/bin/open -n "/Applications/Circlow Beta.app"', command[2])

    def test_shell_open_uses_open_and_skips_windows_targets(self):
        with patch.object(settings_mac.subprocess, "Popen") as popen:
            settings_mac.shell_open("ms-settings:taskbar")
            popen.assert_not_called()
            settings_mac.shell_open(Path("/tmp/x"))
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/open", "/tmp/x"])


class MenuCommandTest(unittest.TestCase):
    def tray(self):
        tray = Mock()
        tray.iq_lock = threading.Lock()
        return tray

    def test_refresh_and_end_commands(self):
        tray = self.tray()
        self.assertIsNone(settings_mac.handle_menu_command(tray, {"name": "refresh", "providers": ["mimo"]}, 5.0))
        tray.scheduler.request.assert_called_once_with(["mimo"], 5.0, "设置页刷新")
        tray.wake.set.assert_called_once()
        self.assertEqual(settings_mac.handle_menu_command(tray, {"name": "restart"}), "restart")
        self.assertEqual(settings_mac.handle_menu_command(tray, {"name": "quit"}), "quit")
        with self.assertLogs("Circlow", "WARNING"):
            self.assertIsNone(settings_mac.handle_menu_command(tray, {"name": "bogus"}))

    def test_reload_rereads_preferences_summaries_and_iq(self):
        import tray_app
        tray = self.tray()
        with patch.object(tray_app, "load_preferences", return_value={"herdr": False}), \
                patch("model_iq.load_config", return_value="cfg"), patch("model_iq.load_state", return_value="st"), \
                patch("grok_iq.load_state", return_value="grok"):
            settings_mac.handle_menu_command(tray, {"name": "reload"})
        tray.apply_preferences.assert_called_once_with({"herdr": False})
        self.assertEqual((tray.iq_config, tray.iq_state, tray.grok_iq), ("cfg", "st", "grok"))
        tray.iq_wake.set.assert_called_once()


class SettingsTrayTest(TempData):
    def settings_tray(self):
        import tray_app
        with patch.object(tray_app.QuotaTray, "__init__", lambda self, icons=None: None):
            tray = settings_mac.make_settings_tray()
        tray.stop_event = threading.Event()
        return tray

    def test_actions_for_the_menu_bar_become_commands(self):
        import tray_app
        tray = self.settings_tray()
        tray.request_refresh()
        tray.exit_app()
        self.assertTrue(tray.stop_event.is_set())
        self.assertEqual([c["name"] for c in settings_mac.take_commands(settings_mac.MENU)], ["refresh", "quit"])
        tray.stop_event.clear()
        saved = ["codex", "grok"]                                 # the switches as saved right now
        with patch.object(tray_app.provider_registry, "enabled_keys", side_effect=lambda: list(saved)), \
                patch.object(tray_app.provider_registry, "set_provider_choices") as choose:
            tray.set_providers({"codex": True})                   # no change: nothing happens
            choose.assert_not_called()
            tray.set_providers({"codex": False})
            choose.assert_called_with({"codex": False})
            saved.remove("codex")
            tray.set_providers({"codex": True})                   # back on in the same sitting: a change too
            choose.assert_called_with({"codex": True})
        self.assertEqual(settings_mac.take_commands(settings_mac.MENU), [{"name": "restart"}, {"name": "restart"}])
        self.assertFalse(tray.stop_event.is_set())                # the window stays open while the menu bar restarts

    def test_startup_switch_uses_the_login_item_and_mimo_login_explains(self):
        tray = self.settings_tray()
        with patch.object(settings_mac, "login_item_enabled", return_value=False), \
                patch.object(settings_mac, "set_login_item") as set_item:
            self.assertFalse(tray.startup_enabled())
            tray.toggle_startup()
        set_item.assert_called_once_with(True)
        with patch.object(settings_mac, "show_text") as show:
            tray.mimo_login()
        self.assertIn("Mac 版暂不支持", tray.mimo_login_result)
        show.assert_called_once()


class RetinaPictureTest(unittest.TestCase):
    """Settings pictures drawn at the screen's scale, Tk given 1x copies."""

    def test_intersect_and_flip_to_the_content_view(self):
        self.assertEqual(settings_mac.intersect((0, 0, 10, 10), (5, 5, 10, 10)), (5, 5, 5, 5))
        self.assertIsNone(settings_mac.intersect((0, 0, 10, 10), (10, 0, 5, 5)))
        self.assertIsNone(settings_mac.intersect(None, (0, 0, 1, 1)))
        overlay = settings_mac.NativeOverlay.__new__(settings_mac.NativeOverlay)
        overlay.content = Mock(isFlipped=Mock(return_value=False))
        self.assertEqual(overlay.to_content((10, 20, 30, 40), 760), ((10, 700), (30, 40)))
        overlay.content.isFlipped.return_value = True
        self.assertEqual(overlay.to_content((10, 20, 30, 40), 760), ((10, 20), (30, 40)))

    def test_picture_factor_follows_the_override(self):
        import settings_window
        with patch.dict(os.environ, {"CODEX_QUOTA_PICTURE_SCALE": "2"}):
            self.assertEqual(settings_window.picture_factor(), 2)
        with patch.dict(os.environ, {"CODEX_QUOTA_PICTURE_SCALE": "1"}):
            self.assertEqual(settings_window.picture_factor(), 1)

    def test_photo_gives_tk_a_one_x_copy_and_keeps_the_original(self):
        from PIL import Image
        import settings_window

        class Photo:
            count = 0

            def __init__(self, image):
                Photo.count += 1
                self.name, self.size, self.pasted = f"photo{Photo.count}", image.size, []

            def width(self):
                return self.size[0]

            def height(self):
                return self.size[1]

            def paste(self, image):
                self.pasted.append(image.size)

            def __str__(self):
                return self.name

        window = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
        window.f, window.hires, window.ImageTk = 2, {}, Mock(PhotoImage=Photo)
        sharp = Image.new("RGB", (480, 338), "#202020")
        photo = window.photo(sharp)
        self.assertEqual(photo.size, (240, 169))                 # Tk lays it out at its own size
        self.assertIs(window.hires["photo1"], sharp)             # the overlay shows this one
        again = window.photo(Image.new("RGB", (480, 338), "#303030"), photo)
        self.assertIs(again, photo)                              # same size: pasted, no new photo
        self.assertEqual(photo.pasted, [(240, 169)])
        window.f, window.hires = 1, {}
        plain = window.photo(Image.new("RGB", (10, 10)))
        self.assertEqual(plain.size, (10, 10))
        self.assertEqual(window.hires, {})                       # Windows: nothing kept


class NativeSwitchTest(unittest.TestCase):
    """Settings switches as NSSwitch over a blank Tk canvas."""

    def overlay(self):
        overlay = settings_mac.NativeOverlay.__new__(settings_mac.NativeOverlay)
        overlay.switches, overlay.by_tag = {}, {}
        return overlay

    def test_flipping_the_native_switch_sets_the_variable_and_runs_the_command(self):
        overlay = self.overlay()
        variable, command = Mock(), Mock()
        variable.get.return_value = False
        overlay.add_switch("canvas.a", variable, command)
        overlay.add_switch("canvas.b", Mock(), None)
        self.assertEqual(overlay.by_tag, {1: "canvas.a", 2: "canvas.b"})
        overlay.switch_toggled(1, True)
        variable.set.assert_called_once_with(True)
        command.assert_called_once()
        variable.get.return_value = True
        overlay.switch_toggled(1, True)                  # already on: no second write
        variable.set.assert_called_once()
        overlay.switch_toggled(99, True)                 # unknown tag: ignored

    def test_switches_made_while_the_window_builds_are_queued(self):
        import settings_window
        with patch.object(settings_window.SettingsWindow, "__init__", lambda self, *a, **k: None), \
                patch.object(settings_mac, "NativeOverlay") as overlay_class:
            window = settings_mac.make_window(None, None)
        overlay_class.assert_called_once_with(window)
        overlay_class.return_value.start.assert_called_once()
        window.tk = Mock()
        variable = Mock()
        with patch.object(settings_mac, "native_switch_size", return_value=(54, 24)):
            canvas = window.switch({"bg": "#202020"}, variable)
        self.assertEqual(window.tk.Canvas.call_args.kwargs["width"], 54)
        overlay_class.return_value.add_switch.assert_called_with(canvas, variable, None)


if __name__ == "__main__":
    unittest.main()
