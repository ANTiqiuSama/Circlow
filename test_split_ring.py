"""Split-ring tray states, native icon frames and quota source boundaries."""
import ctypes
import io
import json
import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw
import agent_panel
import icon_art
import quota_sources
import tray_app
import tray_icons
from quota_sources import QuotaSnapshot


def snapshot(provider='codex', remaining=0, second=61, reset=1007200, second_reset=1086400, **fields):
    return QuotaSnapshot(provider, provider, 300, None if remaining is None else 100-remaining,
                         remaining, reset, 1000000, False, None, 10080 if second is not None else None,
                         None if second is None else 100-second, second, second_reset if second is not None else None,
                         **fields)


class SplitRingTests(unittest.TestCase):
    def test_split_windows_and_single_window(self):
        self.assertEqual(tray_app.icon_windows(snapshot(remaining=83, second=73), 'C')[0], (83,73))
        self.assertEqual(tray_app.icon_windows(snapshot('grok', remaining=60, second=None), 'X')[0], (60,))
        unknown = tray_app.icon_windows(snapshot(remaining=None, second=73), 'C')[0]
        self.assertEqual(unknown, (None,73))

    def test_gemini_shows_all_three_pools_in_panel_order(self):
        pools = [{'key':'argon','remaining_percent':23}, {'key':'claude_gpt','remaining_percent':61},
                 {'key':'gemini','remaining_percent':8}]
        values, colors = tray_app.icon_windows(snapshot('gemini', remaining=23, second=None, pools=pools), 'G')
        self.assertEqual(values,(23,61,8))
        self.assertEqual(colors, tuple(agent_panel.quota_shade('G',i,v,True) for i, v in enumerate((23,61,8))))
        pools[1]['remaining_percent'] = None     # unknown pool keeps its third
        self.assertEqual(tray_app.icon_windows(snapshot('gemini', remaining=23, second=None, pools=pools), 'G')[0], (23,None,8))
        two = [{'key':'claude_gpt','remaining_percent':61}, {'key':'gemini','remaining_percent':8}]   # no Argon access
        self.assertEqual(tray_app.icon_windows(snapshot('gemini', remaining=8, second=None, pools=two), 'G')[0], (61,8))
        self.assertEqual(tray_app.icon_windows(snapshot('gemini', remaining=None, second=None, pools=[]),'G')[0], (None,))

    def test_three_pools_draw_equal_independent_thirds(self):
        full = icon_art.ring_icon((100,100,100),'G')
        self.assertNotEqual(full.tobytes(), icon_art.ring_icon((100,100),'G').tobytes())
        for index in range(3):
            values = tuple(0 if i == index else 100 for i in range(3))
            self.assertNotEqual(full.tobytes(), icon_art.ring_icon(values,'G').tobytes())
        # Each full third covers the same amount of ring.
        def lit(values):
            data = icon_art.ring_icon(values, 'G', colors=((255,0,0,255),)*3).convert('RGB').tobytes()
            return sum(1 for i in range(0, len(data), 3) if data[i] > 200 and data[i+1] < 80)
        counts = [lit(tuple(100 if i == index else 0 for i in range(3))) for index in range(3)]
        self.assertTrue(max(counts) - min(counts) <= max(counts) * 0.1, counts)

    def test_gemini_thirds_sit_top_left_bottom_and_top_right(self):
        image = icon_art.ring_icon((100, 100, 100), 'G', colors=((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)))

        def channel(angle):
            rad = math.radians(angle)
            x = round(31.5 + 26 * math.cos(rad))
            y = round(31.5 + 26 * math.sin(rad))
            return image.getpixel((x, y))[:3]

        top_left, bottom, top_right = channel(210), channel(90), channel(330)
        self.assertGreater(top_left[0], 180, top_left)       # Argon
        self.assertLess(top_left[1], 80, top_left)
        self.assertGreater(bottom[1], 180, bottom)           # Claude/GPT
        self.assertLess(bottom[0], 80, bottom)
        self.assertGreater(top_right[2], 180, top_right)     # Gemini
        self.assertLess(top_right[0], 80, top_right)
        short = icon_art.ring_icon((15, 0, 0), 'G', colors=((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)))
        rad = math.radians(210)
        pixel = short.getpixel((round(31.5 + 26 * math.cos(rad)), round(31.5 + 26 * math.sin(rad))))
        self.assertGreater(pixel[0], 180, pixel)

    def test_reset_countdown_uses_all_exhausted_windows_and_ceil_hours(self):
        now = 1000000
        self.assertEqual(tray_app.usable_again_at(snapshot(reset=now+3601),now), now+3601)
        self.assertEqual(tray_app.usable_again_at(snapshot(remaining=0,second=0,reset=now+3600,second_reset=now+432000),now),now+432000)
        self.assertIsNone(tray_app.usable_again_at(snapshot(remaining=0,second=0,reset=now+3600,second_reset=None),now))
        with patch.object(tray_icons, 'waiting_icon', return_value='waiting') as icon:
            self.assertEqual(tray_app.make_provider_icon(snapshot(reset=now+3601),'C',now=now),'waiting')
            icon.assert_called_once_with(2,'C',False,False,'h')

    def test_countdown_units_round_up_at_boundaries(self):
        for seconds, expected in ((1,(1,'m')), (60,(1,'m')), (61,(2,'m')),
                                  (3599,(60,'m')), (3600,(1,'h')), (3601,(2,'h')),
                                  (86399,(24,'h')), (86400,(1,'d')), (86401,(2,'d'))):
            self.assertEqual(tray_app.countdown_value(seconds),expected)

    def test_five_percent_remaining_is_treated_as_exhausted_without_changing_data(self):
        now = 1000000
        for remaining in (0, 1, 5):
            value = snapshot('claude', remaining=remaining, reset=now+3601)
            self.assertEqual(tray_app.usable_again_at(value, now), now+3601)
            with patch.object(tray_icons, 'waiting_icon', return_value='waiting') as icon:
                self.assertEqual(tray_app.make_provider_icon(value, 'A', now=now), 'waiting')
                icon.assert_called_once_with(2, 'A', False, False, 'h')
            self.assertEqual(value.remaining_percent, remaining)
        self.assertIsNone(tray_app.usable_again_at(snapshot('claude', remaining=5.01, reset=now+3601), now))

    def test_codex_and_grok_use_ten_percent_for_both_windows(self):
        now = time.time()
        for provider in ('codex', 'grok'):
            for primary, secondary in ((10, 60), (60, 10)):
                with self.subTest(provider=provider, primary=primary, secondary=secondary):
                    value = snapshot(provider, primary, secondary, now+7200, now+10800)
                    expected = now+7200 if primary == 10 else now+10800
                    self.assertEqual(tray_app.usable_again_at(value, now), expected)
                    with patch.object(tray_icons, 'make_provider_icon', return_value='countdown'):
                        self.assertEqual(tray_app.make_display_icon(value, 'C', {'active_sessions':1, 'display_tps':25}), 'countdown')
            self.assertIsNone(tray_app.usable_again_at(snapshot(provider, 10.01, 60, now+7200), now))

    def test_gemini_icon_never_switches_to_tps(self):
        value = snapshot('gemini', 40, None)
        with patch.object(tray_icons, 'make_provider_icon', return_value='quota'):
            self.assertEqual(tray_app.make_display_icon(value, 'G', {'active_sessions':1, 'display_tps':25}), 'quota')

    def test_stale_countdown_and_live_tps_priority(self):
        now = time.time()
        value = snapshot('claude', remaining=1, reset=now+3601)
        value.stale = True
        with patch.object(tray_icons, 'waiting_icon', return_value='waiting') as icon:
            self.assertEqual(tray_app.make_provider_icon(value, 'A', now=now), 'waiting')
            icon.assert_called_once_with(2, 'A', False, True, 'h')
        with patch.object(tray_icons, 'make_tps_icon', return_value='live'):
            self.assertEqual(tray_app.make_display_icon(value, 'A', {'active_sessions':1, 'display_tps':12}), 'live')
        self.assertEqual(icon_art.waiting_icon(2, 'A', stale=True, unit='h').tobytes(),
                         icon_art.waiting_icon(2, 'A', stale=False, unit='h').tobytes())

    def test_gemini_recovers_when_any_exhausted_pool_resets(self):
        now = 1000000
        pools = [{'remaining_percent':0,'resets_at':now+7200},{'remaining_percent':0,'resets_at':now+3600}]
        value = snapshot('gemini',remaining=0,second=None,pools=pools)
        self.assertEqual(tray_app.usable_again_at(value,now),now+3600)
        pools[1]['remaining_percent'] = 20
        self.assertIsNone(tray_app.usable_again_at(value,now))
        pools[1]['remaining_percent'] = None
        self.assertIsNone(tray_app.usable_again_at(value,now))

    def test_two_windows_draw_independent_halves_and_encode_ico(self):
        image = icon_art.ring_icon((83,73),'C')
        self.assertNotEqual(image.tobytes(), icon_art.ring_icon((0,73),'C').tobytes())
        self.assertNotEqual(image.tobytes(), icon_art.ring_icon((83,0),'C').tobytes())
        self.assertEqual(icon_art.ring_icon((73,),'C').tobytes(), icon_art.quota_icon(73,'C').tobytes())
        self.assertEqual(image.crop((25,20,39,44)).tobytes(), icon_art.quota_icon(73,'C').crop((25,20,39,44)).tobytes())
        encoded = io.BytesIO()
        image.save(encoded,format='ICO',sizes=[(16,16),(32,32),(64,64)])
        encoded.seek(0)
        ico = Image.open(encoded)
        for size in (16,32,64):
            self.assertEqual(ico.ico.getimage((size,size)).size,(size,size))

    @unittest.skipUnless(os.name == 'nt', 'requires Windows native HICON')
    def test_windows_can_load_the_selected_tray_frame(self):
        from hover_details import HoverIcon
        icon = HoverIcon.__new__(HoverIcon)
        icon._running = False
        icon._icon = icon_art.ring_icon((83,73),'C')
        icon._icon_handle = None
        icon._assert_icon_handle()
        self.assertTrue(icon._icon_handle)
        ctypes.windll.user32.DestroyIcon.argtypes = [ctypes.c_void_p]
        ctypes.windll.user32.DestroyIcon(icon._icon_handle)
        icon._icon_handle = None

    def test_waiting_and_live_icons_are_visually_distinct_at_real_sizes(self):
        waiting = icon_art.waiting_icon(120,'C')
        live = icon_art.live_icon(120,'C')
        self.assertEqual(waiting.getpixel((32, 2)), icon_art.TRACK)
        self.assertNotEqual(icon_art.waiting_icon(2, 'C', unit='d').tobytes(),
                            icon_art.waiting_icon(2, 'C', unit='h').tobytes())
        for size in (16,20,24,32):
            a = waiting.resize((size,size),Image.Resampling.LANCZOS)
            b = live.resize((size,size),Image.Resampling.LANCZOS)
            self.assertNotEqual(a.tobytes(),b.tobytes())
            self.assertEqual((a.size,b.size),((size,size),(size,size)))

    def test_countdown_digits_use_the_tps_face_in_red(self):
        draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        for text in ("4", "12", "120"):
            wide = len(text) >= 3
            same = icon_art._fit(text, icon_art.number_font, 230 if wide else 212,
                                  132 if wide else 176, 220, draw)
            self.assertEqual(icon_art._numeral_face(text, draw).size, same.size)
        waiting = icon_art.waiting_icon(4, "C", unit="h")
        live = icon_art.live_icon(4, "C")

        def span(image, pred):
            rows = [y for y in range(image.height) if pred(image.getpixel((32, y)))]
            return (min(rows), max(rows)) if rows else None

        red = lambda pixel: pixel[0] > 160 and pixel[0] > pixel[1] + 40 and pixel[0] > pixel[2] + 40
        dark = lambda pixel: pixel[0] < 70 and pixel[1] < 70 and pixel[2] < 70 and pixel[3] > 180
        red_span, dark_span = span(waiting, red), span(live, dark)
        self.assertIsNotNone(red_span)
        self.assertAlmostEqual(red_span[1] - red_span[0], dark_span[1] - dark_span[0], delta=2)
        self.assertEqual(waiting.getpixel((32, 2)), icon_art.TRACK)

    def test_reset_units_share_the_hour_size(self):
        draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        hour = icon_art._unit_face(draw)
        self.assertEqual(hour.size, icon_art._fit("h", icon_art.number_font, 96, 96, 132, draw).size)
        self.assertGreater(hour.size, icon_art._fit("m", icon_art.number_font, 96, 96, 132, draw).size)

        def ivory_cols(unit, amount=1):
            image = icon_art.waiting_icon(amount, "C", unit=unit)
            return [x for y in range(image.height) for x in range(image.width)
                    if image.getpixel((x, y))[0] > 210 and image.getpixel((x, y))[1] > 200]

        hour_cols = ivory_cols("h")
        minute_cols = ivory_cols("m")
        day_cols = ivory_cols("d", 12)
        self.assertGreaterEqual(max(minute_cols) - min(minute_cols), max(hour_cols) - min(hour_cols) + 4)
        self.assertGreater(min(day_cols), min(ivory_cols("h", 12)))
        self.assertGreater(min(ivory_cols("m", 12)), min(ivory_cols("h", 12)) - 6)
        self.assertNotEqual(icon_art.waiting_icon(2, "C", unit="m").tobytes(),
                            icon_art.waiting_icon(2, "C", unit="d").tobytes())

        def red_count(amount, unit):
            image = icon_art.waiting_icon(amount, "C", unit=unit)
            return sum(1 for y in range(image.height) for x in range(image.width)
                       if (pixel := image.getpixel((x, y)))[0] > 160
                       and pixel[0] > pixel[1] + 40 and pixel[0] > pixel[2] + 40)

        for amount in (4, 12, 120):
            hour_red = red_count(amount, "h")
            for unit in ("d", "m"):
                self.assertGreaterEqual(red_count(amount, unit), hour_red - 4)

    def test_mimo_ring_letter_is_as_tall_as_the_others(self):
        draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        mimo = icon_art._glyph_face("M", draw)
        shared = icon_art._fit("M", icon_art.letter_font, 176, 168, 212, draw)
        previous = icon_art._fit("M", icon_art.letter_font, 220, 168, 248, draw)
        self.assertEqual(mimo.size, icon_art._fit("M", icon_art.letter_font, 212, 168, 244, draw).size)
        self.assertGreater(mimo.size, shared.size)
        self.assertLess(mimo.size, previous.size)

        def height(letter, face):
            left, top, right, bottom = draw.textbbox((0, 0), letter, font=face)
            return bottom - top

        mimo_height = height("M", mimo)
        for letter in "CXGA":
            face = icon_art._glyph_face(letter, draw)
            self.assertEqual(face.size, icon_art._fit(letter, icon_art.letter_font, 176, 168, 212, draw).size)
            self.assertLess(abs(mimo_height - height(letter, face)), 16)


