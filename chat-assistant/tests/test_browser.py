"""The browser service (src/browser): its address guard always; real-browser tests when Playwright is installed."""

import base64
import importlib.util
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "browser", "main.py")
_spec = importlib.util.spec_from_file_location("browser_main", _PATH)
browser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(browser)

HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None

PAGES = {
    "/": """<html><head><title>Test shop</title></head><body>
      <h1>Superhairpieces test</h1>
      <a href="/product">Lace wig</a>
      <button onclick="document.getElementById('msg').innerText='Added!'">Add to cart</button>
      <p id="msg">nothing yet</p>
      <form action="/search"><label>Search <input name="q" placeholder="Search products"></label></form>
      <label>Password <input type="password" name="pw"></label>
      <label>Card number <input name="cardnumber" autocomplete="cc-number"></label>
      <select id="size" aria-label="Size"><option>Small</option><option>Large</option></select>
      <a href="/product" target="_blank">Open in new tab</a>
      <button onclick="alert('Hello there')">Alert me</button>
      <div style="height:3000px"></div><p>Bottom of the page</p>
    </body></html>""",
    "/product": "<html><head><title>Lace wig</title></head><body><h1>Lace wig</h1><p>$199</p></body></html>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/search":
            body = f"<html><head><title>Results</title></head><body>Results for {self.path.split('q=')[-1]}</body></html>"
        else:
            body = PAGES.get(path, "<html><body>not found</body></html>")
        self.send_response(200 if path in PAGES or path == "/search" else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


class GuardTests(unittest.TestCase):
    def test_internal_addresses_are_blocked(self):
        for url in ("http://169.254.169.254/computeMetadata/v1/", "http://metadata.google.internal/",
                    "http://localhost:8080/", "http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.0.1/",
                    "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "http://printer.local/"):
            self.assertTrue(browser.blocked_reason(url), url)

    def test_names_resolving_to_private_addresses_are_blocked(self):
        browser._dns_cache.clear()
        with mock.patch.object(browser.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 0))]):
            self.assertIn("private", browser.blocked_reason("https://sneaky.example/"))
        with mock.patch.object(browser.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("142.250.80.46", 0))]):
            self.assertEqual(browser.blocked_reason("https://www.superhairpieces.com/"), "")
        self.assertEqual(browser.blocked_reason("data:text/html,hi"), "")

    def test_secret_fields_are_recognised(self):
        for field in ("password pw", "text cardnumber cc-number", "tel cvc", "text iban", "text exp-date expiry"):
            self.assertTrue(browser.SECRET_FIELD.search(field), field)
        for field in ("text q  Search products", "email email", "text name full-name"):
            self.assertFalse(browser.SECRET_FIELD.search(field), field)


@unittest.skipUnless(HAS_PLAYWRIGHT, "Playwright isn't installed in this environment")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.patch = mock.patch.object(browser, "blocked_reason",
                                      lambda url: "" if url.startswith(cls.base) else "blocked in tests")
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.server.shutdown()

    def run_act(self, **body):
        return browser._pool.submit(browser.act, body).result(timeout=60)

    def test_open_click_type_select_scroll_and_guards(self):
        page = self.run_act(action="open", url=self.base + "/")
        sid = page["session"]
        self.assertEqual(page["title"], "Test shop")
        self.assertTrue(base64.b64decode(page["image"]).startswith(b"\xff\xd8"))  # a JPEG
        labels = {e["label"] for e in page["elements"]}
        self.assertIn("Add to cart", labels)
        self.assertIn("Lace wig", labels)

        clicked = self.run_act(session=sid, action="click", target="Add to cart")
        self.assertIn("Added!", clicked["text"])

        with self.assertRaises(PermissionError):
            self.run_act(session=sid, action="type", target="Password", text="hunter2")
        with self.assertRaises(PermissionError):
            self.run_act(session=sid, action="type", target="Card number", text="4242424242424242")

        chosen = self.run_act(session=sid, action="select", target="Size", text="Large")
        self.assertNotIn("error", chosen)

        alert = self.run_act(session=sid, action="click", target="Alert me")
        self.assertTrue(any("dialog" in n for n in alert["notes"]))

        scrolled = self.run_act(session=sid, action="scroll", direction="down")
        self.assertGreater(scrolled["scroll"]["y"], 0)

        results = self.run_act(session=sid, action="type", target="Search products", text="toupee", submit=True)
        self.assertEqual(results["title"], "Results")
        self.assertIn("toupee", results["text"])

        back = self.run_act(session=sid, action="back")
        self.assertEqual(back["title"], "Test shop")

        product = self.run_act(session=sid, action="click", target="Lace wig")
        self.assertEqual(product["title"], "Lace wig")
        self.assertTrue(self.run_act(session=sid, action="close")["closed"])
        self.assertIn("error", self.run_act(session=sid, action="screenshot"))

    def test_new_tab_mobile_and_blocked_requests(self):
        page = self.run_act(action="open", url=self.base + "/", device="mobile")
        self.assertEqual(page["viewport"]["width"], 390)
        sid = page["session"]
        tab = self.run_act(session=sid, action="click", target="Open in new tab")
        self.assertEqual(tab["title"], "Lace wig")
        self.assertTrue(any("new tab" in n for n in tab["notes"]))
        with self.assertRaises(PermissionError):
            self.run_act(session=sid, action="goto", url="http://169.254.169.254/computeMetadata/v1/")
        self.run_act(session=sid, action="close")


if __name__ == "__main__":
    unittest.main()
