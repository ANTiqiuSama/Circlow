import io
import json
import os
import math
import sqlite3
from contextlib import closing
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import agent_panel
import agent_tasks
import icon_art
from agent_tasks import AgentGroups, AgentNotes, Cluster, Summarizer, clean, describe, tool_action
from tps_sources import TpsMonitor


def agent(key, provider="codex", live=True, title="", parent="", doing="", at=100.0):
    return {"key": key, "provider": provider, "source": "Codex", "model": "", "title": title, "thread_title": "",
            "request": title, "doing": doing, "live": live, "at": at, "parent": parent, "role": "", "folder": "", "tps": None}


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + ".000Z"


class TaskTextTests(unittest.TestCase):
    def test_antigravity_pascal_case_tool_arguments(self):
        self.assertEqual(tool_action("view_file", {"AbsolutePath": "D:/repo/agent_panel.py"}), "阅读 agent_panel.py")
        self.assertEqual(tool_action("replace_file_content", {"TargetFile": "D:/repo/tray_app.py"}), "编辑 tray_app.py")
        self.assertEqual(tool_action("run_command", {"CommandLine": "$env:FOO='x'; cd D:/repo; python -m unittest test_agent_tasks"}), "运行测试")

    def test_request_becomes_a_short_task_phrase(self):
        self.assertEqual(clean("请帮我修一下 C:\\repo\\tray_app.py 里的悬停bug，然后跑测试"), "修一下 tray_app.py 里的悬停bug")
        self.assertEqual(clean("<system-reminder>ignore</system-reminder>好的。实现气泡合并动画"), "实现气泡合并动画")
        self.assertEqual(clean("```py\nprint(1)\n```\nPlease add Grok TPS"), "add Grok TPS")
        self.assertEqual(clean("继续"), "")

    def test_tool_calls_become_verbs(self):
        self.assertEqual(tool_action("exec_command", '{"cmd": "cd x && python -m unittest test_a"}'), "运行测试")
        self.assertEqual(tool_action("apply_patch", "*** Begin Patch\n*** Update File: src/hover.py\n"), "编辑 hover.py")
        self.assertEqual(tool_action("Bash", {"command": "git status"}), "git status")
        self.assertEqual(tool_action("Read", {"file_path": "/a/b/readme.md"}), "阅读 readme.md")
        self.assertEqual(tool_action("Task", {"description": "x"}), "派发子任务")
        self.assertEqual(tool_action("mcp__github__create_pr", {}), "调用 github")


