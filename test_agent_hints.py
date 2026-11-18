"""The hover panel: waiting sessions on their bubbles (local), MiMo's one hint, opening a session."""
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import agent_hints
import agent_panel
import agent_tasks
import session_links

URL = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"


def session(key, state, minutes=3, **extra):
    base = {"key": key, "provider": "claude", "state": state, "project": "Lumen", "title": "关闭左侧屏的图标要变红",
            "first": "把右侧的标识放进显示器图标里", "asks": ["关闭左侧屏的图标要变红，并有斜杠提示"],
            "reply": "已部署，点一下左屏图标看看效果。", "steps": ["编辑 xaml_rows.inc", "构建"], "errors": [],
            "older_errors": 0, "minutes": minutes, "running_minutes": None, "at": time.time(), "detail": ""}
    base.update(extra)
    return base


def reply(content):
    return lambda request, timeout: io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


def settle(advisor):
    for _ in range(200):
        if not advisor.pending:
            return
        time.sleep(.01)


class AdvisorTests(unittest.TestCase):
    def advisor(self, content, opener=None):
        return agent_hints.HintAdvisor({"url": URL, "key": "SECRET", "model": "mimo-v2.6-flash"}, opener or reply(content))

    def test_mimo_line_with_its_short_name_and_the_key_stays_out(self):
        seen = []

        def opener(request, timeout):
            seen.append((json.loads(request.data), timeout))
            return reply('{"kind":"idea","session":"s1","name":"VANE 论文","text":"结论只提升 0.84%，先证明不是随机种子波动"}')(request, timeout)

        advisor = self.advisor("", opener)
        data = agent_hints.context([session("a", "working", 0, provider="codex")])
        self.assertEqual(advisor.lookup(data), ())                          # nothing until MiMo answers
        settle(advisor)
        self.assertEqual(advisor.lookup(data), (("idea", "codex", "Codex·VANE 论文：结论只提升 0.84%，先证明不是随机种子波动"),))
        payload, timeout = seen[0]
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual((payload["model"], payload["thinking"]), (agent_hints.HINT_MODEL, {"type": "enabled"}))
        self.assertEqual(timeout, agent_hints.TIMEOUT)
        data["sessions"][0]["minutes"] = 6                                  # minutes move, the answer stays
        advisor.lookup(data)
        self.assertEqual(len(seen), 1)

    def test_a_changed_state_drops_the_old_line_at_once(self):
        advisor = self.advisor('{"kind":"idea","session":"s1","text":"结论只提升 0.84%，先证明不是随机种子波动"}')
        data = agent_hints.context([session("a", "working", 0)])
        advisor.lookup(data)
        settle(advisor)
        self.assertTrue(advisor.lookup(data))
        moved = agent_hints.context([session("a", "done", 0, asks=["关闭左侧屏的图标要变红", "第二页也加斜杠"])])
        advisor.last_request_at = time.time()
        self.assertEqual(advisor.lookup(moved), ())

    def test_answers_that_do_not_hold_up_are_dropped(self):
        data = agent_hints.context([session("a", "working", 0), session("b", "done", 2)])
        parse = agent_hints.HintAdvisor.parse
        for content in ('{"kind":"wait","session":"s2","text":"问你要不要覆盖旧实验结果"}',          # waiting is not a hint
                        '{"kind":"idea","session":"s9","text":"换一种更稳的实验设计试试看"}',
                        '{"kind":"idea","session":"s2","text":"多 agent 分工可以再细一点更高效"}',
                        '{"kind":"idea","session":"s2","text":"把成果沉淀成可复用的知识库"}',
                        '{"kind":"idea","session":"s2","text":"剩余 40% 额度注意节省"}',
                        '{"kind":"idea","session":"s2","text":"好"}',
                        '{"kind":"idea","session":"s2","text":"' + "很长" * 30 + '"}',
                        '{"kind":"none"}', 'no json at all'):
            self.assertEqual(parse(content, data)[0], "none", content)
        self.assertEqual(parse('{"kind":"stuck","session":"s1","text":"同一个谷歌验证报错已重试四次，该换思路"}', data)[0], "stuck")
        self.assertEqual(parse('{"kind":"stuck","session":"s2","text":"同一个谷歌验证报错已重试四次，该换思路"}', data)[0], "idea")

    def test_stuck_is_marked(self):
        advisor = self.advisor('{"kind":"stuck","session":"s1","name":"防 root 检测","text":"同一个谷歌验证报错重试了四次，该换思路"}')
        data = agent_hints.context([session("a", "working", 0, provider="codex", errors=["Exit code 1"] * 3)])
        advisor.lookup(data)
        settle(advisor)
        self.assertEqual(advisor.lookup(data), (("stuck", "codex", "留意 Codex·防 root 检测：同一个谷歌验证报错重试了四次，该换思路"),))

    def test_failures_back_off(self):
        calls = []

        def broken(request, timeout):
            calls.append(1)
            raise TimeoutError

        advisor = self.advisor("", broken)
        data = agent_hints.context([session("a", "working", 1)])
        for _ in range(5):
            self.assertEqual(advisor.lookup(data), ())
            settle(advisor)
        self.assertEqual(len(calls), 1)
        self.assertGreater(advisor.failed_until, time.time())

    def test_no_request_without_sessions_or_without_mimo(self):
        advisor = self.advisor("", lambda *a, **k: self.fail("no request without sessions"))
        self.assertEqual(advisor.lookup(agent_hints.context([])), ())
        other = agent_hints.HintAdvisor({"url": "https://example.com/v1/chat/completions", "key": "", "model": "x"},
                                        lambda *a, **k: self.fail("no request"))
        self.assertEqual(other.lookup(agent_hints.context([session("a", "working", 1)])), ())

    def test_secrets_and_addresses_never_leave(self):
        data = agent_hints.context([session("a", "working", asks=["key: sk-abcdefghijklmnopqrstuv mail me@example.com"])])
        packed = json.dumps(agent_hints._material(data), ensure_ascii=False)
        self.assertNotIn("sk-abcdefghijklmnop", packed)
        self.assertNotIn("me@example.com", packed)
        self.assertNotIn('"key"', packed)

    def test_prompt_rules(self):
        for wanted in ("stuck", "idea", "none", "宁缺毋滥", "herdr", "不要编造", "额度", "面板已经列出"):
            self.assertIn(wanted, agent_hints.SYSTEM_PROMPT)


