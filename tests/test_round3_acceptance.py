"""第三版预先冻结的20场景；测试输入/期望不进入产品规则。"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "round3_interaction_cases.json"
FIXTURE_SHA256 = "1ab6d23bbbf401e39b80f94582e548be6579d9a5e7a6452b09b0345c9581276a"
CASES = {case["id"]: case for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]}


class FrozenInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-round3-frozen-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.clock = ManualClock(datetime(2026, 10, 17, 8))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()

    @property
    def memory(self):
        return self.runtime.organs["memory"]

    @property
    def relation(self):
        self.runtime.organs["relation"].reconcile()
        return self.runtime.organs["relation"]

    async def restart(self):
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()

    async def fact(self, key, value):
        result = await self.runtime.memory_command(f"/memory set {key}={value}")
        self.assertTrue(result["ok"], result)

    async def check_fact_answer(self, case_id):
        case = CASES[case_id]
        if "fact_key" in case:
            await self.fact(case["fact_key"], case["fact_value"])
        if "mood" in case:
            self.runtime.organs["affect"].base_mood = case["mood"]
        if "closeness" in case:
            self.relation.restore_state({"closeness": case["closeness"], "trust": 10})
        answer = await self.runtime.ask(case["input"])
        self.assertIn(case["expect"], answer)

    async def test_I01_low_mood_identity(self):
        await self.check_fact_answer("I01")

    async def test_I02_familiar_identity(self):
        await self.check_fact_answer("I02")

    async def test_I03_low_mood_name(self):
        await self.check_fact_answer("I03")

    async def test_I04_familiar_city(self):
        await self.check_fact_answer("I04")

    async def test_I05_unknown_birthday(self):
        await self.check_fact_answer("I05")

    async def test_I06_brief_survives_restart(self):
        case = CASES["I06"]
        await self.runtime.ask(case["input"])
        await self.restart()
        self.assertEqual(self.memory.get_fact(case["key"]).fact_value, case["value"])
        self.assertEqual(self.runtime.interaction.response_style, case["value"])

    async def test_I07_style_reaches_model(self):
        case, calls = CASES["I07"], []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append((system, prompt))
                return "按用户要求解释。"

        await self.runtime.ask(case["input"])
        self.runtime.chat_service.llm, self.runtime.chat_service.api_enabled = Model(), True
        await self.runtime.ask("解释一下星图投影的思路")
        self.assertEqual(self.memory.get_fact(case["key"]).fact_value, case["value"])
        self.assertEqual(len(calls), 1)
        self.assertIn("必要解释和例子", calls[0][0])

    async def test_I08_off_all_proactive_channels(self):
        case = CASES["I08"]
        await self.runtime.ask(case["input"])
        self.assertEqual(self.memory.get_fact(case["key"]).fact_value, case["value"])
        for kind in ("inner", "narrative", "toast"):
            self.assertFalse(self.runtime.interaction.notification_data("主动消息", kind=kind)["show_proactive"])
        self.assertTrue(await self.runtime.ask("你是谁？"))

    async def test_I09_shared_low_cooldown(self):
        case = CASES["I09"]
        await self.runtime.ask(case["input"])
        self.assertEqual(self.memory.get_fact(case["key"]).fact_value, case["value"])
        self.assertFalse(self.runtime.interaction.can_notify())
        self.clock.advance(case["cooldown_seconds"])
        self.assertTrue(self.runtime.interaction.notification_data("独白", kind="inner")["show_proactive"])
        for kind in ("narrative", "toast"):
            self.assertFalse(self.runtime.interaction.notification_data("后续消息", kind=kind)["show_proactive"])
        self.clock.advance(case["cooldown_seconds"] - 1)
        self.assertFalse(self.runtime.interaction.can_notify())
        self.clock.advance(1)
        self.assertTrue(self.runtime.interaction.can_notify())

    async def test_I10_contact_period(self):
        case = CASES["I10"]
        await self.fact(case["key"], case["value"])
        self.clock.advance((case["hour"] - self.clock.now().hour) * 3600)
        self.assertEqual(self.runtime.interaction.can_notify(), case["allowed"])

    async def test_I11_queued_notification_revoked(self):
        case = CASES["I11"]
        queued = self.runtime.interaction.notification_data("排队中的旧主动消息")
        self.assertTrue(self.runtime.interaction.notification_current(queued))
        await self.runtime.ask(case["input"])
        self.assertEqual(self.runtime.interaction.notification_current(queued), case["expect_allowed"])

    async def test_I12_forget_pref_default_on_restart(self):
        case = CASES["I12"]
        await self.fact(case["key"], case["value"])
        result = await self.runtime.memory_command(f"/memory forget-key {case['key']}")
        self.assertTrue(result["ok"])
        await self.restart()
        self.assertIsNone(self.memory.get_fact(case["key"]))
        self.assertEqual(self.runtime.interaction.response_style, case["default"])

    async def test_I13_feedback_daily_cap(self):
        case = CASES["I13"]
        for _ in range(case["repeat"]):
            await self.runtime.ask(case["input"])
        self.assertEqual(len(self.relation.state()["events"]), case["expect_unique_events"])

    async def test_I14_restart_does_not_add_again(self):
        case = CASES["I14"]
        await self.runtime.ask(case["input"])
        state = self.relation.state()
        self.assertTrue(state["events"])
        await self.restart()
        self.assertEqual(self.relation.state(), state)

    async def test_I15_ordinary_chat_no_score(self):
        case = CASES["I15"]
        before = self.relation.state()
        for text in case["inputs"]:
            await self.runtime.ask(text)
        self.assertEqual(self.relation.closeness - before["closeness"], case["expect_delta"])
        self.assertEqual(self.relation.trust - before["trust"], case["expect_delta"])
        self.assertEqual(self.relation.state()["events"], [])

    async def test_I16_share_content_dedup(self):
        case = CASES["I16"]
        directory = self.runtime.share_service.share_dir
        directory.mkdir(parents=True, exist_ok=True)
        for name in case["filenames"]:
            (directory / name).write_text(case["content"], encoding="utf-8")
        self.assertEqual(await self.runtime.share_service.scan(), len(case["filenames"]))
        await self.runtime.bus.flush()
        self.assertEqual(len(self.relation.state()["events"]), case["expect_unique_events"])

    async def test_I17_forget_feedback_removes_effect(self):
        case = CASES["I17"]
        before = self.relation.state()
        await self.runtime.ask(case["input"])
        source_id = self.relation.state()["events"][0]["source_id"]
        self.assertNotEqual(self.relation.trust, before["trust"])
        result = await self.runtime.memory_command(f"/memory forget {source_id}")
        self.assertTrue(result["ok"], result)
        self.assertNotIn(source_id, json.dumps((await self.runtime.interaction_view())["relation"]))
        self.assertEqual(self.relation.trust, before["trust"])
        self.assertFalse(any(m["source"] == "interaction:" + source_id for m in self.runtime.organs["affect"].modifiers))

    async def test_I18_correct_neutral_removes_effect(self):
        case = CASES["I18"]
        before = self.relation.closeness
        await self.runtime.ask(case["input"])
        source_id = self.relation.state()["events"][0]["source_id"]
        result = await self.runtime.memory_command(f"/memory correct {source_id} {case['correction']}")
        self.assertTrue(result["ok"], result)
        self.assertEqual(next(item for item in self.memory.items if item.id == source_id).content, case["correction"])
        self.assertEqual(self.relation.closeness - before, case["expect_delta"])
        self.assertEqual(self.relation.state()["events"], [])

    async def test_I19_paused_feedback_no_source(self):
        case = CASES["I19"]
        await self.runtime.memory_command("/memory pause")
        before = self.relation.state()
        reply = await self.runtime.ask(case["input"])
        self.assertFalse(any(item.topic == "interaction.feedback" for item in self.memory.items))
        self.assertEqual(self.relation.state(), before)
        self.assertIn("没有新增关系记录", reply)

    async def test_I20_failed_save_no_gain(self):
        case = CASES["I20"]
        before = self.relation.state()
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("injected save failure")):
            reply = await self.runtime.ask(case["input"])
        self.assertIn("没能保存", reply)
        self.assertFalse(any(item.topic == "interaction.feedback" for item in self.memory.items))
        self.assertEqual(self.relation.closeness - before["closeness"], case["expect_delta"])
        await self.restart()
        self.assertEqual(self.relation.state(), before)


if __name__ == "__main__":
    unittest.main()