class NotesTests(unittest.TestCase):
    def test_current_codex_user_records_and_tool_wait_keep_task_live(self):
        notes = AgentNotes()
        notes.codex("a", {"type":"event_msg","timestamp":stamp(100),"payload":{"type":"task_started"}})
        notes.codex("a", {"type":"response_item","timestamp":stamp(101),"payload":{
            "type":"message","role":"user","content":[{"type":"input_text","text":"请检查气泡布局与拖动合并"}]}})
        notes.codex("a", {"type":"response_item","timestamp":stamp(102),"payload":{
            "type":"function_call","name":"exec_command","arguments":json.dumps({"cmd":"python -m unittest test_agent_tasks"})}})
        context={"a":{"source":"Codex","model":"gpt-6.1-sol"}}
        active={"a":("codex",103,"刚完成生成")}
        result=notes.export(active,context,{},110,lambda _:"codex")
        self.assertEqual((result[0]["title"],result[0]["doing"],result[0]["live"]),
                         ("检查气泡布局与拖动合并","运行测试",True))
        notes.codex("a", {"type":"event_msg","timestamp":stamp(111),"payload":{"type":"task_complete"}})
        result=notes.export({},context,{},112,lambda _:"codex")
        self.assertFalse(result[0]["live"])

    def test_codex_follow_up_keeps_the_task_and_tracks_steps(self):
        notes = AgentNotes()
        notes.codex("s", {"type": "session_meta", "payload": {"cwd": "C:/w/CodexQuotaTray",
                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": "main", "agent_nickname": "Ada"}}}}})
        notes.codex("s", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "实现气泡面板"}})
        notes.codex("s", {"type": "event_msg", "timestamp": stamp(101), "payload": {"type": "user_message", "message": "继续"}})
        notes.codex("s", {"type": "response_item", "timestamp": stamp(102), "payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"pytest"}'}})
        note = notes.notes["s"]
        self.assertEqual((note.prompt, note.action, note.parent, note.role), ("实现气泡面板", "运行测试", "main", "Ada"))
        exported = notes.export({"s": ("codex", 102, "生成中")}, {"s": {"source": "Codex", "model": "gpt-5"}}, {}, 110)
        self.assertEqual(exported[0]["folder"], "CodexQuotaTray")
        self.assertEqual(exported[0]["doing"], "运行测试")

    def test_claude_subagent_is_live_without_its_own_activity_and_lingers_dim(self):
        notes = AgentNotes()
        notes.claude("agent-1", {"type": "user", "timestamp": stamp(100), "cwd": "/p", "message": {"content": "检查 README 链接"}}, "main")
        notes.claude("agent-1", {"type": "assistant", "timestamp": stamp(101), "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {}}]}})
        context = {"main": {"source": "Claude Code", "model": ""}}
        live = notes.export({"main": ("claude", 101, "生成中")}, context, {}, 120)
        sub = next(item for item in live if item["key"] == "agent-1")
        self.assertTrue(sub["live"])
        self.assertEqual((sub["provider"], sub["doing"], sub["parent"]), ("claude", "检索代码", "main"))
        later = notes.export({}, context, {}, 101 + agent_tasks.LIVE_SECONDS + 5)
        self.assertEqual([(item["key"], item["live"], item["doing"]) for item in later if item["key"] == "agent-1"], [("agent-1", False, "刚完成")])
        self.assertEqual(notes.export({}, context, {}, 101 + agent_tasks.RECENT_SECONDS + 5), [])

    def test_grok_session_that_only_started_is_not_a_bubble(self):
        notes = AgentNotes()
        start = {"timestamp": stamp(100), "params": {"update": {"sessionUpdate": "hook_execution", "event_name": "session_start"}}}
        context = {key: {"source": "Grok CLI", "model": "grok"} for key in ("probe", "real")}
        notes.grok("probe", start)                          # the tray's own /usage read
        notes.grok("real", start)
        notes.grok("real", {"timestamp": stamp(101), "params": {"update": {"sessionUpdate": "user_message_chunk",
                                                                          "content": {"type": "text", "text": "修复图表配色"}}}})
        notes.grok("real", {"timestamp": stamp(102), "params": {"update": {"sessionUpdate": "agent_message_chunk"}}})
        keys = [item["key"] for item in notes.export({}, context, {}, 110, lambda model: "grok")]
        self.assertEqual(keys, ["real"])

    def test_grok_prompt_chunks_are_joined(self):
        notes = AgentNotes()
        for text in ("修复 Grok ", "重置时间解析"):
            notes.grok("g", {"timestamp": stamp(100), "params": {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}}}})
        notes.grok("g", {"timestamp": stamp(101), "params": {"update": {"sessionUpdate": "tool_call", "kind": "read", "title": "Read"}}})
        self.assertEqual((notes.notes["g"].prompt, notes.notes["g"].action), ("修复 Grok 重置时间解析", "阅读文件"))

    def test_monitor_reads_codex_rollout_and_thread_title(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            now = time.time()
            rollout = home / ".codex/sessions/2026/01/01/rollout-x.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("\n".join(json.dumps(r) for r in [
                {"type": "session_meta", "timestamp": stamp(now - 5), "payload": {"cwd": "D:/proj"}},
                {"type": "event_msg", "timestamp": stamp(now - 4), "payload": {"type": "task_started"}},
                {"type": "event_msg", "timestamp": stamp(now - 4), "payload": {"type": "user_message", "message": "继续"}},
            ]) + "\n", encoding="utf-8")
            with closing(sqlite3.connect(home / ".codex/state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, model TEXT, model_provider TEXT, updated_at INT, title TEXT, cwd TEXT)")
                db.execute("INSERT INTO threads VALUES ('t1', ?, 'gpt-5', 'openai', 1, '整理发布说明', 'D:/proj')", (str(rollout),))
            db.close()
            monitor = TpsMonitor(home)
            monitor.poll(now)
            monitor.last_discovery = 0
            monitor.poll(now + 0.5)
            agents = monitor.agents(now + 0.5)
            self.assertEqual([(a["key"], a["title"], a["live"], a["provider"], a["folder"]) for a in agents],
                             [("t1", "整理发布说明", True, "codex", "proj")])


class ScheduleTests(unittest.TestCase):
    def test_native_codex_database_and_definition_files_ignore_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            native=home/'.codex/sqlite/codex-dev.db'
            native.parent.mkdir(parents=True)
            with closing(sqlite3.connect(native)) as db, db:
                db.execute('CREATE TABLE automations (id TEXT,name TEXT,prompt TEXT,status TEXT,last_run_at INT,next_run_at INT)')
                db.execute("INSERT INTO automations VALUES ('watch','额度巡检','检查额度是否恢复','ACTIVE',1900000000000,1900003600000)")
            memory=home/'.codex/automations/deleted/memory.md'
            memory.parent.mkdir(parents=True)
            memory.write_text('已经删除的旧任务运行笔记',encoding='utf-8')
            found=agent_tasks.read_schedules(home/'.codex',home/'.claude')
            self.assertEqual([x['id'] for x in found],['watch'])
            self.assertEqual((found[0]['last_at'],found[0]['next_at']),(1900000000,1900003600))

    def test_real_heartbeat_envelope_is_turn_scoped_and_never_a_bubble(self):
        notes=AgentNotes()
        schedules=[{'key':'Codex:额度巡检','id':'watch','title':'额度巡检','prompt':'检查额度是否恢复'}]
        context={'thread':{'source':'Codex','model':'gpt-6.1-sol'}}
        notes.ask('thread','修改面板动画',100)
        notes.codex('thread',{'type':'event_msg','timestamp':stamp(110),'payload':{'type':'task_started'}})
        text='<heartbeat><automation_id>watch</automation_id><instructions>检查额度是否恢复</instructions></heartbeat>'
        notes.codex('thread',{'type':'response_item','timestamp':stamp(111),'payload':{
            'type':'message','role':'user','content':[{'type':'input_text','text':text}]}})
        active=notes.export({'thread':('codex',112,'生成中')},context,{},112,schedules=schedules)
        self.assertEqual(active[0]['schedule'],'Codex:额度巡检')
        self.assertIn('heartbeat:watch',active[0]['origin'])
        self.assertTrue(agent_tasks.schedule_chips(schedules,active,112)[0]['running'])
        self.assertEqual([a for a in active if not a['schedule']],[])
        notes.codex('thread',{'type':'event_msg','timestamp':stamp(115),'payload':{'type':'task_complete'}})
        finished=notes.export({},context,{},116,schedules=schedules)
        self.assertEqual(finished,[])
        self.assertFalse(agent_tasks.schedule_chips(schedules,finished,116)[0]['running'])
        notes.codex('thread',{'type':'event_msg','timestamp':stamp(120),'payload':{'type':'task_started'}})
        notes.ask('thread','手动检查气泡颜色',121)
        manual=notes.export({'thread':('codex',121,'生成中')},context,{},122,schedules=schedules)
        self.assertEqual(manual[0]['schedule'],'')
        self.assertNotIn('heartbeat:',manual[0]['origin'])

    def test_definitions_from_claude_tasks_and_codex_automation_table(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            task = home / ".claude/scheduled-tasks/daily-digest/SKILL.md"
            task.parent.mkdir(parents=True)
            task.write_text("---\nname: daily-digest\ndescription: 汇总昨日提交\n---\n汇总昨天的提交并写日报", encoding="utf-8")
            (home / ".codex").mkdir()
            with closing(sqlite3.connect(home / ".codex/state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE automations (id TEXT, name TEXT, prompt TEXT, status TEXT, next_run_at INT)")
                db.execute("INSERT INTO automations VALUES ('a1', '依赖巡检', '检查依赖是否有安全更新', 'ACTIVE', 1900000000)")
                db.execute("INSERT INTO automations VALUES ('a2', '旧任务', '不再运行', 'PAUSED', 0)")
                db.execute("CREATE TABLE automation_runs (id TEXT, prompt TEXT)")
                db.execute("INSERT INTO automation_runs VALUES ('r1', '某次运行')")
            found = agent_tasks.read_schedules(home / ".codex", home / ".claude")
        self.assertEqual(sorted((item["title"], item["prompt"]) for item in found),
                         [("daily-digest", "汇总昨天的提交并写日报"), ("依赖巡检", "检查依赖是否有安全更新")])
        self.assertEqual(next(item for item in found if item["source"] == "Codex")["next_at"], 1900000000)

    def test_scheduled_runs_are_flagged_and_dropped_once_finished(self):
        notes = AgentNotes()
        schedules = [{"key": "Codex:依赖巡检", "title": "依赖巡检", "prompt": "检查依赖是否有安全更新"}]
        notes.codex("run", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "检查依赖是否有安全更新"}})
        notes.codex("cron", {"type": "session_meta", "payload": {"originator": "codex_exec", "source": "automation"}})
        notes.codex("cron", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "清理日志"}})
        notes.codex("me", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "实现气泡"}})
        context = {key: {"source": "Codex", "model": "gpt-5"} for key in ("run", "cron", "me")}
        live = {key: ("codex", 100, "生成中") for key in context}
        flagged = {a["key"]: a["schedule"] for a in notes.export(live, context, {}, 101, schedules=schedules)}
        self.assertEqual(flagged, {"run": "Codex:依赖巡检", "cron": "origin:清理日志", "me": ""})
        finished = notes.export({}, context, {}, 130, schedules=schedules)
        self.assertEqual([a["key"] for a in finished], ["me"])
        chips = agent_tasks.schedule_chips(schedules, notes.export(live, context, {}, 101, schedules=schedules), 101)
        self.assertEqual([(c["title"], c["running"]) for c in chips], [("依赖巡检", True), ("清理日志", True)][:2])

    def test_same_request_in_sessions_far_apart_is_recurring(self):
        notes = AgentNotes()
        for key, moment in (("a", 1000), ("b", 1000 + agent_tasks.RECUR_SPREAD + 1), ("c", 1000)):
            notes.ask(key, "汇总每日数据报表" if key != "c" else "别的事情", moment)
        notes.ask("burst1", "同时派发的任务", 5000)
        notes.ask("burst2", "同时派发的任务", 5010)
        self.assertEqual(list(notes.recurring()), ["汇总每日数据报表"])


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "groups.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_spawned_agents_group_with_their_parent_automatically(self):
        agents = [agent("main", title="重构悬停面板"), agent("c1", parent="main", title="写测试"),
                  agent("c2", parent="main", title="改文档"), agent("solo", title="别的事")]
        clusters = AgentGroups(self.path).compose(agents)
        keys = sorted(sorted(m["key"] for m in c.members) for c in clusters)
        self.assertEqual(keys, [["c1", "c2", "main"], ["solo"]])
        group = next(c for c in clusters if len(c.members) == 3)
        self.assertTrue(group.key.startswith("auto:"))
        self.assertEqual(describe(group)[0], "重构悬停面板")

    def test_siblings_group_even_when_parent_is_idle(self):
        clusters = AgentGroups(self.path).compose([agent("c1", parent="p"), agent("c2", parent="p")])
        self.assertEqual(len(clusters), 1)

    def test_merge_persists_and_split_detaches_auto_links(self):
        agents = [agent("a", title="修复额度卡片显示"), agent("b", title="额度卡片测试"), agent("c", parent="a")]
        groups = AgentGroups(self.path)
        clusters = {next(iter(m["key"] for m in c.members)): c for c in groups.compose(agents)}
        first = next(c for c in groups.compose(agents) if any(m["key"] == "a" for m in c.members))
        second = next(c for c in groups.compose(agents) if any(m["key"] == "b" for m in c.members))
        merged = groups.merge(first, second)
        again = AgentGroups(self.path).compose(agents)
        self.assertEqual([(c.key, sorted(m["key"] for m in c.members), c.manual) for c in again], [(merged, ["a", "b", "c"], True)])
        self.assertTrue(clusters)
        AgentGroups(self.path).split(again[0])
        self.assertEqual(sorted(len(c.members) for c in AgentGroups(self.path).compose(agents)), [1, 1, 1])

    def test_group_description_counts_parallel_parts(self):
        cluster = Cluster("group:x", (agent("a", title="额度卡片重构", doing="编辑 card.py"), agent("b", title="额度卡片测试", doing="运行测试"),
                                      agent("c", live=False, title="其他")), True)
        title, doing, detail = describe(cluster)
        self.assertIn(title, ("额度卡片重构", "额度卡片测试"))
        self.assertEqual(doing, "2 路并行 · 编辑 card.py、运行测试")
        self.assertIn("/", detail)


