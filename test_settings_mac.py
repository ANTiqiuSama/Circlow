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
        shown = next(iter(tray_app.PROVIDERS))
        with patch.object(tray_app.provider_registry, "set_provider_choices") as choose:
            tray.set_providers({shown: True})                 # no change: nothing happens
            choose.assert_not_called()
            tray.set_providers({shown: False})
        choose.assert_called_once_with({shown: False})
        self.assertEqual(settings_mac.take_commands(settings_mac.MENU), [{"name": "restart"}])
        self.assertTrue(tray.stop_event.is_set())

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


if __name__ == "__main__":
    unittest.main()
