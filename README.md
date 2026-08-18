# skycode ✈️💬

**Claude Code over iMessage.** Use Claude Code on a Mac with no internet — like on a plane, where free in-flight messaging tiers still deliver iMessages — by tunneling the Anthropic API through text messages to a Mac at home.

```
        THE PLANE                                      HOME
┌──────────────────────────┐               ┌──────────────────────────┐
│  Claude Code             │               │                          │
│    │ ANTHROPIC_BASE_URL  │               │                          │
│    ▼                     │   iMessage    │                          │
│  sky-client   ───────────┼──────────────▶│  home-host               │
│  (local HTTP proxy that  │  SKY1|REQ|…   │  (reads chat.db,         │
│   mimics the Anthropic   │               │   calls the real API)    │
│   API, texts requests    │◀──────────────┼──    │                   │
│   out, reassembles       │  SKY1|RSP|…   │      ▼                   │
│   replies)               │               │  api.anthropic.com       │
└──────────────────────────┘               └──────────────────────────┘
```

Claude Code runs completely unmodified — it just thinks it's talking to a slow Anthropic API.

**New here? See [USAGE.md](USAGE.md)** for a staged walkthrough from first test to in-flight use.

## How it works

- **sky-client** (offline Mac) runs a local HTTP server that mimics the Anthropic Messages API. Point Claude Code at it with `ANTHROPIC_BASE_URL`. Each `POST /v1/messages` is serialized, compressed, split into text-message-sized frames, and sent via Messages.app to your home Mac's number.
- **home-host** (online Mac) polls the Messages database for frames from your number, reassembles the request, forwards it to `api.anthropic.com` with the real API key (which never crosses the wire), and texts the response back the same way.
- sky-client reassembles the response and — since Claude Code asks for a streaming response — replays it as a synthesized SSE event stream, with keep-alive pings while the round trip is in flight.

### How it stays small

Two things are built in so a turn doesn't cost hundreds of texts:

1. **Compression** — every payload is zlib-compressed (level 9) then base64-encoded. Claude Code request bodies are extremely repetitive JSON and compress ~4–5:1.
2. **Delta sync** — Claude Code resends the *entire conversation* every turn. Both sides cache recent request bodies; after the first turn, sky-client sends only a byte-level prefix/suffix diff against a cached body (plus hashes for integrity). A turn that would be ~25 texts becomes 2–4. If the home host restarts and loses its cache, it replies `delta_base_missing` and the client transparently resends the full body.

### Wire format

One iMessage per frame, pure ASCII so nothing gets smart-quoted:

```
SKY1|REQ|a1b2c3d4|3/12|<base64 chunk>
```

`REQ` goes client→host; `RSP`/`ERR` come back with the same message id, so concurrent requests correlate correctly. Anything that isn't a `SKY1|` frame is ignored — you can still text the other number normally.

## Requirements

- Two Macs, each signed into Messages with a **different** Apple ID / phone number that can iMessage each other.
- Python 3.9+ (stock macOS Python works; stdlib only, nothing to install).
- An Anthropic API key on the **home** Mac only.

## Setup guide: two people, two Macs

skycode is a two-player game: **Person 1** keeps a Mac online at home and pays for the API; **Person 2** is on the plane using Claude Code. (They can be the same human with two Macs — the "people" are just roles. The two Macs must be signed into **different** Apple IDs / numbers, or iMessage syncs instead of delivering.)

### Person 1 — Host (the Mac that stays home, online)

**One-time setup (~15 min):**

1. Clone the repo: `git clone https://github.com/jwlaboratory/skycode.git && cd skycode`. Stock macOS Python is fine — nothing to install.
2. Sign into Messages.app. Send a normal iMessage to Person 2's number and have them reply — if this doesn't work, nothing else will.
3. Grant **Full Disk Access**: System Settings → Privacy & Security → Full Disk Access → toggle **Terminal** ON → quit and reopen Terminal. Verify:
   ```sh
   sqlite3 -readonly ~/Library/Messages/chat.db 'SELECT count(*) FROM message;'   # prints a number = good
   ```
4. Grant **Automation**: run the line below and click **Allow** on the popup ("Terminal wants to control Messages"):
   ```sh
   osascript shared/send_message.applescript "+1PERSON2NUMBER" "skycode setup test"
   ```
5. Get an Anthropic API key — ideally a **dedicated key with a spend limit** (console.anthropic.com → Billing), since the only auth on this tunnel is "the text came from Person 2's number."

**Every time Person 2 flies:**

```sh
cd skycode
export ANTHROPIC_API_KEY=sk-ant-...
export SKY_PEER_NUMBER=+1PERSON2NUMBER
caffeinate -s zsh -c 'while true; do python3 home-host/main.py; sleep 5; done'
```

Leave the Mac plugged in, on Wi-Fi, Messages signed in, auto-updates off. `caffeinate -s` keeps it awake; the loop auto-restarts the host if it ever dies (a restart is harmless — the next request just auto-resends in full). You'll see a log line per request, e.g. `[home-host] a1b2c3d4: delta req 248B -> 200 in 1.0s`. Your Messages thread with Person 2 will fill with `SKY1|...` gibberish — that's the tunnel.

### Person 2 — User (on the plane)

**One-time setup (~10 min), done at home before flying:**

