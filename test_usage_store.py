import gzip
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from forecast import Sample
from tps_sources import Observation, TpsMonitor
import settings_view as view
import usage_store
from usage_store import UsageStore, insights, local_day


def reply(identity, ended, tokens, provider="codex", counted=True, source="Codex"):
    return Observation(identity, source, "s1", provider, "gpt-5", ended, tokens, 4.0, "测试", counted, 50.0, 10, 5)


class Ledger:
    """usage_ledger.UsageLedger's answers, for days it read."""
    def __init__(self, totals, first):
        self.totals, self.first, self.refreshed_at = totals, first, 1.0

    def day(self, provider, midnight):
        return self.totals.get(provider, {}).get(midnight, 0)

    def covers(self, midnight, now):
        return midnight >= self.first


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "usage.json.gz"

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_daily_totals_count_every_token_and_survive_a_restart(self):
        noon = local_day(time.time()) + 12 * 3600
        today, yesterday = local_day(noon), local_day(noon - 86400)
        store = UsageStore(self.path)
        store.days_since = yesterday                             # yesterday's totals were recorded in full
        store.note(reply("a", noon - 60, 100, "gemini", source="Antigravity CLI"))   # 100 output + 10 input + 5 cache
        store.note(reply("b", noon - 86400, 40, "gemini", source="Antigravity CLI"))  # yesterday
        store.note(reply("c", noon - 30, 999, "gemini", counted=False, source="Antigravity CLI"))  # left out of the rate
        store.note(reply("d", noon - 20, 500))                   # Codex: the ledger reads it from the logs
        self.assertTrue(store.save(noon, force=True))
        ledger = Ledger({"codex": {today: 7000, yesterday: 3000}, "gemini": {today: 1}}, yesterday)
        rows = UsageStore(self.path).daily(("codex", "gemini"), noon, 3, ledger)
        self.assertEqual([len(rows), rows[-1][1], rows[-2][1]], [3, {"codex": 7000, "gemini": 116}, {"codex": 3000, "gemini": 55}])
        self.assertEqual([complete for _start, _tokens, complete in rows], [True, True, True])   # no output stands in anywhere
        self.assertEqual(rows[-1][0], today)
        plain = UsageStore(self.path).daily(("codex", "gemini"), noon, 3)          # the logs not read yet
        self.assertEqual(plain[-1], (today, {"codex": 500, "gemini": 115}, False))

    def test_daily_falls_back_to_output_before_the_totals_began(self):
        noon = local_day(time.time()) + 12 * 3600
        old = local_day(noon - 3 * 86400)
        store = UsageStore(self.path)
        store.note(reply("old", noon - 3 * 86400, 70, "gemini", source="Antigravity CLI"))
        store.note(reply("older", noon - 3 * 86400, 30))
        store.save(noon, force=True)
        payload = json.loads(gzip.decompress(self.path.read_bytes()))
        del payload["other_days"], payload["other_since"]          # a file saved before these totals
        payload["days"] = {"claude": {str(old): 999}}                 # the old totals, which held the ledger's share too
        self.path.write_bytes(gzip.compress(json.dumps(payload).encode()))
        reopened = UsageStore(self.path)
        rows = reopened.daily(("codex", "gemini"), noon, 4, Ledger({"codex": {old: 900}}, old))
        self.assertEqual(rows[0], (old, {"codex": 900, "gemini": 70}, False))   # Gemini's output only that day
        self.assertEqual(reopened.days_since, local_day(time.time()))
        rows = reopened.daily(("codex",), noon, 4)
        self.assertEqual(rows[0], (old, {"codex": 30}, False))
        caption = view.daily_totals(rows)
        self.assertEqual([c for c, _v, _w in caption], ["今日", "昨日", "近 7 天 · 部分仅输出"])
        self.assertEqual(view.daily_spend([{"codex": 1.0}, {"codex": 2.0}, {"codex": 0.5, "gemini": 0.25}]), (0.75, 2.0, 3.75))
        image = view.daily_chart(rows, {"codex": "C"}, 1, costs=[{"codex": 1.0}] * len(rows), hover=0)
        self.assertEqual(image.size, (652, 190))

    def test_the_ledger_counts_claude_codex_and_grok_from_their_logs(self):
        self.assertTrue(usage_store.ledger_counts(reply("a", 0, 1)))
        self.assertTrue(usage_store.ledger_counts(reply("a", 0, 1, "claude", source="Claude Code")))
        self.assertTrue(usage_store.ledger_counts(reply("a", 0, 1, "mimo", source="Claude Code")))
        self.assertTrue(usage_store.ledger_counts(reply("a", 0, 1, "grok", source="Grok CLI")))
        self.assertFalse(usage_store.ledger_counts(reply("a", 0, 1, "gemini")))                    # Gemini through Codex
        self.assertFalse(usage_store.ledger_counts(reply("a", 0, 1, "gemini", source="Codex · Gemini桥接回传")))
        self.assertFalse(usage_store.ledger_counts(reply("a", 0, 1, "gemini", source="Antigravity CLI")))
        self.assertFalse(usage_store.ledger_counts(reply("a", 0, 1, "mimo", source="MiMo Code")))

    def test_daily_board_draws_and_picks_a_day(self):
        noon = local_day(time.time()) + 12 * 3600
        rows = [(local_day(noon - (13 - i) * 86400), {"codex": i * 1000, "claude": 500}, i > 10) for i in range(14)]
        letters = {"codex": "C", "claude": "A"}
        for scale in (1, 1.5):
            image = view.daily_chart(rows, letters, scale, width=640, height=190, hover=13)
            self.assertEqual(image.size, (round(640 * scale), round(190 * scale)))
        self.assertIsNotNone(view.daily_chart([], letters))
        left, right, _top, _bottom, _slot = view.daily_frame(640, 190, 14)
        self.assertEqual(view.daily_index(right - 1, 640, 190, 14), 13)
        self.assertEqual(view.daily_index(left, 640, 190, 14), 0)
        self.assertIsNone(view.daily_index(left - 1, 640, 190, 14))
        self.assertNotEqual(view.daily_chart(rows, letters, hover=13).tobytes(), view.daily_chart(rows, letters).tobytes())

    def test_output_records_survive_a_restart_without_counting_twice(self):
        now = time.time()
        store = UsageStore(self.path)
        first = TpsMonitor(self.folder, store=store)
        first.add(reply("a", now - 7200, 800), now)
        first.add(reply("b", now - 60, 300), now)
        first.add(reply("old", now - 2 * 86400, 999), now)          # the monitor only takes a day
        self.assertTrue(store.save(now, force=True))
        self.assertFalse(store.save(now + 5))                         # nothing new, and saved a moment ago

        reopened = UsageStore(self.path)
        second = TpsMonitor(self.folder, store=reopened)
        self.assertEqual([e.identity for e in second.events], ["a", "b"])
        self.assertEqual(second.events[1], reply("b", now - 60, 300))   # every field back as it was
        self.assertEqual(second.added_total, 0)                       # ledgers that took them do not again
        second.add(reply("b", now - 60, 300), now)                    # a log tail read again at start
        self.assertEqual(len(second.events), 2)
        self.assertEqual(sum(tokens for tokens, _ in reopened.hours["codex"].values()), 1100)
        self.assertEqual(reopened.replies(["codex"], now - 86400), 2)

    def test_old_replies_fold_into_hourly_totals_and_the_file_stays_small(self):
        now = time.time()
        store = UsageStore(self.path)
        for index in range(3000):                                     # a busy week of replies
            store.note(reply(f"r{index}", now - index * 200, 400 + index % 50))
        store.note(reply("ancient", now - 100 * 86400, 5))
        store.note(reply("uncounted", now - 30, 7, counted=False))
        store.save(now, force=True)
        with gzip.open(self.path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertTrue(all(row[5] >= now - usage_store.RAW_SECONDS for row in payload["raw"]))
        self.assertLess(len(payload["raw"]), 500)
        self.assertNotIn(str(int((now - 100 * 86400) // 3600 * 3600)), payload["hours"]["codex"])
        self.assertLess(self.path.stat().st_size, 60_000)
        rows = store.hour_rows(["codex"], now - 7 * 86400, now)
        week = view.activity(rows, now, 10080, ["codex"])
        self.assertEqual((week.step, len(week.buckets)), (21600, 28))
        self.assertGreater(week.total, sum(400 + i % 50 for i in range(3000) if i * 200 < 6 * 86400))   # beyond the raw day
        self.assertEqual(store.hour_rows(["claude"], now - 86400, now), [])

    def test_replies_saved_before_newer_fields_still_load(self):
        now = time.time()
        old = list(reply("a", now - 60, 300).__dict__.values())[:13]          # saved before stream / turn_end
        with gzip.open(self.path, "wt", encoding="utf-8") as handle:
            json.dump({"version": usage_store.VERSION, "raw": [old, old[:5]], "hours": {}}, handle)
        restored = UsageStore(self.path).restore(now)
        self.assertEqual(restored, [reply("a", now - 60, 300)])                  # the short-by-8 row is not a reply

    def test_a_damaged_file_starts_empty(self):
        self.path.write_bytes(b"not gzip")
        store = UsageStore(self.path)
        self.assertEqual((store.raw, store.hours), ([], {}))

    def test_insights_compare_days_and_price_a_percent_of_quota(self):
        now = time.mktime((2026, 11, 1, 15, 30, 0, 0, 0, -1))
        store = UsageStore(self.path)
        self.assertIn("暂无输出", insights(store, "codex", now)[0])
        day = local_day(now)
        for back in range(1, 8):                                      # 14:xx every day of the week
            store.note(reply(f"d{back}", day - back * 86400 + 14.5 * 3600, 10_000))
        store.note(reply("y-late", day - 86400 + 20 * 3600, 6_000))    # yesterday evening, after "now"
        store.note(reply("t1", day + 14 * 3600 + 600, 12_000))
        store.note(reply("t2", day + 15 * 3600 + 60, 3_000))
        samples = [Sample(day + 14 * 3600, 10.0, 99), Sample(day + 15 * 3600 + 120, 25.0, 99),
                   Sample(day + 15 * 3600 + 600, 5.0, 77)]           # a reset: not counted as use
        lines = insights(store, "codex", now, samples, "5 小时额度")
        self.assertEqual(lines[0], "今天：输出 15K tokens · 2 次回复")
        priced = insights(store, "codex", now, samples, "5 小时额度", day_total=(205_600_000, 97.3))
        self.assertEqual(priced[0], "今天：205.6M tokens（输出 15K） · 约 $97.3 · 2 次回复")
        self.assertEqual(lines[1], "比昨天此时：+50%（昨天全天 16K）")
        self.assertEqual(lines[2], "近 7 天日均：10.9K tokens（7 天有输出）")
        self.assertEqual(lines[3], "最忙时段：14:00–16:00")
        self.assertEqual(lines[4], "额度换算：5 小时额度每 1% 约 807 输出 tokens")
        self.assertEqual(insights(store, "claude", now)[0][:4], "记录：暂")

    def test_the_hint_gets_one_usage_insight_from_output_only(self):
        now = time.mktime((2026, 11, 6, 15, 30, 0, 0, 0, -1))
        day = local_day(now)
        keys = ["codex", "claude", "grok"]
        store = UsageStore(self.path)
        self.assertEqual(usage_store.hint_line(store, keys, now), "")
        store.note(reply("y1", day - 86400 + 10 * 3600, 10_000, provider="claude"))     # yesterday by this time
        store.note(reply("t1", day + 10 * 3600, 12_000, provider="claude"))
        store.note(reply("t2", day + 11 * 3600, 4_000, provider="codex"))
        self.assertEqual(usage_store.hint_line(store, keys, now), "今天输出 16K，Claude 占 75%")   # never "比昨天此时"
        reset = int(now + 20 * 3600)
        flat = [Sample(now - 7200, 40, reset), Sample(now - 3600, 40, reset), Sample(now, 40, reset)]
        weekly = {"grok": (flat, 60, reset)}                                            # 60% left, reset tomorrow
        # The quota forecast lives in the panel's forecast line only (forecast.outlook): the hint never repeats it.
        self.assertEqual(usage_store.hint_line(store, keys, now, weekly), "今天输出 16K，Claude 占 75%")
        solo = UsageStore(self.folder / "solo.json.gz")
        solo.note(reply("s1", day - 2 * 86400 + 14 * 3600, 9_000, provider="grok"))
        self.assertEqual(usage_store.hint_line(solo, keys, now), "近 7 天 14–16 点最忙")
        self.assertNotIn("额度", usage_store.hint_line(store, keys, now))

    def test_tile_lines_never_start_with_closing_or_end_with_opening_marks(self):
        from PIL import Image, ImageDraw
        draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        face = view.ui_font(12)
        text = "11:02 刚重置；上周还剩 38% 没用完（约 29.4K 输出 tokens），本周可多用"
        for limit in range(60, 260, 7):
            lines = view._wrap_text(draw, text, face, limit)
            self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
            self.assertFalse(any(line[:1] in view.NO_LINE_START for line in lines if line), (limit, lines))
            self.assertFalse(any(line[-1:] in view.NO_LINE_END for line in lines[:-1] if line), (limit, lines))

    def test_the_hint_names_the_quota_to_use_first_before_it_resets(self):
        from types import SimpleNamespace
        import quota_charts
        from quota_sources import QuotaSnapshot
        now = time.mktime((2026, 11, 21, 12, 0, 0, 0, 0, -1))
        soon, later, far = int(now + 10 * 3600), int(now + 30 * 3600), int(now + 80 * 3600)
        def idle(name, used, reset):
            return {name: [Sample(now - 7200, used, reset), Sample(now - 60, used, reset)]}
        series = {**idle("grok", 40, later), **idle("claude:secondary", 30, soon), **idle("codex:secondary", 30, soon),
                  **idle("gemini:pool:argon", 50, soon), **idle("gemini:pool:claude_gpt", 97, soon),
                  **idle("gemini:pool:gemini", 20, far)}
        pools = [{"key": "argon", "label": "Argon", "remaining_percent": 50.0, "resets_at": soon, "window_minutes": 10080},
                 {"key": "claude_gpt", "label": "Claude/GPT", "remaining_percent": 3.0, "resets_at": soon, "window_minutes": 10080},
                 {"key": "gemini", "label": "Gemini", "remaining_percent": 80.0, "resets_at": far, "window_minutes": 10080}]
        snapshots = {
            "grok": QuotaSnapshot("grok", "Grok", 10080, 40, 60, later, now - 60, False, None),
            "claude": QuotaSnapshot("claude", "Claude Code", 300, 98, 2, soon, now - 60, False, None, 10080, 30, 70, soon),
            "codex": QuotaSnapshot("codex", "Codex", 300, 10, 90, soon, now - 60, False, None, 10080, 30, 70, soon),
            "gemini": QuotaSnapshot("gemini", "Gemini · Argon", 10080, 50, 50, soon, now - 60, False, None, pools=pools),
        }
        tray = SimpleNamespace(snapshots=snapshots, history=SimpleNamespace(samples=lambda name: series.get(name, [])))
        spares = quota_charts.ChartData.reset_spares(tray, now)
        # Claude's 5-hour window is used up; Argon's pool is the one about to go to waste; Gemini's own
        # pool and Claude/GPT's are too far off or nearly empty.
        self.assertEqual([(item["provider"], item["pool"]) for item in spares], [("codex", ""), ("gemini", "Argon"), ("grok", "")])
        tray.reset_spares = lambda moment: quota_charts.ChartData.reset_spares(tray, moment)
        rows = quota_charts.ChartData.spare_hints(tray, now)
        self.assertEqual(rows, (("spare", "codex", "优先用 Codex：10 小时后重置，照现在的用法还会剩约 70%"),
                                ("spare", "gemini", "优先用 Gemini 的 Argon：10 小时后重置，照现在的用法还会剩约 50%")))
        snapshots["codex"].updated_at = now - 3 * 3600                    # an old reading says nothing
        self.assertEqual([item["provider"] for item in quota_charts.ChartData.reset_spares(tray, now)], ["gemini", "grok"])

    def test_a_weekly_quota_that_will_go_unused_is_pointed_out_before_its_reset(self):
        now = time.mktime((2026, 11, 7, 15, 0, 0, 0, 0, -1))
        reset = int(now + 30 * 3600)
        readings = [Sample(now - 6 * 3600 + 1800 * k, 20 + k / 2, reset) for k in range(13)]   # 1 %/h, 26 % used
        spare = usage_store.weekly_spare(readings, now, 74, reset)
        self.assertAlmostEqual(spare["spare"], 74 - 1 * 30, delta=0.5)                      # 44 % left at this pace
        fast = [Sample(now - 6 * 3600 + 1800 * k, 20 + k, reset) for k in range(13)]        # 2 %/h: 8 % left, no note
        self.assertIsNone(usage_store.weekly_spare(fast, now, 68, reset))
        self.assertIsNone(usage_store.weekly_spare([], now, 74, reset))                     # no readings: pace unknown
        self.assertIsNone(usage_store.weekly_spare(readings, now, 74, int(now + 40 * 3600)))   # too far ahead
        self.assertIsNone(usage_store.weekly_spare(readings, now, 68, int(now - 60)))      # already reset
        self.assertIsNone(usage_store.weekly_spare(readings, now, None, reset))
        idle = [Sample(now - 7200, 40, reset), Sample(now, 40, reset)]
        self.assertEqual(usage_store.weekly_spare(idle, now, 60, reset)["spare"], 60)       # no use: all of it
        self.assertIsNone(usage_store.weekly_spare(idle, now, 15, reset))                   # under 20 %: nothing to say
        store = UsageStore(self.path)
        line = usage_store.spare_line(store, "grok", usage_store.weekly_spare(idle, now, 60, reset), now)
        self.assertEqual(line, "周额度：1 天后重置，按现在的用法到时还剩约 60%，趁重置前多用")
        for day in range(1, 6):
            store.note(reply(f"w{day}", now - day * 86400, 10_000, provider="grok"))       # 50K for the 40 % used
        line = usage_store.spare_line(store, "grok", usage_store.weekly_spare(idle, now, 60, reset), now)
        self.assertIn("（约 75K 输出 tokens）", line)
        lines = insights(store, "grok", now, weekly=idle, weekly_quota=(60, reset))
        self.assertTrue(lines[0].startswith("周额度：1 天后重置"))
        self.assertFalse(any(line.startswith("周额度") for line in insights(store, "grok", now, weekly=idle, weekly_quota=(15, reset))))

    def test_completed_weeks_are_recorded_once_and_inform_the_weekly_tips(self):
        from usage_store import WeeklyLedger, last_completed_week, weeks_line
        now = time.mktime((2026, 11, 7, 15, 0, 0, 0, 0, -1))
        week = 7 * 86400
        reset = int(time.mktime((2026, 11, 3, 11, 2, 0, 0, 0, -1)))
        readings = [Sample(reset - week + 3600 * k, min(62, k), reset) for k in range(0, 160, 8)]
        readings += [Sample(reset - 600, 62, reset), Sample(reset + 600, 0, reset + week), Sample(now, 30, reset + week)]
        done = last_completed_week(readings)
        self.assertEqual((done["reset_at"], done["used"], done["left"]), (reset, 62, 38))
        self.assertIsNone(last_completed_week(readings[:-2]))                                  # no reset yet
        store = UsageStore(self.path)
        for day in range(1, 6):
            store.note(reply(f"w{day}", reset - day * 86400, 10_000, provider="grok"))
        ledger = WeeklyLedger(self.folder / "weekly.json")
        self.assertTrue(ledger.note("grok", "grok", readings, store))
        self.assertFalse(ledger.note("grok", "grok", readings, store))                        # recorded once
        earlier = [Sample(reset - 2 * week + 3600 * k, min(85, k), reset - week) for k in range(0, 168, 8)]
        earlier += [Sample(reset - week - 300, 85, reset - week), Sample(reset - week + 300, 0, reset)]
        self.assertTrue(ledger.note("grok", "grok", earlier, store))                           # an older week, by its reset
        again = WeeklyLedger(self.folder / "weekly.json")                                       # saved to disk
        recent = again.recent("grok")
        self.assertEqual([(row["used"], row["tokens"]) for row in recent], [(62.0, 50_000), (85.0, 0)])   # newest first
        self.assertEqual(weeks_line(recent), "近几周：周额度用掉 62% · 85%（最新在前）")
        self.assertEqual(weeks_line(recent[:1]), "近几周：周额度用掉 62%")
        self.assertEqual(weeks_line([]), "")
        leftover = usage_store.weekly_leftover(readings, reset + 3600)
        line = usage_store.weekly_line(store, "grok", leftover, reset + 3600, recent)
        self.assertIn("；近 2 周有 1 周没用完，本周可多用", line)
        flat = [Sample(now - 7200, 40, int(now + 86400)), Sample(now, 40, int(now + 86400))]
        spare = usage_store.weekly_spare(flat, now, 60, int(now + 86400))
        full = [{"reset_at": 1.0, "used": 95.0, "tokens": 0}, {"reset_at": 2.0, "used": 90.0, "tokens": 0}]
        self.assertIn("；近 2 周都用完了，趁重置前多用", usage_store.spare_line(store, "grok", spare, now, full))
        lines = insights(store, "grok", now, weekly=flat, weekly_quota=(60, int(now + 86400)), weeks=recent)
        self.assertTrue(lines[0].startswith("周额度：1 天后重置"))
        self.assertEqual(lines[1], "近几周：周额度用掉 62% · 85%（最新在前）")

    def test_a_weekly_reset_with_much_left_unused_is_pointed_out(self):
        now = time.mktime((2026, 11, 3, 15, 0, 0, 0, 0, -1))
        reset = int(time.mktime((2026, 11, 3, 11, 2, 0, 0, 0, -1)))
        week = 7 * 86400

        def week_of(last_used, last_read=reset - 600, new_reset=reset + week, early=None):
            readings = [Sample(reset - week + 3600 * k, min(last_used, k), reset) for k in range(0, 160, 8)]
            readings.append(Sample(last_read, last_used, reset if early is None else early))
            readings += [Sample(reset + 600, 0, new_reset), Sample(reset + 3600, 1, new_reset)]
            return readings
        found = usage_store.weekly_leftover(week_of(62), now)
        self.assertEqual((found["left"], found["used"], found["reset_at"]), (38, 62, reset))
        self.assertIsNone(usage_store.weekly_leftover(week_of(88), now))                   # used a lot: no note
        self.assertIsNone(usage_store.weekly_leftover(week_of(62), now + 3 * 86400))       # old news
        self.assertIsNone(usage_store.weekly_leftover(week_of(62, last_read=reset - 20 * 3600), now))  # end unknown
        self.assertIsNone(usage_store.weekly_leftover(week_of(62)[:-2], now))               # no reset yet
        early = usage_store.weekly_leftover(week_of(40, early=reset + 3 * 86400), now)      # reset before schedule
        self.assertEqual(early["reset_at"], reset + 600)
        back = week_of(62)[:-2] + [Sample(reset - 300, 0, reset + week), Sample(reset - 200, 62, reset)]
        self.assertIsNone(usage_store.weekly_leftover(back, now - 7200))                    # a window that went back

        store = UsageStore(self.path)
        line = usage_store.weekly_line(store, "grok", found, now)
        self.assertEqual(line, "周额度：11:02 刚重置；上周还剩 38% 没用完，本周可多用")
        for day in range(1, 7):                                                             # last week's own output
            store.note(reply(f"w{day}", reset - day * 86400, 10_000, provider="grok"))
        line = usage_store.weekly_line(store, "grok", found, now)
        self.assertEqual(line, "周额度：11:02 刚重置；上周还剩 38% 没用完（约 36.8K 输出 tokens），本周可多用")  # 60K for 62%
        lines = insights(store, "grok", now, weekly=week_of(62))
        self.assertTrue(lines[0].startswith("周额度：11:02 刚重置"))                          # first, ahead of the day's figures
        self.assertFalse(any(line.startswith("周额度") for line in insights(store, "grok", now, weekly=week_of(88))))
        self.assertTrue(insights(UsageStore(self.folder / "empty.json.gz"), "grok", now, weekly=week_of(62))[0]
                        .startswith("周额度"))                                               # even before any output record


class PricingTests(unittest.TestCase):
    """pricing: LiteLLM's token prices, the way ccusage applies them."""

    TABLE = {
        "claude-opus-5-5": {"input_cost_per_token": 4e-06, "output_cost_per_token": 2e-05, "cache_creation_input_token_cost": 5e-06,
                            "cache_read_input_token_cost": 2e-07, "cache_creation_input_token_cost_above_1hr": 8e-06,
                            "max_tokens": 128000, "litellm_provider": "anthropic"},
        "azure/gpt-6.1-sol": {"input_cost_per_token": 2e-06, "output_cost_per_token": 1e-05, "cache_read_input_token_cost": 1e-07,
                              "input_cost_per_token_above_272k_tokens": 4e-06, "output_cost_per_token_above_272k_tokens": 1.5e-05,
                              "cache_read_input_token_cost_above_272k_tokens": 2e-07, "input_cost_per_token_above_272k_tokens_flex": 9.0},
        "xai/grok-4.7": {"input_cost_per_token": 2e-06, "output_cost_per_token": 6e-06},
        "gemini/gemini-4-argon": {"input_cost_per_token": 2e-06, "output_cost_per_token": 1e-05, "cache_read_input_token_cost": 1e-07},
        "sample_spec": {"mode": "chat"},
    }

    def table(self):
        import pricing
        return pricing.PriceTable(pricing.slim(self.TABLE))

    def test_replies_are_priced_part_by_part(self):
        table = self.table()
        self.assertNotIn("max_tokens", table.price("claude-opus-5-5"))                     # only token prices are kept
        self.assertAlmostEqual(table.cost("claude-opus-5-5", 5, 1000, 100, 10_000, 50),
                               5 * 4e-06 + 1000 * 2e-05 + 100 * 5e-06 + 10_000 * 2e-07 + 50 * 8e-06)
        self.assertAlmostEqual(table.cost("gpt-6.1-sol", 1000, 100, 0, 9000), 1000 * 2e-06 + 100 * 1e-05 + 9000 * 1e-07)
        long = table.cost("gpt-6.1-sol", 100_000, 100, 0, 200_000)                        # a 300K prompt: the long tier
        self.assertAlmostEqual(long, 100_000 * 4e-06 + 100 * 1.5e-05 + 200_000 * 2e-07)
        self.assertAlmostEqual(table.cost("grok-4.7-build", 10, 10), 10 * 2e-06 + 10 * 6e-06)   # a trailing part dropped
        self.assertAlmostEqual(table.cost("gemini-4-argon", 0, 0, 100, 0), 100 * 2e-06)   # no write price: the input one
        self.assertIsNone(table.cost("mimo-v2.6-pro", 10, 10))
        self.assertIsNone(table.price("claude-opus"))                                       # a family name is too loose

    def test_the_table_is_downloaded_once_a_day_and_kept_on_disk(self):
        import pricing
        folder = Path(tempfile.mkdtemp())
        try:
            path = folder / "model_prices.json"
            big = {f"model-{index}": {"input_cost_per_token": 1e-06} for index in range(150)}
            big.update(self.TABLE)
            table = pricing.PriceTable()
            calls = []
            fetch = lambda url: calls.append(url) or json.dumps(big).encode()          # noqa: E731
            self.assertTrue(table.refresh(1000.0, path, fetch))
            self.assertFalse(table.refresh(2000.0, path, fetch))                         # within the day
            self.assertEqual(len(calls), 1)
            again = pricing.PriceTable()
            again.load(path)
            self.assertAlmostEqual(again.cost("claude-opus-5-5", 0, 1000), 0.02)
            broken = pricing.PriceTable()
            self.assertFalse(broken.refresh(1000.0, folder / "other.json", lambda url: b"{}"))   # too small: refused
            self.assertFalse(broken)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.assertEqual([pricing.money(v) for v in (0, 0.4234, 12.34, 123.4, 12_345)], ["", "$0.42", "$12.3", "$123", "$12.3K"])


class UsageLedgerTests(unittest.TestCase):
    """usage_ledger counts the logs the way ccusage does."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.claude, self.codex, self.grok = self.folder / "claude", self.folder / "codex", self.folder / "grok"
        self.noon = local_day(time.time()) + 12 * 3600

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def stamp(self, seconds_before_noon=0.0):
        return datetime.fromtimestamp(self.noon - seconds_before_noon, timezone.utc).isoformat().replace("+00:00", "Z")

    def write(self, path, records, mode="w"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def ledger(self):
        import usage_ledger
        return usage_ledger.UsageLedger([self.claude], [self.codex], self.grok)

    def claude_reply(self, mid, request, output, *, side=False, model="claude-opus-5-5", before=60.0, read=1000):
        usage = {"input_tokens": 5, "output_tokens": output, "cache_creation_input_tokens": 100, "cache_read_input_tokens": read}
        return {"type": "assistant", "timestamp": self.stamp(before), "requestId": request, "isSidechain": side,
                "sessionId": "s", "message": {"id": mid, "model": model, "usage": usage}}

    def test_claude_replies_count_once_with_every_token(self):
        today, yesterday = local_day(self.noon), local_day(self.noon - 86400)
        session = self.claude / "proj" / "s1.jsonl"
        self.write(session, [self.claude_reply("m1", "r1", 10), self.claude_reply("m1", "r1", 40),     # two blocks of one reply
                             {"type": "user", "timestamp": self.stamp(50), "message": {"content": "hi"}},
                             self.claude_reply("m2", "r2", 1, model="mimo-v2.6-pro", read=0),
                             self.claude_reply("m3", "r3", 7, before=86400)])                         # yesterday
        self.write(self.claude / "proj" / "s2.jsonl", [self.claude_reply("m1", "r1", 40)])           # a resumed copy
        self.write(self.claude / "proj" / "s1" / "subagents" / "agent-a.jsonl",
                   [self.claude_reply("m1", "r1", 45, side=True), self.claude_reply("m4", "r4", 3, side=True)])
        ledger = self.ledger()
        ledger.refresh(self.noon)
        # m1 once (the main copy, 5 + 40 + 100 + 1000), m4 from the sub-agent; m2 routed to MiMo
        self.assertEqual(ledger.day("claude", today), 1145 + 1108)
        self.assertEqual(ledger.day("mimo", today), 106)
        self.assertEqual(ledger.day("claude", yesterday), 1112)
        self.assertTrue(ledger.covers(yesterday, self.noon))
        self.assertFalse(ledger.covers(local_day(self.noon - 20 * 86400), self.noon))
        self.write(session, [self.claude_reply("m5", "r5", 2)], mode="a")                            # the session goes on
        ledger.refresh(self.noon)
        self.assertEqual(ledger.day("claude", today), 1145 + 1108 + 1107)
        self.assertGreater(ledger.files[session].offset, 0)

    def test_codex_token_counts_take_each_reply_once(self):
        today = local_day(self.noon)
        def count(before, total, last=None):
            info = {"total_token_usage": {"input_tokens": total, "cached_input_tokens": total // 2, "output_tokens": 0,
                                          "total_tokens": total}}
            if last is not None:
                info["last_token_usage"] = {"input_tokens": last, "output_tokens": 0, "total_tokens": last}
            return {"type": "event_msg", "timestamp": self.stamp(before), "payload": {"type": "token_count", "info": info}}
        context = {"type": "turn_context", "timestamp": self.stamp(300), "payload": {"model": "gpt-6.2-astra"}}
        rollout = self.codex / "sessions" / "2026" / "11" / "22" / "rollout-a.jsonl"
        self.write(rollout, [context, count(200, 1000, 1000), count(190, 1000, 1000),   # repeated: nothing new
                             count(100, 1600, 600), count(90, 1800)])                   # no last usage: the difference
        fork = self.codex / "sessions" / "2026" / "11" / "22" / "rollout-b.jsonl"
        self.write(fork, [context, count(200, 1000, 1000), count(100, 1600, 600),       # the parent's events replayed
                          {"type": "turn_context", "timestamp": self.stamp(80), "payload": {"model": "gemini-4-argon"}},
                          count(50, 1700, 100)])                                         # Gemini: the Antigravity side counts it
        ledger = self.ledger()
        ledger.refresh(self.noon)
        self.assertEqual(ledger.day("codex", today), 1000 + 600 + 200)
        self.assertEqual(ledger.day("gemini", today), 0)

    def test_grok_turns_count_on_their_own_day(self):
        today, yesterday = local_day(self.noon), local_day(self.noon - 86400)
        usage = {"sessionId": "g1", "turns": [
            {"turnNumber": 1, "endedAt": self.stamp(86400), "totalTokens": 900, "modelUsage": {"grok-4.7-build": {"totalTokens": 900}}},
            {"turnNumber": 2, "endedAt": self.stamp(60), "inputTokens": 300, "outputTokens": 20, "primaryModelId": "grok-4.7"}]}
        path = self.grok / "sessions" / "home" / "g1" / "usage.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(usage), encoding="utf-8")
        ledger = self.ledger()
        ledger.refresh(self.noon)
        self.assertEqual((ledger.day("grok", yesterday), ledger.day("grok", today)), (900, 320))

    def test_each_day_is_priced_with_grok_at_its_own_cost(self):
        import pricing
        from unittest.mock import patch
        today = local_day(self.noon)
        self.write(self.claude / "proj" / "s1.jsonl", [self.claude_reply("m1", "r1", 1000, read=10_000),
                                                       self.claude_reply("m2", "r2", 10, model="mimo-v2.6-pro")])
        usage = {"turns": [{"turnNumber": 1, "endedAt": self.stamp(60), "modelUsage": {
            "grok-4.7-build": {"inputTokens": 1000, "outputTokens": 10, "cachedReadTokens": 400, "totalTokens": 1010,
                               "costUsdTicks": 25_000_000_000}}}]}
        path = self.grok / "sessions" / "home" / "g1" / "usage.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(usage), encoding="utf-8")
        with patch.object(pricing, "TABLE", pricing.PriceTable(pricing.slim(PricingTests.TABLE))):
            ledger = self.ledger()
            ledger.refresh(self.noon)
        self.assertAlmostEqual(ledger.cost("claude", today), 5 * 4e-06 + 1000 * 2e-05 + 100 * 5e-06 + 10_000 * 2e-07)
        self.assertEqual(ledger.cost("grok", today), 2.5)                       # xAI's own figure, 1e-10 $ a tick
        self.assertEqual(ledger.cost("mimo", today), 0.0)                       # no price: tokens only
        self.assertEqual(ledger.unpriced["mimo"][today], 1115)


if __name__ == "__main__":
    unittest.main()
