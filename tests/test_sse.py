import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sky-client"))

import sse


def parse_events(data: bytes):
    events = []
    for block in data.decode("utf-8").strip().split("\n\n"):
        lines = block.split("\n")
        name = lines[0].removeprefix("event: ")
        payload = json.loads(lines[1].removeprefix("data: "))
        events.append((name, payload))
    return events


MESSAGE = {
    "id": "msg_01ABC",
    "type": "message",
    "role": "assistant",
    "model": "claude-fable-5",
    "content": [
        {"type": "thinking", "thinking": "let me think " * 200, "signature": "sig123"},
        {"type": "text", "text": "Here is the answer. " * 300},
        {"type": "tool_use", "id": "toolu_01", "name": "Bash",
         "input": {"command": "ls -la", "description": "List files"}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 1234, "output_tokens": 567},
}


class TestSSE(unittest.TestCase):
    def setUp(self):
        self.events = parse_events(b"".join(sse.synthesize(MESSAGE)))

    def test_event_ordering(self):
        names = [n for n, _ in self.events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])
        # each content block: start ... stop, in index order
        starts = [p["index"] for n, p in self.events if n == "content_block_start"]
        stops = [p["index"] for n, p in self.events if n == "content_block_stop"]
        self.assertEqual(starts, [0, 1, 2])
        self.assertEqual(stops, [0, 1, 2])

    def test_message_start_shape(self):
        _, payload = self.events[0]
        msg = payload["message"]
        self.assertEqual(msg["id"], "msg_01ABC")
        self.assertEqual(msg["content"], [])
        self.assertIsNone(msg["stop_reason"])
        self.assertEqual(msg["usage"]["input_tokens"], 1234)

    def test_deltas_reconstruct_content(self):
        text = "".join(p["delta"]["text"] for n, p in self.events
                       if n == "content_block_delta" and p["delta"]["type"] == "text_delta")
        self.assertEqual(text, MESSAGE["content"][1]["text"])

        thinking = "".join(p["delta"]["thinking"] for n, p in self.events
                           if n == "content_block_delta" and p["delta"]["type"] == "thinking_delta")
        self.assertEqual(thinking, MESSAGE["content"][0]["thinking"])

        sigs = [p["delta"]["signature"] for n, p in self.events
                if n == "content_block_delta" and p["delta"]["type"] == "signature_delta"]
        self.assertEqual(sigs, ["sig123"])

        partial = "".join(p["delta"]["partial_json"] for n, p in self.events
                          if n == "content_block_delta" and p["delta"]["type"] == "input_json_delta")
        self.assertEqual(json.loads(partial), MESSAGE["content"][2]["input"])

    def test_tool_use_start_has_empty_input(self):
        starts = [p for n, p in self.events if n == "content_block_start" and p["index"] == 2]
        self.assertEqual(starts[0]["content_block"]["input"], {})
        self.assertEqual(starts[0]["content_block"]["name"], "Bash")

    def test_message_delta_carries_stop_reason_and_usage(self):
        payload = dict(self.events)["message_delta"]
        self.assertEqual(payload["delta"]["stop_reason"], "tool_use")
        self.assertEqual(payload["usage"]["output_tokens"], 567)


if __name__ == "__main__":
    unittest.main()