class SummarizerTests(unittest.TestCase):
    def test_settings_save_reload_and_disable_preserves_local_key(self):
        from settings_window import load_summary, save_summary
        with tempfile.TemporaryDirectory() as folder, patch.object(agent_tasks, "SUMMARY_CONFIG", Path(folder) / "summary.json"), patch.dict("os.environ", {}, clear=True):
            config = {"url": "https://token-plan-cn.xiaomimimo.com/v1", "key": "test-only-key", "model": "mimo-v2.6-flash"}
            save_summary(config, True)
            self.assertEqual(agent_tasks.summary_config()["url"], config["url"] + "/chat/completions")
            save_summary(config, False)
            self.assertIsNone(agent_tasks.summary_config())
            self.assertEqual(load_summary()["key"], "test-only-key")
            self.assertFalse((Path(folder) / "summary.tmp").exists())
            with self.assertRaises(ValueError):
                save_summary({**config, "url": "bad-url"}, True)
            self.assertFalse(load_summary()["enabled"])

    def test_live_action_updates_even_when_summary_is_cached(self):
        member=agent("a",title="验证布局",doing="运行测试")
        self.assertEqual(describe(Cluster("a",(member,)),{"title":"布局验证","doing":"阅读代码"})[1],"运行测试")

    def test_mimo_uses_native_auth_and_disables_thinking(self):
        observed = []
        class Reply(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.close()
        def opener(request, timeout):
            observed.append(request)
            return Reply(json.dumps({"choices": [{"message": {"content": '{"title":"验证气泡","doing":"检查接口"}'}}]}).encode())
        summarizer = Summarizer({"url":"https://token-plan-sgp.xiaomimimo.com/v1/chat/completions",
                                 "key":"test-key", "model":"mimo-v2.6-flash"}, opener)
        result = summarizer.request(Cluster("a", (agent("a", title="验证气泡交互"),)))
        self.assertEqual(result["title"], "验证气泡")
        self.assertEqual(observed[0].get_header("Api-key"), "test-key")
        self.assertIsNone(observed[0].get_header("Authorization"))
        payload = json.loads(observed[0].data)
        self.assertEqual(payload["thinking"], {"type":"disabled"})
        self.assertEqual(payload["max_completion_tokens"], 160)
        self.assertNotIn("max_tokens", payload)

    def test_disabled_without_configuration(self):
        self.assertEqual(Summarizer(None).lookup(Cluster("a", (agent("a", title="x"),))), ({}, False))

    def test_group_summary_runs_in_background_and_is_cached(self):
        calls = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            content = '{"title":"悬停面板改版","doing":"并行开发中","parts":{"_one_a":"界面","_one_b":"测试"}}'
            return Reply(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        summarizer = Summarizer({"url": "https://x/v1/chat/completions", "key": "k", "model": "m"}, opener)
        cluster = Cluster("group:g", (agent("item_one_a", title="界面"), agent("item_one_b", title="测试")), True)
        first, busy = summarizer.lookup(cluster)
        self.assertTrue(busy or first)
        deadline = time.time() + 5
        while summarizer.pending and time.time() < deadline:
            time.sleep(0.01)
        result, busy = summarizer.lookup(cluster)
        self.assertFalse(busy)
        self.assertEqual(result["title"], "悬停面板改版")
        self.assertEqual(result["parts"], {"item_one_a": "界面", "item_one_b": "测试"})
        self.assertEqual(describe(cluster, result)[:2], ("悬停面板改版", "并行开发中"))
        self.assertEqual(len(calls), 1)
        self.assertIn("同一个任务", calls[0]["messages"][0]["content"])


def bubble(key, members=(), doing="运行测试"):
    return agent_panel.BubbleModel(key, f"任务 {key}", doing, "C", True, "", members)


def panel(*bubbles):
    return agent_panel.PanelModel("codex", "C", "GPT / Codex", "2 个在工作", (("5 小时", 70.0), ("每周", 40.0)), 80.0, bubbles)


class SceneTests(unittest.TestCase):
    def test_burn_chart_sits_between_bubbles_and_footer(self):
        model = panel(bubble("a"), bubble("b"))
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        field = scene.field.value
        self.assertGreater(agent_panel.chart_top(model, field), agent_panel.field_top(model) + field)
        # Field plate ends 4 below the field, the chart plate starts 4 above the chart: keep them apart.
        self.assertGreaterEqual((agent_panel.chart_top(model, field) - 4) - (agent_panel.field_top(model) + field + 4), 8)
        self.assertGreater(agent_panel.footer_middle(model, field) - 8,
                           agent_panel.chart_top(model, field) + agent_panel.CHART_H + agent_panel.chart_extra_for(model) + 4)
        self.assertLess(agent_panel.footer_middle(model, field) + 10, agent_panel.panel_height(model, field))
        for scale in (1, 1.25, 1.5):
            self.assertIsNotNone(scene.render(scale))

    def test_daily_usage_switches_units_and_promotes_rounded_boundaries(self):
        cases = [(None, ("—", "")), (0, ("0", "")), (999, ("999", "")), (1000, ("1", "K")),
                 (12500, ("12.5", "K")), (999500, ("1", "M")), (1250000, ("1.25", "M")),
                 (999500000, ("1", "B")), (1530000000, ("1.53", "B"))]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(agent_panel.compact_usage(value), expected)

    def test_mimo_daily_usage_is_animated_beside_quota_and_tps(self):
        model = agent_panel.PanelModel("mimo", "X", "Xiaomi MiMo", "空闲", (("套餐额度", 97, ""),), 42, (), "idle", today_used=1.53e9)
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        self.settle(scene, frames=300)
        self.assertAlmostEqual(scene.today_usage.value, model.today_used, delta=100)
        for scale in (1, 1.25, 1.5):
            self.assertIsNotNone(scene.render(scale))
            image, _ = agent_panel.figure(*agent_panel.compact_usage(model.today_used), agent_panel.TEXT, agent_panel.SECONDARY_TEXT, scale)
            columns = agent_panel.meter_columns(2)
            self.assertLess(image.width, (columns[2][0] - columns[1][0]) * scale)
        from dataclasses import replace
        self.assertEqual(model.header, replace(model, today_used=2e9).header)

    def test_tps_takes_the_rightmost_header_slot_for_every_source(self):
        right = agent_panel.meter_columns(2)[-1]
        for count in (1, 2):                                   # Grok has a single quota column
            columns = agent_panel.meter_columns(count)
            self.assertEqual(len(columns), count + 1)
            self.assertEqual(columns[-1], right)
        self.assertGreater(agent_panel.meter_columns(3)[-1][0], right[0])   # Gemini's three pools push it further

    def test_forecast_wraps_between_pools_not_inside_a_name(self):
        from PIL import Image, ImageDraw
        text = "Argon 预计 2 小时后用完；Gemini 预计 13 小时后用完；Claude/GPT 预计 20 小时后用完"
        for scale in (1.0, 1.25, 1.5):
            face = agent_panel.ui_font(round(11 * scale))
            probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            limit = round((agent_panel.WIDTH - 2 * agent_panel.PAD - 48) * scale)
            lines = agent_panel.forecast_lines(text, limit, face, probe)
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].endswith("；"))
            self.assertTrue(lines[1].startswith("Claude/GPT"))
            self.assertTrue(all(probe.textlength(line, font=face) <= limit for line in lines))

    def test_long_quota_forecast_wraps_without_losing_the_ending_at_each_dpi(self):
        from PIL import Image, ImageDraw
        text = "5 小时额度：按当前速度，预计 2 小时 40 分后用完，早于下次重置约 35 分钟"
        for scale in (1.0, 1.25, 1.5):
            face = agent_panel.ui_font(round(11 * scale))
            probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            limit = round((agent_panel.WIDTH - 2 * agent_panel.PAD - 64) * scale)
            lines = agent_panel.forecast_lines(text, limit, face, probe)
            self.assertEqual(len(lines), 2)
            self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
            self.assertTrue(all(probe.textlength(line, font=face) <= limit for line in lines))
            scene = agent_panel.Scene()
            scene.set_model(agent_panel.PanelModel("codex", "C", "GPT / Codex", "空闲", (("5 小时", 70, "2小时后重置"),),
                                                  0, (), "idle", "Codex 重置 22%", text, "warn"), 0)
            self.assertIsNotNone(scene.render(scale))

    def test_switching_source_rolls_figures_faster_than_opening(self):
        def model(provider, letter, value, tps):
            return agent_panel.PanelModel(provider, letter, provider, "空闲", (("5 小时", value, ""), ("每周", value, "")), tps, ())

        def settle_time(scene, start, target):
            moment = start          # until the shown figures (bar % and TPS) reach the new values
            while moment < start + 5 and (abs(scene.meters[0].value - target[0]) >= 0.5
                                          or abs(scene.rate.value - target[1]) >= 0.5):
                moment += 1 / 60
                scene.step(moment, None, False, False)
            return moment - start

        scene = agent_panel.Scene()
        scene.set_model(model("codex", "C", 80, 100), 0.0)
        opening = settle_time(scene, 0.0, (80, 100))
        scene.set_model(model("grok", "X", 20, 300), 10.0)
        switching = settle_time(scene, 10.0, (20, 300))
        self.assertLess(switching, opening * 0.5)
        self.assertLess(switching, 0.4)
        fresh = agent_panel.Scene()                          # a new opening counts up at the easy pace again
        fresh.set_model(model("codex", "C", 80, 100), 0.0)
        self.assertEqual((fresh.meters[0].stiffness, fresh.rate.stiffness),
                         (agent_panel.METER_SPRING[0], agent_panel.RATE_SPRING[0]))

    def test_gear_release_and_right_click_open_settings(self):
        scene = agent_panel.Scene()
        model = panel()
        scene.set_model(model, 0)
        moment = self.settle(scene)
        point = agent_panel.gear_center(model, scene.field.value)
        self.assertEqual(scene.step(moment + .01, point, True, False), [])
        self.assertEqual(scene.step(moment + .02, point, False, False), [("settings",)])
        self.assertEqual(scene.step(moment + .03, point, False, True), [("settings",)])
        scene.step(moment + .04, point, False, False)
        scene.step(moment + .05, point, True, False)
        self.assertEqual(scene.step(moment + .06, None, False, False), [])
        self.assertIsNotNone(scene.render(1.5))

    def settle(self, scene, start=0.0, frames=90, pointer=None, left=False):
        moment = start
        for _ in range(frames):
            moment += 1 / 60
            scene.step(moment, pointer, left, False)
        return moment

    def test_layout_never_overlaps_footprints(self):
        bubbles = [bubble(str(index), doing="编辑 some_long_file_name.py") for index in range(7)] + [bubble("g", (("C", True),) * 4)]
        homes, height = agent_panel.pack(bubbles)
        boxes = []
        for b in bubbles:
            x, y = homes[b.key]
            boxes.append((x - b.half_width, y - b.radius, x + b.half_width, y + b.radius + agent_panel.LABEL))
        for index, a in enumerate(boxes):
            self.assertGreaterEqual(a[0], agent_panel.PAD - 0.01)
            self.assertLessEqual(a[2], agent_panel.WIDTH - agent_panel.PAD + 0.01)
            self.assertLessEqual(a[3], agent_panel.FIELD_TOP + height)
            for other in boxes[index + 1:]:
                self.assertFalse(min(a[2], other[2]) > max(a[0], other[0]) and min(a[3], other[3]) > max(a[1], other[1]))

    def test_dragging_onto_another_bubble_requests_merge(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a"), bubble("b")), 0.0)
        moment = self.settle(scene)
        a, b = scene.bodies["a"], scene.bodies["b"]
        start, end = (a.x, a.y), (b.x, b.y)
        scene.step(moment + 0.01, start, True, False)
        events = []
        for index in range(1, 31):
            p = index / 30
            events += scene.step(moment + 0.01 + index / 60, (start[0] + (end[0] - start[0]) * p, start[1] + (end[1] - start[1]) * p), True, False)
        self.assertEqual(scene.target, "b")
        events += scene.step(moment + 0.6, end, False, False)
        self.assertEqual(events, [("merge", "a", "b")])
        scene.absorb("a", "b", "group:ab", moment + 0.6)
        scene.set_model(panel(bubble("group:ab", (("C", True), ("C", True)))), moment + 0.6)
        self.assertEqual(list(scene.bodies), ["group:ab"])
        self.assertEqual([g.kind for g in scene.ghosts], ["absorb"])
        later = self.settle(scene, moment + 0.6, 60)
        self.assertAlmostEqual(scene.bodies["group:ab"].radius.value, bubble("g", (1, 2)).radius, delta=0.5)
        self.assertFalse(scene.ghosts)
        self.assertGreater(later, moment)

    def test_click_without_moving_or_drop_on_empty_space_does_not_merge(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a"), bubble("b")), 0.0)
        moment = self.settle(scene)
        a = scene.bodies["a"]
        events = scene.step(moment + 0.02, (a.x, a.y), True, False) + scene.step(moment + 0.04, (a.x, a.y), False, False)
        self.assertEqual(events, [])
        home = (a.x, a.y)
        scene.step(moment + 0.06, home, True, False)
        events = []
        for index in range(1, 20):
            events += scene.step(moment + 0.06 + index / 60, (home[0], home[1] + 40), True, False)
        events += scene.step(moment + 0.5, (home[0], home[1] + 40), False, False)
        self.assertEqual(events, [])
        self.settle(scene, moment + 0.5, 120)
        self.assertAlmostEqual(scene.bodies["a"].y, home[1], delta=1.0)

    def test_right_click_splits_only_grouped_bubbles_and_members_burst_out(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("solo"), bubble("g", (("C", True), ("G", True)))), 0.0)
        moment = self.settle(scene)
        solo, group = scene.bodies["solo"], scene.bodies["g"]
        self.assertEqual(scene.step(moment + 0.02, (solo.x, solo.y), False, True), [("settings",)])
        scene.step(moment + 0.04, None, False, False)
        self.assertEqual(scene.step(moment + 0.06, (group.x, group.y), False, True), [("split", "g")])
        origin = (group.x, group.y)
        scene.burst("g", ["m1", "m2"], moment + 0.06)
        scene.set_model(panel(bubble("solo"), bubble("m1"), bubble("m2")), moment + 0.06)
        self.assertLess(abs(scene.bodies["m1"].x - origin[0]), 10)
        self.assertEqual([g.kind for g in scene.ghosts], ["pop"])

    def test_client_regrouping_keeps_the_same_bubble(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(agent_panel.BubbleModel("main", "任务", "运行测试", "C", keys=("main",))), 0.0)
        body = scene.bodies["main"]
        self.settle(scene)
        grouped = agent_panel.BubbleModel("auto:main", "任务", "2 路并行", "C", members=(("C", True),) * 2, keys=("main", "child"))
        scene.set_model(panel(grouped), 2.0)
        self.assertIs(scene.bodies["auto:main"], body)
        self.assertEqual(scene.ghosts, [])

    def test_frames_render_with_effects(self):
        scene = agent_panel.Scene()
        busy = agent_panel.BubbleModel("g", "长长的任务名称需要截断显示", "2 路并行", "C", True, "细节", (("C", True), ("G", False)), True)
        scene.set_model(panel(bubble("a"), busy), 0.0)
        moment = self.settle(scene, frames=10)
        scene.ripples.append(agent_panel.Ripple(100, 150, 30, moment, "C"))
        image = scene.render(1.25)
        self.assertEqual(image.width, round(agent_panel.WIDTH * 1.25) + 2 * round(agent_panel.MARGIN * 1.25))
        self.assertGreater(image.getbbox()[3], round(agent_panel.FIELD_TOP * 1.25))
        scene.set_model(panel(), moment)
        self.settle(scene, moment, 30)
        self.assertIsNotNone(scene.render(1.0))

    def test_header_figures_count_up_and_follow_new_readings(self):
        scene = agent_panel.Scene()
        model = agent_panel.PanelModel("codex", "C", "Codex", "空闲", (("5 小时", 80.0, "1 小时后重置"), ("每周", None, "")),
                                       30.0, (), "idle")
        scene.set_model(model, 0.0)
        self.assertEqual(scene.meters[0].value, 0.0)
        self.assertTrue(scene.animating)
        moment = self.settle(scene, frames=120)
        self.assertAlmostEqual(scene.meters[0].value, 80.0, delta=0.5)
        self.assertAlmostEqual(scene.rate.value, 30.0, delta=0.5)
        self.assertEqual(scene.meters[1].target, 0.0)
        # Values are not part of the cached background; only labels and reset texts are.
        self.assertNotIn("80.0", repr(model.header))
        scene.set_model(agent_panel.PanelModel("codex", "C", "Codex", "空闲", (("5 小时", 60.0, ""), ("每周", None, "")),
                                               30.0, (), "idle"), moment)
        self.settle(scene, moment, 120)
        self.assertAlmostEqual(scene.meters[0].value, 60.0, delta=0.5)
        self.assertIsNotNone(scene.render(1.0))


class SteadyBubbleTests(unittest.TestCase):
    def run_frames(self, scene, start, frames=60):
        moment = start
        for _ in range(frames):
            moment += 1 / 60
            scene.step(moment, None, False, False)
        return moment

    def test_bubbles_keep_their_slots_when_activity_order_changes(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a"), bubble("b"), bubble("c")), 0.0)
        moment = self.run_frames(scene, 0.0, 120)
        before = {key: (round(b.x), round(b.y)) for key, b in scene.bodies.items()}
        # The client reports c, then a, then b as most recent, and actions change length.
        scene.set_model(panel(bubble("c", doing="编辑 a_very_long_file_name_for_testing.py"), bubble("a"), bubble("b", doing="思考中")), moment)
        self.run_frames(scene, moment, 120)
        self.assertEqual(scene.order[:3], ["a", "b", "c"])
        self.assertEqual({key: (round(b.x), round(b.y)) for key, b in scene.bodies.items()}, before)

    def test_burn_projection_stops_at_zero_or_its_reset(self):
        Series, Chart = agent_panel.BurnSeries, agent_panel.BurnChart
        fast = Series("5 小时", 0, ((0, 100), (1800, 80), (3600, 60)), 40.0, 5 * 3600)
        chart = Chart(0, 5 * 3600, 3600, (fast,))
        history, projection, exhausts = agent_panel.burn_points(fast, chart)
        self.assertEqual(history[-1], (3600, 60))
        self.assertAlmostEqual(exhausts, 3600 + 1.5 * 3600)
        self.assertEqual(projection[-1], (exhausts, 0.0))
        slow = Series("Gemini", 1, ((0, 100), (3600, 95)), 5.0, 2 * 3600)          # resets before it runs out
        _, projection, exhausts = agent_panel.burn_points(slow, Chart(0, 5 * 3600, 3600, (slow,)))
        self.assertIsNone(exhausts)
        self.assertEqual(projection[-1][0], 2 * 3600)
        distant = Series("Codex", 0, ((0, 100), (600, 80)), 80.0, 5 * 3600)
        closeup = Chart(0, 1200, 600, (distant,))
        _, projection, exhausts = agent_panel.burn_points(distant, closeup)
        self.assertEqual(projection[-1][0], 1200)   # line stays within the view
        self.assertGreater(exhausts, closeup.end)   # text can still name the later estimate
        self.assertEqual(agent_panel.burn_future(600), 60)  # 10 min history, 1 min dashed future
        for chart in (chart, Chart(0, 5 * 3600, 3600, (fast, slow)), None, Chart(0, 10, 5, (Series("x", 0, ((5, 50),)),))):
            for scale in (1.0, 1.5):
                colors = tuple(agent_panel.accent("G") for _ in (chart.series if chart else ()))
                sprite = agent_panel.burn_sprite(chart, colors, scale)
                self.assertEqual(sprite.size, (round(agent_panel.CHART_WIDTH * scale), round(agent_panel.CHART_H * scale)))

    def test_quota_axis_adapts_to_the_readings_and_always_shows_zero(self):
        scale = agent_panel.burn_scale([[90, 85, 80]])
        self.assertEqual(scale.top, 90)
        self.assertEqual((scale.position(90), scale.position(0)), (0, 1))
        self.assertEqual((scale.position(101), scale.position(-1)), (0, 1))
        values = [scale.position(value) for value in (90, 85, 80, 50, 0)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(scale.position(80), .3)                     # 10 points take real height, not 11%
        self.assertLess(scale.position(80), agent_panel.BURN_BAND + .01)  # ...and leave room down to 0%
        self.assertGreater(scale.position(85) - scale.position(90), .25 * scale.position(80))  # not bent too hard
        ticks = agent_panel.burn_ticks(scale, [90, 85, 80])
        self.assertEqual((ticks[0], ticks[-1]), (90, 0))
        gaps = [scale.position(b) - scale.position(a) for a, b in zip(ticks, ticks[1:])]
        self.assertTrue(all(gap >= agent_panel.BURN_TICK_GAP - 1e-9 for gap in gaps))
        # A flat reading is not magnified; the top is the next round level.
        flat = agent_panel.burn_scale([[94.45, 94.45]])
        self.assertEqual((flat.top, flat.width), (95, 95))
        self.assertEqual(agent_panel.burn_scale([]), agent_panel.BurnScale())
        # A full and a busy window share the plot; neither is squeezed flat.
        both = agent_panel.burn_scale([[100, 99.5, 99], [90, 80]])
        self.assertGreater(min(both.position(99), both.position(80) - both.position(90)), .12)
        curve = agent_panel.burn_projection_curve([(0, 100), (100, 0)])
        self.assertEqual((curve[0], curve[-1]), ((0, 100), (100, 0)))
        self.assertEqual(curve[16], (50, 50))
        self.assertGreater(both.position(curve[16][1]), .5)      # projection follows the curve, not an endpoint chord

    def test_legend_lists_the_fuller_quota_first(self):
        series = (
            agent_panel.BurnSeries("low", 0, ((0, 90.0), (10, 20.0))),
            agent_panel.BurnSeries("high", 1, ((0, 40.0), (10, 80.0))),
            agent_panel.BurnSeries("mid", 2, ((0, 70.0), (10, 50.0))),
            agent_panel.BurnSeries("also high", 3, ((0, 80.0),)),
        )
        chart = agent_panel.BurnChart(0, 20, 10, series)
        self.assertEqual([text for *_, text in agent_panel.burn_legend(chart)], ["high", "also high", "mid", "low"])

    def test_global_chart_legends_wrap_and_keep_the_plot_and_footer_clear(self):
        labels = ('Codex · 5 小时', 'Codex · 每周', 'Claude Code · 5 小时', 'Claude Code · 每周',
                  'Gemini · Argon', 'Gemini · Claude/GPT', 'Xiaomi MiMo · 套餐额度', 'Grok · 每周')
        series = tuple(agent_panel.BurnSeries(label, 0, ((0, 100), (3600, 70)), 30, 7200)
                       for label in labels)
        chart = agent_panel.BurnChart(0, 7200, 3600, series)
        extra = agent_panel.chart_extra(chart)
        self.assertGreater(extra, 0)
        from PIL import Image, ImageDraw
        draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        for _, x, _, text in agent_panel.burn_legend(chart):
            self.assertLessEqual(x + 8 + draw.textlength(text, font=agent_panel.ui_font(10)), agent_panel.CHART_WIDTH - 10)
        model = agent_panel.PanelModel('grok', 'X', 'Grok', '空闲', (), None, (), burn=chart)
        self.assertEqual(agent_panel._header_model(model.header).chart_padding, extra)
        for scale in (1.0, 1.25, 1.5):
            sprite = agent_panel.burn_sprite(chart, tuple(agent_panel.accent('C') for _ in series), scale)
            self.assertEqual(sprite.height, round((agent_panel.CHART_H + extra) * scale))
            scene = agent_panel.Scene()
            scene.set_model(model, 0)
            self.assertIsNotNone(scene.render(scale))

    def test_lines_use_the_colour_of_their_header_bar(self):
        import colorsys
        shade = agent_panel.quota_shade

        def hls(color):
            return colorsys.rgb_to_hls(*(v / 255 for v in color[:3]))

        def apart(a, b):
            return sum(abs(x - y) for x, y in zip(a[:3], b[:3]))

        # One hue per source; its windows run light -> deep from the one that
        # runs out first to the one that lasts longest.
        orders = {"A": (0, 1), "C": (0, 1), "G": (1, 0, 2)}     # Gemini: Claude/GPT, Argon, Gemini
        hues = {"A": (10, 40), "C": (100, 170), "G": (215, 240)}
        for letter, order in orders.items():
            colors = [shade(letter, slot, 60.0, True) for slot in order]
            light = [hls(color)[1] for color in colors]
            self.assertEqual(light, sorted(light, reverse=True), letter)
            for color in colors:
                self.assertTrue(hues[letter][0] <= hls(color)[0] * 360 <= hues[letter][1], (letter, color))
            for a, b in zip(colors, colors[1:]):
                if letter == "A":
                    self.assertGreater(apart(a, b), 100, (letter, a, b))
                else:   # Codex and Gemini: near shades, each step only slightly darker
                    self.assertTrue(25 <= apart(a, b) <= 100, (letter, a, b))
        # Signature colours stay: Claude clay for the week, GPT mint for 5 h,
        # Gemini periwinkle for its first-to-run-out pool.
        self.assertEqual(shade("A", 1, 60.0, True), agent_panel.accent("A"))
        self.assertEqual(shade("C", 0, 60.0, True), agent_panel.accent("C"))
        self.assertEqual(shade("G", 1, 40.0, True), agent_panel.accent("G"))
        # A window keeps its colour when low; only its number turns amber or rose.
        self.assertEqual(shade("C", 1, 11.0, True), shade("C", 1, 90.0, True))
        self.assertEqual(shade("X", 0, 11.0, False), agent_panel.accent("X"))
        self.assertEqual(shade("C", 0, None, True), icon_art.MUTED)
        model = agent_panel.PanelModel("gemini", "G", "Gemini", "2 个在工作", (("Argon", 21.0), ("Claude/GPT", 50.0), ("Gemini", 79.0)),
                                       0.0, (), "live")
        self.assertTrue(agent_panel.pooled(model))
        self.assertTrue(agent_panel.pooled(panel()))                     # Codex 5 h + weekly: one shade each, like Gemini

    def test_split_ring_matches_its_panel_bars_at_any_level(self):
        with patch.object(icon_art, "_finish", lambda canvas: canvas):   # full-size canvas, exact colours
            big = icon_art.ring_icon.__wrapped__((15.0, 30.0), "A")
        pixels = {color[:3] for _, color in big.getcolors(1 << 20) if color[3] == 255}
        for slot in range(2):
            self.assertIn(icon_art.WINDOW_SHADES["A"][slot][:3], pixels)
        self.assertNotIn(icon_art.AMBER[:3], pixels)
        self.assertNotIn(icon_art.ROSE[:3], pixels)

    def test_line_end_has_no_source_glyph_or_round_forecast_dot(self):
        color = agent_panel.accent("C")
        points = ((0.0, 60.0), (90.0, 50.0))
        a = agent_panel.BurnSeries("Codex", 0, points, 60.0, 1000.0, "A")
        c = agent_panel.BurnSeries("Codex", 0, points, 60.0, 1000.0, "C")
        first = agent_panel.burn_sprite(agent_panel.BurnChart(0, 110, 90, (a,)), (color,), 2.0)
        second = agent_panel.burn_sprite(agent_panel.BurnChart(0, 110, 90, (c,)), (color,), 2.0)
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_chart_draws_in_on_opening_then_rests(self):
        chart = agent_panel.BurnChart(0, 5 * 3600, 3600, (agent_panel.BurnSeries("5 小时", 0, ((0, 100), (3600, 60)), 40.0, 5 * 3600),))
        model = agent_panel.PanelModel("codex", "C", "Codex", "空闲", (("5 小时", 60.0),), 0.0,
                                       (agent_panel.BubbleModel("a", "任务", "完成", "C", False),), "idle", burn=chart)
        scene = agent_panel.Scene()
        scene.set_model(model, 0.0)
        moment = self.run_frames(scene, 0.0, 10)
        self.assertLess(scene.chart_progress, 1)
        self.assertTrue(scene.animating)
        self.assertIsNotNone(scene.render(1.25))
        # A 60 min chart zooms at once to 2 h, rests, then eases out to 5 h (~4.95 s).
        self.run_frames(scene, moment, 360)
        self.assertEqual(scene.chart_progress, 1)
        self.assertFalse(scene.animating)

    def test_footprint_does_not_depend_on_caption(self):
        self.assertEqual(bubble("a", doing="思考中").half_width, bubble("a", doing="编辑 a_very_long_file_name.py").half_width)

    def test_follower_glides_without_overshoot_and_lands_exactly(self):
        for dt in (1 / 240, 1 / 60, 1 / 20):
            follow = agent_panel.Follow(0.0)
            follow.target = 100.0
            previous = 0.0
            for _ in range(int(2 / dt)):
                value = follow.step(dt)
                self.assertGreaterEqual(value, previous - 1e-9)       # monotonic
                self.assertLessEqual(value, 100.0)                    # never past the target
                previous = value
            self.assertTrue(follow.settled)
            self.assertEqual(follow.value, 100.0)

    def test_panel_comes_to_rest_exactly_on_its_slots(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a"), bubble("b")), 0.0)
        moment = self.run_frames(scene, 0.0, 30)
        scene.set_model(panel(bubble("a"), bubble("b"), bubble("c"), bubble("d")), moment)
        self.assertTrue(scene.animating)
        moment = self.run_frames(scene, moment, 90)
        self.assertFalse(scene.animating)
        for key, body in scene.bodies.items():
            self.assertEqual((body.x, body.y), scene.homes[key])
        idle = agent_panel.PanelModel("codex", "C", "GPT / Codex", "空闲", (("5 小时", 70.0),), 0.0,
                                      tuple(agent_panel.BubbleModel(k, f"任务 {k}", "完成", "C", False) for k in "abcd"), "idle")
        scene.set_model(idle, moment)
        moment = self.run_frames(scene, moment, 100)
        scene.set_model(idle, moment)                   # the next refresh, after LIVE_HOLD
        moment = self.run_frames(scene, moment, 60)
        self.assertFalse(scene.animating or scene.ambient)
        frame = scene.render(1.0)
        self.run_frames(scene, moment, 30)
        self.assertEqual(scene.render(1.0).tobytes(), frame.tobytes())   # idle bubbles are pixel-still

    def test_arriving_and_leaving_bubbles_never_overlap_their_neighbours(self):
        scene = agent_panel.Scene()
        first = [bubble(str(index)) for index in range(3)]
        scene.set_model(panel(*first), 0.0)
        moment = self.run_frames(scene, 0.0, 60)
        for bubbles in (first + [bubble("n1"), bubble("g", (("C", True),) * 3), bubble("n2")], first[1:] + [bubble("n2")], first):
            scene.set_model(panel(*bubbles), moment)
            for _ in range(60):
                moment = self.run_frames(scene, moment, 1)
                bodies = list(scene.bodies.values())
                for index, a in enumerate(bodies):
                    for b in bodies[index + 1:]:
                        gap = math.hypot(a.x - b.x, a.y - b.y) - a.size - b.size
                        self.assertGreater(gap, -1.0, f"{a.model.key} overlaps {b.model.key}")

    def test_sliced_background_matches_a_full_render(self):
        from PIL import ImageChops
        model = panel(bubble("a"))
        for scale in (1.0, 1.5, 2.0):
            for field in (96, 140, 220):
                sliced = agent_panel.background(model.header, round(field * scale), scale)
                full = agent_panel._background(model.header, field, scale)
                self.assertEqual(sliced.size, full.size)
                top = round((agent_panel.MARGIN + agent_panel.field_top(model) + agent_panel.SLICE_INSET) * scale)
                self.assertIsNone(ImageChops.difference(sliced.crop((0, 0, full.width, top)),
                                                        full.crop((0, 0, full.width, top))).getbbox())

    def test_brief_live_flicker_is_ignored(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a")), 0.0)
        moment = self.run_frames(scene, 0.0, 60)
        idle = agent_panel.BubbleModel("a", "任务 a", "刚完成", "C", False)
        scene.set_model(panel(idle), moment)
        self.assertTrue(scene.bodies["a"].model.live)            # held while it may be a gap between tool calls
        scene.set_model(panel(bubble("a")), moment + 0.5)
        scene.set_model(panel(idle), moment + 0.6)
        scene.set_model(panel(idle), moment + 1.0)
        self.assertTrue(scene.bodies["a"].model.live)
        scene.set_model(panel(idle), moment + 2.2)
        self.assertFalse(scene.bodies["a"].model.live)



class HerdrAndHelperTests(unittest.TestCase):
    def test_grok_opens_herdr_and_starts_in_a_new_workspace(self):
        import herdr_source
        from types import SimpleNamespace

        def done(code, out="", err=""):
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        calls, opened = [], []

        def run(args, **_kwargs):
            request = args[1:]
            calls.append(request)
            if request[:2] == ["workspace", "list"]:
                return done(0, '{"result":{"workspaces":[]}}')
            if request[:2] == ["workspace", "create"]:
                return done(0, json.dumps({"result": {"root_pane": {"pane_id": "w1:p1"}}}))
            if request[:2] == ["agent", "list"]:
                return done(0, json.dumps({"result": {"agents": [{"name": "grok"}]}}))
            if request[:2] == ["agent", "start"] and request[2] == "grok-2":   # taken by another client meanwhile
                return done(1, err='{"error":{"code":"agent_name_taken","message":"taken"}}')
            return done(0, '{"result":{"type":"agent_started"}}')

        with patch.dict(os.environ, {"CODEX_QUOTA_HERDR_SESSION": ""}), \
                patch.object(herdr_source.shutil, "which", return_value=r"C:\WindowsApps\wt.exe"):
            name = herdr_source.start_agent("grok", exe="herdr.exe", runner=run, popen=lambda *a, **k: opened.append(a[0]),
                                            window_open=lambda: False, sleep=lambda _s: None, cwd=r"C:\home")
        self.assertEqual(name, "grok-3")
        self.assertEqual(opened, [[r"C:\WindowsApps\wt.exe", "-w", "new", "-d", str(Path.home()), "--title", "herdr", "herdr.exe"]])
        self.assertIn(["workspace", "create", "--label", "grok", "--cwd", r"C:\home", "--focus"], calls)
        self.assertFalse(any(request[:2] == ["tab", "create"] for request in calls))   # a workspace, never a tab
        starts = [request for request in calls if request[:2] == ["agent", "start"]]
        self.assertEqual(starts[-1], ["agent", "start", "grok-3", "--kind", "grok", "--pane", "w1:p1"])
        # An open Herdr window is reused, and any other failure is reported, not retried.
        opened.clear()

        def broken(args, **_kwargs):
            request = args[1:]
            if request[:2] == ["workspace", "create"]:
                return done(0, json.dumps({"result": {"root_pane": {"pane_id": "w1:p9"}}}))
            if request[:2] == ["agent", "start"]:
                return done(1, err='{"error":{"code":"agent_not_ready","message":"grok is not ready"}}')
            return done(0, '{"result":{"agents":[]}}')

        with self.assertRaisesRegex(OSError, "grok is not ready"):
            herdr_source.start_agent("grok", exe="herdr.exe", runner=broken, popen=lambda *a, **k: opened.append(a[0]),
                                     window_open=lambda: True, sleep=lambda _s: None)
        self.assertEqual(opened, [])

        # Launched but not seen ready in time: the workspace is up, so no error box.
        def slow(args, **_kwargs):
            request = args[1:]
            if request[:2] == ["workspace", "create"]:
                return done(0, json.dumps({"result": {"root_pane": {"pane_id": "w1:p9"}}}))
            if request[:2] == ["agent", "start"]:
                return done(1, err='{"error":{"code":"agent_start_failed","message":"timed out waiting for agent startup"}}')
            return done(0, '{"result":{"agents":[]}}')

        with self.assertRaises(herdr_source.AgentSlow):
            herdr_source.start_agent("grok", exe="herdr.exe", runner=slow, popen=lambda *a, **k: None,
                                     window_open=lambda: True, sleep=lambda _s: None)
        import tray_app
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        with patch.object(herdr_source, "start_agent", side_effect=herdr_source.AgentSlow("slow")),                 patch.object(tray_app.ctypes.windll.user32, "MessageBoxW") as box:
            tray.open_in_herdr("grok")
        box.assert_not_called()

    def test_herdr_window_is_the_client_without_a_command(self):
        import herdr_source
        from types import SimpleNamespace
        row = lambda *args: SimpleNamespace(info={"name": "herdr.exe", "cmdline": [r"C:\h\herdr.exe", *args]})
        with patch.dict(os.environ, {"CODEX_QUOTA_HERDR_SESSION": ""}):
            self.assertTrue(herdr_source.client_open([row("server"), row()]))
            self.assertFalse(herdr_source.client_open([row("server"), row("agent", "list")]))
            self.assertFalse(herdr_source.client_open([SimpleNamespace(info={"name": "python.exe", "cmdline": []})]))

    def test_herdr_window_is_the_terminal_hosting_the_client(self):
        import herdr_source
        from types import SimpleNamespace

        def proc(pid, args, parents):
            return SimpleNamespace(pid=pid, info={"name": "herdr.exe", "cmdline": [r"C:\h\herdr.exe", *args]},
                                   parents=lambda: [SimpleNamespace(pid=p) for p in parents])
        processes = [proc(5, ["server"], [9]), proc(3, [], [2, 1])]      # client in shell 2, terminal 1
        windows = [(70, 9, True, "server host"), (80, 1, False, ""), (81, 1, True, "BL4CK0UT: herdr"), (90, 4, True, "other")]
        with patch.dict(os.environ, {"CODEX_QUOTA_HERDR_SESSION": ""}):
            self.assertEqual(herdr_source.client_window(processes, windows), 81)
            self.assertIsNone(herdr_source.client_window(processes[:1], windows))     # no client open

    def test_claude_and_grok_clicks_open_herdr_once_until_started(self):
        import herdr_source
        import tray_app
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        threads = []
        with patch.object(herdr_source, "find_exe", return_value="herdr.exe"), \
                patch.object(herdr_source, "raise_client") as raise_client, \
                patch.object(tray_app.threading, "Thread") as thread:
            thread.side_effect = lambda **kwargs: threads.append(kwargs) or Mock()
            for provider in ("claude", "claude", "grok"):
                tray.open_provider(provider)
        self.assertEqual([t["args"] for t in threads], [("claude",), ("grok",)])     # a second click waits
        self.assertEqual(raise_client.call_count, 2)
        with patch.object(herdr_source, "start_agent", return_value="claude"):
            tray.open_in_herdr("claude")
        self.assertNotIn("claude", tray.herdr_opening)                                # finished: clicks work again

    @staticmethod
    def agent(key, **extra):
        base = {"key": key, "provider": "codex", "source": "Codex", "model": "", "title": "", "thread_title": "",
                "request": "", "doing": "生成中", "live": True, "at": 100.0, "parent": "", "role": "", "folder": "",
                "tps": None, "schedule": "", "origin": ""}
        base.update(extra)
        return base

    def groups(self):
        import tempfile
        from pathlib import Path
        folder = tempfile.mkdtemp()
        return agent_tasks.AgentGroups(Path(folder) / "groups.json")

    def test_untitled_side_threads_join_the_main_session(self):
        main = self.agent("main", title="修复气泡", folder="repo", at=90.0)
        side = self.agent("side", at=99.0)                       # title generation / log-only stream
        review = self.agent("review", title="审查", folder="repo", origin='{"subagent": "review"}')
        clusters = self.groups().compose([main, side, review])
        self.assertEqual(1, len(clusters))
        self.assertEqual({"main", "side", "review"}, {m["key"] for m in clusters[0].members})

    def test_separate_sessions_stay_separate(self):
        first = self.agent("a", title="任务一", folder="repo")
        second = self.agent("b", title="任务二", folder="repo")
        other = self.agent("c", provider="claude")               # anonymous, but no Claude host
        self.assertEqual(3, len(self.groups().compose([first, second, other])))

    def test_same_herdr_pane_is_one_bubble(self):
        import herdr_source
        listing = json.dumps({"id": "1", "result": {"type": "agent_list", "agents": [
            {"pane_id": "w1:p1", "terminal_id": "t1", "workspace_id": "w1", "tab_id": "w1:t1", "focused": True, "revision": 1,
             "agent": "codex", "agent_status": "working", "cwd": "/x/repo",
             "agent_session": {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "aaaaaaaa-0000-0000-0000-000000000001"}},
            {"pane_id": "w1:p2", "terminal_id": "t2", "workspace_id": "w1", "tab_id": "w1:t1", "focused": False, "revision": 1,
             "agent": "claude", "agent_status": "blocked", "cwd": "/x/site", "title": "部署网站"},
            {"pane_id": "w1:p3", "terminal_id": "t3", "workspace_id": "w1", "tab_id": "w1:t1", "focused": False, "revision": 1,
             "agent": "grok", "agent_status": "idle"},
        ]}})
        calls = []

        def runner(command, **_):
            calls.append(command)
            return type("Result", (), {"returncode": 0, "stdout": listing})()

        herdr = herdr_source.Herdr(exe="herdr", runner=runner, enabled=True)
        native = [self.agent("aaaaaaaa-0000-0000-0000-000000000001", title="主任务", folder="repo"),
                  self.agent("bbbbbbbb-0000-0000-0000-000000000002", title="另一个线程", folder="repo")]
        native[1]["parent"] = ""
        agents = herdr.attach(native, 100.0)
        self.assertEqual(["herdr", "agent", "list"], calls[0])
        self.assertEqual("w1:p1", agents[0]["pane"] if agents[0]["key"].startswith("aaaa") else
                         next(a for a in agents if a["key"].startswith("aaaa"))["pane"])
        blocked = next(a for a in agents if a["key"] == "herdr:w1:p2")
        self.assertEqual(("claude", "等你确认", "部署网站", "site"), (blocked["provider"], blocked["doing"], blocked["title"], blocked["folder"]))
        self.assertFalse(any(a["key"] == "herdr:w1:p3" for a in agents))      # idle pane without logs: no bubble
        # the second native thread joins via a shared pane once herdr links it
        for agent in agents:
            if agent["key"].startswith("bbbb"):
                agent["pane"] = "w1:p1"
        clusters = self.groups().compose(agents)
        pane = next(c for c in clusters if c.key == "pane:w1:p1")
        self.assertEqual(2, len(pane.members))
        herdr.attach(native, 101.0)
        self.assertEqual(1, len(calls))                                      # rate limited

    def test_discovered_herdr_is_called_by_its_on_disk_name(self):
        # shutil.which spells the PATHEXT extension in upper case ("herdr.EXE");
        # Herdr's managed launcher only runs when invoked as bin/herdr.exe.
        import herdr_source
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "bin" / "herdr.exe"
            real.parent.mkdir()
            real.write_bytes(b"")
            calls = []

            def runner(command, **_):
                calls.append(command[0])
                ok = Path(command[0]).name == "herdr.exe"
                return type("R", (), {"returncode": 0 if ok else 1, "stdout": '{"result": {"agents": []}}' if ok else ""})()
            with patch.object(herdr_source.shutil, "which", return_value=str(real.with_name("herdr.EXE"))), \
                    patch.object(herdr_source.os, "name", "nt"):
                herdr = herdr_source.Herdr(runner=runner, enabled=True)
                herdr.exe = None
                herdr.poll(1000.0)
            self.assertEqual(calls, [str(real)])
            self.assertNotEqual(herdr.status, "服务未运行")
            explicit = herdr_source.Herdr(exe=str(real.with_name("herdr.EXE")), runner=runner, enabled=True)
            self.assertEqual(explicit._locate(1000.0), str(real.with_name("herdr.EXE")))   # an override is used as given

    def test_herdr_without_server_changes_nothing(self):
        import herdr_source
        herdr = herdr_source.Herdr(exe="herdr", runner=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(), enabled=True)
        native = [self.agent("x", title="任务")]
        self.assertEqual(native, herdr.attach(native, 10.0))
        self.assertEqual("服务未运行", herdr.status)
        self.assertEqual([], herdr_source.Herdr(enabled=False).poll(10.0))

    def test_idle_or_stopped_herdr_is_polled_less_often(self):
        import herdr_source
        results = {"code": 1, "out": ""}
        calls = []

        def runner(command, **_):
            calls.append(command)
            return type("R", (), {"returncode": results["code"], "stdout": results["out"]})()
        herdr = herdr_source.Herdr(exe="herdr", runner=runner, enabled=True)
        herdr.poll(100.0)
        herdr.poll(100.0 + herdr_source.POLL_SECONDS + 0.1)      # server not running: no new process yet
        self.assertEqual(1, len(calls))
        herdr.poll(100.0 + herdr_source.IDLE_POLL_SECONDS + 0.1)
        self.assertEqual(2, len(calls))
        results.update(code=0, out='{"result": {"agents": []}}')
        herdr.poll(200.0)
        self.assertEqual(herdr_source.QUIET_POLL_SECONDS, herdr.interval)

    def test_unmatched_pane_claims_single_same_folder_session(self):
        import herdr_source
        listing = json.dumps({"result": {"agents": [{"pane_id": "p9", "agent": "codex", "agent_status": "working", "foreground_cwd": "C:\\\\work\\\\repo"}]}})
        herdr = herdr_source.Herdr(exe="herdr", runner=lambda *a, **k: type("R", (), {"returncode": 0, "stdout": listing})(), enabled=True)
        agents = herdr.attach([self.agent("k", title="任务", folder="repo")], 5.0)
        self.assertEqual(["p9"], [a.get("pane") for a in agents])

    def test_agy_pane_claims_the_live_folderless_conversation(self):
        import herdr_source
        listing = json.dumps({"result": {"agents": [{"pane_id": "p2", "agent": "agy", "agent_status": "working", "cwd": "/w/app"}]}})
        herdr = herdr_source.Herdr(exe="herdr", runner=lambda *a, **k: type("R", (), {"returncode": 0, "stdout": listing})(), enabled=True)
        conversation = self.agent("conv", provider="gemini", source="Antigravity CLI", title="重构", tps=55.0)
        agents = herdr.attach([conversation], 5.0)
        self.assertEqual([("conv", "p2", 55.0)], [(a["key"], a.get("pane"), a["tps"]) for a in agents])
        self.assertEqual(0, herdr.working("gemini", {"conv"}))
        self.assertEqual(1, herdr.working("gemini", set()))


class GrokIqColumnTests(unittest.TestCase):
    CSV = ("model,long_agentic,reasoning,coding,browser,data_analysis,research,design\n"
           "grok-4.6,28.0,91.2,89.1,49.4,75.3,84.5,79.8\n"
           "grok-4.7,46.1,82.7,84.5,49.3,76.9,86.4,78.0\n"
           "claude-fable-5.1,80.0,90.0,88.0,70.0,85.0,90.0,86.0\n")
    CATEGORIES = {"Long Agentic": ["long_agentic"], "Reasoning": ["reasoning"], "Coding": ["coding"],
                  "Browser": ["browser"], "Data Analysis": ["data_analysis"], "Research": ["research"],
                  "Design": ["design"], "_overall_weights": {"Long Agentic": 2.0, "Coding": 1.5, "Design": 1.5}}
    HTML = '<script src="./static/js/main.abc123.js"></script>'
    SCRIPT = 'before Yt=["20261014","20261025"] after'

    def fetch(self, pages):
        def get(url):
            self.assertIn(url, pages)
            return pages[url]
        return get

    def test_livebench_overall_for_grok_4_7_is_the_published_70_point_1(self):
        import grok_iq
        row = grok_iq.parse_row(self.CSV)
        self.assertEqual(grok_iq.score_text(grok_iq.overall(row, self.CATEGORIES)), "70.1")
        self.assertIsNone(grok_iq.parse_row(self.CSV, "grok-4"))
        self.assertEqual(grok_iq.latest_release(self.SCRIPT), "20261025")
        self.assertIsNone(grok_iq.latest_release("Yt=[]"))
        pages = {"https://livebench.ai/": self.HTML,
                 "https://livebench.ai/static/js/main.abc123.js": self.SCRIPT,
                 "https://livebench.ai/data/20261025/table.csv": self.CSV,
                 "https://livebench.ai/data/20261025/categories.json": json.dumps(self.CATEGORIES)}
        state = grok_iq.read(grok_iq.GrokIq(), 1000.0, self.fetch(pages))
        best = grok_iq.overall(grok_iq.parse_row(self.CSV, "claude-fable-5.1"), self.CATEGORIES)
        self.assertEqual(grok_iq.leader(self.CSV, self.CATEGORIES), ("claude-fable-5.1", best))
        # The smartest model is the 100 % mark; the caption names both models.
        self.assertEqual(grok_iq.column(state), ("智商", f"{100 * state.score / best:.0f}", "Grok 4.7/Fable 5.1", "%"))
        self.assertEqual(grok_iq.column(None), ("智商", "—", "Grok 4.7", ""))
        self.assertEqual(state.release, "20261025")
        self.assertEqual(state.error, "")
        # A cache from before the comparison is read again to learn the leader.
        stale = grok_iq.GrokIq(score=state.score, release=state.release, script=state.script, attempted_at=1490.0)
        self.assertTrue(grok_iq.due(stale, 1500.0))
        self.assertFalse(grok_iq.due(state, 1500.0))
        self.assertEqual(grok_iq.read(stale, 1500.0, self.fetch(pages)).top_model, "claude-fable-5.1")
        # The same bundle is not downloaded again; a failed refresh keeps 70.1.
        state = grok_iq.read(state, 2000.0, self.fetch({"https://livebench.ai/": self.HTML}))
        self.assertEqual(round(state.score, 1), 70.1)

        def down(_url):
            raise OSError("down")

        broken = grok_iq.read(state, 3000.0, down)
        self.assertEqual(grok_iq.score_text(broken.score), "70.1")
        self.assertIn("OSError", broken.error)

    def test_grok_spare_column_shows_the_score_and_leaves_tps_on_the_right(self):
        from PIL import Image, ImageChops, ImageDraw
        grok = agent_panel.PanelModel("grok", "X", "Grok", "空闲", (("每周", 75, ""),), 12, (),
                                      column_iq=("智商", "82", "Grok 4.7/Fable 5.1", "%"))
        self.assertEqual(grok.header[-2], ("智商", "82", "Grok 4.7/Fable 5.1", "%"))
        self.assertTrue(grok.header[-1])
        codex = agent_panel.PanelModel("codex", "C", "Codex", "空闲", (("5小时", 40, ""), ("每周", 80, "")), 3, ())
        gemini = agent_panel.PanelModel("gemini", "G", "Gemini", "空闲",
                                        (("Argon", 30), ("Claude/GPT", 40), ("Gemini", 50)), None, ())
        mimo = agent_panel.PanelModel("mimo", "M", "MiMo", "空闲", (("套餐额度", 90, ""),), 4, (), today_used=10)
        self.assertEqual(codex.header[-2], ())
        self.assertTrue(codex.header[-1])
        self.assertEqual(gemini.header[-2], ())
        self.assertFalse(gemini.header[-1])
        self.assertEqual(mimo.header[-2], ())
        spare = agent_panel.spare_slot(1, True)
        columns = agent_panel.meter_columns(1)
        self.assertIsNotNone(spare)
        self.assertLess(columns[0][0], spare[0])
        self.assertLess(spare[0], columns[-1][0])
        self.assertIsNone(agent_panel.spare_slot(2, True))
        self.assertEqual(columns[-1], agent_panel.meter_columns(2)[-1])
        from dataclasses import replace
        shown = agent_panel.background(grok.header, 120, 1)
        plain = agent_panel.background(replace(grok, column_iq=()).header, 120, 1)
        self.assertEqual(shown.size, plain.size)
        span = (agent_panel.WIDTH - 2 * agent_panel.PAD) / 3
        left = round(agent_panel.MARGIN + agent_panel.PAD + span)
        right = round(agent_panel.MARGIN + agent_panel.PAD + 2 * span)
        self.assertIsNone(ImageChops.difference(shown.crop((0, 0, left - 2, shown.height)),
                                                plain.crop((0, 0, left - 2, plain.height))).getbbox())
        self.assertIsNone(ImageChops.difference(shown.crop((right + 2, 0, shown.width, shown.height)),
                                                plain.crop((right + 2, 0, plain.width, plain.height))).getbbox())
        self.assertIsNotNone(ImageChops.difference(shown.crop((left, 0, right, shown.height)),
                                                   plain.crop((left, 0, right, plain.height))).getbbox())
        number, _ = agent_panel.figure("82", "%", agent_panel.TEXT, agent_panel.TEXT, 1)
        self.assertLess(number.width, span)
        caption = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength("Grok 4.7/Fable 5.1", font=agent_panel.ui_font(10))
        self.assertLess(caption, span - 16)


if __name__ == "__main__":
    unittest.main()
