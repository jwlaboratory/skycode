"""Send iMessages via osascript. The payload travels as an argv item — never
interpolated into the script — so no escaping issues are possible."""

import subprocess
import time
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("send_message.applescript")


def send_text(peer, text, timeout=15):
    result = subprocess.run(
        ["osascript", str(SCRIPT_PATH), peer, text],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript send failed: {result.stderr.strip()}")


def send_frames(peer, frames, delay=0.7):
    """Send frames paced by `delay` seconds to reduce reordering/throttling."""
    for i, frame in enumerate(frames):
        if i:
            time.sleep(delay)
        send_text(peer, frame)