class WaitingTests(unittest.TestCase):
    """Who waits for the user, read locally, and how it reaches the bubbles."""

    def notes_with(self, key, reply_text, ended=True, minutes_ago=2):
        notes = agent_tasks.AgentNotes()
        moment = time.time() - minutes_ago * 60
        notes.ask(key, "把右侧的标识放进显示器图标里", moment - 30)
        notes.said(key, reply_text, moment)
        if ended:
            notes.ended(key, "done", moment)
        return notes

    def test_states(self):
        now = time.time()
        asked = self.notes_with("a", "已部署。第二页要不要也加斜杠？").work([], {}, now)[0]
        self.assertEqual((asked["waiting"], asked["detail"]), ("asked", "第二页要不要也加斜杠？"))
        done = self.notes_with("d", "两个都做完了。").work([], {}, now)[0]
        self.assertEqual(done["waiting"], "done")
        old = self.notes_with("o", "两个都做完了。", minutes_ago=30).work([], {}, now)[0]
        self.assertEqual(old["waiting"], "")                               # finished long ago, no unseen mark
        unseen = self.notes_with("u", "两个都做完了。", minutes_ago=30).work([], {}, now, panes={"u": ("done", "w1:p2", "")})[0]
        self.assertEqual((unseen["waiting"], unseen["pane"]), ("done", "w1:p2"))   # herdr: finished, not viewed
        viewed = self.notes_with("v", "第二页要不要也加斜杠？").work([], {}, now, panes={"v": ("idle", "w1:p3", "")})[0]
        self.assertEqual(viewed["waiting"], "")                            # herdr: the user has looked at it
        looked = self.notes_with("l", "第二页要不要也加斜杠？").work([], {}, now, seen=lambda key: True)[0]
        self.assertEqual(looked["waiting"], "")                            # Claude desktop: focused since the turn
        running = self.notes_with("r", "我先看看", ended=False).work([{"key": "r", "live": True}], {}, now)[0]
        self.assertEqual(running["waiting"], "")
        blocked = self.notes_with("b", "要删除临时目录", ended=False).work([], {}, now, panes={"b": ("blocked", "w1:p4", "审批")})[0]
        self.assertEqual((blocked["waiting"], blocked["title"]), ("blocked", "审批"))

    def test_waiting_sessions_join_the_bubbles_and_lead_their_group(self):
        live = {"key": "x", "provider": "codex", "live": True, "doing": "运行命令", "title": "VANE", "at": time.time()}
        work = [session("x", "done", 1, waiting="done", detail="跑完了"),
                session("y", "asked", 4, waiting="asked", detail="要不要加斜杠？", pane="w1:p2"),
                session("z", "working", 0, waiting="")]
        agents = agent_tasks.with_waiting([live], work)
        self.assertEqual([a["key"] for a in agents], ["x", "y"])            # the working one has its own bubble already
        self.assertEqual((agents[0]["waiting"], agents[1]["waiting"], agents[1]["pane"]), ("done", "asked", "w1:p2"))
        group = agent_tasks.Cluster("g", tuple(agents), True)
        self.assertEqual(agent_tasks.waiting_of(group), "asked")
        self.assertEqual(min(agents, key=agent_tasks.wait_rank)["key"], "y")

    def test_a_click_opens_but_a_drag_does_not(self):
        from test_agent_tasks import panel
        model = panel(agent_panel.BubbleModel("a", "Lumen", "在问你", "C", False, waiting="asked"),
                      agent_panel.BubbleModel("b", "VANE", "运行命令", "C", True))
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        for step in range(120):
            scene.step(step / 60, None, False, False)
        a, b = scene.bodies["a"], scene.bodies["b"]
        scene.step(2.1, (a.x, a.y), True, False)
        self.assertEqual(scene.step(2.2, (a.x, a.y), False, False), [("open", "a")])
        scene.step(2.3, (a.x, a.y), True, False)
        events = []
        for index in range(1, 20):
            events += scene.step(2.3 + index / 60, (a.x + index * 3, a.y), True, False)
        events += scene.step(2.8, (a.x + 60, a.y), False, False)
        self.assertNotIn(("open", "a"), events)
        self.assertTrue(scene.ambient)                                     # the question pulses
        self.assertIsNotNone(scene.render(1.25))


