"""Frame format, chunking, and reassembly for the iMessage wire.

One iMessage text per frame:

    SKY1|<kind>|<msg_id>|<seq>/<total>|<chunk>

The payload is base64(zlib(json)) so every frame is pure ASCII — immune to
iMessage smart-quote/emoji mangling. Anything that doesn't parse as a frame
is ignored, so you can still text the peer normally.
"""

import base64
import json
import time
import zlib
from dataclasses import dataclass

MAGIC = "SKY1"
KINDS = ("REQ", "RSP", "ERR")

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_STALE_SECS = 120.0


@dataclass
class Frame:
    kind: str
    msg_id: str
    seq: int
    total: int
    chunk: str


def encode_payload(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode_payload(payload: str):
    return json.loads(zlib.decompress(base64.b64decode(payload)))


def encode_frames(kind, msg_id, obj, chunk_size=DEFAULT_CHUNK_SIZE):
    if kind not in KINDS:
        raise ValueError(f"unknown frame kind: {kind}")
    payload = encode_payload(obj)
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)] or [payload]
    total = len(chunks)
    return [f"{MAGIC}|{kind}|{msg_id}|{i + 1}/{total}|{c}" for i, c in enumerate(chunks)]


def parse_frame(text):
    """Parse one message text into a Frame, or None if it isn't ours."""
    if not text:
        return None
    start = text.find(MAGIC + "|")
    if start < 0:
        return None
    parts = text[start:].strip().split("|", 4)
    if len(parts) != 5:
        return None
    _, kind, msg_id, seq_total, chunk = parts
    if kind not in KINDS or not msg_id:
        return None
    try:
        seq_s, total_s = seq_total.split("/", 1)
        seq, total = int(seq_s), int(total_s)
    except ValueError:
        return None
    if not (1 <= seq <= total):
        return None
    return Frame(kind, msg_id, seq, total, chunk)


class Reassembler:
    """Collects frames into complete messages.

    Duplicate frames are idempotent, order doesn't matter, and partial
    messages that go quiet for `stale_secs` are dropped.
    """

    def __init__(self, stale_secs=DEFAULT_STALE_SECS):
        self.stale_secs = stale_secs
        self._partial = {}  # (kind, msg_id) -> {"total", "chunks": {seq: str}, "last": ts}

    def add(self, frame, now=None):
        """Feed one frame; returns (kind, msg_id, obj) on completion, else None."""
        now = time.time() if now is None else now
        self._evict(now)
        key = (frame.kind, frame.msg_id)
        entry = self._partial.get(key)
        if entry is None or entry["total"] != frame.total:
            entry = self._partial[key] = {"total": frame.total, "chunks": {}, "last": now}
        entry["chunks"][frame.seq] = frame.chunk
        entry["last"] = now
        if len(entry["chunks"]) < entry["total"]:
            return None
        del self._partial[key]
        payload = "".join(entry["chunks"][i] for i in range(1, entry["total"] + 1))
        try:
            obj = decode_payload(payload)
        except Exception:
            return None
        return (frame.kind, frame.msg_id, obj)

    def _evict(self, now):
        stale = [k for k, e in self._partial.items() if now - e["last"] > self.stale_secs]
        for k in stale:
            del self._partial[k]
