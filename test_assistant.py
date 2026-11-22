import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

import assistant as a


class PolicyTests(unittest.TestCase):
    def test_chat_scope_and_group_trigger(self):
        cfg = {"chats": {"-1001": "test"}, "paused": False}
        self.assertTrue(a.eligible(-1001, False, True, True, False, cfg))
        self.assertTrue(a.eligible(-1001, False, True, False, True, cfg))
        self.assertFalse(a.eligible(-1001, False, True, False, False, cfg))
        self.assertFalse(a.eligible(-1002, True, False, True, True, cfg))
        cfg["paused"] = True
        self.assertFalse(a.eligible(-1001, False, True, True, True, cfg))

    def test_claim_and_limit_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.sqlite"
            state = a.State(path)
            self.assertTrue(state.claim(1, 3))
            self.assertTrue(state.reserve(1))
            state.db.close()
            state = a.State(path)
            self.assertFalse(state.claim(1, 3))
            self.assertTrue(state.claim(2, 3))
            self.assertFalse(state.reserve(1))
            state.db.close()

    def test_api_payload_and_output(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({"status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "Здравствуйте"}]}]}).encode()
        with patch("urllib.request.urlopen", return_value=Response()) as call:
            self.assertEqual(a.api_reply({"model": "test", "openai_key": "secret"}, "rules", "hello"), "Здравствуйте")
            request = call.call_args.args[0]
            self.assertNotIn("secret", request.full_url)
            self.assertFalse(json.loads(request.data)["store"])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, *, allowed=True, paused=False, deleted=False, edit=False,
                       bot=False, stop_in_flight=False, duplicate=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {"api_id": 1, "api_hash": "x", "owner_id": 10, "openai_key": "x",
                   "model": "test", "chats": {"-1001": "test"} if allowed else {},
                   "paused": paused, "daily_limit": 100}
            path = root / "config.json"
            path.write_text(json.dumps(cfg))
            (root / "instructions.txt").write_text("test")
            calls = []
            class Client:
                def on(self, builder):
                    def register(handler):
                        setattr(self, "outgoing" if builder.outgoing else "incoming", handler)
                        return handler
                    return register
                async def connect(self): pass
                async def disconnect(self): pass
                async def is_user_authorized(self): return True
                async def get_me(self): return Obj(id=10, bot=False)
                async def send_message(self, *args, **kwargs): calls.append((args, kwargs))
                async def get_messages(self, *args, **kwargs):
                    return None if deleted else Obj(raw_text="edited" if edit else "hello")
                async def run_until_disconnected(self):
                    class Event:
                        chat_id = -1001
                        sender_id = 20
                        fwd_from = None
                        raw_text = "hello"
                        id = 5
                        is_private = False
                        is_group = True
                        is_reply = False
                        message = Obj(action=None, via_bot_id=None, mentioned=True)
                        date = datetime.now(timezone.utc)
                        async def get_sender(self): return Obj(bot=bot)
                    task = asyncio.create_task(self.incoming(Event()))
                    if stop_in_flight:
                        await asyncio.sleep(0.01)
                        await self.outgoing(Obj(chat_id=10, sender_id=10, raw_text=".ai off"))
                    await task
                    if duplicate:
                        await self.incoming(Event())
            client = Client()
            def reply(*args):
                import time
                if stop_in_flight: time.sleep(0.08)
                return "reply"
            with patch.object(a, "ROOT", root), patch.object(a, "DATA", root), \
                 patch.object(a, "CONFIG", path), patch.object(a, "make_client", return_value=client), \
                 patch.object(a, "api_reply", side_effect=reply) as api:
                await a.run()
            return [c for c in calls if c[0][0] == -1001], api.call_count

    async def test_allowed_group_send_once(self):
        calls, count = await self.scenario(duplicate=True)
        self.assertEqual((len(calls), count), (1, 1))
        self.assertEqual(calls[0][1]["reply_to"], 5)

    async def test_unselected_chat_never_reaches_ai(self):
        self.assertEqual(await self.scenario(allowed=False), ([], 0))

    async def test_paused_never_reaches_ai(self):
        self.assertEqual(await self.scenario(paused=True), ([], 0))

    async def test_bot_does_not_trigger(self):
        self.assertEqual(await self.scenario(bot=True), ([], 0))

    async def test_deleted_message_not_answered(self):
        self.assertEqual(await self.scenario(deleted=True), ([], 1))

    async def test_edited_message_not_answered(self):
        self.assertEqual(await self.scenario(edit=True), ([], 1))

    async def test_pause_cancels_in_flight_answer(self):
        self.assertEqual(await self.scenario(stop_in_flight=True), ([], 1))


if __name__ == "__main__":
    unittest.main()
