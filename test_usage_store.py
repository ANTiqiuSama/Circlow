import gzip
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from forecast import Sample
from tps_sources import Observation, TpsMonitor
import settings_view as view
import usage_store
from usage_store import UsageStore, insights, local_day


def reply(identity, ended, tokens, provider="codex", counted=True):
    return Observation(identity, "Codex", "s1", provider, "gpt-5", ended, tokens, 4.0, "测试", counted, 50.0, 10, 5)


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "usage.json.gz"

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

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
        self.assertEqual(lines[0], "今天：15K tokens · 2 次回复")
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
        self.assertEqual(usage_store.hint_line(store, keys, now, weekly), "Grok 周额度 20 小时后重置，预计剩 60%，可多用")
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


if __name__ == "__main__":
    unittest.main()
