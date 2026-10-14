"""Offline and fake-provider checks. Never makes billable model calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from makma import memory, store
from makma.brain import MakmaBrain
from makma.providers import OpenAIProvider


class FakeProvider:
    def __init__(self, rounds=None):
        self.rounds = rounds
        self.requests = []

    async def stream(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        await asyncio.sleep(0)
        if self.rounds:
            for event in self.rounds[min(len(self.requests) - 1, len(self.rounds) - 1)]:
                yield event
        else:
            yield {"type": "delta", "text": "Verified "}
            yield {"type": "delta", "text": "answer."}
            yield {"type": "completed", "output": []}


class BrainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MAKMA_DB_PATH": str(Path(self.temp.name) / "test.db")})
        self.env.start()
        memory.init_db()
        store.patch_settings({"provider": "offline"})

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def test_local_memory_and_mission_persist_across_instances(self):
        brain = MakmaBrain()
        self.assertIn("Remembered", await brain.ask("remember: the launch is Friday"))
        self.assertIn("Friday", await MakmaBrain().ask("recall launch"))
        self.assertIn("Created mission", await brain.ask("create a mission: prepare launch"))
        self.assertIn("prepare launch", await MakmaBrain().ask("list missions"))
        self.assertEqual(8, len(store.list_messages("default")))

    async def test_missing_provider_and_empty_memory_are_useful(self):
        brain = MakmaBrain()
        self.assertIn("Local mode", await brain.ask("explain quantum mechanics"))
        await brain.ask("remember this")
        self.assertEqual([], memory.recall())
        self.assertFalse(brain.model_verified)

    async def test_simulation_never_claims_mutation(self):
        store.patch_settings({"simulation": True})
        brain = MakmaBrain()
        self.assertIn("Simulation", await brain.ask("remember: Friday"))
        self.assertIn("Simulation", await brain.ask("create a mission: prepare launch"))
        self.assertEqual([], memory.recall())
        self.assertEqual([], memory.list_missions())

    async def test_attention_includes_real_pending_approval(self):
        from makma.policy import create_approval
        create_approval("type_text", {"text": "test"})
        self.assertIn("Approval", await MakmaBrain().ask("what needs my attention?"))

    async def test_stream_and_conversation_isolation(self):
        provider = FakeProvider()
        brain = MakmaBrain(provider=provider)
        events = [e async for e in brain.stream("hello Alice", "alice")]
        self.assertEqual("Verified answer.", events[-1]["reply"])
        await brain.ask("hello Bob", "bob")
        await brain.ask("follow up", "alice")
        self.assertEqual([{"role": "user", "content": "hello Bob"}], provider.requests[1]["messages"])
        self.assertEqual(3, len(provider.requests[2]["messages"]))
        self.assertTrue(brain.model_verified)

    async def test_same_conversation_serializes(self):
        provider = FakeProvider()
        brain = MakmaBrain(provider=provider)
        await asyncio.gather(brain.ask("first"), brain.ask("second"))
        self.assertEqual(["user", "assistant", "user", "assistant"], [m["role"] for m in store.list_messages("default")])
        self.assertEqual(3, len(provider.requests[1]["messages"]))

    async def test_tool_loop_forwards_json_and_preserves_reasoning(self):
        reasoning = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": []}
        call = {"type": "function_call", "name": "get_system_status", "call_id": "call1", "arguments": "{}"}
        provider = FakeProvider([
            [{"type": "completed", "output": [reasoning, call]}],
            [{"type": "delta", "text": "Checked."}, {"type": "completed", "output": []}],
        ])
        brain = MakmaBrain(provider=provider)
        with patch("makma.brain.execute_tool", return_value={"ok": True, "cpu_percent": 10}) as execute:
            self.assertEqual("Checked.", await brain.ask("inspect the computer"))
        execute.assert_called_once_with("get_system_status", "{}")
        second = provider.requests[1]["messages"]
        self.assertIn(reasoning, second)
        self.assertEqual("call1", second[-1]["call_id"])
        self.assertEqual(10, json.loads(second[-1]["output"])["cpu_percent"])
        self.assertNotIn("previous_response_id", provider.requests[1])

    async def test_tool_rounds_are_bounded(self):
        provider = FakeProvider([[{"type": "completed", "output": [{"type": "function_call", "name": "get_system_status", "call_id": "call", "arguments": "{}"}]}]])
        with patch("makma.brain.execute_tool", return_value={"ok": True}) as execute:
            reply = await MakmaBrain(provider=provider).ask("inspect the computer")
        self.assertIn("round limit", reply)
        self.assertEqual(8, len(provider.requests))
        self.assertEqual(7, execute.call_count)

    async def test_provider_failure_is_sanitized(self):
        class FailingProvider:
            async def stream(self, **kwargs):
                raise RuntimeError(('sk-' + 'secret' + 'x' * 32))
                yield
        reply = await MakmaBrain(provider=FailingProvider()).ask("hello")
        self.assertNotIn("sk-secret", reply)
        self.assertIn("could not finish", reply)

    async def test_stop_cancels_stream_and_releases_conversation(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class WaitingProvider:
            async def stream(self, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                yield
        brain = MakmaBrain(provider=WaitingProvider())
        task = asyncio.create_task(brain.ask("hello"))
        await entered.wait()
        reply = await brain.ask("stop")
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertIn("Stopped", reply)

    async def test_adapter_stream_closes_and_disables_remote_storage(self):
        class Stream:
            closed = False
            def __aiter__(self):
                return self.events()
            async def events(self):
                yield SimpleNamespace(type="response.output_text.delta", delta="hello")
                yield SimpleNamespace(type="response.completed", response=SimpleNamespace(output=[]))
            async def close(self):
                self.closed = True
        stream = Stream()
        class Responses:
            async def create(self, **kwargs):
                self.kwargs = kwargs
                return stream
        responses = Responses()
        provider = OpenAIProvider("unused", client=SimpleNamespace(responses=responses))
        events = [e async for e in provider.stream(model="test", instructions="test", messages=[], tools=[])]
        self.assertTrue(stream.closed)
        self.assertFalse(responses.kwargs["store"])
        self.assertTrue(responses.kwargs["stream"])
        self.assertNotIn("previous_response_id", responses.kwargs)
        self.assertEqual("completed", events[-1]["type"])


if __name__ == "__main__":
    unittest.main()