class OpenTests(unittest.TestCase):
    def test_herdr_pane_first(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="{}", stderr=""))
        raised, urls = Mock(), []
        with unittest.mock.patch("herdr_source.find_exe", return_value="herdr.exe"):
            way = session_links.open_session({"key": "abc", "provider": "claude", "pane": "w9:p6"}, runner=runner,
                                             start_url=urls.append, raise_client=raised)
        self.assertEqual(way, "herdr")
        self.assertEqual(runner.call_args[0][0][-3:], ["agent", "focus", "w9:p6"])
        raised.assert_called_once()
        self.assertEqual(urls, [])

    def test_app_links(self):
        urls = []
        index = session_links.DesktopIndex(lambda: [])
        index.read_at, index.sessions = time.time(), {"cli-1": ("local_2f79", False)}
        self.assertEqual(session_links.open_session({"key": "019a-thread", "provider": "codex"}, start_url=urls.append,
                                                    desktop=index), "url")
        self.assertEqual(session_links.open_session({"key": "cli-1", "provider": "claude"}, start_url=urls.append,
                                                    desktop=index), "url")
        self.assertEqual(urls, ["codex://threads/019a-thread", "claude://code/needs-input?session=local_2f79"])
        self.assertIsNone(session_links.open_session({"key": "cli-2", "provider": "claude"}, start_url=urls.append,
                                                     desktop=index))      # a CLI session outside herdr: no link

    def test_desktop_index_reads_ids_and_seen(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "claude-code-sessions" / "acct" / "org"
            target.mkdir(parents=True)
            (target / "local_a.json").write_text(json.dumps({"sessionId": "local_a", "cliSessionId": "c1",
                                                              "lastFocusedAt": 5, "lastTurnEdgeAt": 9}), encoding="utf-8")
            (target / "local_b.json").write_text(json.dumps({"sessionId": "local_b", "cliSessionId": "c2",
                                                              "lastFocusedAt": 9, "lastTurnEdgeAt": 5}), encoding="utf-8")
            (target / "local_c.json").write_text(json.dumps({"sessionId": "local_c", "cliSessionId": "c3",
                                                              "isArchived": True}), encoding="utf-8")
            index = session_links.DesktopIndex(lambda: [Path(folder)])
            self.assertEqual(index.lookup("c1"), ("local_a", False))
            self.assertEqual(index.lookup("c2"), ("local_b", True))
            self.assertIsNone(index.lookup("c3"))


class PanelHintTests(unittest.TestCase):
    """The hint band is as tall as its rows, and absent without any."""

    def test_no_hint_no_band_and_each_item_adds_its_rows(self):
        from dataclasses import replace
        from test_agent_tasks import panel
        base = panel()
        one = replace(base, hints=(("stuck", "C", "留意 Claude·Lumen：同一个构建错误重试三次"),))
        two = replace(base, hints=one.hints + (("idea", "M", "Codex·VANE：" + "故事应落在物理约束贡献的量化上" * 3),))
        field = 120
        self.assertEqual(agent_panel.hint_height(base), 0)
        heights = [agent_panel.panel_height(model, field) for model in (base, one, two)]
        self.assertEqual(heights[1] - heights[0], agent_panel.HINT_LINE + 8)
        self.assertEqual(heights[2] - heights[1], 2 * agent_panel.HINT_LINE + agent_panel.HINT_GAP)   # wraps to two lines
        self.assertNotEqual(base.header, one.header)                       # the cached card follows the height
        self.assertEqual(agent_panel._header_model(two.header).hint_space, agent_panel.hint_height(two))
        for model in (base, one, two):
            scene = agent_panel.Scene()
            scene.set_model(model, 0)
            self.assertAlmostEqual(scene.render(1.25).height, (agent_panel.panel_height(model, scene.field.value)
                                                               + 2 * agent_panel.MARGIN) * 1.25, delta=2)

    def test_a_line_never_starts_with_punctuation(self):
        for text in ("Claude·Lumen 屏幕图标：红斜杠已部署，点一次左屏图标验收，并决定第二页是否也加斜杠 · 4 分钟",
                     "一" * 27 + "，二三"):
            for line in agent_panel._hint_lines(text, 1.0)[1:]:
                self.assertNotIn(line[0], "，。、；：·, ")


class NoteTests(unittest.TestCase):
    """What the notes keep for the hint, per client."""

    def test_codex_turn_reply_errors_and_end(self):
        notes = agent_tasks.AgentNotes()
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        notes.codex("c", {"type": "event_msg", "timestamp": t, "payload": {"type": "task_started"}})
        notes.codex("c", {"type": "event_msg", "timestamp": t, "payload": {"type": "user_message", "message": "构思 VANE 论文的故事"}})
        notes.codex("c", {"type": "response_item", "timestamp": t, "payload": {
            "type": "function_call_output", "output": "Exit code: 1\nTraceback (most recent call last): boom"}})
        notes.codex("c", {"type": "response_item", "timestamp": t, "payload": {
            "type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "Script completed\n读到：负结果、失败的尝试"}]}})
        note = notes.notes["c"]
        self.assertEqual(len(note.errors), 1)                               # a file that mentions 失败 is no failure
        notes.codex("c", {"type": "event_msg", "timestamp": t, "payload": {
            "type": "task_complete", "last_agent_message": "要不要把对照实验也重跑一遍？"}})
        self.assertEqual((note.end_kind, note.reply), ("done", "要不要把对照实验也重跑一遍？"))
        self.assertEqual(note.asks[-1][1], "构思 VANE 论文的故事")
        work = notes.work([], {"c": {"model": "gpt"}}, time.time() + 60, lambda model: "codex")
        self.assertEqual((work[0]["state"], work[0]["provider"]), ("asked", "codex"))

    def test_claude_reply_tool_error_and_end_turn(self):
        notes = agent_tasks.AgentNotes()
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        notes.claude("k", {"type": "user", "timestamp": t, "message": {"content": "继续任务，并且检查两个 hint 有没有实现的必要"}})
        notes.claude("k", {"type": "user", "timestamp": t, "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "Exit code 1 permission denied"}]}})
        notes.claude("k", {"type": "assistant", "timestamp": t, "message": {
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "两个都做完了，判据记在 LEDGER.md。"}]}})
        work = notes.work([], {}, time.time() + 60)
        self.assertEqual(work[0]["state"], "done")
        self.assertEqual(work[0]["errors"], ["Exit code 1 permission denied"])
        self.assertTrue(work[0]["title"].startswith("检查两个 hint"))      # not "继续任务"

    def test_herdr_blocked_and_live_states_and_skipped_sessions(self):
        notes = agent_tasks.AgentNotes()
        now = time.time()
        for key in ("a", "b", "sub", "cron"):
            notes.ask(key, "把右侧的标识放进显示器图标里", now - 30)
        notes.notes["sub"].parent = "a"
        notes.notes["cron"].schedule_id = "x"
        notes.touch("quiet", now)                                           # no request from the user at all
        work = notes.work([{"key": "a", "herdr_status": "blocked"}, {"key": "b", "live": True}], {}, now)
        self.assertEqual([(w["key"], w["state"]) for w in work], [("a", "blocked"), ("b", "working")])


if __name__ == "__main__":
    unittest.main()
