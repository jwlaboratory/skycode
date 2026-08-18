"""Delta sync: send only the changed bytes of the request body.

Claude Code resends the entire conversation every turn; each turn's body is
mostly the previous one with new content inserted near the end. We diff the
canonical JSON *string* with a prefix/suffix delta — correct for arbitrary
changes, and after turn 1 it typically shrinks a request by ~90%.

Both sides keep an LRU of recent bodies keyed by a short sha256. If the host
no longer has the referenced base, it replies ERR delta_base_missing and the
client resends the full body.
"""

import hashlib
from collections import OrderedDict

CACHE_SIZE = 8
# Only bother with a delta if it saves at least this fraction of the body.
WORTHWHILE_RATIO = 0.9


class DeltaError(Exception):
    pass


class BaseMissing(Exception):
    """The delta references a base body we don't have cached."""


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def canonicalize(obj) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))


def make_delta(prev: str, new: str) -> dict:
    limit = min(len(prev), len(new))
    p = 0
    while p < limit and prev[p] == new[p]:
        p += 1
    s = 0
    while s < limit - p and prev[len(prev) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    return {
        "base": body_hash(prev),
        "prefix_len": p,
        "suffix_len": s,
        "middle": new[p:len(new) - s] if s else new[p:],
        "result_hash": body_hash(new),
    }


def apply_delta(prev: str, delta: dict) -> str:
    if body_hash(prev) != delta["base"]:
        raise DeltaError("delta base hash mismatch")
    p, s = delta["prefix_len"], delta["suffix_len"]
    out = prev[:p] + delta["middle"] + (prev[len(prev) - s:] if s else "")
    if body_hash(out) != delta["result_hash"]:
        raise DeltaError("delta result hash mismatch")
    return out


class BodyCache:
    def __init__(self, size=CACHE_SIZE):
        self.size = size
        self._items = OrderedDict()  # hash -> body str

    def add(self, body: str):
        h = body_hash(body)
        self._items.pop(h, None)
        self._items[h] = body
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def get(self, h):
        return self._items.get(h)

    def bodies(self):
        return list(self._items.values())

    def clear(self):
        self._items.clear()


def encode_body(body: str, cache: BodyCache) -> dict:
    """Client side: pick the cached base yielding the smallest delta, or fall
    back to the full body. Returns the envelope fragment."""
    best = None
    for prev in cache.bodies():
        d = make_delta(prev, body)
        if best is None or len(d["middle"]) < len(best["middle"]):
            best = d
    if best is not None and len(best["middle"]) < len(body) * WORTHWHILE_RATIO:
        return {"body_delta": best}
    return {"body_full": body}


def materialize(env: dict, cache: BodyCache) -> str:
    """Host side: recover the full body from an envelope and cache it for
    future deltas. Raises BaseMissing if the referenced base isn't cached."""
    if "body_full" in env:
        body = env["body_full"]
    elif "body_delta" in env:
        d = env["body_delta"]
        prev = cache.get(d["base"])
        if prev is None:
            raise BaseMissing(d["base"])
        body = apply_delta(prev, d)
    else:
        raise DeltaError("envelope has neither body_full nor body_delta")
    cache.add(body)
    return body
