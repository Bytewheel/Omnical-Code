#!/usr/bin/env python3
"""serve-hawkbook-apk.py — serve exactly one file: the HawkBook APK.

Why this exists: the HawkBook download is the single largest artifact this
deployment ships (18 MB), and the router's f2fs overlay is 98 MB at 95% —
so the APK no longer lives on the router's flash. It lives in
`out/hawkbook.apk` on THIS host; `router/etc/init.d/rustical` fetches it
into the router's tmpfs right before rustical starts (rustical reads the
package ONCE at startup and serves it from process memory — the file is
never touched again while the process lives). If this host is down at a
rustical (re)start, the fetch silently fails and rustical degrades the
way it already does for an unreadable package: the download route 404s
and the profile intro omits the link and its checksum.

Deliberately NOT `python3 -m http.server --directory out/`: out/ also
holds the LIVE TLS key and must never be listable. This server answers
one path (`/hawkbook.apk`) and nothing else — the APK is a public build
artifact served unauthenticated by rustical too, so no secret crosses
this port.

Run by the user crontab (`@reboot`) and by hand after a new APK build:
    scripts/serve-hawkbook-apk.py
"""

import http.server
import os
import sys

APK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "out", "hawkbook.apk"
)
BIND = ("0.0.0.0", 8123)


class OneFileHandler(http.server.BaseHTTPRequestHandler):
    def _serve(self, head_only: bool) -> None:
        if self.path != "/hawkbook.apk":
            self.send_error(404)
            return
        try:
            with open(APK_PATH, "rb") as apk:
                stat = os.fstat(apk.fileno())
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.android.package-archive")
                self.send_header("Content-Length", str(stat.st_size))
                self.end_headers()
                if not head_only:
                    while chunk := apk.read(64 * 1024):
                        self.wfile.write(chunk)
        except OSError:
            # No APK built yet (or unreadable): the honest answer is 404,
            # same as rustical's own download route in that state.
            self.send_error(404)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def log_message(self, *_args: object) -> None:
        # Cron runs this with output to /dev/null anyway; keep it quiet.
        pass


if not os.path.isfile(APK_PATH):
    print(f"!! {APK_PATH} does not exist — build the APK first", file=sys.stderr)
    sys.exit(1)

http.server.ThreadingHTTPServer(BIND, OneFileHandler).serve_forever()