1. Clone the repo and do the same **Full Disk Access** (step 3) and **Automation** (step 4, using Person 1's number) grants as above, on your laptop.
2. **Dress rehearsal over real iMessage** — do the exact flight-day steps below while still on home Wi-Fi, with Person 1's host running. Don't skip this: it's the full production path, and it catches number typos, permission gaps, and Apple ID issues while you can still fix them.

**Flight day:**

1. Buy/enable the in-flight Wi-Fi **free messaging tier** and confirm a normal iMessage to Person 1 actually delivers.
2. Start the tunnel:
   ```sh
   cd skycode
   export SKY_PEER_NUMBER=+1PERSON1NUMBER
   python3 sky-client/main.py
   ```
3. In a **second terminal**, start Claude Code pointed at the tunnel:
   ```sh
   ANTHROPIC_BASE_URL=http://localhost:8377 ANTHROPIC_API_KEY=skycode-dummy claude
   ```
   (The dummy key just satisfies Claude Code's startup check — it never reaches Anthropic; Person 1's real key is used at home and never crosses the wire.)
4. Warm up with "say hi", then work normally. Expect minutes per turn — every tool call Claude makes is its own iMessage round trip — so batched, specific prompts beat rapid back-and-forth. Regular `claude` sessions in other terminals (without `ANTHROPIC_BASE_URL`) still use the internet directly and are unaffected.

If turns time out: raise `SKY_RESPONSE_TIMEOUT` (default 600s), and if the carrier seems to drop bursts of texts, raise `SKY_SEND_DELAY` / lower `SKY_CHUNK_SIZE`.

### Config

Env vars first, `~/.skycode.json` as fallback:

| Variable | Side | Default | Meaning |
|---|---|---|---|
| `SKY_PEER_NUMBER` | both | — | The other Mac's iMessage number or email |
| `ANTHROPIC_API_KEY` | home | — | Real API key (never sent over iMessage) |
| `SKY_PORT` | plane | `8377` | Local HTTP port |
| `SKY_POLL_INTERVAL` | both | `1.0` | Seconds between chat.db polls |
| `SKY_CHUNK_SIZE` | both | `1200` | Payload chars per text message |
| `SKY_SEND_DELAY` | both | `0.7` | Seconds between sending chunks |
| `SKY_RESPONSE_TIMEOUT` | plane | `600` | How long to wait for a reply |
| `SKY_STALE_SECS` | both | `120` | Drop half-received messages after this |
| `SKY_UPSTREAM_URL` | home | `https://api.anthropic.com` | Override for e2e tests |

## Testing

Three tiers, from fastest to most real:

**1. Unit + in-process e2e** (no Mac APIs at all):

```sh
python3 -m unittest discover -s tests
```

This includes `tests/test_e2e_loopback.py`, which spawns the *real* sky-client and home-host processes wired together through a file-spool transport and a mock Anthropic upstream — the entire pipeline (HTTP → framing → compression → delta sync → SSE synthesis) minus only iMessage itself. No API key needed.

**2. One-Mac loopback against the real API** (still no iMessage):

```sh
# terminal 1
ANTHROPIC_API_KEY=sk-ant-... python3 home-host/main.py --transport file
# terminal 2
python3 sky-client/main.py --transport file
# terminal 3
ANTHROPIC_BASE_URL=http://localhost:8377 ANTHROPIC_API_KEY=skycode-dummy claude
```

**3. One-Mac real-iMessage self-test** (experimental): set `SKY_PEER_NUMBER` to your **own** number on both sides and add `--include-from-me`, and the frames genuinely travel through iMessage back to the same machine. Echo loops can't happen because each side only consumes the frame kinds addressed to it.

## Limitations

- A turn takes minutes: chunk pacing + delivery latency + generation time. Bring patience (you're on a plane anyway).
- First turn of a big conversation can be ~25+ texts; carriers or free messaging tiers may throttle bursts.
- Trust is caller-ID only. iMessage sender spoofing is hard but this is still a hobby-grade trust model — use a scoped/disposable API key.
- Both Messages histories will fill with `SKY1|...` gibberish (it's your conversation, compressed).
- No request cancellation — pressing Esc in Claude Code doesn't stop the home host mid-request.
- `count_tokens` is answered locally with a ~4-chars-per-token heuristic (only affects the context display).
- Images/PDFs in the conversation balloon payloads enormously.

## Future ideas

- **Smarter compression**: zstd with a pre-shared dictionary trained on Claude Code's system prompt and tool schemas, on top of (or replacing) zlib.
- **Persistent delta cache**: write the body cache to disk on the home host so restarts almost never trigger the full-body fallback.
- **Attachment transport**: send the payload as a single iMessage *file attachment* instead of dozens of text frames — much larger size ceiling.
- **ACK/retransmit**: receiver texts back `SKY1|ACK|<id>|missing=3,7` so lost chunks get re-sent instead of the whole request timing out.
- **Multiplexed sessions**: message ids already correlate; add flow control so several Claude Code instances can share one phone-number pipe.
- **Payload encryption**: NaCl box with pre-shared keys so conversation content isn't readable in either Messages history / iCloud backup.
- **Satellite mode**: relay through an iPhone using Messages via satellite — Claude Code from a mountaintop.
- **Response cache**: key idempotent retries by request hash so a re-sent request doesn't re-bill.