class CreditStateTests(unittest.TestCase):
    def test_codex_credit_payload_and_missing_balance(self):
        primary = {'usedPercent': 20, 'windowDurationMins': 300, 'resetsAt': 2000000}
        bucket = {'primary':primary,'credits':{'hasCredits':True,'unlimited':False,'balance':'63508.2485'}}
        result = quota_sources.parse_codex_rate_limits(bucket,1)
        self.assertEqual(result.credits_balance,63508.2485)
        self.assertEqual(result.reset_credits,1)
        self.assertIsNone(quota_sources.parse_codex_rate_limits(bucket,-1).reset_credits)
        self.assertIsNone(quota_sources.parse_codex_rate_limits(bucket,True).reset_credits)
        bucket['credits'].pop('balance')
        self.assertIsNone(quota_sources.parse_codex_rate_limits(bucket).credits_balance)
        bucket['credits']['balance'] = 'nan'
        self.assertIsNone(quota_sources.parse_codex_rate_limits(bucket).credits_balance)

    def test_unlimited_credits_cache_as_valid_json_and_reload(self):
        primary = {'usedPercent': 100, 'windowDurationMins': 300, 'resetsAt': 2000000}
        item = quota_sources.parse_codex_rate_limits({'primary':primary,'credits':{'hasCredits':True,'unlimited':True}})
        payload = json.dumps(item.to_dict(),allow_nan=False)
        self.assertTrue(json.loads(payload)['credits_unlimited'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            path.write_text(quota_sources.dump_snapshot({'codex':item}),encoding='utf-8')
            with patch.object(quota_sources,'CACHE_PATH',path):
                self.assertEqual(quota_sources.load_cached_snapshots()['codex'].credits_balance,float('inf'))

    def test_paid_status_is_not_invented_from_activity_alone(self):
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        tray.snapshots = {'codex':snapshot(credits_balance=0)}
        tray.agents = [{'provider':'codex','live':True}]
        tray.performance = {'providers':{}}
        tray.tps_monitor = SimpleNamespace(events=[])
        self.assertEqual(tray.exhausted_state('codex'),('working',(0,)))
        tray.snapshots['codex'].credits_balance = 100
        self.assertEqual(tray.exhausted_state('codex'),('paid',(0,)))
        tray.agents = []
        self.assertEqual(tray.exhausted_state('codex'),('paid',(0,)))
        tray.snapshots['codex'].credits_balance = 0
        self.assertEqual(tray.exhausted_state('codex'),('blocked',(0,)))
        tray.snapshots['codex'].stale = True
        self.assertEqual(tray.exhausted_state('codex'),(None,()))

    def test_threshold_is_not_claimed_as_paid_or_exactly_empty(self):
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        tray.snapshots = {'codex': snapshot(remaining=5, credits_balance=100)}
        tray.agents = []
        tray.performance = {'providers': {}}
        self.assertEqual(tray.exhausted_state('codex'), ('near', (0,)))
        tray.agents = [{'provider': 'codex', 'live': True}]
        self.assertEqual(tray.exhausted_state('codex'), ('near_working', (0,)))

    def test_claude_extra_usage_is_explicitly_inferred_from_recent_output(self):
        now = time.time()
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        value = snapshot('claude',remaining=0)
        value.updated_at = now-5
        tray.snapshots = {'claude':value}
        tray.agents = [{'provider':'claude','live':True}]
        tray.performance = {'providers':{}}
        tray.tps_monitor = SimpleNamespace(events=[])
        self.assertEqual(tray.exhausted_state('claude'),('working',(0,)))
        tray.tps_monitor.events.append(SimpleNamespace(provider='claude',output_tokens=100,ended_at=now-1))
        self.assertEqual(tray.exhausted_state('claude'),('inferred',(0,)))

    def test_session_refresh_does_not_reuse_stale_purchased_credit_information(self):
        for stale in (False,True):
            with self.subTest(stale=stale):
                tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
                old = snapshot(remaining=80, credits_balance=500, reset_credits=1)
                old.stale, old.updated_at = stale, 100.0
                tray.snapshots = {'codex':old}
                tray.history = None
                tray.announce_resets = lambda *args: None
                tray.update_ui = lambda: None
                tray.tps_monitor = SimpleNamespace(codex_rate_limits=(200.0,{
                    'primary':{'used_percent':30,'window_minutes':300,'resets_at':9000}}), codex_limit_hit_at=0)
                tray.update_codex_locally(201.0)
                self.assertEqual(tray.snapshots['codex'].credits_balance, None if stale else 500)
                self.assertEqual(tray.snapshots['codex'].reset_credits, None if stale else 1)


if __name__ == '__main__':
    unittest.main()
