import time
import shutil
import os
from datetime import datetime
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cli_usage
import cli_quota
import json
import subprocess
import tempfile
import quota_sources
import sys
import psutil
from cli_usage import parse_agy_usage_json, parse_grok_usage, parse_reset, run_temporary_usage

# Avoid Store/venv application aliases in real process-lifecycle fixtures.
PYTHON_EXE = psutil.Process().exe()


class JsonRpcLifecycleTests(unittest.TestCase):
    def test_repeated_reads_reap_launcher_descendants_and_reader_threads(self):
        self._check_tree_cleanup(timeout=False, repeats=3)

    def test_timeout_reaps_launcher_descendants_without_stopping_other_clients(self):
        self._check_tree_cleanup(timeout=True, repeats=1)

    def _check_tree_cleanup(self, timeout, repeats):
        # A real launcher -> intermediate -> server chain reproduces Windows
        # cmd -> Node -> Codex, including the inherited stdout pipe.
        worker = '''import json, os, subprocess, sys, time
if len(sys.argv) > 1:
    child = subprocess.Popen([sys.executable, "-u", __file__, *sys.argv[2:]])
    child.wait()
else:
    for line in sys.stdin:
        request = json.loads(line)
        if request["method"] == "hang":
            time.sleep(60)
        else:
            print(json.dumps({"id": request["id"], "result": {"pid": os.getpid()}}), flush=True)
'''
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "rpc_worker.py"
            script.write_text(worker, encoding="utf-8")
            unrelated = subprocess.Popen([PYTHON_EXE, "-c", "import time; time.sleep(60)"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                for _ in range(repeats):
                    rpc = quota_sources._JsonRpcProcess([PYTHON_EXE, "-u", str(script), "launcher", "intermediate"])
                    owned = []
                    try:
                        leaf_pid = rpc.request("ping", None)["pid"]
                        owned = psutil.Process(rpc.process.pid).children(recursive=True)
                        self.assertGreaterEqual(len(owned), 2)
                        self.assertIn(leaf_pid, [child.pid for child in owned])
                        if timeout:
                            with self.assertRaises(TimeoutError):
                                rpc.request("hang", None, timeout=0.05)
                    finally:
                        rpc.close()
                    _, alive = psutil.wait_procs(owned, timeout=3)
                    self.assertEqual(alive, [])
                    self.assertIsNotNone(rpc.process.poll())
                    self.assertFalse(rpc._reader.is_alive())
                    self.assertTrue(rpc.process.stdin.closed)
                    self.assertTrue(rpc.process.stdout.closed)
                    self.assertIsNone(unrelated.poll())
            finally:
                unrelated.kill()
                unrelated.wait(timeout=3)


class GrokIsolationTests(unittest.TestCase):
    def test_native_startup_hang_is_bounded_and_preserves_last_good_quota(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("worker", 35)
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}), \
                patch.object(cli_quota, "no_quota_reason") as claude_status, \
                patch.object(cli_quota.process_jail, "popen", return_value=process), \
                patch.object(cli_quota, "stop_worker") as stop:
            snapshot = quota_sources.get_grok_quota()
            self.assertIn("35 秒", snapshot.error)
            self.assertIn("隐藏终端初始化", snapshot.error)
            process.wait.assert_called_once_with(timeout=35)
            stop.assert_called_once_with(process)
            claude_status.assert_not_called()
            old = quota_sources.QuotaSnapshot("grok", "Grok", 10080, 30, 70, 2_000_000, 1_000_000, False, None)
            kept = quota_sources.keep_last_good(snapshot, old)
            self.assertEqual(kept.remaining_percent, 70)
            self.assertTrue(kept.stale)

    def test_repeated_frozen_reads_get_fresh_workers_and_transfer_cli_reset(self):
        commands = []
        def start(command, **kwargs):
            commands.append(command)
            self.assertEqual(command[1], "--grok-quota-worker")
            Path(command[-1]).write_text(json.dumps({"quota": {"remaining_percent": 70},
                "cli": {"next_reset": "Next reset: October 27, 11:02"}}), encoding="utf-8")
            return Mock()
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}), \
                patch.object(cli_quota.sys, "frozen", True, create=True), \
                patch.object(cli_quota.process_jail, "popen", side_effect=start):
            for _ in range(2):
                raw = {}
                self.assertEqual(cli_quota.read_usage("grok.exe", "grok", raw)["remaining_percent"], 70)
                self.assertEqual(raw["next_reset"], "Next reset: October 27, 11:02")
            self.assertNotEqual(commands[0][-1], commands[1][-1])
            self.assertEqual(list((Path(folder) / "Circlow/work").iterdir()), [])

    def test_grok_worker_exports_only_quota_lines_without_identity(self):
        text = "private@example.test\nWeekly limit: 30%\nNext reset: October 27, 11:02\nSECRET-TOKEN"
        with tempfile.TemporaryDirectory() as folder, patch.object(cli_quota, "run_temporary_usage", return_value=text):
            destination = Path(folder) / "result.json"
            self.assertEqual(cli_quota.worker("grok.exe", str(destination), "grok"), 0)
            result = destination.read_text(encoding="utf-8")
            self.assertNotIn("private@", result)
            self.assertNotIn("SECRET-TOKEN", result)
            parsed = json.loads(result)
            self.assertEqual(parsed["quota"]["remaining_percent"], 70)
            self.assertEqual(parsed["cli"]["next_reset"], "Next reset: October 27, 11:02")

    def test_timeout_cleanup_targets_only_the_owned_worker_tree(self):
        process = Mock(pid=1234)
        children = [Mock(), Mock()]
        with patch.object(cli_quota.psutil, "Process") as lookup:
            lookup.return_value.children.return_value = children
            cli_quota.stop_worker(process)
        lookup.assert_called_once_with(1234)
        lookup.return_value.children.assert_called_once_with(recursive=True)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        for child in children:
            child.kill.assert_called_once_with()


