#!/usr/bin/env python3
"""Serve this host's watchdog heartbeat to its peer, over the tailnet only.

The two boxes watch each other. Each health check writes a heartbeat when it
runs; each box reads its peer's over Tailscale and alerts when the peer has gone
quiet. That covers the one case ``OnFailure=`` cannot: a check that never runs at
all, because the timer was disabled (2026-08-04, six days unnoticed) or the box is
off.

BINDS TO THE TAILNET ADDRESS, NOT 0.0.0.0. The bind address is resolved from
`tailscale ip -4` and a failure to resolve it is fatal rather than a fallback to
all interfaces. hostinger has a public IP, and a heartbeat endpoint answering on
it would be a new public surface added by a monitoring tool -- exactly the kind of
quiet mistake this repo exists to avoid.

The payload carries no secrets: a host label, the unit, a timestamp, and the last
verdict. Anyone on the tailnet may read it, which is the intended audience.
"""
from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import subprocess
import sys

DEFAULT_PORT = 8299
PATH = "/heartbeat"


def tailnet_ip() -> str:
    """This host's tailnet address. Empty string when Tailscale cannot answer."""
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                           text=True, timeout=15)
        return (r.stdout or "").strip().splitlines()[0].strip()
    except Exception:
        return ""


def make_handler(hb_path: pathlib.Path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                  # noqa: N802
            if self.path.rstrip("/") != PATH:
                self.send_error(404)
                return
            try:
                body = hb_path.read_bytes()
            except Exception as e:
                # 503, not 200-with-empty: the peer must be able to tell "no
                # heartbeat yet" from "here is a heartbeat", and a soft empty
                # body would read as the latter.
                self.send_error(503, f"no heartbeat ({type(e).__name__})")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass                                           # journal noise, 4x/day

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", required=True, help="heartbeat json written by the check")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bind", default="", help="override the tailnet bind address")
    args = ap.parse_args()

    bind = args.bind or tailnet_ip()
    if not bind:
        print("REFUSING TO START: no tailnet address from `tailscale ip -4`. "
              "Binding 0.0.0.0 would expose this on hostinger's public IP.",
              file=sys.stderr)
        return 1

    srv = http.server.ThreadingHTTPServer((bind, args.port),
                                          make_handler(pathlib.Path(args.file).expanduser()))
    print(f"heartbeat serving http://{bind}:{args.port}{PATH} from {args.file}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
