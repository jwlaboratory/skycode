"""sky-client entrypoint — run on the offline Mac (the one on the plane).

    python3 sky-client/main.py                      # real iMessage transport
    python3 sky-client/main.py --transport file     # loopback via spool dir
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import config
from shared.transport import FileTransport, IMessageTransport

import server


def main():
    parser = argparse.ArgumentParser(description="skycode sky-client")
    parser.add_argument("--transport", choices=["imessage", "file"], default="imessage")
    parser.add_argument("--spool", default="/tmp/sky-spool",
                        help="spool dir for --transport file")
    parser.add_argument("--include-from-me", action="store_true",
                        help="also read your own outgoing texts (single-Mac "
                             "self-texting e2e test)")
    args = parser.parse_args()

    required = ["SKY_PEER_NUMBER"] if args.transport == "imessage" else []
    cfg = config.load(required=required)

    if args.transport == "imessage":
        transport = IMessageTransport(cfg, include_from_me=args.include_from_me)
        print(f"[sky-client] transport: iMessage <-> {cfg['SKY_PEER_NUMBER']}")
    else:
        transport = FileTransport(args.spool, role="client")
        print(f"[sky-client] transport: file spool at {args.spool}")

    server.serve(cfg, transport)


if __name__ == "__main__":
    main()
