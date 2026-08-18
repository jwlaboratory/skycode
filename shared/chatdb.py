"""Receive iMessages by polling ~/Library/Messages/chat.db.

Requires Full Disk Access for the terminal running us. Only messages from the
configured peer are yielded — that's the entire trust model.

Ventura+ gotcha: message.text is often NULL with the body stored in
attributedBody, a serialized NSAttributedString typedstream blob. We decode
it with the well-trodden NSString-marker hack, with a regex fallback that
works because our frames are pure ASCII starting with SKY1|.
"""

import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

FRAME_RE = re.compile(r"SKY1\|(?:REQ|RSP|ERR)\|[0-9a-f]+\|\d+/\d+\|[A-Za-z0-9+/=]*")

QUERY = """
SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, h.id AS sender
FROM message m
JOIN handle h ON m.handle_id = h.ROWID
WHERE m.ROWID > ?{from_me_filter}
ORDER BY m.ROWID ASC
"""


def normalize_handle(s):
    """Compare phone numbers by their last 10 digits (handles +1 vs bare
    formats); emails case-insensitively."""
    s = (s or "").strip()
    if "@" in s:
        return s.lower()
    digits = re.sub(r"\D", "", s)
    return digits[-10:] if digits else s.lower()


def decode_attributed_body(blob):
    if not blob:
        return None
    try:
        idx = blob.find(b"NSString")
        if idx < 0:
            idx = blob.find(b"NSMutableString")
        if idx >= 0:
            # Skip past the class name and typedstream framing to the '+'
            # marker; the UTF-8 string follows, length-prefixed.
            i = blob.index(b"+", idx) + 1
            if blob[i] == 0x81:
                length = int.from_bytes(blob[i + 1:i + 3], "little")
                i += 3
            else:
                length = blob[i]
                i += 1
            text = blob[i:i + length].decode("utf-8", errors="ignore")
            if text:
                return text
    except (ValueError, IndexError):
        pass
    m = FRAME_RE.search(blob.decode("utf-8", errors="ignore"))
    return m.group(0) if m else None


class ChatDBWatcher:
    def __init__(self, peer, poll_interval=1.0, db_path=DB_PATH, include_from_me=False):
        self.peer = normalize_handle(peer)
        self.poll_interval = poll_interval
        self.db_path = Path(db_path)
        self.include_from_me = include_from_me
        self._watermark = None
        self._query = QUERY.format(
            from_me_filter="" if include_from_me else " AND m.is_from_me = 0"
        )
        self._check_access()

    def _check_access(self):
        try:
            self._connect().close()
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                f"cannot open {self.db_path} ({e}). Grant Full Disk Access to "
                "your terminal in System Settings > Privacy & Security > Full "
                "Disk Access, then restart the terminal."
            )

    def _connect(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)

    def poll_once(self):
        """One poll; returns a list of message texts from the peer."""
        try:
            con = self._connect()
        except sqlite3.OperationalError:
            return []
        try:
            cur = con.cursor()
            if self._watermark is None:
                # Start at the current tip so old texts are never replayed.
                self._watermark = cur.execute(
                    "SELECT COALESCE(MAX(ROWID), 0) FROM message"
                ).fetchone()[0]
                return []
            rows = cur.execute(self._query, (self._watermark,)).fetchall()
        except sqlite3.OperationalError:
            # Messages.app writes with WAL; transient "database is locked" is
            # normal — try again next poll.
            return []
        finally:
            con.close()

        texts = []
        for rowid, text, attributed, _is_from_me, sender in rows:
            self._watermark = max(self._watermark, rowid)
            if normalize_handle(sender) != self.peer:
                continue
            body = text or decode_attributed_body(attributed)
            if body:
                texts.append(body)
        return texts

    def poll(self):
        """Generator yielding message texts forever."""
        while True:
            yield from self.poll_once()
            time.sleep(self.poll_interval)
