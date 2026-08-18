"""Transports: how frames travel between sky-client and home-host.

- IMessageTransport: the real thing (osascript out, chat.db in).
- FileTransport:     frames as files in a spool dir — run both sides on one
                     Mac with no iMessage at all. Primary loopback/e2e story.
- MemoryTransport:   in-memory queue pair for unit tests, with optional frame
                     shuffle/duplication to exercise reassembly.

Echo loops are impossible regardless of transport because each side consumes
only the frame kinds addressed to it (host: REQ; client: RSP/ERR).
"""

import queue
import random
import threading
import time
from pathlib import Path

from . import chatdb, imessage_send, protocol


class Transport:
    def send(self, kind, msg_id, obj):
        raise NotImplementedError

    def receive(self):
        """Yields (kind, msg_id, obj) for each fully reassembled message."""
        raise NotImplementedError


class IMessageTransport(Transport):
    def __init__(self, cfg, include_from_me=False):
        self.peer = cfg["SKY_PEER_NUMBER"]
        self.chunk_size = cfg["SKY_CHUNK_SIZE"]
        self.send_delay = cfg["SKY_SEND_DELAY"]
        self.watcher = chatdb.ChatDBWatcher(
            self.peer,
            poll_interval=cfg["SKY_POLL_INTERVAL"],
            include_from_me=include_from_me,
        )
        self.reassembler = protocol.Reassembler(cfg["SKY_STALE_SECS"])
        self._send_lock = threading.Lock()

    def send(self, kind, msg_id, obj):
        frames = protocol.encode_frames(kind, msg_id, obj, self.chunk_size)
        with self._send_lock:
            imessage_send.send_frames(self.peer, frames, self.send_delay)

    def receive(self):
        for text in self.watcher.poll():
            frame = protocol.parse_frame(text)
            if frame is None:
                continue
            done = self.reassembler.add(frame)
            if done:
                yield done


class FileTransport(Transport):
    def __init__(self, spool_dir, role, poll_interval=0.2,
                 chunk_size=protocol.DEFAULT_CHUNK_SIZE,
                 stale_secs=protocol.DEFAULT_STALE_SECS):
        if role not in ("client", "host"):
            raise ValueError("role must be 'client' or 'host'")
        spool = Path(spool_dir)
        self.outbox = spool / ("to_host" if role == "client" else "to_client")
        self.inbox = spool / ("to_client" if role == "client" else "to_host")
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval
        self.chunk_size = chunk_size
        self.reassembler = protocol.Reassembler(stale_secs)
        self._counter = 0
        self._lock = threading.Lock()

    def send(self, kind, msg_id, obj):
        for frame in protocol.encode_frames(kind, msg_id, obj, self.chunk_size):
            with self._lock:
                self._counter += 1
                name = f"{time.time_ns():020d}-{self._counter:06d}.frame"
            # Write-then-rename so the reader never sees a partial file.
            tmp = self.outbox / (name + ".tmp")
            tmp.write_text(frame)
            tmp.rename(self.outbox / name)

    def receive(self):
        while True:
            for path in sorted(self.inbox.glob("*.frame")):
                try:
                    text = path.read_text()
                except OSError:
                    continue
                path.unlink(missing_ok=True)
                frame = protocol.parse_frame(text)
                if frame is None:
                    continue
                done = self.reassembler.add(frame)
                if done:
                    yield done
            time.sleep(self.poll_interval)


class MemoryTransport(Transport):
    """One endpoint of an in-memory pair. Put None into an endpoint's inbox to
    stop its receive() loop (used by tests to shut down cleanly)."""

    def __init__(self, inbox, outbox, shuffle=False, duplicate=False, seed=0,
                 chunk_size=protocol.DEFAULT_CHUNK_SIZE,
                 stale_secs=protocol.DEFAULT_STALE_SECS):
        self.inbox = inbox
        self.outbox = outbox
        self.shuffle = shuffle
        self.duplicate = duplicate
        self.rng = random.Random(seed)
        self.chunk_size = chunk_size
        self.reassembler = protocol.Reassembler(stale_secs)

    @classmethod
    def pair(cls, **kwargs):
        a, b = queue.Queue(), queue.Queue()
        client = cls(inbox=a, outbox=b, **kwargs)
        host = cls(inbox=b, outbox=a, **kwargs)
        return client, host

    def send(self, kind, msg_id, obj):
        frames = protocol.encode_frames(kind, msg_id, obj, self.chunk_size)
        if self.duplicate and frames:
            frames = frames + [frames[0]]
        if self.shuffle:
            self.rng.shuffle(frames)
        for frame in frames:
            self.outbox.put(frame)

    def stop_peer(self):
        self.outbox.put(None)

    def receive(self):
        while True:
            text = self.inbox.get()
            if text is None:
                return
            frame = protocol.parse_frame(text)
            if frame is None:
                continue
            done = self.reassembler.add(frame)
            if done:
                yield done
