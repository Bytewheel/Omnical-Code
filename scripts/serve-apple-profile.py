#!/usr/bin/env python3
"""Serve the Omnical Apple configuration profile over LAN HTTP for iOS install.

iOS Safari only offers "Profile Downloaded" (Settings -> Profile Downloaded /
VPN & Device Management) when the file is served as
`application/x-apple-aspen-config`; served as a generic octet-stream the
download lands in the Files app and no install entry appears (found 2026-09-05).
Content-Type/Content-Disposition mirror RustiCal's own Apple profile route
(crates/frontend/src/routes/app_token.rs).

Serves ONLY the profile file (nothing else) on all interfaces. Meant to run
briefly on the LAN; stop with Ctrl-C or kill.
"""
import http.server
import sys
from pathlib import Path

PROFILE = Path(__file__).resolve().parent.parent / "out" / "omnical-iphone.mobileconfig"
PORT = 8917


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self, head_only=False):
        if self.path not in ("/", f"/{PROFILE.name}"):
            self.send_error(404)
            return
        data = PROFILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-apple-aspen-config; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{PROFILE.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve(head_only=True)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if not PROFILE.is_file():
        sys.exit(f"missing {PROFILE} — run scripts/make-apple-profile.py first")
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print(f"serving {PROFILE.name} ({PROFILE.stat().st_size} bytes) on 0.0.0.0:{PORT}",
              flush=True)
        httpd.serve_forever()
