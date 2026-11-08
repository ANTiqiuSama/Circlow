"""Claude readiness: registry, /usage parsing and probe, transcript reader."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import cli_usage
import cli_quota as claude_quota
import subprocess
import providers
import quota_sources
from cli_usage import CLAUDE_PROBE, claude_usage_complete, parse_claude_reset, parse_claude_usage, run_temporary_usage
from icon_art import ACCENTS, ROSE, accent, quota_icon
from refresh_scheduler import IDLE, SETTLE, RefreshScheduler
from tps_sources import TpsMonitor

USAGE = (
    "\x1b[2m Settings:  Status   Config   \x1b[1mUsage\x1b[0m  (tab to cycle)\n\n"
    " Current session\n \x1b[36m█████████▌\x1b[0m                         19% used\n Resets 5pm (Asia/Shanghai)\n\n"
    " Current week (all models)\n ███▌                                7% used\n Resets Oct 23, 3:30pm (Asia/Shanghai)\n\n"
    " Current week (Opus)\n                                     0% used\n"
)
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)  # 14:00 in Shanghai


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        environment = patch.dict("os.environ", {"LOCALAPPDATA": self.data.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_explicit_list_keeps_registry_order(self):
        self.assertEqual(providers.enabled_keys("claude, codex"), ["codex", "claude"])
        self.assertEqual(providers.enabled_keys("grok;nope"), ["grok"])

    def test_installed_claude_waits_for_manual_enable(self):
        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": home, "CIRCLOW_CLAUDE_EXE": ""}):
            with patch.object(providers.shutil, "which", return_value=None), patch.object(providers.Path, "home", return_value=Path(home)):
                self.assertNotIn("claude", providers.enabled_keys(""))
                (Path(home) / "projects").mkdir()
                self.assertNotIn("claude", providers.enabled_keys(""))
                providers.set_claude_enabled(True)
                self.assertIn("claude", providers.enabled_keys("codex,grok,gemini,mimo"))
                providers.set_claude_enabled(False)
                self.assertNotIn("claude", providers.enabled_keys("claude,codex"))

    def test_everything_derives_from_registry(self):
        self.assertEqual(quota_sources.LABELS["claude"], ("Claude Code", 300))
        self.assertEqual(IDLE["claude"], providers.get("claude").idle_seconds)
        self.assertEqual(SETTLE["claude"], 5)
        self.assertEqual(accent("A"), providers.get("claude").accent)
        self.assertEqual(set(ACCENTS), {"C", "G", "M", "A", "X"})
        # Danger is no longer Claude's clay, so a low Claude ring stays distinct.
        self.assertNotEqual(ROSE, accent("A"))
        self.assertEqual(quota_icon(50, "A").size, (64, 64))
        scheduler = RefreshScheduler(["codex", "claude"], 0)
        self.assertEqual(scheduler.take_due(0), ["codex", "claude"])


class UsageParsingTests(unittest.TestCase):
    def setUp(self):
        quota_sources._CLAUDE_PROBE.clear()
        self.addCleanup(quota_sources._CLAUDE_PROBE.clear)
        self.native = patch('claude_status.latest', return_value=None)
        self.bridge = patch('claude_status.bridge_installed', return_value=False)
        self.native.start()
        self.bridge.start()
        self.addCleanup(self.native.stop)
        self.addCleanup(self.bridge.stop)
    def test_session_and_week_windows(self):
        with patch.object(cli_usage, "datetime", wraps=datetime) as fake:
            fake.now.side_effect = lambda tz=None: NOW
            result = parse_claude_usage(USAGE)
        self.assertEqual((result["used_percent"], result["remaining_percent"], result["window_minutes"]), (19, 81, 300))
        self.assertEqual((result["secondary_used_percent"], result["secondary_window_minutes"]), (7, 10080))
        self.assertEqual(result["resets_at"], int(datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(result["secondary_resets_at"], int(datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(claude_usage_complete(USAGE))
        self.assertFalse(claude_usage_complete(USAGE.split("Current week")[0]))

    def test_reset_formats(self):
        stamp = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp())
        self.assertEqual(parse_claude_reset("5pm (UTC)", NOW), stamp(2026, 10, 16, 17))
        self.assertEqual(parse_claude_reset("3am (UTC)", NOW), stamp(2026, 10, 17, 3))      # already past -> tomorrow
        self.assertEqual(parse_claude_reset("12am (UTC)", NOW), stamp(2026, 10, 17, 0))
        self.assertEqual(parse_claude_reset("Oct 23 at 9:15am (UTC)", NOW), stamp(2026, 10, 23, 9, 15))
        self.assertEqual(parse_claude_reset("Thu, Oct 23, 3pm (UTC)", NOW), stamp(2026, 10, 23, 15))
        self.assertEqual(parse_claude_reset("Jan 2, 1pm (UTC)", NOW), stamp(2027, 1, 2, 13))
        self.assertIsNone(parse_claude_reset("soon", NOW))

    def test_percent_left_and_missing_week(self):
        result = parse_claude_usage("Current session\n 60% left\n")
        self.assertEqual(result["used_percent"], 40)
        self.assertNotIn("secondary_used_percent", result)

    def test_api_key_accounts_get_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "API Key"):
            parse_claude_usage("Total cost: $0.12\nUsage by model: ...")

    def test_snapshot_carries_both_windows(self):
        with patch.object(providers, "find_claude", return_value=Path("claude.exe")), \
                patch.object(claude_quota, "read_usage", return_value=parse_claude_usage(USAGE)):
            snapshot = quota_sources.get_claude_quota()
        self.assertIsNone(snapshot.error)
        self.assertEqual((snapshot.remaining_percent, snapshot.secondary_remaining_percent, snapshot.display_remaining), (81, 93, 81))

    def test_missing_cli_is_reported_not_raised(self):
        with patch.object(providers, "find_claude", return_value=None):
            snapshot = quota_sources.get_claude_quota()
        self.assertIn("CIRCLOW_CLAUDE_EXE", snapshot.error)


class ReaderIsolationTests(unittest.TestCase):
    def setUp(self):
        quota_sources._CLAUDE_PROBE.clear()
        self.addCleanup(quota_sources._CLAUDE_PROBE.clear)
        self.native = patch('claude_status.latest', return_value=None)
        self.bridge = patch('claude_status.bridge_installed', return_value=False)
        self.native.start()
        self.bridge.start()
        self.addCleanup(self.native.stop)
        self.addCleanup(self.bridge.stop)
    def test_logged_out_account_skips_native_terminal_and_sleeps_twelve_hours(self):
        status = Mock(stdout=json.dumps({"loggedIn": False, "email": "private@example.test"}))
        with patch.object(providers, "find_claude", return_value=Path("claude.exe")), \
                patch.object(claude_quota.process_jail, "run", return_value=status), \
                patch.object(claude_quota.subprocess, "Popen") as spawn:
            snapshot = quota_sources.get_claude_quota()
        spawn.assert_not_called()
        self.assertIn("未登录", snapshot.error)
        self.assertNotIn("private@", snapshot.error)
        scheduler = RefreshScheduler(["claude"], 100)
        scheduler.completed("claude", snapshot, 100)
        self.assertEqual(scheduler.due["claude"], 100 + 12 * 3600)
        self.assertEqual(scheduler.reason["claude"], "无可用额度")

    def test_free_and_api_accounts_have_no_subscription_quota(self):
        for fields in ({"loggedIn": True, "subscriptionType": "free"},
                       {"loggedIn": True, "authMethod": "api_key"}):
            with self.subTest(fields=fields), patch.object(claude_quota.process_jail, "run",
                    return_value=Mock(stdout=json.dumps(fields))):
                self.assertIn("订阅额度", claude_quota.no_quota_reason("claude.exe"))

    def test_old_status_command_still_uses_bounded_worker(self):
        with patch.object(claude_quota.process_jail, "run", return_value=Mock(stdout="unknown command")):
            self.assertIsNone(claude_quota.no_quota_reason("claude.exe"))
        with patch.object(claude_quota.process_jail, "run", side_effect=subprocess.TimeoutExpired("claude", 10)):
            self.assertIsNone(claude_quota.no_quota_reason("claude.exe"))

    def test_native_terminal_hang_cannot_hold_refresh_thread_forever(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("worker", 45)
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}), \
                patch.object(claude_quota, "no_quota_reason", return_value=None), \
                patch.object(claude_quota.process_jail, "popen", return_value=process), \
                patch.object(claude_quota, "stop_worker") as stop:
            with self.assertRaisesRegex(TimeoutError, "隐藏终端初始化"):
                claude_quota.read_usage("claude.exe")
            process.wait.assert_called_once_with(timeout=45)
            stop.assert_called_once_with(process)
            self.assertEqual(list((Path(folder) / "Circlow/work").iterdir()), [])

    def test_worker_transfers_only_parsed_quota_numbers(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(claude_quota, "run_temporary_usage", return_value=USAGE + "\nprivate@example.test"):
            destination = Path(folder) / "result.json"
            self.assertEqual(claude_quota.worker("claude.exe", str(destination)), 0)
            raw = destination.read_text(encoding="utf-8")
            self.assertNotIn("private@", raw)
            self.assertEqual(json.loads(raw)["quota"]["remaining_percent"], 81)

    def test_frozen_parent_reads_worker_result_without_recursing(self):
        def start(command, **kwargs):
            self.assertEqual(command[1], "--claude-quota-worker")
            Path(command[-1]).write_text(json.dumps({"quota": {"remaining_percent": 81}}), encoding="utf-8")
            return Mock()
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}), \
                patch.object(claude_quota, "no_quota_reason", return_value=None), \
                patch.object(claude_quota.sys, "frozen", True, create=True), \
                patch.object(claude_quota.process_jail, "popen", side_effect=start):
            self.assertEqual(claude_quota.read_usage("claude.exe"), {"remaining_percent": 81})


@unittest.skipUnless(os.name == "nt", "Windows pywinpty behavior")
class ProbeTests(unittest.TestCase):
    def run_probe(self, screens, executable="claude.exe", probe=CLAUDE_PROBE):
        clock, writes = [0.0], []
        terminal = Mock(pid=321)
        terminal.spawn.return_value = True
        terminal.isalive.return_value = True
        terminal.read.side_effect = lambda blocking=False: screens(writes, clock[0])
        terminal.write.side_effect = writes.append
        with tempfile.TemporaryDirectory() as data, patch.dict("os.environ", {"LOCALAPPDATA": data}), \
                patch.object(cli_usage, "PTY", object()), \
                patch.object(cli_usage, "_create_terminal", return_value=(terminal, False)), \
                patch.object(cli_usage.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(cli_usage.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                patch.object(cli_usage.os, "kill"):
            text = run_temporary_usage(executable, ["--strict-mcp-config"], timeout=20, probe=probe)
            return text, writes, terminal

    def test_trust_prompt_is_answered_then_usage_read_in_probe_folder(self):
        def screens(writes, now):
            if "/usage" in writes:
                return USAGE
            if "\r" in writes:
                return "? for shortcuts"
            return "Do you trust the files in this folder?" if now > 0.5 else ""
        text, writes, terminal = self.run_probe(screens)
        self.assertTrue(claude_usage_complete(text))
        self.assertEqual(writes[0], "\r")          # trust answered once, before /usage
        self.assertEqual(writes.count("/usage"), 1)
        cwd = terminal.spawn.call_args[0][2]
        self.assertTrue(cwd.endswith("claude-probe"))

    def test_workspace_dialog_opening_on_no_steps_down_before_confirming(self):
        # Claude Code 2.1.316: words placed by cursor moves, "No, exit" preselected.
        dialog = ("\x1b[2GQuick\x1b[8Gsafety\x1b[15Gcheck:\r\n"
                  "\x1b[2G\x1b[38;2;177;185;249m❯\x1b[4GNo,\x1b[8Gexit\x1b[39m\r\n"
                  "\x1b[4GYes,\x1b[9GI\x1b[11Gtrust\x1b[17Gthis\x1b[22Gfolder\r\n")
        moved = ("\x1b[4GNo,\x1b[8Gexit\r\n"
                 "\x1b[2G❯\x1b[4GYes,\x1b[9GI\x1b[11Gtrust\x1b[17Gthis\x1b[22Gfolder\r\n")

        def screens(writes, now):
            if "/usage" in writes:
                return USAGE
            if "\r" in writes:
                return "? for shortcuts"
            if "\x1b[B" in writes:
                return dialog + moved
            return dialog if now > 0.5 else ""
        text, writes, _ = self.run_probe(screens)
        self.assertTrue(claude_usage_complete(text))
        self.assertEqual(writes[:2], ["\x1b[B", "\r"])   # never Enter on "No, exit"
        self.assertEqual(writes.count("/usage"), 1)

    def test_grok_billing_outage_fails_at_once_with_its_reason(self):
        # Grok's own service failing; a partial redraw drops the "o" of "Couldn't".
        outage = ("Session usage: no model calls yet in this session.\r\n"
                  "\x1b[9;34HC\x1b[9;36Huld\x1b[9;40H't load usage: Internal error: "
                  "\"Billing service error: cursor billing: meter\x1b[10;34Hunavailable\"")
        started = []

        def screens(writes, now):
            started.append(now)
            return outage if "/usage" in writes else "minimal · /help"
        with self.assertRaisesRegex(RuntimeError, "Grok 官方额度服务出错"):
            self.run_probe(screens, "grok.exe", cli_usage.GROK_PROBE)
        self.assertLess(started[-1], 5)          # not the 20-second timeout

    def test_cursor_placed_words_keep_their_spaces(self):
        self.assertEqual(cli_usage.clean_terminal_text("\x1b[2GCurrent\x1b[10Gsession\x1b[1C4%"), " Current session 4%")

    def test_npm_shim_runs_through_cmd(self):
        _, _, terminal = self.run_probe(lambda writes, now: USAGE if "/usage" in writes else "? for shortcuts", "C:/npm/claude.cmd")
        app, cmdline = terminal.spawn.call_args[0][:2]
        self.assertTrue(app.lower().endswith("cmd.exe"))
        self.assertIn("claude.cmd", cmdline)


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.folder = self.home / ".claude/projects/C--work-demo"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "sess-1.jsonl"
        self.path.write_text("")
        self.monitor = TpsMonitor(home=self.home)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *records):
        with self.path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    @staticmethod
    def assistant(mid, output, moment, stop=None, model="claude-sonnet-4-5"):
        iso = datetime.fromtimestamp(moment, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"type": "assistant", "timestamp": iso, "sessionId": "sess-1",
                "message": {"id": mid, "model": model, "stop_reason": stop, "content": [{"type": "text", "text": "SECRET"}],
                            "usage": {"input_tokens": 900, "cache_read_input_tokens": 5000, "output_tokens": output}}}

    def test_streamed_blocks_count_once_with_final_usage(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_1", 12, now - 4), self.assistant("msg_1", 300, now - 2))
        snapshot = self.monitor.poll(now)
        claude = snapshot["providers"]["claude"]
        self.assertEqual(claude["active_sessions"], 1)           # still pending: generating
        self.assertEqual(claude["output_tokens_60"], 0)
        self.write(self.assistant("msg_1", 420, now - 1, stop="end_turn"))
        claude = self.monitor.poll(now)["providers"]["claude"]
        self.assertEqual(claude["output_tokens_60"], 420)         # not 12+300+420, no input/cache
        self.assertIsNone(claude["generation_tps"])                # no native TPS -> none invented
        self.assertNotIn("SECRET", json.dumps(self.monitor.snapshot(now), ensure_ascii=False))

    def test_next_record_finalises_previous_message_and_sidechains_are_read(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_a", 50, now - 10, stop="tool_use"),
                   {"type": "user", "timestamp": datetime.fromtimestamp(now - 8, timezone.utc).isoformat(), "message": {"content": []}})
        sub = self.folder / "sess-1/subagents/agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps(self.assistant("msg_b", 70, now - 5, stop="end_turn")) + "\n")
        self.monitor.last_discovery = 0
        self.monitor.last_slow_discovery = 0
        self.monitor.poll(now)
        claude = self.monitor.poll(now + 0.1)["providers"]["claude"]
        self.assertEqual(claude["output_tokens_60"], 120)
        self.assertEqual(self.monitor.health["Claude Code"]["status"], "已接入")

    def test_quiet_unfinished_message_is_flushed_and_activity_cleared(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_x", 80, now - 1))
        self.monitor.poll(now)
        later = now + 100
        claude = self.monitor.poll(later)["providers"]["claude"]
        self.assertEqual(claude["active_sessions"], 0)
        self.assertEqual(sum(e.output_tokens for e in self.monitor.events if e.provider == "claude"), 80)

    def test_synthetic_messages_are_ignored(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_s", 0, now - 1, stop="end_turn", model="<synthetic>"))
        self.assertEqual(self.monitor.poll(now)["providers"]["claude"]["output_tokens_60"], 0)

    def test_api_request_reference_measures_reply_without_tool_time_or_duplicates(self):
        now = time.time()
        self.monitor.poll(now)
        reply = self.assistant('msg-measured', 500, now - 1, stop='end_turn', model='claude-opus-5-5')
        reply['requestRef'] = 'request-2'
        self.write({'type':'user', 'timestamp': now - 60, 'message':{}},
                   {'type':'api-request', 'id':'request-2', 'timestamp':now - 6, 'params':{'model':'claude-opus-5-5'}},
                   reply, reply)
        value = self.monitor.poll(now)['providers']['claude']
        self.assertEqual(value['display_tps'], 100.0)
        self.assertEqual(value['output_tokens_60'], 500)
        self.assertIn('本次 API 请求', value['display_basis'])
        self.assertIn('含首字延迟', value['display_basis'])

    def test_completed_reply_replay_does_not_leave_a_generating_session(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant('msg-ended', 500, now - 10, stop='end_turn'))
        self.assertEqual(self.monitor.poll(now)['providers']['claude']['active_sessions'], 0)


if __name__ == "__main__":
    unittest.main()
