"""Everything this service used to serve now lives on the group dashboard.

The Shopping dashboard was a second URL for the same business, and one of its
five tabs duplicated the group page. Rather than leave a stale copy running,
the service redirects: links already shared keep working, and there is one
page to bookmark.

The build that produced it has not gone anywhere - group-dashboard/collect.py
calls google-merchant/dashboard/build_data.py for the .ca feed, which is the
Shopping feed tab there.
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = os.environ.get("REDIRECT_TO", "https://group-dashboard-304363458561.us-central1.run.app/")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(301)
        self.send_header("Location", TARGET + "#feed")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(
            f'<!doctype html><meta charset="utf-8"><title>Moved</title>'
            f'<p style="font:15px system-ui;margin:40px">The Shopping dashboard is now the '
            f'<b>Shopping feed</b> tab of the group dashboard. '
            f'<a href="{TARGET}#feed">Continue</a>.</p>'.encode()
        )

    def log_message(self, fmt, *args):
        print(f'{self.address_string()} "{fmt % args}"', flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
