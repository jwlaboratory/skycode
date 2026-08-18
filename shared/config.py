"""Config: env vars first, optional ~/.skycode.json fallback, defaults last."""

import json
import os
from pathlib import Path

CONFIG_FILE = Path.home() / ".skycode.json"

DEFAULTS = {
    "SKY_PEER_NUMBER": None,      # the other Mac's iMessage number/email (both sides)
    "SKY_PORT": 8377,             # sky-client HTTP port
    "SKY_POLL_INTERVAL": 1.0,     # chat.db poll seconds
    "SKY_CHUNK_SIZE": 1200,       # payload chars per frame
    "SKY_SEND_DELAY": 0.7,        # seconds between chunk sends
    "SKY_RESPONSE_TIMEOUT": 600.0,  # sky-client wait for a reply
    "SKY_STALE_SECS": 120.0,      # drop partial reassemblies after this
    "SKY_UPSTREAM_URL": "https://api.anthropic.com",  # home-host upstream (mock in e2e tests)
    "ANTHROPIC_API_KEY": None,    # home-host only
}


def load(required=()):
    file_cfg = {}
    if CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"could not parse {CONFIG_FILE}: {e}")

    cfg = {}
    for key, default in DEFAULTS.items():
        value = os.environ.get(key)
        if value is None:
            value = file_cfg.get(key, default)
        if value is not None and default is not None and not isinstance(default, str):
            value = type(default)(value)
        cfg[key] = value

    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise SystemExit(
            "missing required config: " + ", ".join(missing)
            + f" (set as env var or in {CONFIG_FILE})"
        )
    return cfg
