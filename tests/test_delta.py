import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import delta


def body(turns):
    """A realistic canonical Claude Code request body with N user/assistant turns."""
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": f"question {i}: " + "a" * 200})
        messages.append({"role": "assistant", "content": f"answer {i}: " + "b" * 300})
    return delta.canonicalize({
        "model": "claude-fable-5",
        "max_tokens": 8096,
        "system": "You are Claude Code. " + "s" * 500,
        "messages": messages,
    })


class TestDelta(unittest.TestCase):
    def test_roundtrip_append(self):
        prev, new = body(2), body(3)
        d = delta.make_delta(prev, new)
        # The appended turn is small relative to the whole conversation
        self.assertLess(len(d["middle"]), len(new) * 0.5)
        self.assertEqual(delta.apply_delta(prev, d), new)

    def test_roundtrip_middle_edit(self):
        prev = body(3)
        new = prev.replace("question 1", "QUESTION-EDITED 1")
        d = delta.make_delta(prev, new)
        self.assertEqual(delta.apply_delta(prev, d), new)

    def test_base_mismatch_raises(self):
        d = delta.make_delta(body(1), body(2))
        with self.assertRaises(delta.DeltaError):
            delta.apply_delta(body(3), d)

    def test_result_tamper_raises(self):
        prev, new = body(1), body(2)
        d = delta.make_delta(prev, new)
        d["middle"] = d["middle"][:-1] + "!"
        with self.assertRaises(delta.DeltaError):
            delta.apply_delta(prev, d)

    def test_encode_body_full_when_no_useful_base(self):
        cache = delta.BodyCache()
        env = delta.encode_body(body(1), cache)
        self.assertIn("body_full", env)
        # A totally different body shouldn't be sent as a delta either
        cache.add(body(1))
        different = delta.canonicalize({"completely": "different", "x": "q" * 3000})
        self.assertIn("body_full", delta.encode_body(different, cache))

    def test_encode_body_delta_when_worthwhile(self):
        cache = delta.BodyCache()
        cache.add(body(2))
        env = delta.encode_body(body(3), cache)
        self.assertIn("body_delta", env)

    def test_materialize_full_then_delta(self):
        client_cache, host_cache = delta.BodyCache(), delta.BodyCache()
        b1, b2 = body(1), body(2)

        env1 = delta.encode_body(b1, client_cache)
        self.assertEqual(delta.materialize(env1, host_cache), b1)
        client_cache.add(b1)

        env2 = delta.encode_body(b2, client_cache)
        self.assertIn("body_delta", env2)
        self.assertEqual(delta.materialize(env2, host_cache), b2)

    def test_materialize_base_missing(self):
        cache = delta.BodyCache()
        cache.add(body(1))
        env = delta.encode_body(body(2), cache)
        self.assertIn("body_delta", env)
        with self.assertRaises(delta.BaseMissing):
            delta.materialize(env, delta.BodyCache())  # empty host cache

    def test_cache_lru_eviction(self):
        cache = delta.BodyCache(size=2)
        bodies = [body(i + 1) for i in range(3)]
        for b in bodies:
            cache.add(b)
        self.assertIsNone(cache.get(delta.body_hash(bodies[0])))
        self.assertEqual(cache.get(delta.body_hash(bodies[2])), bodies[2])


if __name__ == "__main__":
    unittest.main()
