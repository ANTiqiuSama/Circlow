import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_status
import quota_sources
import tps_sources


class ClaudeStatusTests(unittest.TestCase):
    def setUp(self):
        quota_sources._CLAUDE_PROBE.clear()
        self.addCleanup(quota_sources._CLAUDE_PROBE.clear)

    def test_desktop_usage_requires_a_real_increase_and_same_account_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'plan-usage-history.json'
            rows = [{'t':1000_000,'org':'private-scope','u':{'fh':13,'sd':1}},
                    {'t':1100_000,'org':'private-scope','u':{'fh':15,'sd':2}}]
            path.write_text(json.dumps({'samples':rows}))
            self.assertEqual(claude_status.recent_desktop_usage(1200, roots=[root]), 1100)
            self.assertEqual(claude_status.recent_desktop_usage(3001, roots=[root]), 0)
            rows[-1]['org'] = 'another-scope'
            path.write_text(json.dumps({'samples':rows}))
            self.assertEqual(claude_status.recent_desktop_usage(1200, roots=[root]), 0)
            rows[-1]['org'] = 'private-scope'
            rows[-1]['u'] = {'fh':0,'sd':0}
            path.write_text(json.dumps({'samples':rows}))
            self.assertEqual(claude_status.recent_desktop_usage(1200, roots=[root]), 0)

    def test_expired_window_rolls_over_to_an_unused_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.json').write_text(json.dumps({'updated_at': 1000, 'rate_limits': {
                'five_hour': {'used_percentage': 20, 'resets_at': 2000},
                'seven_day': {'used_percentage': 40, 'resets_at': 9000}}}))
            (root / 'bad.json').write_text('{broken')
            result = claude_status.latest(2100, root)['rate_limits']
            self.assertEqual(result['five_hour'], {'used_percentage': 0.0, 'resets_at': None, 'idle': True})
            self.assertEqual(result['seven_day'], {'used_percentage': 40, 'resets_at': 9000})
            self.assertIsNone(claude_status.latest(1000 + 8 * 86400, root))

    def test_window_left_out_by_claude_code_is_idle_not_unknown(self):
        # Claude Code drops five_hour from its status line once that reading passes its reset.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.json').write_text(json.dumps({'updated_at': 1000, 'rate_limits': {
                'seven_day': {'used_percentage': 10, 'resets_at': 9000}}}))
            (root / 'empty.json').write_text(json.dumps({'updated_at': 1500, 'rate_limits': {}}))
            result = claude_status.latest(1600, root)
            self.assertEqual(result['updated_at'], 1000)  # a report without readings says nothing
            self.assertEqual(result['rate_limits']['five_hour']['used_percentage'], 0.0)
            self.assertTrue(result['rate_limits']['five_hour']['idle'])

    def test_each_window_takes_its_newest_reading_from_any_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'old.json').write_text(json.dumps({'updated_at': 1000, 'rate_limits': {
                'five_hour': {'used_percentage': 30, 'resets_at': 5000},
                'seven_day': {'used_percentage': 5, 'resets_at': 9000}}}))
            (root / 'new.json').write_text(json.dumps({'updated_at': 1200, 'rate_limits': {
                'seven_day': {'used_percentage': 7, 'resets_at': 9000},
                # carried over by the receiver: its own observation time wins over the file's
                'five_hour': {'used_percentage': 12, 'resets_at': 5000, 'observed_at': 900}}}))
            result = claude_status.latest(1300, root)
            self.assertEqual(result['updated_at'], 1200)
            self.assertEqual(result['rate_limits']['five_hour'], {'used_percentage': 30, 'resets_at': 5000})
            self.assertEqual(result['rate_limits']['seven_day']['used_percentage'], 7)

    def test_rolled_over_window_reads_as_full_quota(self):
        now = time.time()
        report = {'updated_at': now, 'rate_limits': {
            'five_hour': {'used_percentage': 0.0, 'resets_at': None, 'idle': True},
            'seven_day': {'used_percentage': 10, 'resets_at': int(now + 10000)}}}
        with patch.object(claude_status, 'latest', return_value=report):
            value = quota_sources.get_claude_quota()
        self.assertEqual((value.remaining_percent, value.resets_at, value.secondary_remaining_percent), (100.0, None, 90))

    def test_native_quota_avoids_cli_and_uses_real_update_time(self):
        now = time.time()
        report = {'updated_at': now, 'rate_limits': {
            'five_hour': {'used_percentage': 23, 'resets_at': int(now + 1000)},
            'seven_day': {'used_percentage': 8, 'resets_at': int(now + 10000)}}}
        with patch.object(claude_status, 'latest', return_value=report), \
                patch.object(quota_sources.cli_quota, 'read_usage') as cli:
            value = quota_sources.get_claude_quota()
        cli.assert_not_called()
        self.assertEqual((value.remaining_percent, value.secondary_remaining_percent), (77, 92))
        self.assertEqual(value.updated_at, now)

    def stale_report(self, now):
        # Desktop sessions run no status line: the last CLI reading ages out.
        seen = now - quota_sources.CLAUDE_NATIVE_FRESH_SECONDS - 60
        return {'updated_at': now, 'observed_at': seen, 'rate_limits': {
            'five_hour': {'used_percentage': 45, 'resets_at': int(now + 1000)},
            'seven_day': {'used_percentage': 39, 'resets_at': int(now + 10000)}}}

    def test_stale_native_quota_falls_back_to_usage_once_per_gap(self):
        now = time.time()
        usage = {'window_minutes': 300, 'used_percent': 4, 'remaining_percent': 96, 'resets_at': int(now + 900),
                 'secondary_window_minutes': 10080, 'secondary_used_percent': 42,
                 'secondary_remaining_percent': 58, 'secondary_resets_at': int(now + 9000)}
        with patch.object(claude_status, 'latest', return_value=self.stale_report(now)),                 patch.object(quota_sources.providers, 'find_claude', return_value=Path('claude.exe')),                 patch.object(quota_sources.cli_quota, 'read_usage', return_value=usage) as cli:
            first = quota_sources.get_claude_quota()
            second = quota_sources.get_claude_quota()
        self.assertEqual(cli.call_count, 1)
        self.assertEqual((first.remaining_percent, first.secondary_remaining_percent, first.stale), (96, 58, False))
        self.assertIs(second, first)

    def test_hovering_checks_an_older_status_line_against_usage(self):
        now = time.time()
        report = {'updated_at': now, 'observed_at': now - 120, 'rate_limits': {   # 2 min old: "fresh" unhovered
            'five_hour': {'used_percentage': 20, 'resets_at': int(now + 1000)},
            'seven_day': {'used_percentage': 30, 'resets_at': int(now + 10000)}}}
        usage = {'window_minutes': 300, 'used_percent': 26, 'remaining_percent': 74, 'resets_at': int(now + 1000),
                 'secondary_window_minutes': 10080, 'secondary_used_percent': 31,
                 'secondary_remaining_percent': 69, 'secondary_resets_at': int(now + 10000)}
        quota_sources._CLAUDE_PROBE.clear()
        with patch.object(claude_status, 'latest', return_value=report), \
                patch.object(quota_sources.providers, 'find_claude', return_value=Path('claude.exe')), \
                patch.object(quota_sources.cli_quota, 'read_usage', return_value=usage) as cli:
            self.assertEqual(quota_sources.get_claude_quota().remaining_percent, 80)     # not looked at: status line
            cli.assert_not_called()
            with patch.object(quota_sources, 'WATCHED', frozenset({'claude'})):
                looked = quota_sources.get_claude_quota()
                again = quota_sources.get_claude_quota()
                quota_sources._CLAUDE_PROBE['at'] -= quota_sources.CLAUDE_HOVER_PROBE_GAP_SECONDS
                quota_sources.get_claude_quota()
                quota_sources._CLAUDE_PROBE['at'] -= 5                      # checked 5 s ago, inside the gap...
                with patch.dict(quota_sources.HOVER_STARTED, {'claude': time.time() - 1}):
                    quota_sources.get_claude_quota()                        # ...but a new hover checks at once
                    quota_sources.get_claude_quota()                        # its later reads reuse that
        self.assertEqual((looked.remaining_percent, looked.secondary_remaining_percent), (74, 69))   # Desktop use shows
        self.assertIs(again, looked)                                        # one /usage session per hover gap
        self.assertEqual(cli.call_count, 3)
        quota_sources._CLAUDE_PROBE.clear()

    def test_sessions_holding_older_figures_do_not_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            reset = int(now + 3600)
            rows = {'live': (now - 30, 26.0, reset), 'idle': (now - 2, 16.0, reset),     # redrew last, older figure
                    'old-window': (now - 1, 90.0, reset - 5 * 3600)}                     # a previous window
            for name, (seen, used, at) in rows.items():
                (Path(tmp) / f'{name}.json').write_text(json.dumps({'updated_at': seen, 'rate_limits': {
                    'five_hour': {'used_percentage': used, 'resets_at': at, 'observed_at': seen}}}), encoding='utf-8')
            report = claude_status.latest(now, Path(tmp))
        self.assertEqual(report['rate_limits']['five_hour'], {'used_percentage': 26.0, 'resets_at': reset})
        self.assertAlmostEqual(report['observed_at'], now - 30)

    def test_claude_is_checked_against_usage_while_in_use_and_after_a_doubtful_reading(self):
        now = time.time()
        report = {'updated_at': now, 'observed_at': now - 5, 'rate_limits': {
            'five_hour': {'used_percentage': 16, 'resets_at': int(now + 1000)},
            'seven_day': {'used_percentage': 8, 'resets_at': int(now + 10000)}}}
        usage = {'window_minutes': 300, 'used_percent': 25, 'remaining_percent': 75, 'resets_at': int(now + 1000),
                 'secondary_window_minutes': 10080, 'secondary_used_percent': 9,
                 'secondary_remaining_percent': 91, 'secondary_resets_at': int(now + 10000)}
        quota_sources._CLAUDE_PROBE.clear()
        with patch.object(claude_status, 'latest', return_value=report), \
                patch.object(quota_sources.providers, 'find_claude', return_value=Path('claude.exe')), \
                patch.object(quota_sources.cli_quota, 'read_usage', return_value=usage) as cli:
            self.assertEqual(quota_sources.get_claude_quota().remaining_percent, 84)    # idle: the status line
            with patch.object(quota_sources, 'ACTIVE', frozenset({'claude'})):
                self.assertEqual(quota_sources.get_claude_quota().remaining_percent, 75)  # in use: /usage, further along
                self.assertEqual(quota_sources.get_claude_quota().remaining_percent, 75)  # checked moments ago: kept
                self.assertEqual(cli.call_count, 1)
            old = quota_sources.QuotaSnapshot('claude', 'Claude Code', 300, 25, 75, int(now + 1000), now, False, None,
                                              10080, 9, 91, int(now + 10000))
            quota_sources._CLAUDE_PROBE.clear()                                       # no recent /usage result...
            held = quota_sources.keep_last_good(quota_sources.get_claude_quota(), old)   # ...so 16 after 25: set aside
            self.assertTrue(held.held)
            self.assertEqual(held.remaining_percent, 75)
            self.assertEqual(quota_sources.get_claude_quota().remaining_percent, 75)      # verified at once
            self.assertEqual(cli.call_count, 2)
        quota_sources._CLAUDE_PROBE.clear()
        from refresh_scheduler import HELD_RECHECK, RefreshScheduler
        scheduler = RefreshScheduler(('claude',), 0)
        for moment in range(1, 6):                                      # never the 12 h "no quota" pause
            scheduler.take_due(moment * 100)
            scheduler.completed('claude', held, moment * 100)
            self.assertFalse(scheduler.dormant['claude'])
            self.assertEqual(scheduler.due['claude'], moment * 100 + HELD_RECHECK)

    def test_failed_usage_read_keeps_last_native_numbers_marked_stale(self):
        now = time.time()
        with patch.object(claude_status, 'latest', return_value=self.stale_report(now)),                 patch.object(quota_sources.providers, 'find_claude', return_value=Path('claude.exe')),                 patch.object(quota_sources.cli_quota, 'read_usage', side_effect=RuntimeError('exited')):
            value = quota_sources.get_claude_quota()
        self.assertEqual((value.remaining_percent, value.secondary_remaining_percent), (55, 61))
        self.assertTrue(value.stale)
        self.assertLess(value.updated_at, now - quota_sources.CLAUDE_NATIVE_FRESH_SECONDS)

    def test_held_readings_age_by_observation_not_by_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            (Path(tmp) / 'a.json').write_text(json.dumps({'updated_at': now, 'rate_limits': {
                'seven_day': {'used_percentage': 39, 'resets_at': int(now + 9000), 'observed_at': now - 7 * 3600}}}),
                encoding='utf-8')
            report = claude_status.latest(now, Path(tmp))
        self.assertEqual(report['updated_at'], now)
        self.assertEqual(report['observed_at'], now - 7 * 3600)

    @unittest.skipUnless(os.name == 'nt', 'Windows status-line receiver')
    def test_receiver_saves_only_numeric_quota_and_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver = Path(tmp) / 'receiver.ps1'
            receiver.write_bytes(Path(claude_status.__file__).with_name('claude_status.ps1').read_bytes())
            payload = {'session_id': 'test-session', 'secret': 'NEVER-SAVE',
                       'rate_limits': {'five_hour': {'used_percentage': 15, 'resets_at': 1900000000}}}
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(receiver)],
                                    input=json.dumps(payload), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            rolling = Path(tmp) / 'claude-status.json'
            output = rolling.read_text(encoding='utf-8-sig')
            self.assertNotIn('NEVER-SAVE', output)
            self.assertFalse((Path(tmp) / 'claude-status').exists())          # one rolling file, no per-session files
            saved = json.loads(output)['sessions']['test-session']
            self.assertEqual(saved['rate_limits']['five_hour']['used_percentage'], 15)
            self.assertIn('5h left 85%', result.stdout)
            # Another session's reading goes into the same file.
            other = {'session_id': 'other', 'rate_limits': {'five_hour': {'used_percentage': 30, 'resets_at': 1900000000}}}
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(receiver)],
                           input=json.dumps(other), capture_output=True, text=True, timeout=10)
            # A later status line without five_hour keeps the last reading for the tray, not for display.
            payload['rate_limits'] = {'seven_day': {'used_percentage': 4, 'resets_at': 1900000000}}
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(receiver)],
                                    input=json.dumps(payload), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('5h', result.stdout)
            sessions = json.loads(rolling.read_text(encoding='utf-8-sig'))['sessions']
            self.assertEqual(set(sessions), {'test-session', 'other'})
            saved = sessions['test-session']
            self.assertEqual(saved['rate_limits']['five_hour']['used_percentage'], 15)
            self.assertLess(saved['rate_limits']['five_hour']['observed_at'], saved['rate_limits']['seven_day']['observed_at'] + 0.001)
            self.assertEqual(saved['rate_limits']['seven_day']['used_percentage'], 4)
            # The tray reads the rolling file: the furthest reading per window across sessions.
            report = claude_status.latest(time.time(), rolling=rolling)
            self.assertEqual(report['rate_limits']['five_hour']['used_percentage'], 30)
            self.assertEqual(report['rate_limits']['seven_day']['used_percentage'], 4)

    def test_rolling_file_and_leftover_session_files_are_read_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            reset = int(now + 3600)
            rolling = Path(tmp) / 'claude-status.json'
            rolling.write_text(json.dumps({'schema': 3, 'updated_at': now, 'sessions': {
                'a': {'updated_at': now - 10, 'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': reset, 'observed_at': now - 10}}},
                'bad': {'updated_at': 'x'},
                'b': {'updated_at': now - 5, 'rate_limits': {'seven_day': {'used_percentage': 12, 'resets_at': reset + 86400, 'observed_at': now - 5}}}}}),
                encoding='utf-8')
            legacy = Path(tmp) / 'claude-status'
            legacy.mkdir()
            (legacy / 'old.json').write_text(json.dumps({'updated_at': now - 60, 'rate_limits': {
                'five_hour': {'used_percentage': 44, 'resets_at': reset, 'observed_at': now - 60}}}), encoding='utf-8')
            report = claude_status.latest(now, folder=legacy, rolling=rolling)
            self.assertEqual(report['rate_limits']['five_hour']['used_percentage'], 44)      # the furthest, from the old file
            self.assertEqual(report['rate_limits']['seven_day']['used_percentage'], 12)
            self.assertEqual(claude_status.latest(now, rolling=rolling)['rate_limits']['five_hour']['used_percentage'], 40)
            self.assertEqual(claude_status.rolling_records(rolling), claude_status.rolling_records(rolling))   # cached
            rolling.write_text('{not json', encoding='utf-8')
            self.assertIsNone(claude_status.latest(now, rolling=rolling))

    def test_processes_distinguish_desktop_from_code(self):
        rows = [SimpleNamespace(info={'name':'claude.exe', 'exe': path}) for path in (
            r'C:\node_modules\@anthropic-ai\claude-code\bin\claude.exe',
            r'D:\WindowsApps\Claude_1\app\claude.exe', r'D:\WindowsApps\Claude_1\app\claude.exe')]
        with patch.object(tps_sources.psutil, 'process_iter', return_value=rows):
            result = {r['name']:r['processes'] for r in tps_sources.running_software()}
        self.assertEqual(result, {'Claude Code':1, 'Claude 桌面':2})


if __name__ == '__main__':
    unittest.main()
