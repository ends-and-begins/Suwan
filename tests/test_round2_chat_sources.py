"""有限中文规则、来源隔离与生成记忆的回归验证；全部使用临时数据。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from suwan.assets.local_replies import extract_intro_name, fact_query_key, parse_fact_statement
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import DIARY_TRIGGER, MEMORY_ADD_REQUEST, STORY_TRIGGER
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.organs.sensory import SensoryBufferOrgan
from suwan.services.autonomous import AutonomousService
from suwan.services.chat import ChatService
from suwan.services.doc import DocService
from suwan.services.monologue import MonologueService
from suwan.services.reflection import ReflectionService
from suwan.services.share import ShareWatcherService


class LimitedChineseGrammarTest(unittest.TestCase):
    def test_name_queries_negation_quotes_and_third_person_do_not_register(self):
        for text in ("我叫什么名字？", "如果我叫小明", "我不是小明", "别记我叫小明", "他说我叫小明", '“我叫小明”', "我是学生", "我叫小明吗", "你知道我叫小明吗"):
            with self.subTest(text=text):
                self.assertIsNone(extract_intro_name(text))
                self.assertIsNone(parse_fact_statement(text))
        self.assertEqual(extract_intro_name("你好，我叫小明"), "小明")
        self.assertEqual(extract_intro_name("我的名字现在改为明远"), "明远")

    def test_supported_updates_need_context_when_subject_and_slot_are_omitted(self):
        self.assertIsNone(parse_fact_statement("现在改喝茶了"))
        self.assertEqual(parse_fact_statement("现在改喝茶了", current_values={"preference.drink": "咖啡"}), ("preference.drink", "茶"))
        self.assertIsNone(parse_fact_statement("最近不骑车，改为游泳"))
        self.assertEqual(parse_fact_statement("最近不骑车，改为游泳", current_values={"preference.weekend_activity": "骑车"}), ("preference.weekend_activity", "游泳"))
        self.assertIsNone(parse_fact_statement("最近不跑步，改为游泳", current_values={"preference.weekend_activity": "骑车"}))
        self.assertEqual(parse_fact_statement("上午不要提醒我了"), ("preference.contact_time", "上午别提醒"))
        self.assertIsNone(parse_fact_statement("我喜欢蓝色"))

    def test_unpunctuated_questions_uncertainty_and_examples_are_not_facts(self):
        for text in (
            "我叫小明对不对", "我叫小明好不好", "我的名字是张三吧",
            "我叫张三这只是举例", "我叫小明可能只是开玩笑",
            "我现在住在杭州吧", "我现在住在杭州应该可以",
            "我喜欢喝茶比如龙井", "周末我喜欢游泳呢",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_intro_name(text))
                self.assertIsNone(parse_fact_statement(text))
        self.assertEqual(parse_fact_statement("我现在住在杭州"), ("user.city", "杭州"))
        self.assertEqual(parse_fact_statement("我喜欢喝茶"), ("preference.drink", "茶"))

    def test_unknown_fact_questions_have_deterministic_intents(self):
        examples = {"我的生日是哪天？": "user.birthday", "我住在哪个城市？": "user.city", "我叫什么名字？": "user.name", "休息日我一般做什么？": "preference.weekend_activity", "上午可以主动提醒我吗？": "preference.contact_time"}
        for text, key in examples.items():
            self.assertEqual(fact_query_key(text), key)


class ChatAndProducerSourcesTest(unittest.TestCase):
    def exercise(self, body):
        with tempfile.TemporaryDirectory(prefix="suwan-round2-sources-") as directory:
            async def scenario():
                home = Path(directory)
                bus, clock = EventBus(), ManualClock()
                organs = {
                    "memory": MemoryOrgan(bus, clock, file_path=home / "memories.json"),
                    "identity": IdentityOrgan(bus, clock), "affect": AffectOrgan(bus, clock),
                    "circadian": CircadianOrgan(bus, clock), "drive": DriveOrgan(bus, clock),
                    "sensory": SensoryBufferOrgan(bus, clock),
                }
                await bus.start()
                for organ in organs.values():
                    await organ.start()
                try:
                    await body(home, bus, clock, organs)
                finally:
                    for organ in reversed(list(organs.values())):
                        await organ.stop()
                    await bus.stop()
            asyncio.run(scenario())

    def test_user_episode_never_contains_assistant_invention_and_prompt_labels_sources(self):
        async def body(home, bus, clock, organs):
            prompts = []
            llm = SimpleNamespace(available=True, chat=lambda system, prompt, tokens: prompts.append(prompt) or "用户住在北京，喜欢甜咖啡")
            service = ChatService(bus, clock, organs=organs, llm=llm, api_enabled=True)
            await service.start()
            try:
                await service.chat("上周我去图书馆借书")
                episode = organs["memory"].list_items()[0]
                self.assertEqual(episode.content, "上周我去图书馆借书")
                self.assertEqual((episode.kind, episode.source), ("event", "user"))
                self.assertNotIn("北京", episode.content)
                await service.chat("图书馆借书的事")
                self.assertIn("ID=" + episode.id, prompts[-1])
                self.assertIn("来源=user", prompts[-1])
                self.assertIsNone(organs["memory"].get_fact("user.city"))
                answer = await service.chat("我住在哪个城市？")
                self.assertIn("不知道", answer)
                self.assertNotIn("北京", answer)
                self.assertEqual(len(prompts), 2)
            finally:
                await service.stop()
        self.exercise(body)

    def test_share_saves_full_text_with_provenance_and_pause_keeps_source(self):
        async def body(home, bus, clock, organs):
            folder = home / "share"
            folder.mkdir()
            full_text = "用户资料\n" + "正文" * 400 + "末尾证据"
            (folder / "资料.md").write_bytes(full_text.encode("utf-8"))
            service = ShareWatcherService(bus, clock, organs=organs, share_dir=folder)
            self.assertEqual(await service.scan(), 1)
            item = organs["memory"].list_items()[0]
            self.assertEqual(item.content, full_text)
            self.assertEqual((item.kind, item.source, item.source_ref), ("knowledge", "share", "资料.md"))
            organs["memory"].set_recording_enabled(False)
            (folder / "暂停.md").write_text("不应被消费", encoding="utf-8")
            self.assertEqual(await service.scan(), 0)
            self.assertTrue((folder / "暂停.md").exists())
        self.exercise(body)

    def test_generated_producers_preserve_type_and_all_used_source_ids(self):
        async def body(home, bus, clock, organs):
            source = organs["memory"].add("我今天去骑车", kind="event", source="user")
            llm = SimpleNamespace(available=True, chat=lambda *args: "苏晚生成的文学内容")
            reflection = ReflectionService(bus, clock, organs=organs, data_dir=home, llm=llm, api_enabled=True)
            monologue = MonologueService(bus, clock, organs=organs, llm=llm, api_enabled=True)
            autonomous = AutonomousService(bus, clock, organs=organs, data_dir=home)
            await reflection._on_story(None)
            story = next(item for item in organs["memory"].list_items() if item.topic == "story")
            self.assertEqual((story.kind, story.source), ("generated", "reflection"))
            self.assertIn(source.id, story.derived_from)
            used_ids = {item.id for item in organs["memory"].get_recent(3)}
            await monologue._compose()
            mono = next(item for item in organs["memory"].list_items() if item.topic == "monologue")
            self.assertEqual((mono.kind, mono.source), ("generated", "monologue"))
            self.assertEqual(set(mono.derived_from), used_ids)
            await autonomous._do_learning()
            await autonomous._do_creation()
            for item in organs["memory"].list_items():
                if item.topic in {"learning", "creation"}:
                    self.assertEqual((item.kind, item.source), ("generated", "autonomous"))
            self.assertTrue(all(hit.item.kind != "generated" for hit in organs["memory"].search("苏晚生成的文学内容")))
        self.exercise(body)

    def test_deleted_sources_prevent_late_diary_or_story_file_creation(self):
        for action in ("diary", "story"):
            async def body(home, bus, clock, organs):
                source = organs["memory"].add("用户告诉的私密事项", kind="event", source="user")
                entered, release = asyncio.Event(), asyncio.Event()

                async def fake_thread(*args):
                    entered.set()
                    await release.wait()
                    return "迟到的私密生成内容"

                from unittest.mock import patch
                service = ReflectionService(bus, clock, organs=organs, data_dir=home, llm=SimpleNamespace(available=True, chat=lambda *args: None), api_enabled=True)
                with patch("suwan.services.reflection.asyncio.to_thread", side_effect=fake_thread):
                    task = asyncio.create_task(service._on_diary(None) if action == "diary" else service._on_story(None))
                    await entered.wait()
                    organs["memory"].delete(source.id)
                    release.set()
                    await task
                self.assertFalse((home / ("diary.txt" if action == "diary" else "story.txt")).exists())
                self.assertFalse(organs["memory"].search("私密"))
            with self.subTest(action=action):
                self.exercise(body)

    def test_late_document_is_not_exported_after_source_delete_or_context_reset(self):
        for action in ("delete", "reset"):
            async def body(home, bus, clock, organs):
                source = organs["memory"].add("private marker", kind="event", source="user")
                manager = SimpleNamespace(epoch=0)
                entered, release = asyncio.Event(), asyncio.Event()

                async def fake_thread(*args):
                    entered.set()
                    await release.wait()
                    return "private marker from stale context"

                from unittest.mock import patch
                service = DocService(bus, clock, organs=organs, output_dir=home / "output",
                                     llm=SimpleNamespace(available=True, chat=lambda *args: None),
                                     api_enabled=True, memory_manager=manager)
                with patch("suwan.services.doc.asyncio.to_thread", side_effect=fake_thread):
                    task = asyncio.create_task(service.generate("private marker"))
                    await entered.wait()
                    if action == "delete":
                        organs["memory"].delete(source.id)
                    else:
                        manager.epoch += 1
                    release.set()
                    self.assertIsNone(await task)
                self.assertFalse((home / "output").exists())
            with self.subTest(action=action):
                self.exercise(body)



class QueuedMemoryPrivacyTest(unittest.IsolatedAsyncioTestCase):
    async def test_queued_chat_share_and_generated_writes_are_rejected_after_forget_and_resume(self):
        for producer in ("chat", "share", "learning", "creation"):
            with self.subTest(producer=producer), tempfile.TemporaryDirectory(prefix="suwan-stale-memory-") as directory:
                home = Path(directory)
                runtime = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=home),
                                       clock=ManualClock(), migrate_legacy_data=False)
                await runtime.start()
                memory = runtime.organs["memory"]
                source = memory.add("private marker", kind="event", source="user", explicit=True)
                entered, release = asyncio.Event(), asyncio.Event()
                subscription = next(sub for sub in runtime.bus._subs if sub.pattern == MEMORY_ADD_REQUEST)
                original_handler = subscription.handler

                async def delayed_handler(event):
                    entered.set()
                    await release.wait()
                    await original_handler(event)

                subscription.handler = delayed_handler
                if producer == "chat":
                    runtime.chat_service.llm = SimpleNamespace(available=True, chat=lambda *args: "private marker reply")
                    runtime.chat_service.api_enabled = True
                    operation = runtime.chat_service.chat("private marker")
                elif producer == "share":
                    runtime.share_service.share_dir.mkdir(parents=True, exist_ok=True)
                    source_file = runtime.share_service.share_dir / "private.md"
                    source_file.write_bytes(b"private marker")
                    operation = runtime.share_service.scan()
                elif producer == "learning":
                    operation = runtime.autonomous_service._do_learning()
                else:
                    operation = runtime.autonomous_service._do_creation()
                task = asyncio.create_task(operation)
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    self.assertTrue((await runtime.memory_command(f"/memory forget {source.id}"))["ok"])
                    self.assertTrue((await runtime.memory_command("/memory pause"))["ok"])
                    self.assertTrue((await runtime.memory_command("/memory resume"))["ok"])
                    release.set()
                    result = await asyncio.wait_for(task, timeout=3)
                    await runtime.bus.flush()
                    self.assertFalse(memory.items)
                    self.assertFalse(any("private marker" in line["text"] for line in runtime.organs["sensory"].lines))
                    if producer == "chat":
                        self.assertNotIn("private marker", result)
                    elif producer == "share":
                        self.assertEqual(result, 0)
                        self.assertTrue(source_file.exists())
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    subscription.handler = original_handler
                    await runtime.stop()

