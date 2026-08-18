"""Synthesize an Anthropic SSE event stream from a complete Message JSON.

Claude Code requests stream=true; the iMessage leg is non-streaming, so once
the full response arrives we replay it as the exact event sequence the real
API produces: message_start -> per block (content_block_start, deltas,
content_block_stop) -> message_delta -> message_stop.
"""

import json

TEXT_DELTA_SIZE = 1024


def event(name, obj):
    return f"event: {name}\ndata: {json.dumps(obj, separators=(',', ':'))}\n\n".encode("utf-8")


def ping():
    return event("ping", {"type": "ping"})


def error(err_obj):
    return event("error", {"type": "error", "error": err_obj})


def synthesize(message):
    start = dict(message)
    start["content"] = []
    start["stop_reason"] = None
    start["stop_sequence"] = None
    yield event("message_start", {"type": "message_start", "message": start})

    for i, block in enumerate(message.get("content") or []):
        btype = block.get("type")
        if btype == "text":
            yield _block_start(i, {"type": "text", "text": ""})
            text = block.get("text", "")
            for j in range(0, len(text), TEXT_DELTA_SIZE):
                yield _block_delta(i, {"type": "text_delta", "text": text[j:j + TEXT_DELTA_SIZE]})
        elif btype == "thinking":
            yield _block_start(i, {"type": "thinking", "thinking": ""})
            thinking = block.get("thinking", "")
            for j in range(0, len(thinking), TEXT_DELTA_SIZE):
                yield _block_delta(i, {"type": "thinking_delta", "thinking": thinking[j:j + TEXT_DELTA_SIZE]})
            # signature_delta is required so the client can replay the
            # thinking block in the next request.
            yield _block_delta(i, {"type": "signature_delta", "signature": block.get("signature", "")})
        elif btype == "tool_use":
            yield _block_start(i, {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": {},
            })
            yield _block_delta(i, {
                "type": "input_json_delta",
                "partial_json": json.dumps(block.get("input", {}), separators=(",", ":")),
            })
        else:
            # Unknown block types (e.g. redacted_thinking) are emitted whole
            # in the start event with no deltas.
            yield _block_start(i, block)
        yield event("content_block_stop", {"type": "content_block_stop", "index": i})

    usage = message.get("usage") or {}
    yield event("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason"),
            "stop_sequence": message.get("stop_sequence"),
        },
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield event("message_stop", {"type": "message_stop"})


def _block_start(index, content_block):
    return event("content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": content_block,
    })


def _block_delta(index, delta):
    return event("content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": delta,
    })
