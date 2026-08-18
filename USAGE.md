# skycode — Usage & Testing Guide

Step-by-step from "just cloned" to "using Claude Code on a plane." Each stage builds on the last, and each has a checkpoint so you know it's working before moving on.

## What's already been verified

Everything below was e2e-tested on a real machine — the entire pipeline (HTTP → framing → compression → delta sync → SSE synthesis → real Anthropic API → real Claude Code binary) works. The **only** leg that needs testing on your machines is iMessage itself (stages 3–4), because it needs your Full Disk Access + Automation permissions and your phone numbers.

| Verified | How |
|---|---|
| Protocol: chunking, out-of-order, duplicates, staleness | 27-test unit suite |
| Delta sync: 20-turn soak → 19/20 requests went as small deltas | stress run |
| 300KB request bodies | stress run |
| 6 concurrent requests, correct correlation | stress run |
| home-host restart mid-session → automatic full-body recovery | stress run |
| Streaming SSE + non-streaming against the **real API** | curl through the stack |
| **Real `claude` CLI through the full stack** (137KB request, tunneled, answered) | `claude -p` through sky-client + home-host |

## Stage 0 — sanity check (30 seconds, any Mac)

```sh
cd skycode
python3 -m unittest discover -s tests
```

**Checkpoint:** `Ran 27 tests ... OK`. This includes an e2e test that spawns the real sky-client and home-host processes against a mock API — if this passes, all the plumbing works on your machine.

## Stage 1 — real Claude Code, one Mac, no iMessage (5 minutes)

This runs the exact production setup, with the "iMessage" being a folder of frame files you can watch. Three terminals:

```sh
# terminal 1 — the "home" side (uses your real API key)
export ANTHROPIC_API_KEY=sk-ant-...
python3 home-host/main.py --transport file

# terminal 2 — the "plane" side
python3 sky-client/main.py --transport file

# terminal 3 — Claude Code, pointed at the tunnel
ANTHROPIC_BASE_URL=http://localhost:8377 ANTHROPIC_API_KEY=skycode-dummy claude
```

Ask Claude Code something. It works normally — just via the tunnel.

**Checkpoints:**
- Terminal 1 logs each request: `[home-host] a1b2c3d4: full req 137785B -> 200 in 1.9s`
- Your **second** message in the same session should log `delta req` with a much smaller byte count — that's delta sync working.
- Fun: `ls /tmp/sky-spool/to_host/` mid-request to see the frames that would be iMessages.

## Stage 2 — iMessage permissions (one-time, on BOTH Macs)

1. **Full Disk Access** (to read the Messages database): System Settings → Privacy & Security → Full Disk Access → enable your terminal app. **Restart the terminal.** Verify:
   ```sh
   python3 -c "import sqlite3; sqlite3.connect('file:' + __import__('os').path.expanduser('~/Library/Messages/chat.db') + '?mode=ro', uri=True).execute('SELECT 1'); print('chat.db readable — OK')"
   ```
2. **Automation permission** (to send via Messages.app) — this pops up on first send; trigger it now instead of mid-flight:
   ```sh
   osascript shared/send_message.applescript "+1YOURNUMBER" "skycode permission test"
   ```
   Click **Allow** on the "Terminal wants to control Messages" prompt. The text should appear in Messages.

## Stage 3 — real iMessage on ONE Mac (self-texting test)

Prove the actual iMessage leg without needing the second Mac yet. Frames genuinely leave through Messages and come back to the same machine (echo loops can't happen — each side only reads the frame kinds addressed to it):

```sh
# terminal 1
export ANTHROPIC_API_KEY=sk-ant-...
export SKY_PEER_NUMBER=+1YOUROWNNUMBER      # your own number/Apple ID
python3 home-host/main.py --include-from-me

# terminal 2
export SKY_PEER_NUMBER=+1YOUROWNNUMBER
python3 sky-client/main.py --include-from-me

# terminal 3
ANTHROPIC_BASE_URL=http://localhost:8377 ANTHROPIC_API_KEY=skycode-dummy claude -p "say hi"
```

**Checkpoint:** `SKY1|REQ|...` texts appear in Messages, then `SKY1|RSP|...` texts, then Claude answers. This validates chat.db decoding + sending on your macOS version — the two things that couldn't be pre-verified for you.

Expect this to be slow (~0.7s per frame sent, 1s poll) — a first turn is a couple minutes. That's the price of the transport, not a bug.

## Stage 4 — the real thing (two Macs)

**Home Mac** (stays online; do Stage 2 permissions here too):

```sh
export ANTHROPIC_API_KEY=sk-ant-...
export SKY_PEER_NUMBER=+1PLANE_MAC_NUMBER
python3 home-host/main.py
```

**Plane Mac:**

```sh
export SKY_PEER_NUMBER=+1HOME_MAC_NUMBER
python3 sky-client/main.py
# then, in another terminal:
ANTHROPIC_BASE_URL=http://localhost:8377 ANTHROPIC_API_KEY=skycode-dummy claude
```

The two Macs must be on **different Apple IDs** that can iMessage each other. Do a full dry run at home over normal Wi-Fi before flying.

### Pre-flight checklist

- [ ] Dry run from Stage 4 worked at home
- [ ] home-host is running and will stay awake: `caffeinate -s python3 home-host/main.py`
- [ ] Home Mac on power + reliable Wi-Fi, Messages signed in
- [ ] Plane Mac: Full Disk Access + Automation already granted (Stage 2)
- [ ] In-flight Wi-Fi tier that includes messaging; confirm a normal iMessage sends
- [ ] Start with a trivial prompt ("say hi") before a real coding task

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `cannot open .../chat.db` on startup | No Full Disk Access, or terminal not restarted after granting it |
| `osascript send failed: ... not authorized` | Automation permission denied — System Settings → Privacy & Security → Automation → your terminal → enable Messages |
| Sends work, replies never arrive | Other side isn't running, wrong `SKY_PEER_NUMBER` (it's matched on the **sender** of incoming texts — use the number/email Messages actually shows), or its FDA is missing |
| `504 ... SKY_RESPONSE_TIMEOUT` | Round trip exceeded 10 min — big first turn + slow delivery. Raise `SKY_RESPONSE_TIMEOUT`, or lower `SKY_CHUNK_SIZE`/raise `SKY_SEND_DELAY` if the carrier is dropping bursts |
| `delta base missing` in home-host log | Normal after a home-host restart — the next request auto-resends in full |
| Claude Code shows a connectors warning | Harmless — `ANTHROPIC_API_KEY=skycode-dummy` takes precedence over claude.ai login for that session |
| Messages full of `SKY1\|` gibberish | Working as intended ✈️ |

## Knobs

All env vars (see README for the full table). The ones you'll actually touch:

- `SKY_CHUNK_SIZE` (default 1200) — smaller = more, safer texts; larger = fewer texts
- `SKY_SEND_DELAY` (default 0.7s) — raise if the carrier drops bursts
- `SKY_RESPONSE_TIMEOUT` (default 600s) — raise for long generations on slow links