class QuotaTests(unittest.TestCase):
    def test_claude_same_window_drop_is_held_as_stale_not_a_reset(self):
        now = time.time()
        reset, week = int(now + 3600), int(now + 7200)
        old = quota_sources.QuotaSnapshot("claude", "Claude Code", 300, 80, 20, reset, now - 60, False, None,
                                           10080, 83, 17, week)
        false = quota_sources.QuotaSnapshot("claude", "Claude Code", 300, 30, 70, reset, now, False, None,
                                             10080, 71, 29, week)
        held = quota_sources.keep_last_good(false, old)
        self.assertEqual((held.remaining_percent, held.secondary_remaining_percent), (20, 17))
        self.assertTrue(held.stale)
        self.assertIn("异常回升", held.error)
        recovered = quota_sources.QuotaSnapshot("claude", "Claude Code", 300, 81, 19, reset, now + 60, False, None,
                                                 10080, 84, 16, week)
        self.assertIs(quota_sources.keep_last_good(recovered, old), recovered)
        full_same_reset = quota_sources.QuotaSnapshot("claude", "Claude Code", 300, 3, 97, reset, now + 60,
                                                       False, None, 10080, 84, 16, week)
        self.assertIs(quota_sources.keep_last_good(full_same_reset, old), full_same_reset)
        changed_reset = quota_sources.QuotaSnapshot("claude", "Claude Code", 300, 0, 100, reset + 5 * 3600, now + 60,
                                                     False, None, 10080, 84, 16, week)
        self.assertIs(quota_sources.keep_last_good(changed_reset, old), changed_reset)

    def test_gemini_refresh_replaces_old_argon_pools_and_failure_does_not_reuse_them(self):
        def read(groups):
            parsed = parse_agy_usage_json({"command": {"data": {"groups": groups}}})
            with patch.object(quota_sources, "run_agy_usage", return_value=parsed):
                return quota_sources.get_gemini_quota()
        argon = {"name": "Argon", "buckets": [{"id": "argon-weekly", "remaining_fraction": .5}]}
        gemini = {"name": "Gemini Models", "buckets": [{"id": "gemini-5h", "remaining_fraction": .8}]}
        third = {"name": "Claude Models", "buckets": [{"id": "3p-5h", "remaining_fraction": .6}]}
        old = read([argon, gemini, third])
        fresh = quota_sources.keep_last_good(read([gemini, third]), old)
        self.assertEqual([p["key"] for p in fresh.pools], ["claude_gpt", "gemini"])
        self.assertEqual(fresh.remaining_percent, 60)
        failed = quota_sources.keep_last_good(quota_sources.placeholder("gemini", "读取失败"), old)
        self.assertIsNone(failed.pools)
        self.assertIsNone(failed.remaining_percent)
        weekly = read([{"name": "Gemini Models", "buckets": [{"id": "gemini-weekly", "remaining_fraction": .8}]}])
        self.assertIn("每周", weekly.label)

    def sample(self, remaining=40.0, stale=False, error=None):
        return quota_sources.QuotaSnapshot(
            "sample", "Sample", 300, 100 - remaining, remaining, int(time.time()) + 100,
            time.time(), stale, error,
        )

    def test_unknown_does_not_invent_values(self):
        value = quota_sources._unknown("sample", "Sample", 300, "not available")
        self.assertIsNone(value.used_percent)
        self.assertIsNone(value.remaining_percent)
        self.assertIn("not available", value.error)

    def test_gemini_label_and_window(self):
        gemini = {"name": "Gemini Models", "buckets": [{"id": "gemini-5h", "remaining_fraction": 0.9}]}
        argon = {"name": "Argon", "buckets": [{"id": "argon-weekly", "remaining_fraction": 0.75}]}
        claude = {"name": "Claude/GPT Models", "buckets": [{"id": "claude-gpt-5h", "remaining_fraction": 0.8}]}
        for groups, minutes, label, keys in (
            ([gemini], 300, "5 小时", ["gemini"]),
            ([gemini, claude, argon], 10080, "Argon", ["argon", "claude_gpt", "gemini"]),
        ):
            with self.subTest(pools=keys):
                parsed = parse_agy_usage_json({"status": "SUCCESS", "command": {"data": {"groups": groups}}})
                with patch.object(quota_sources, "run_agy_usage", return_value=parsed):
                    value = quota_sources.get_gemini_quota()
                self.assertIsNone(value.error)
                self.assertEqual(value.window_minutes, minutes)
                self.assertIn(label, value.label)
                self.assertEqual([pool["key"] for pool in value.pools], keys)
                self.assertEqual(value.remaining_percent, 90 if len(keys) == 1 else 75)

    def test_failure_preserves_old_value_and_marks_stale(self):
        old = self.sample(42)
        original_codex = quota_sources.get_codex_quota
        quota_sources.get_codex_quota = lambda: quota_sources._unknown(
            "codex", "Codex", None, "read failed"
        )
        try:
            # Only Codex: collect_all would also start the real Grok/Claude CLIs.
            result = quota_sources.collect(["codex"], {"codex": old})["codex"]
        finally:
            quota_sources.get_codex_quota = original_codex
        self.assertEqual(result.remaining_percent, 42)
        self.assertTrue(result.stale)
        self.assertIsNotNone(result.error)

    def test_dump_snapshot_redacts_account_identity(self):
        text = quota_sources.dump_snapshot({"sample": self.sample()})
        self.assertIn("Sample", text)
        self.assertNotIn("accountId", text)
        self.assertNotIn("token", text.lower())

    def test_cli_usage_parser_reads_grok_percentage_and_reset(self):
        text = "Weekly limit: 56%\nNext reset: September 28, 11:53"
        parsed = parse_grok_usage(text)
        self.assertEqual(parsed["used_percent"], 56.0)
        self.assertEqual(parsed["remaining_percent"], 44.0)
        self.assertIsNotNone(parsed["resets_at"])

    def test_cli_usage_parser_reads_grok_panel_layout(self):
        # Grok 1.0.77 draws /usage as a panel: title, bar + percent, reset row,
        # each placed with a cursor move instead of a newline.
        box = "\x1b[38;2;88;88;88m│"
        text = (f"\x1b[14;31H{box}  \x1b[1mWeekly limit (SuperGrok)\x1b[22m          {box}"
                f"\x1b[16;31H{box}  ███████████████████████░░░░░░░  76%          {box}"
                f"\x1b[17;31H{box}  Resets: November 10, 11:02          {box}"
                f"\x1b[18;31H{box}  Loading session usage   {box}")
        parsed = parse_grok_usage(text)
        self.assertEqual(parsed["used_percent"], 76.0)
        self.assertEqual(parsed["remaining_percent"], 24.0)
        reset = datetime.fromtimestamp(parsed["resets_at"])
        self.assertEqual((reset.month, reset.day, reset.hour, reset.minute), (11, 10, 11, 2))

    @unittest.skipUnless(os.name == "nt", "Windows pywinpty behavior")
    def test_grok_waits_for_slow_session_and_retries_unavailable_usage(self):
        clock = [0.0]
        queries = []
        terminal = Mock()
        terminal.spawn.return_value = True
        terminal.isalive.return_value = True
        terminal.pid = 123

        def read(blocking=False):
            self.assertFalse(blocking)
            if queries and queries[-1] >= 6:
                return "Weekly limit: 17%\nNext reset: October 5, 11:53"
            if queries:
                return "Session usage is unavailable until the session starts."
            return "Signing in… starting your session."

        def write(text):
            if text == "/usage":
                queries.append(clock[0])

        terminal.read.side_effect = read
        terminal.write.side_effect = write
        with patch.object(cli_usage, "_create_terminal", return_value=(terminal, False)), patch.object(
            cli_usage.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(cli_usage.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch.object(cli_usage.os, "kill") as kill:
            text = run_temporary_usage("grok.exe", ["--minimal"], timeout=10)
        self.assertEqual(len(queries), 2)
        self.assertGreaterEqual(queries[0], 4)
        self.assertEqual(parse_grok_usage(text)["remaining_percent"], 83)
        kill.assert_called_once_with(123, cli_usage.signal.SIGTERM)

    @unittest.skipUnless(os.name == "nt", "Windows pywinpty behavior")
    def test_grok_timeout_does_not_use_context_percent_or_leak_terminal_output(self):
        clock = [0.0]
        terminal = Mock()
        terminal.spawn.return_value = True
        terminal.isalive.return_value = True
        terminal.pid = 123
        terminal.read.return_value = "minimal · /help 2.2K /256K (1%) identity-must-stay-private"
        with patch.object(cli_usage, "_create_terminal", return_value=(terminal, False)), patch.object(
            cli_usage.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(cli_usage.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch.object(cli_usage.os, "kill") as kill:
            with self.assertRaisesRegex(TimeoutError, "Weekly limit") as caught:
                run_temporary_usage("grok.exe", ["--minimal"], timeout=1)
        self.assertNotIn("identity", str(caught.exception))
        kill.assert_called_once_with(123, cli_usage.signal.SIGTERM)

    @unittest.skipUnless(os.name == "nt", "Windows console initialization")
    def test_grok_initializes_hidden_console_only_when_standard_handles_are_missing(self):
        kernel32 = Mock()
        kernel32.GetStdHandle.return_value = None
        kernel32.GetConsoleWindow.return_value = 456
        kernel32.AllocConsole.return_value = 1
        user32 = Mock()
        with patch.object(cli_usage.ctypes.windll, "kernel32", kernel32), patch.object(
            cli_usage.ctypes.windll, "user32", user32
        ), patch.object(cli_usage, "PTY", return_value=Mock()):
            _, owned = cli_usage._create_terminal()
            self.assertTrue(owned)
            kernel32.AllocConsole.assert_called_once()
            user32.ShowWindow.assert_called_once_with(456, 0)
            kernel32.reset_mock()
            kernel32.GetStdHandle.return_value = 123
            _, owned = cli_usage._create_terminal()
            self.assertFalse(owned)
            kernel32.AllocConsole.assert_not_called()

    def test_grok_ignores_session_context_and_handles_terminal_escape_sequences(self):
        text = "2.2K /256K (1%)\nWeekly \x1b[32mlimit:\x1b[0m 17%\nNext reset: October 5, 11:53"
        self.assertEqual(parse_grok_usage(text)["remaining_percent"], 83)
        with self.assertRaises(ValueError):
            parse_grok_usage("2.2K /256K (1%)")

    def test_cli_reset_parser_reads_next_reset_line(self):
        text = "5-hour limit: 25% used\nNext reset: 2026-09-23 14:00"
        self.assertIsNotNone(parse_reset(text))

    def test_agy_usage_json_reads_gemini_five_hour_bucket(self):
        payload = {
            "status": "SUCCESS",
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"id": "gemini-weekly", "remaining_fraction": 0.88},
                                {
                                    "id": "gemini-5h",
                                    "remaining_fraction": 0.625,
                                    "reset_time": "2026-09-23T06:00:00Z",
                                },
                            ],
                        }
                    ]
                }
            },
        }
        parsed = parse_agy_usage_json(payload)
        self.assertEqual(parsed["window_minutes"], 300)
        self.assertEqual(parsed["remaining_percent"], 62.5)
        self.assertEqual(parsed["used_percent"], 37.5)
        self.assertEqual(parsed["resets_at"], 1790143200)

    def test_agy_three_pools_ring_prefers_argon_then_claude_gpt_then_gemini(self):
        def payload(*groups):
            return {"status": "SUCCESS", "command": {"data": {"groups": list(groups)}}}
        gemini = {"name": "Gemini Models", "buckets": [{"id": "gemini-5h", "remaining_fraction": 0.9, "reset_time": "2026-09-23T06:00:00Z"},
                                                        {"id": "gemini-weekly", "remaining_fraction": 0.7}]}
        claude = {"name": "Claude and GPT Models", "buckets": [{"id": "claude-gpt-5h", "remaining_fraction": 0.4}]}
        argon = {"name": "Argon", "buckets": [{"id": "argon-5h", "remaining_fraction": 0.25, "reset_time": "2026-09-23T07:00:00Z"}]}
        parsed = parse_agy_usage_json(payload(gemini, claude, argon))
        self.assertEqual([pool["key"] for pool in parsed["pools"]], ["argon", "claude_gpt", "gemini"])
        self.assertEqual((parsed["pool"], parsed["remaining_percent"], parsed["window_minutes"]), ("Argon", 25.0, 300))
        self.assertEqual(parsed["pools"][2]["remaining_percent"], 70.0)          # binding weekly bucket
        self.assertEqual(parsed["pools"][2]["window_minutes"], 10080)
        parsed = parse_agy_usage_json(payload(gemini, claude))
        self.assertEqual((parsed["pool"], parsed["remaining_percent"]), ("Claude/GPT", 40.0))
        # Buckets for several families inside one group are split by their own ids.
        mixed = {"name": "Models", "buckets": [{"id": "gemini-5h", "remaining_fraction": 0.5}, {"id": "gpt-oss-5h", "remaining_fraction": 0.6}]}
        self.assertEqual([p["key"] for p in parse_agy_usage_json(payload(mixed))["pools"]], ["claude_gpt", "gemini"])
        with self.assertRaises(ValueError):
            parse_agy_usage_json(payload())

    def test_cached_snapshots_are_marked_stale(self):
        original = quota_sources.CACHE_PATH
        folder = tempfile.mkdtemp()                # never the real data folder
        temporary = Path(folder) / "state-test.json"
        quota_sources.CACHE_PATH = temporary
        try:
            quota_sources.save_cached_snapshots({"sample": self.sample(42)})
            loaded = quota_sources.load_cached_snapshots()["sample"]
            self.assertEqual(loaded.remaining_percent, 42)
            self.assertTrue(loaded.stale)
        finally:
            quota_sources.CACHE_PATH = original
            shutil.rmtree(folder, ignore_errors=True)
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
