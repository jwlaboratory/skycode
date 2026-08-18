import random
import string
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import protocol


def incompressible(n, seed=1):
    # Repeated chars zlib-compress into a single frame; use random text so
    # multi-chunk paths actually get exercised.
    return "".join(random.Random(seed).choices(string.ascii_letters + string.digits, k=n))


class TestFrames(unittest.TestCase):
    def test_single_frame_roundtrip(self):
        obj = {"hello": "world"}
        frames = protocol.encode_frames("REQ", "aabbccdd", obj)
        self.assertEqual(len(frames), 1)
        frame = protocol.parse_frame(frames[0])
        self.assertEqual(frame.kind, "REQ")
        self.assertEqual(frame.msg_id, "aabbccdd")
        self.assertEqual((frame.seq, frame.total), (1, 1))

    def test_parse_rejects_garbage(self):
        for text in [None, "", "hey what's up", "SKY1|NOPE|aabbccdd|1/1|abc",
                     "SKY1|REQ|aabbccdd|0/1|abc", "SKY1|REQ|aabbccdd|2/1|abc",
                     "SKY1|REQ", "SKY1|REQ|aabbccdd|x/y|abc"]:
            self.assertIsNone(protocol.parse_frame(text), text)

    def test_parse_with_leading_junk(self):
        # attributedBody fallback decoding can leave junk before the frame
        frame_text = protocol.encode_frames("RSP", "aabbccdd", {"x": 1})[0]
        frame = protocol.parse_frame("\x00\x01junk" + frame_text)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.kind, "RSP")


class TestReassembler(unittest.TestCase):
    def roundtrip(self, obj, shuffle=False, duplicate=False, chunk_size=48):
        frames = protocol.encode_frames("REQ", "deadbeef", obj, chunk_size)
        if duplicate:
            frames = frames + frames[:2]
        if shuffle:
            random.Random(42).shuffle(frames)
        reasm = protocol.Reassembler()
        results = []
        for text in frames:
            done = reasm.add(protocol.parse_frame(text))
            if done:
                results.append(done)
        return frames, results

    def test_multichunk_in_order(self):
        obj = {"messages": [{"role": "user", "content": incompressible(2000)}]}
        frames, results = self.roundtrip(obj)
        self.assertGreater(len(frames), 3)
        self.assertEqual(results, [("REQ", "deadbeef", obj)])

    def test_out_of_order_and_duplicates(self):
        obj = {"messages": [{"role": "user", "content": incompressible(3000, seed=2)}]}
        _, results = self.roundtrip(obj, shuffle=True, duplicate=True)
        self.assertEqual(results, [("REQ", "deadbeef", obj)])

    def test_stale_partials_are_dropped(self):
        obj = {"data": incompressible(2000, seed=3)}
        frames = [protocol.parse_frame(t)
                  for t in protocol.encode_frames("REQ", "deadbeef", obj, 800)]
        self.assertGreaterEqual(len(frames), 2)
        reasm = protocol.Reassembler(stale_secs=120)
        self.assertIsNone(reasm.add(frames[0], now=0))
        # First chunk went stale; the rest can't complete without it...
        for f in frames[1:]:
            self.assertIsNone(reasm.add(f, now=200))
        # ...until it is resent.
        self.assertEqual(reasm.add(frames[0], now=201), ("REQ", "deadbeef", obj))


if __name__ == "__main__":
    unittest.main()
