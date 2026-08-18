"""In-process e2e: SkyClient <-> fake home host over a MemoryTransport with
frame shuffling and duplication turned on, multi-turn so the delta path and
the base-miss fallback both get exercised."""

import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sky-client"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import delta as deltalib
from shared.transport import MemoryTransport

from mock_upstream import canned_message
from server import SkyClient


def run_fake_host(host_tp, cache, seen_envelopes):
    for kind, msg_id, env in host_tp.receive():
        if kind != "REQ":
            continue
        seen_envelopes.append(env)
        try:
            body_str = deltalib.materialize(env, cache)
        except deltalib.BaseMissing:
            host_tp.send("ERR", msg_id, {"code": "delta_base_missing"})
            continue
        body = json.loads(body_str)
        host_tp.send("RSP", msg_id, {"status": 200, "body": canned_message(body)})


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        # Small chunks force multi-frame messages; shuffle+duplicate stress
        # the reassembler exactly like a flaky message pipe would.
        self.client_tp, self.host_tp = MemoryTransport.pair(
            shuffle=True, duplicate=True, seed=7, chunk_size=96)
        self.host_cache = deltalib.BodyCache()
        self.seen = []
        self.host_thread = threading.Thread(
            target=run_fake_host, args=(self.host_tp, self.host_cache, self.seen),
            daemon=True)
        self.host_thread.start()
        self.client = SkyClient({"SKY_RESPONSE_TIMEOUT": 15.0}, self.client_tp)

    def tearDown(self):
        self.client_tp.stop_peer()
        self.host_thread.join(timeout=5)

    def body(self, turns):
        messages = []
        for i in range(turns):
            messages.append({"role": "user", "content": f"question {i} " + "x" * 300})
            if i < turns - 1:
                messages.append({"role": "assistant", "content": f"answer {i} " + "y" * 300})
        return deltalib.canonicalize({
            "model": "claude-fable-5",
            "max_tokens": 1024,
            "system": "You are Claude Code. " + "s" * 400,
            "messages": messages,
        })

    def test_multi_turn_with_delta_and_base_miss(self):
        # Turn 1: no cache anywhere -> full body
        status, resp = self.client.roundtrip("/v1/messages", {}, self.body(1))
        self.assertEqual(status, 200)
        self.assertEqual(resp["type"], "message")
        self.assertIn("question 0", resp["content"][0]["text"])
        self.assertIn("body_full", self.seen[0])

        # Turn 2: both sides have turn 1 cached -> delta
        status, resp = self.client.roundtrip("/v1/messages", {}, self.body(2))
        self.assertEqual(status, 200)
        self.assertIn("question 1", resp["content"][0]["text"])
        self.assertIn("body_delta", self.seen[1])
        self.assertLess(len(self.seen[1]["body_delta"]["middle"]), len(self.body(2)) * 0.5)

        # Turn 3 with the host cache wiped (simulated restart): the delta is
        # rejected with base_missing and the client transparently resends full.
        self.host_cache.clear()
        status, resp = self.client.roundtrip("/v1/messages", {}, self.body(3))
        self.assertEqual(status, 200)
        self.assertIn("question 2", resp["content"][0]["text"])
        self.assertIn("body_delta", self.seen[2])
        self.assertIn("body_full", self.seen[3])

    def test_error_reply_passthrough(self):
        # A host-side ERR with a status/body surfaces as that HTTP error.
        def erroring_host():
            for kind, msg_id, env in self.host_tp.receive():
                if kind == "REQ":
                    self.host_tp.send("ERR", msg_id, {
                        "status": 529,
                        "body": {"type": "error", "error": {
                            "type": "overloaded_error", "message": "overloaded"}},
                    })

        # replace the default fake host with an erroring one
        self.client_tp.stop_peer()
        self.host_thread.join(timeout=5)
        self.host_thread = threading.Thread(target=erroring_host, daemon=True)
        self.host_thread.start()

        status, resp = self.client.roundtrip("/v1/messages", {}, self.body(1))
        self.assertEqual(status, 529)
        self.assertEqual(resp["error"]["type"], "overloaded_error")


if __name__ == "__main__":
    unittest.main()
