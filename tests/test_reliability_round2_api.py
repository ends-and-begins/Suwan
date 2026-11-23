"""Offline size limits, cancellable audio ownership and ledger shutdown."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests  # Install the network/dotenv guard before importing production code.

import asyncio
import base64
from concurrent.futures import Future
import io
import json
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from suwan.adapters import http_chat
from suwan.adapters.artifacts import create_artifact
from suwan.adapters.llm import LLMClient
from suwan.adapters.tts import TTSClient
from suwan.app import SuwanRuntime
from suwan.calls import CallDenied, CallManager
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.services.tts import TtsService
from suwan.rpc import request as rpc_request
from suwan.events import TTS_DONE
from suwan.instance_lock import InstanceAlreadyRunningError, InstanceLock


def sdk_response(*, text=None, audio=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=text, audio=SimpleNamespace(data=audio)))], usage=None)


def pcm_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16)
    return output.getvalue()


def audio_client(home):
    client = TTSClient(Settings.from_env(env={}, load_env=False, home_dir=home))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: sdk_response(audio=base64.b64encode(pcm_wav()).decode("ascii")))))
    return client


class PayloadBoundaryTest(unittest.TestCase):
    def test_sdk_and_http_text_use_utf8_bytes_and_identical_limits(self):
        with patch.object(http_chat, "MAX_TEXT_BYTES", 8):
            for text in ("12345678", "你好ab"):
                self.assertEqual(LLMClient._response_text(sdk_response(text=text)), text)
                self.assertEqual(http_chat._completion({"choices": [{"message": {"content": text}}]}).choices[0].message.content, text)
            for text, kind in (("123456789", "response_too_large"), ("你好啊", "response_too_large"),
                               ("\ud800", "invalid_response")):
                for parse in (lambda: LLMClient._response_text(sdk_response(text=text)),
                              lambda: http_chat._completion({"choices": [{"message": {"content": text}}]})):
                    with self.subTest(text=repr(text)), self.assertRaises(http_chat.HTTPChatError) as caught:
                        parse()
                    self.assertEqual(caught.exception.kind, kind)

    def test_audio_encoded_and_decoded_limits_reject_before_decode(self):
        with patch.object(http_chat, "MAX_ENCODED_AUDIO_BYTES", 8), patch.object(http_chat, "MAX_AUDIO_BYTES", 4):
            self.assertEqual(http_chat.decode_response_audio(base64.b64encode(b"abcd").decode()), b"abcd")
            for raw in (b"abcde", b"abcdefg"):
                encoded = base64.b64encode(raw).decode()
                with patch.object(http_chat.base64, "b64decode") as decode:
                    with self.assertRaises(http_chat.HTTPChatError) as caught:
                        http_chat.decode_response_audio(encoded)
                    self.assertEqual(caught.exception.kind, "response_too_large")
                    decode.assert_not_called()
            with self.assertRaises(http_chat.HTTPChatError) as caught:
                http_chat.decode_response_audio("!@#$")
            self.assertEqual(caught.exception.kind, "invalid_response")

    def test_sdk_tts_and_http_reject_same_oversized_audio_without_artifact(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r2-audio-size-") as home:
            client = audio_client(home)
            encoded = base64.b64encode(b"abcde").decode()
            client._client.chat.completions.create = lambda **kw: sdk_response(audio=encoded)
            with patch.object(http_chat, "MAX_AUDIO_BYTES", 4), patch("suwan.adapters.tts.create_artifact") as write:
                path, error = client.synthesize("offline", save_dir=home)
                self.assertIsNone(path)
                self.assertIn("响应过大", error)
                write.assert_not_called()
                with self.assertRaises(http_chat.HTTPChatError) as caught:
                    http_chat._completion({"choices": [{"message": {"audio": {"data": encoded}}}]})
                self.assertEqual(caught.exception.kind, "response_too_large")


class ReceiptBus:
    def __init__(self):
        self.receipts = []

    async def publish(self, topic, data):
        self.receipts.append(data)


class AudioOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r2-audio-owner-")
        self.home = Path(self.temp.name)
        self.client = audio_client(self.home)
        self.bus = ReceiptBus()
        self.service = TtsService(self.bus, ManualClock(), tts_client=self.client, save_dir=self.home, api_enabled=True)

    async def asyncTearDown(self):
        await self.service._on_stop()
        self.client.set_active(False)
        await asyncio.to_thread(self.client.wait_for_writes)
        self.temp.cleanup()

    def event(self):
        return SimpleNamespace(data={"rid": "offline", "text": "offline", "generation": self.client.request_generation,
                                     "delivery_id": self.service.open_delivery()})

    async def test_slow_publication_can_be_revoked_without_blocking_owner_loop(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release")
            return create_artifact(*args)
        with patch("suwan.adapters.tts.create_artifact", side_effect=slow):
            task = asyncio.create_task(asyncio.to_thread(self.client.synthesize, "offline", save_dir=self.home))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                started = time.monotonic()
                self.client.set_api_enabled(False)
                self.client.set_active(False)
                self.assertLess(time.monotonic() - started, .1)
                drained = asyncio.create_task(asyncio.to_thread(self.client.wait_for_writes))
                await asyncio.sleep(.02)
                self.assertFalse(drained.done())
                release.set()
                self.assertIsNone((await task)[0])
                await drained
                self.assertEqual(list(self.home.glob("*.wav")), [])
            finally:
                release.set()
                await task

    async def test_timeout_during_write_disposes_the_late_wav(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release")
            return create_artifact(*args)
        with patch("suwan.adapters.tts.create_artifact", side_effect=slow), patch("suwan.services.tts.TTS_TIMEOUT_SECONDS", .03):
            try:
                await self.service._on_request(self.event())
                self.assertTrue(entered.is_set())
                self.assertFalse(self.bus.receipts[-1]["ok"])
                self.assertIn("TimeoutError", self.bus.receipts[-1]["error"])
                self.client.set_active(False)
                release.set()
                await asyncio.to_thread(self.client.wait_for_writes)
                self.assertEqual(list(self.home.glob("*.wav")), [])
            finally:
                release.set()

    async def test_completed_future_cancelled_before_delivery_is_disposed(self):
        loop = asyncio.get_running_loop()
        def schedule(work, **kwargs):
            future = Future()
            future.set_result(work())
            loop.call_soon(asyncio.current_task().cancel)
            return future
        self.client.call_manager = SimpleNamespace(schedule=schedule, run=lambda send, **kwargs: send())
        with self.assertRaises(asyncio.CancelledError):
            await self.service._on_request(self.event())
        self.client.set_active(False)
        await asyncio.to_thread(self.client.wait_for_writes)
        self.assertEqual(list(self.home.glob("*.wav")), [])
        self.assertEqual(self.bus.receipts, [])
        await asyncio.sleep(.01)

    async def test_drain_includes_disposal_after_writer_hands_off_ownership(self):
        handed_off, release_writer = threading.Event(), threading.Event()
        deleting, release_delete = threading.Event(), threading.Event()
        synthesize, unlink = self.client.synthesize, Path.unlink
        def pause_after_handoff(*args, on_artifact, **kwargs):
            def handoff(path):
                on_artifact(path)
                handed_off.set()
                if not release_writer.wait(3):
                    raise TimeoutError("test writer release")
            return synthesize(*args, on_artifact=handoff, **kwargs)
        def slow_delete(path, *args, **kwargs):
            if path.suffix == ".wav":
                deleting.set()
                if not release_delete.wait(3):
                    raise TimeoutError("test disposal release")
            return unlink(path, *args, **kwargs)
        with patch.object(self.client, "synthesize", side_effect=pause_after_handoff), \
                patch.object(Path, "unlink", slow_delete), patch("suwan.services.tts.TTS_TIMEOUT_SECONDS", .03):
            try:
                await self.service._on_request(self.event())
                self.assertTrue(handed_off.is_set())
                self.assertTrue(await asyncio.to_thread(deleting.wait, 1))
                self.client.set_active(False)
                drained = asyncio.create_task(asyncio.to_thread(self.client.wait_for_writes))
                release_writer.set()
                await asyncio.sleep(.02)
                self.assertFalse(drained.done())
                release_delete.set()
                await drained
                self.assertEqual(list(self.home.glob("*.wav")), [])
            finally:
                release_writer.set()
                release_delete.set()

    async def test_success_delivers_a_valid_wav_and_preserves_it_on_shutdown(self):
        event = self.event()
        await self.service._on_request(event)
        receipt = self.bus.receipts[-1]
        self.assertTrue(receipt["ok"])
        self.assertTrue(self.service.claim_delivery(event.data["delivery_id"], receipt["file"]))
        self.service.close_delivery(event.data["delivery_id"])
        self.client.set_active(False)
        await asyncio.to_thread(self.client.wait_for_writes)
        with wave.open(receipt["file"], "rb") as stream:
            self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()),
                             (1, 2, 16000, 16))

    async def test_cancelled_or_timed_out_queue_has_no_late_loop_exception(self):
        loop = asyncio.get_running_loop()
        errors = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        try:
            for timeout in (False, True):
                manager = CallManager(self.home / f"calls-{timeout}.json")
                self.assertTrue(manager.start())
                self.client.call_manager = manager
                entered, release = threading.Event(), threading.Event()
                held = manager.schedule(lambda: (entered.set(), release.wait(3)))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    with patch("suwan.services.tts.TTS_TIMEOUT_SECONDS", .02 if timeout else 2):
                        task = asyncio.create_task(self.service._on_request(self.event()))
                        for _ in range(100):
                            if manager.snapshot()["pending"]:
                                break
                            await asyncio.sleep(.001)
                        self.assertEqual(manager.snapshot()["pending"], 1)
                        if timeout:
                            await task
                        else:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        manager.close()
                        release.set()
                        await asyncio.to_thread(held.result, 2)
                        await asyncio.sleep(.02)
                finally:
                    release.set()
                    manager.close()
                    await asyncio.to_thread(manager.wait_closed, 2)
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)


class RuntimeAudioDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r2-rpc-delivery-")
        self.runtime = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=self.temp.name),
                                    api_enabled=True, migrate_legacy_data=False)
        self.runtime.tts._client = audio_client(self.temp.name)._client
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()
        await asyncio.to_thread(self.runtime.call_manager.wait_closed, 2)
        self.temp.cleanup()

    async def assert_no_unclaimed_audio(self):
        await self.runtime.bus.flush()
        await asyncio.to_thread(self.runtime.tts.wait_for_writes)
        self.assertEqual(self.runtime.tts_service._deliveries, {})
        self.assertEqual(list(self.runtime.settings.audio_dir.glob("*.wav")), [])

    async def test_cancelled_caller_before_success_receipt_is_disposed(self):
        original = self.runtime.bus.publish
        pending = None
        async def publish(topic, data=None, **kwargs):
            if topic == TTS_DONE and data.get("ok"):
                pending.cancel()
            return await original(topic, data, **kwargs)
        with patch.object(self.runtime.bus, "publish", side_effect=publish):
            pending = asyncio.create_task(self.runtime.speak("offline"))
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await self.assert_no_unclaimed_audio()

    async def test_cancelled_caller_after_rpc_reply_before_claim_is_disposed(self):
        async def receive_then_cancel(*args, **kwargs):
            result = await rpc_request(*args, **kwargs)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
            return result
        with patch("suwan.app.request", side_effect=receive_then_cancel):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime.speak("offline")
        await self.assert_no_unclaimed_audio()

    async def test_no_receipt_subscriber_times_out_and_disposes_published_audio(self):
        original = self.runtime.bus.publish
        async def no_receiver(topic, data=None, **kwargs):
            if topic != TTS_DONE:
                return await original(topic, data, **kwargs)
        async def short_request(*args, **kwargs):
            kwargs["timeout"] = .05
            return await rpc_request(*args, **kwargs)
        with patch.object(self.runtime.bus, "publish", side_effect=no_receiver), patch("suwan.app.request", side_effect=short_request):
            path, error = await self.runtime.speak("offline")
        self.assertIsNone(path)
        self.assertIn("超时", error)
        await self.assert_no_unclaimed_audio()

    async def test_claimed_rpc_result_survives_stop_and_ticket_is_removed(self):
        path, error = await self.runtime.speak("offline")
        self.assertIsNone(error)
        self.assertEqual(self.runtime.tts_service._deliveries, {})
        await self.runtime.stop()
        self.assertEqual(Path(path).read_bytes(), pcm_wav())

    async def test_stop_drains_real_audio_write_before_other_process_can_take_directory(self):
        entered, release = threading.Event(), threading.Event()
        def slow_write(*args):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("test artifact release")
            return create_artifact(*args)
        async def contend():
            result = await asyncio.to_thread(subprocess.run,
                [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--contend", self.temp.name],
                capture_output=True, text=True, encoding="utf-8", timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(data["network_blocks"], 0)
            return data["denied"]
        request = stopping = repeat = None
        try:
            with patch("suwan.adapters.tts.create_artifact", side_effect=slow_write):
                request = asyncio.create_task(self.runtime.speak("offline"))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                stopping = asyncio.create_task(self.runtime.stop())
                await asyncio.sleep(.02)
                stopping.cancel()
                repeat = asyncio.create_task(self.runtime.stop())
                await asyncio.sleep(.02)
                self.assertFalse(stopping.done())
                self.assertFalse(repeat.done())
                self.assertTrue(await contend())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await stopping
                await repeat
                with self.assertRaises(asyncio.CancelledError):
                    await request
            self.assertFalse(await contend())
            self.assertEqual(self.runtime.tts_service._deliveries, {})
            self.assertEqual(list(self.runtime.settings.audio_dir.glob("*.wav")), [])
        finally:
            release.set()
            await asyncio.gather(*(task for task in (request, stopping, repeat) if task), return_exceptions=True)


class LedgerDrainTest(unittest.TestCase):
    def test_close_revokes_during_reservation_and_drain_retains_disk_fence(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r2-ledger-drain-") as home:
            path = Path(home) / "calls.json"
            manager = CallManager(path)
            self.assertTrue(manager.start())
            entered, release, drained = threading.Event(), threading.Event(), threading.Event()
            original = manager._write
            def slow(records):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release")
                return original(records)
            send = Mock(return_value=sdk_response(text="offline"))
            try:
                with patch.object(manager, "_write", side_effect=slow):
                    future = manager.schedule(lambda: manager.run(send, kind="llm", provider="fake", model="fake", max_output_tokens=4))
                    self.assertTrue(entered.wait(1))
                    started = time.monotonic()
                    manager.close()
                    self.assertLess(time.monotonic() - started, .1)
                    self.assertFalse(manager.snapshot()["active"])
                    waiter = threading.Thread(target=lambda: (manager.wait_for_writes(), drained.set()))
                    waiter.start()
                    self.assertFalse(drained.wait(.03))
                    release.set()
                    with self.assertRaises(CallDenied):
                        future.result(2)
                    waiter.join(2)
                    self.assertTrue(drained.is_set())
                    send.assert_not_called()
                successor = CallManager(path)
                self.assertTrue(successor.start())
                self.assertEqual(successor.snapshot()["today_unknown_calls"], 1)
                successor.close()
            finally:
                release.set()
                manager.close()
                manager.wait_closed(2)

    def test_reserve_before_send_and_old_network_result_cannot_overwrite_successor(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r2-ledger-epoch-") as home:
            path = Path(home) / "calls.json"
            manager = CallManager(path)
            manager.start()
            entered, release = threading.Event(), threading.Event()
            def send():
                row = json.loads(path.read_text(encoding="utf-8"))["records"][-1]
                self.assertEqual((row["state"], row["max_output_tokens"]), ("in_flight", 4))
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release")
                return sdk_response(text="old")
            future = manager.schedule(lambda: manager.run(send, kind="llm", provider="fake", model="fake", max_output_tokens=4))
            try:
                self.assertTrue(entered.wait(1))
                manager.close()
                started = time.monotonic()
                manager.wait_for_writes()
                self.assertLess(time.monotonic() - started, .1)
                successor = CallManager(path)
                self.assertTrue(successor.start())
                try:
                    successor.run(lambda: sdk_response(text="new"), kind="llm", provider="fake", model="new", max_output_tokens=4)
                    committed = path.read_bytes()
                    release.set()
                    future.result(2)
                    self.assertEqual(path.read_bytes(), committed)
                    self.assertEqual(successor.snapshot()["used_calls"], 2)
                finally:
                    successor.close()
                    successor.wait_closed(2)
            finally:
                release.set()
                manager.close()
                manager.wait_closed(2)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--contend":
        lock = InstanceLock(Settings.from_env(env={}, load_env=False, home_dir=sys.argv[2]).data_dir)
        try:
            lock.acquire()
        except InstanceAlreadyRunningError:
            denied = True
        else:
            denied = False
            lock.release()
        print(json.dumps({"denied": denied, "network_blocks": sys._suwan_offline_guard["network_blocks"]}))
    else:
        unittest.main()
