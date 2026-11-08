import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import settings_view as view
from icon_art import AMBER, accent, quota_icon, ring_color
from quota_sources import QuotaSnapshot


def snapshot(**extra):
    now = time.time()
    base = dict(provider="gemini", label="Gemini", window_minutes=300, used_percent=60, remaining_percent=40,
                resets_at=int(now + 3600), updated_at=now, stale=False, error=None)
    base.update(extra)
    return QuotaSnapshot(**base)


class SettingsViewTests(unittest.TestCase):
    def test_dark_title_bar_is_left_alone_off_windows(self):
        import ui_motion
        with patch.object(ui_motion.sys, 'platform', 'darwin'):
            self.assertFalse(ui_motion.dark_title_bar(object(), '#000000', '#ffffff', '#202020'))
            self.assertFalse(ui_motion.keep_unchanged_widgets(object()))

    def test_gemini_panel_uses_full_width_for_quota_without_tps(self):
        import agent_panel
        model = agent_panel.PanelModel('gemini', 'G', 'Gemini', '空闲',
            (('Argon', 30), ('Claude/GPT', 40), ('Gemini', 50)), 99, ())
        self.assertFalse(model.header[-1])
        self.assertEqual(agent_panel._header_model(model.header).provider, 'gemini')
        self.assertEqual(len(agent_panel.meter_columns(3, False)), 3)
        self.assertGreater(agent_panel.meter_columns(3, False)[0][1], agent_panel.meter_columns(3, True)[0][1])
        agent_panel.background(model.header, 120, 1.0)

    def test_gemini_always_uses_its_tps_colour(self):
        self.assertEqual(accent("G"), ring_color(40, "G"))
        self.assertEqual(accent("G"), ring_color(10, "G"))
        self.assertEqual(AMBER, ring_color(40, "C"))          # other sources keep the warning colours

    def test_quota_rows_are_written_like_the_panel_columns(self):
        now = time.time()
        pools = [{"key": "argon", "label": "Argon", "remaining_percent": 40, "resets_at": now + 4000},
                 {"key": "opus", "label": "Claude/GPT", "remaining_percent": 85}]
        rows = view.quota_rows(snapshot(pools=pools), now)
        self.assertEqual([("1小时7分钟", 40, "Argon"), ("Claude/GPT", 85, "")], [row[:3] for row in rows])
        two = view.quota_rows(snapshot(provider="codex", resets_at=int(now + 3600), secondary_window_minutes=10080,
                                       secondary_remaining_percent=38), now)
        self.assertEqual([("1小时", "5 小时"), ("每周", "")], [(row[0], row[2]) for row in two])   # no weekly reset known
        self.assertEqual(view.window_name(525600), "年度")
        self.assertEqual(view.refresh_text(12.4, "悬停"), "12 秒后刷新 · 悬停")
        self.assertEqual(view.refresh_text(300, "空闲"), "5 分钟后刷新 · 空闲")
        self.assertEqual(view.refresh_text(0, "正在刷新"), "正在刷新")
        self.assertEqual(view.refresh_text(None, ""), "")

    def test_pictures_render_at_scale(self):
        card = view.SourceCard("gemini", "G", "Gemini", "1 个在工作", "live", view.quota_rows(snapshot()), 142.0, 11.0, 1,
                               "按当前速度可用到重置", "ok")
        self.assertEqual((round(view.CARD_W * 1.5), round(view.CARD_H * 1.5)), view.source_card(card, quota_icon(40, "G"), 1.5).size)
        # A grid row of one-window sources is shorter: no empty band under a single bar.
        self.assertEqual(view.card_height(1), view.CARD_H - 2 * view.ROW_H)
        self.assertEqual(view.source_card(card, None, 1.0, slots=1).size, (view.CARD_W, view.card_height(1)))
        paid = view.SourceCard("codex", "C", "Codex", "可用购买额度", "paid",
                               (("1小时", 0.0, "购买额度", "paid", "59.8K"), ("5天3小时", 70.0, "每周", "", None)), 0.0, 0.0, 0, "", "warn")
        self.assertIsNotNone(view.source_card(paid, None, 1.25))
        now = time.time()
        events = [SimpleNamespace(ended_at=now - 100, provider="gemini", output_tokens=500, count_in_rate=True)]
        chart = view.activity(events, now, 30, ["gemini"])
        self.assertEqual((652, 150), view.activity_chart(chart, {"gemini": "G"}).size)
        self.assertEqual((978, 225), view.activity_chart(chart, {"gemini": "G"}, 1.5, hover=29, grow=.4).size)
        self.assertEqual((360, view.CARD_H), view.source_card(card, None, 1.0, width=360).size)
        agents = [{"key": "a", "provider": "gemini", "title": "补测试", "live": True, "doing": "运行测试", "tps": 50.0, "pane": "w1:p1"}]
        self.assertGreater(view.agent_rows(agents, {"gemini": "G"}).size[1], 60)

    def test_cards_follow_the_panel_and_tray_icon(self):
        import settings_window
        icon = quota_icon(40, "G")
        app = SimpleNamespace(snapshots={"gemini": snapshot()},
                              performance={"providers": {"gemini": {"display_tps": 99.0, "output_tps_60": 3.0, "active_sessions": 1}}},
                              icons={"gemini": SimpleNamespace(icon=icon)},
                              panel=lambda provider: SimpleNamespace(status="1 个在工作", status_tone="live", forecast="够用", forecast_tone="ok"))
        [(card, image)] = settings_window.build_cards(app)
        self.assertIs(image, icon)
        self.assertEqual(("G", "1 个在工作", None, 1), (card.letter, card.status, card.tps, card.active))

    def test_cards_use_the_panel_columns_in_tray_order_with_the_refresh_plan(self):
        import settings_window
        from refresh_scheduler import RefreshScheduler
        now = time.time()
        codex = snapshot(provider="codex", label="Codex", remaining_percent=0.0, used_percent=100.0,
                         secondary_window_minutes=10080, secondary_remaining_percent=70.0)
        model = SimpleNamespace(status="可用购买额度", status_tone="paid", forecast="5 小时额度已用完", forecast_tone="warn",
                                quota=(("1小时", 0.0, "购买额度"), ("5天3小时", 70.0, "")),
                                tone_of=lambda slot: "paid" if slot == 0 else "",
                                figure_of=lambda slot: ("59.8", "K") if slot == 0 else None)
        scheduler = RefreshScheduler(("codex", "claude"), now)
        scheduler.take_due(now)
        for provider in ("codex", "claude"):
            scheduler.completed(provider, SimpleNamespace(error=None, resets_at=None, secondary_resets_at=None), now)
        app = SimpleNamespace(snapshots={"codex": codex, "claude": snapshot(provider="claude", label="Claude Code")},
                              performance={"providers": {}}, icons={}, scheduler=scheduler,
                              panel=lambda provider: model if provider == "codex" else None)
        cards = [card for card, _ in settings_window.build_cards(app, now)]
        self.assertEqual([card.provider for card in cards], ["claude", "codex"])          # the tray's order
        self.assertEqual(cards[1].rows, (("1小时", 0.0, "购买额度", "paid", "59.8K"), ("5天3小时", 70.0, "每周", "", None)))
        self.assertRegex(cards[1].refresh, r"^\d+ (秒|分钟)后刷新 · 空闲$")
        self.assertEqual(scheduler.plan("codex", now), (scheduler.due["codex"] - now, "空闲"))
        self.assertEqual(scheduler.plan("grok", now), (None, ""))

    def test_paragraphs_wrap_chinese_by_character_and_keep_latin_words_whole(self):
        import settings_window
        measure = len                                   # one unit per character
        text = "Grok 不在 AI Stupid Level 的榜单上，所以改用 LiveBench 最新公开榜单"
        lines = settings_window.wrap_text(text, measure, 16).split("\n")
        self.assertTrue(all(len(line) <= 16 for line in lines))
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
        self.assertEqual(lines[1], "Stupid Level 的榜单")                    # Chinese fills the line, not cut at its space
        self.assertTrue(all(word in " ".join(lines).split() or word in "".join(lines) for word in ("Stupid", "LiveBench")))
        self.assertFalse(any(line.endswith(("Stu", "LiveBe")) for line in lines))   # a Latin word moves whole
        self.assertEqual(settings_window.PAGE_ALIASES["mimo"], "sources")
        self.assertEqual({key for key, _ in settings_window.PAGES}, {"overview", "sources", "iq", "tasks", "general"})

    def test_chart_bars_start_on_round_clock_times(self):
        now = time.mktime((2026, 10, 31, 14, 37, 20, 0, 0, -1))
        events = [SimpleNamespace(ended_at=now - 60, provider="codex", output_tokens=300, count_in_rate=True),
                  SimpleNamespace(ended_at=now - 5, provider="claude", output_tokens=200, count_in_rate=True),
                  SimpleNamespace(ended_at=now - 5, provider="codex", output_tokens=99, count_in_rate=False),
                  SimpleNamespace(ended_at=now - 7300, provider="codex", output_tokens=1, count_in_rate=True)]
        for minutes, step in view.CHART_RANGES.items():
            chart = view.activity(events, now, minutes, ["codex", "claude"])
            self.assertEqual(chart.step, step)
            self.assertEqual(len(chart.buckets), minutes * 60 // step)
            self.assertEqual(time.localtime(chart.start).tm_sec, 0)
            self.assertEqual((chart.start + time.localtime(now).tm_gmtoff) % step, 0)    # bars on round times
            self.assertTrue(chart.start + (len(chart.buckets) - 1) * chart.step <= now < chart.start + len(chart.buckets) * chart.step)
        two_hours = view.activity(events, now, 120, ["codex", "claude"])
        self.assertEqual(two_hours.totals, (300, 200))                  # not the uncounted copy, not the old one
        self.assertEqual(two_hours.buckets[-1], (300, 200))             # 14:35–14:40 holds both
        self.assertEqual(time.strftime("%H:%M", time.localtime(two_hours.bar_span(len(two_hours.buckets) - 1)[0])), "14:35")
        ticks = [label for _stamp, label in view.time_ticks(two_hours)]
        self.assertIn("14:20", ticks)
        self.assertTrue(all(label.endswith(("00", "20", "40")) for label in ticks))
        day = view.activity(events, now, 1440, ["codex"])
        self.assertIn("11/1" if time.localtime(now).tm_mon == 11 else "10/31", [label for _s, label in view.time_ticks(day)])

    def test_chart_zooms_in_on_a_fast_run_and_draws_its_slope(self):
        import settings_window
        now = time.mktime((2026, 10, 31, 14, 37, 20, 0, 0, -1))
        out = lambda ago, tokens, provider="codex": SimpleNamespace(  # noqa: E731
            ended_at=now - ago, provider=provider, output_tokens=tokens, count_in_rate=True)
        trickle = [out(3600 + 60 * k, 200) for k in range(10)]               # slow work an hour ago
        ramp = [out(380 - 60 * k, 600 + 600 * k) for k in range(7)]        # 14:31 on, climbing to 4200/min
        run = view.burst(trickle + ramp, now, ["codex"])
        self.assertEqual(time.strftime("%H:%M:%S", time.localtime(run.start)), "14:31:00")
        self.assertEqual(run.rate, 4200 / 60)
        self.assertIsNone(view.burst(trickle, now, ["codex"]))                # slow output never zooms
        self.assertIsNone(view.burst(trickle + ramp, now, ["claude"]))        # another source's run
        self.assertIsNone(view.burst([out(185, 9000)], now, ["codex"]))       # over for three minutes
        self.assertIsNotNone(view.burst([out(140, 9000)], now, ["codex"]))    # between replies: held
        # The shortest window under the picked range that holds the run and its lead-in.
        self.assertEqual([view.zoom_minutes(run, chosen, now) for chosen in view.CHART_RANGES], [10, 10, 10, 10, 10])
        long_run = view.Burst(now - 40 * 60, 50)
        self.assertEqual([view.zoom_minutes(long_run, chosen, now) for chosen in view.CHART_RANGES], [None, None, 120, 120, 120])
        self.assertIsNone(view.zoom_minutes(None, 1440, now))

        zoomed = view.activity(trickle + ramp, now, 10, ["codex"])
        self.assertEqual((zoomed.step, len(zoomed.buckets)), (15, 40))
        self.assertEqual(view._span_text(*zoomed.bar_span(0), zoomed.step).count(":"), 4)    # seconds shown
        self.assertEqual(settings_window.step_text(15), "15 秒")
        frame = view.chart_frame(652, 172, len(zoomed.buckets))
        points = view.trend_points(zoomed, frame, view.nice_ceiling(4200))
        self.assertEqual(len(points), len(zoomed.buckets) - [sum(b) > 0 for b in zoomed.buckets].index(True))
        self.assertLess(points[-1][1], points[0][1])                          # climbing: the line rises
        self.assertEqual(view.activity_chart(zoomed, {"codex": "C"}, 1.5, trend=True, hover=20).size, (978, 225))

        # The window zooms for the run until the user zooms back out of it.
        window = SimpleNamespace(zoom_dismissed=None, chart_minutes=120)
        chart = {"zoom": None, "burst": None}
        span = lambda: settings_window.SettingsWindow._chart_span(window, chart, trickle + ramp, now, ["codex"])  # noqa: E731
        self.assertEqual(span(), 10)
        zoom_out = SimpleNamespace(charts={"overview": chart}, zoom_dismissed=None)
        settings_window.SettingsWindow._dismiss_burst(zoom_out)
        self.assertEqual(zoom_out.zoom_dismissed, run.start)
        window.zoom_dismissed = zoom_out.zoom_dismissed
        self.assertEqual((span(), chart["zoom"]), (120, None))
        window.zoom_dismissed = run.start - 3600                               # an earlier run's dismissal
        self.assertEqual(span(), 10)

    def test_chart_hover_hit_testing_and_labels(self):
        left, right, _top, _bottom, slot = view.chart_frame(652, 172, 30)
        self.assertIsNone(view.chart_index(left - 1, 652, 172, 30))
        self.assertEqual(view.chart_index(left + 0.5, 652, 172, 30), 0)
        self.assertEqual(view.chart_index(right - 0.5, 652, 172, 30), 29)
        self.assertEqual(view.chart_index(left + slot * 4.5, 652, 172, 30), 4)
        self.assertEqual([view.tokens_text(v) for v in (950, 1000, 12_340, 4_900_000)], ["950", "1K", "12.3K", "4.9M"])
        self.assertEqual([view.nice_ceiling(v) for v in (0, 7, 1300, 2600, 6100)], [1.0, 10, 2000, 5000, 10000])

    def test_tiles_keep_their_width_and_wrap_like_windows_settings(self):
        import settings_window
        cards = [240] * 5
        # 3, 4, then all 5 a row as the page widens; the cards never stretch.
        self.assertEqual([len(row) for row in settings_window.flow_rows(780, cards, 14)], [3, 2])
        self.assertEqual([len(row) for row in settings_window.flow_rows(1030, cards, 14)], [4, 1])
        self.assertEqual(settings_window.flow_rows(1256, cards, 14), [[0, 1, 2, 3, 4]])
        self.assertEqual(settings_window.flow_rows(100, cards[:2], 14), [[0], [1]])     # a row always takes one
        asked = []

        def heights(rows):
            asked.append(rows)
            return [100, 160, 100, 120, 100]
        rows, spots, total, widest = settings_window.flow_spots(780, cards, heights, 14)
        self.assertEqual(asked, [rows])
        self.assertEqual([spot[:2] for spot in spots], [(0, 0), (254, 0), (508, 0), (0, 174), (254, 174)])
        self.assertTrue(all(spot[2] == 240 for spot in spots))
        self.assertEqual(total, 160 + 14 + 120)                          # each row as tall as its tallest tile
        self.assertEqual(widest, 3 * 240 + 2 * 14)                       # the chart below ends with the cards
        _rows, spots, _total, _widest = settings_window.flow_spots(500, [280, 440], lambda rows: [10, 20], 14)
        self.assertEqual(spots[1], (0, 24, 440))                         # too narrow: the facts go under the card
        _rows, spots, _total, _widest = settings_window.flow_spots(300, [440], lambda rows: [10], 14)
        self.assertEqual(spots[0][2], 300)                               # only narrowed when the page is narrower
        groups = settings_window.detail_groups([("额度", ["剩余 60%", "  Argon：剩余 40%"]), ("读取", ["", "下次刷新约 3 分钟后"]),
                                                ("空", [""])])
        self.assertEqual(groups, [("额度", [("", "剩余 60%", False), ("Argon", "剩余 40%", False)]),
                                  ("读取", [("", "下次刷新约 3 分钟后", False)])])          # empty groups get no tile
        import settings_view as view
        rows = [("重置时间", "10-31 18:20", False), ("距离重置", "3 小时 12 分", False)]
        tall = view.fact_height(rows, 1.0, view.CARD_W, "重置")
        self.assertEqual(view.fact_tile("重置", rows, 1.0, view.CARD_W, 200).size, (view.CARD_W, 200))    # matches its row
        self.assertEqual(view.fact_tile("重置", rows, 1.0, view.CARD_W).size[1], round(tall))
        self.assertEqual(view.source_card(view.SourceCard("codex", "C", "Codex", "空闲", "idle", (), None, 0, 0, "", "ok"),
                                          None, 1.0, slots=1, height=220).size, (view.CARD_W, 220))
        self.assertEqual(settings_window.step_text(300), "5 分钟")
        self.assertEqual(settings_window.step_text(1800), "30 分钟")

    def test_tweens_retarget_from_where_they_are(self):
        import ui_motion
        jobs = []
        root = SimpleNamespace(after=lambda ms, fn: jobs.append(fn) or len(jobs))
        motion = ui_motion.Motion(root, enabled=True)
        seen = []
        with patch.object(ui_motion.time, "perf_counter", side_effect=[0.0, 0.05, 0.05, 0.10, 0.5, 0.5]):
            motion.to("x", 0.0, 100.0, .2, seen.append)
            jobs.pop(0)()                       # t = 0.05: a quarter of the way in time
            moved = seen[-1]
            self.assertTrue(0 < moved < 100)
            motion.to("x", 0.0, 0.0, .2, seen.append)   # turn around: starts from where it is, not from 0
            jobs.pop(0)()                       # t = 0.5: finished
        self.assertEqual(seen[-1], 0.0)
        self.assertFalse(motion.busy("x"))
        instant = ui_motion.Motion(root, enabled=False)
        instant.to("y", 0, 5, 1.0, seen.append)
        self.assertEqual(seen[-1], 5)
        self.assertEqual(ui_motion.blend("#000000", "#ffffff", .5), "#808080")
        widths = ui_motion.CharWidths(SimpleNamespace(measure=lambda char: 2 if ord(char) > 127 else 1))
        self.assertEqual(widths("ab额度"), 6)

    def test_throttle_runs_first_call_and_the_last(self):
        import ui_motion
        jobs, calls = [], []
        root = SimpleNamespace(after=lambda ms, fn: jobs.append(fn) or len(jobs))
        throttle = ui_motion.Throttle(root, 50, lambda: calls.append(len(calls)))
        throttle(); throttle(); throttle()
        self.assertEqual(len(calls), 1)
        jobs.pop(0)()                           # the gap passes: the held-back call runs once
        self.assertEqual(len(calls), 2)
        jobs.pop(0)()
        self.assertEqual(len(calls), 2)

    def test_detail_sections_feed_the_window_and_the_tray_box(self):
        import tray_app
        from unittest.mock import Mock
        snap = snapshot(provider="codex", label="Codex", secondary_window_minutes=10080, secondary_remaining_percent=30,
                        secondary_resets_at=int(time.time() + 86400), error="读取超时")
        tray = SimpleNamespace(snapshots={"codex": snap}, forecasts=lambda p: [],
                               performance_line=lambda p: "最近生成 TPS：80.0 tok/s（14:00:00）\n来源：会话",
                               output_rate_line=lambda p: "最近60秒输出速率：1.0 tok/s（60 输出tokens）",
                               menu_reset=lambda p: "重置时间：18:00", menu_countdown=lambda p: "距离重置：3 小时",
                               menu_updated=lambda p: "更新时间：未知",
                               scheduler=SimpleNamespace(describe=lambda p, now: "下次刷新约 3 分钟后（空闲）"))
        sections = tray_app.QuotaTray.detail_sections(tray, "codex")
        self.assertEqual([title for title, _lines in sections], ["额度", "速度", "重置", "读取"])
        self.assertIn("来源：会话", sections[1][1])
        self.assertIn("刷新计划：下次刷新约 3 分钟后（空闲）", sections[3][1])
        self.assertEqual(sections[3][1][-1], "读取状态：读取超时")
        import settings_window
        groups = settings_window.detail_groups(sections)
        self.assertEqual(groups[3][1][-1], ("读取状态", "读取超时", True))         # a read error reads as a warning
        self.assertEqual(groups[0][1][0][0], "")                                 # the quota line has no title of its own
        tray.detail_sections = lambda p: tray_app.QuotaTray.detail_sections(tray, p)
        with patch.object(tray_app.threading, "Thread") as thread, patch.object(tray_app, "ctypes", Mock()):
            tray_app.QuotaTray.open_details(tray, "codex")
        self.assertIn("\n\n重置时间：18:00", thread.call_args.kwargs["args"][1])
        # With the saved output records, a usage tile sits before 读取.
        from usage_store import UsageStore
        tray.usage, tray.history = UsageStore(Path(tempfile.mkdtemp()) / "usage.json.gz"), None
        tray.weekly_windows = lambda: tray_app.QuotaTray.weekly_windows(tray)
        sections = tray_app.QuotaTray.detail_sections(tray, "codex")
        self.assertEqual([title for title, _lines in sections], ["额度", "速度", "重置", "用量", "读取"])
        self.assertIn("暂无输出", sections[3][1][0])
        # The weekly window's readings feed the "周额度" note: Codex's second window.
        from forecast import QuotaHistory
        tray.history = QuotaHistory(Path(tempfile.mkdtemp()) / "history.json")
        reset = int(time.time() - 3600)
        for at, used, window in ((reset - 7200, 55, reset), (reset - 600, 58, reset), (reset + 600, 0, reset + 7 * 86400)):
            tray.history.add("codex:secondary", at, used, window)
        lines = tray_app.QuotaTray.detail_sections(tray, "codex")[3][1]
        self.assertTrue(lines[0].startswith("周额度：") and "还剩 42%" in lines[0])
        tray.history = QuotaHistory(Path(tempfile.mkdtemp()) / "five-hour.json")      # a 5-hour reset says nothing
        for at, used, window in ((reset - 7200, 55, reset), (reset - 600, 58, reset), (reset + 600, 0, reset + 5 * 3600)):
            tray.history.add("codex", at, used, window)
        self.assertFalse(tray_app.QuotaTray.detail_sections(tray, "codex")[3][1][0].startswith("周额度"))

    def test_preferences_round_trip_and_apply(self):
        import tray_app
        folder = Path(tempfile.mkdtemp())
        herdr = SimpleNamespace(enabled=True, status="已接入", rows=[1])
        tray = SimpleNamespace(tps_monitor=SimpleNamespace(herdr=herdr))
        with patch.object(tray_app, "PREFERENCES_PATH", folder / "settings.json"), patch.dict("os.environ", {}, clear=False) as env:
            env.pop("CIRCLOW_HERDR", None)
            (folder / "settings.json").write_text('{"herdr": true, "tray_order": "auto"}', encoding="utf-8")
            tray_app.QuotaTray.save_preferences(SimpleNamespace(apply_preferences=lambda prefs: tray_app.QuotaTray.apply_preferences(tray, prefs)),
                                                {"herdr": False})
            self.assertEqual({"herdr": False}, tray_app.load_preferences())   # retired tray_order dropped
            self.assertFalse(herdr.enabled)
            self.assertFalse(hasattr(tray_app, "TRAY_ORDER_MODE"))
            tray_app.QuotaTray.apply_preferences(tray, {"tray_order": "auto", "herdr": True})
            self.assertTrue(herdr.enabled)


class SourceSwitchTests(unittest.TestCase):
    def test_states_explain_every_source_and_saved_switches_win(self):
        import providers
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"LOCALAPPDATA": root}):
            states = {item["key"]: item for item in providers.provider_states("codex,grok")}
            self.assertEqual({"codex", "grok", "gemini", "claude", "mimo"}, set(states))
            self.assertFalse(states["claude"]["enabled"])
            self.assertIn("未包含", states["claude"]["reason"])
            providers.set_provider_choices({"claude": True, "grok": False})
            states = {item["key"]: item for item in providers.provider_states("codex,grok")}
            self.assertTrue(states["claude"]["enabled"])
            self.assertEqual("手动开启", states["claude"]["reason"])
            self.assertFalse(states["grok"]["enabled"])
            self.assertEqual(["codex", "claude"], providers.enabled_keys("codex,grok"))
            providers.set_provider_choices({"grok": None})
            self.assertNotIn("grok", providers.provider_choices())

    def test_mimo_manual_plan_from_settings_form(self):
        import json
        import mimo_usage
        self.assertEqual(49.2e9, mimo_usage.parse_credits("49.2B"))
        self.assertEqual(4.92e10, mimo_usage.parse_credits("492亿"))
        self.assertIsNone(mimo_usage.parse_credits("abc"))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mimo.json"
            mimo_usage.save_manual_plan("49.2B", "2027年6月1日", "1.5B", "Max", path=path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("2027-06-01", saved["period_end"])
            self.assertEqual(1.5e9, saved["used_credits"])
            with self.assertRaises(ValueError):
                mimo_usage.save_manual_plan("", "2027-06-01", path=path)
            with self.assertRaises(ValueError):
                mimo_usage.save_manual_plan("1B", "next year", path=path)
            mimo_usage.remove_manual_plan(path)
            self.assertFalse(path.exists())


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class AutoSaveTests(unittest.TestCase):
    """No save button: each group is saved on its own a moment after an edit."""

    def window(self, app=None):
        from unittest.mock import Mock
        import settings_window
        window = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
        window.app = app or Mock()
        window.vars = {"summary": Var(False), "startup": Var(False), "herdr": Var(True), "source:claude": Var(True)}
        window.fields = {"url": Var(""), "model": Var(""), "key": Var("")}
        window.iq_key, window.iq_selected = Var(""), []
        window.mimo_fields = {name: Var("") for name in ("total", "end", "used", "name")}
        window.root = Mock()
        window.root.after.return_value = "job"
        window.status, window.status_label = Var(""), Mock()
        window.status_label.cget.return_value = ""
        window.notice = ("", 0.0)
        window._fill_mimo = Mock()
        window.origins = window.staged()
        window.seen = dict(window.origins)
        window.save_problems = {}
        return window

    def test_delays_follow_what_changed(self):
        import settings_window
        self.assertEqual(settings_window.save_delay(["startup"]), settings_window.SAVE_DELAYS["switch"])
        self.assertEqual(settings_window.save_delay(["startup", "field:url"]), settings_window.SAVE_DELAYS["text"])
        self.assertEqual(settings_window.save_delay(["iq:key"]), settings_window.SAVE_DELAYS["text"])
        self.assertEqual(settings_window.save_delay(["field:url", "source:grok"]), settings_window.SAVE_DELAYS["source"])

    def test_only_a_real_edit_schedules_a_save(self):
        window = self.window()
        window.update_footer()                                    # nothing changed
        window.root.after.assert_not_called()
        window.vars["startup"].set(True)
        window.update_footer()
        window.root.after.assert_called_once()
        self.assertEqual(window.root.after.call_args.args[0], 150)
        window.update_footer()                                    # same values again (a notice, a redraw): no new save
        window.root.after.assert_called_once()

    def test_a_failing_group_does_not_hold_back_the_others(self):
        window = self.window()
        window.app.apply_summary_settings.side_effect = ValueError("API 地址无效")
        window.fields["url"].set("not a url")
        window.vars["startup"].set(True)
        window.vars["herdr"].set(False)
        window.save()
        window.app.toggle_startup.assert_called_once()
        window.app.save_preferences.assert_called_once_with({"herdr": False})
        self.assertEqual(window.save_problems, {"summary": "任务命名：API 地址无效"})
        self.assertTrue(window.origins["startup"])                # saved
        self.assertEqual(window.origins["field:url"], "")         # still pending
        self.assertIn("未能保存", window.status.get())
        window.app.apply_summary_settings.side_effect = None
        window.save()                                             # fixed on the next edit
        self.assertEqual(window.save_problems, {})
        self.assertEqual(window.origins["field:url"], "not a url")

    def test_sources_go_last_and_mimo_waits_for_its_required_fields(self):
        window = self.window()
        order = []
        window.app.toggle_startup.side_effect = lambda: order.append("startup")
        window.app.set_providers.side_effect = lambda changes: order.append(("sources", changes))
        window.vars["source:claude"].set(False)
        window.vars["startup"].set(True)
        window.mimo_fields["total"].set("49.2B")              # no end date yet
        window.save()
        self.assertEqual(order, ["startup", ("sources", {"claude": False})])
        window.app.save_mimo_manual.assert_not_called()
        self.assertEqual(window.changed_keys(), ["mimo:total"])   # waits, without an error
        self.assertNotIn("mimo", window.save_problems)
        self.assertTrue(window.notice[0].startswith("✓ 已保存，托盘正在重启"))
        window.mimo_fields["end"].set("2027-01-01")
        window.save()
        window.app.save_mimo_manual.assert_called_once_with("49.2B", "2027-01-01", "", "")
        self.assertEqual(window.changed_keys(), [])

    def test_closing_saves_what_is_waiting(self):
        window = self.window()
        window.save_job = "job"
        window.vars["startup"].set(True)
        window.close()
        window.root.after_cancel.assert_called_once_with("job")
        window.app.toggle_startup.assert_called_once()
        window.root.withdraw.assert_called_once()


if __name__ == "__main__":
    unittest.main()
