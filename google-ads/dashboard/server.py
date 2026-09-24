"""Tiny static server for Cloud Run: serves site/ on $PORT with sane caching."""
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):  # keep Cloud Run logs to one line per request
        print(f'{self.address_string()} "{fmt % args}"', flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), partial(Handler, directory=SITE)).serve_forever()
