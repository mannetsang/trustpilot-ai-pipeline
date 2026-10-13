"""Password mode: AUTH_DISABLED=1 with DASHBOARD_PASSWORD set (the live deployment since
2026-10-13). The page asks for the shared password once, /api/login answers with a signed cookie,
everyone who has it is AP, and wrong passwords are throttled."""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase  # noqa: E402  (sets the env and imports main)
import main  # noqa: E402

PASSWORD = "test-pass-4321"
PAGE = {"X-Requested-With": "XMLHttpRequest"}     # what the page sends on every call


class PasswordAuthTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        main._login_failures.clear()
        self.patches = [mock.patch.object(main, "DASHBOARD_PASSWORD", PASSWORD),
                        mock.patch.object(main, "PASSWORD_MODE", True)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def login(self, password, headers=None, **kwargs):
        h = dict(PAGE)
        h.update(headers or {})
        return self.client.post("/api/login", json={"password": password}, headers=h, **kwargs)

    def test_locked_until_the_password_is_given(self):
        self.assertEqual(self.client.get("/api/config").get_json()["auth"], "password")
        self.assertEqual(self.client.get("/health?deep=1").get_json()["checks"]["auth"]["mode"], "password")
        self.assertEqual(self.client.get("/").status_code, 200)       # the page itself shows the password box
        r = self.client.get("/api/me")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"], "enter the dashboard password")
        self.assertEqual(self.client.get("/api/months").status_code, 401)
        self.assertEqual(self.client.get("/api/transactions?year=2026&month=9").status_code, 401)
        self.assertEqual(self.upload().status_code, 401)
        self.assertEqual(self.client.post("/api/jobs?kind=match&year=2026&month=9", headers=PAGE).status_code, 401)

        r = self.login("nope")
        self.assertEqual((r.status_code, r.get_json()["error"]), (401, "wrong password"))
        self.assertNotIn("Set-Cookie", r.headers)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

        r = self.login(PASSWORD)
        self.assertEqual(r.status_code, 200, r.get_json())
        cookie = r.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("ap_recon_session="), cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Max-Age=2592000", cookie)
        self.assertNotIn("Secure", cookie)          # only on Cloud Run (K_SERVICE), where TLS ends at the front end
        me = self.client.get("/api/me", headers={"X-Dashboard-User": "Kelly"}).get_json()
        self.assertEqual((me["email"], me["role"]), ("Kelly", "ap"))
        self.assertEqual(self.client.get("/api/me").get_json()["email"], "anonymous")

        # Identity rides on the cookie now, so changes need the page's header, as behind IAP.
        self.assertEqual(self.upload().status_code, 403)
        r = self.client.post("/api/statements", data={"file": (__import__("io").BytesIO(self.xlsx), "t.xlsx")},
                             content_type="multipart/form-data", headers=PAGE)
        self.assertEqual(r.status_code, 200, r.get_json())
        ids = self.ids()
        r = self.client.patch(f"/api/transactions/{ids['anth']}", json={"cost_center": "Marketing"})
        self.assertEqual(r.status_code, 403)
        r = self.client.patch(f"/api/transactions/{ids['anth']}", json={"cost_center": "Marketing"},
                              headers={**PAGE, "X-Dashboard-User": "Kelly"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["transaction"]["updated_by"], "Kelly")

    def test_secure_cookie_on_cloud_run(self):
        with mock.patch.dict(os.environ, {"K_SERVICE": "ap-reconciliation"}):
            r = self.login(PASSWORD)
        self.assertIn("Secure", r.headers["Set-Cookie"])

    def test_login_input_handling(self):
        self.assertEqual(self.login(" " + PASSWORD + "\n").status_code, 200)     # pasted with whitespace
        self.assertEqual(self.login("").status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={}, headers=PAGE).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": None}, headers=PAGE).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": 4321}, headers=PAGE).status_code, 401)
        self.assertEqual(self.client.post("/api/login", data="junk", headers=PAGE).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json=["x"], headers=PAGE).status_code, 401)
        # The login itself must come from the page: a cross-site form cannot set the header.
        r = self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("Set-Cookie", r.headers)
        self.assertEqual(self.client.get("/api/login").status_code, 405)

    def test_session_token_is_signed_and_expires(self):
        token = main.session_token()
        self.assertTrue(main.session_ok(token))
        self.assertRegex(token, r"^\d+\.[0-9a-f]{64}$")
        issued, _, sig = token.partition(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        self.assertFalse(main.session_ok(f"{issued}.{flipped}"))
        self.assertFalse(main.session_ok(f"{int(issued) + 1}.{sig}"))
        for bad in ("", None, "abc", "123.", ".abc", "123", "x.y", issued, sig):
            self.assertFalse(main.session_ok(bad), bad)
        self.assertFalse(main.session_ok(main.session_token(time.time() - main.SESSION_DAYS * 86400 - 10)))
        self.assertTrue(main.session_ok(main.session_token(time.time() - main.SESSION_DAYS * 86400 + 60)))
        self.assertFalse(main.session_ok(main.session_token(time.time() + 3600)))      # from the future
        self.assertTrue(main.session_ok(main.session_token(time.time() + 60)))         # clock skew
        # Changing the password (or the Supabase key the signature also depends on) signs everyone out.
        with mock.patch.object(main, "DASHBOARD_PASSWORD", "another"):
            self.assertFalse(main.session_ok(token))
        with mock.patch.object(main, "SUPABASE_SERVICE_KEY", "rotated"):
            self.assertFalse(main.session_ok(token))
        # The password itself is not the cookie, and the signature is not a hash of it.
        self.assertNotIn(PASSWORD, token)

        self.client.set_cookie("ap_recon_session", "1.deadbeef")
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.client.set_cookie("ap_recon_session", f"{issued}.{flipped}")
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.client.set_cookie("ap_recon_session", token)
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_logout_clears_the_cookie(self):
        self.login(PASSWORD)
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.assertEqual(self.client.post("/api/logout").status_code, 403)       # needs the page header too
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        r = self.client.post("/api/logout", headers=PAGE)
        self.assertEqual(r.status_code, 200)
        self.assertIn("ap_recon_session=;", r.headers["Set-Cookie"])
        self.assertIn("Max-Age=0", r.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_wrong_passwords_are_throttled_per_address(self):
        for _ in range(main.LOGIN_MAX_FAILURES):
            self.assertEqual(self.login("bad").status_code, 401)
        r = self.login("bad")
        self.assertEqual((r.status_code, r.get_json()["error"]), (429, "too many wrong passwords; wait 15 minutes and try again"))
        self.assertEqual(self.login(PASSWORD).status_code, 429)          # the right one waits too
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        # A made-up X-Forwarded-For does not dodge the count: Cloud Run's front end appends the real
        # address last, and that is the one counted (here the test client's 127.0.0.1).
        self.assertEqual(self.login("bad", headers={"X-Forwarded-For": "8.8.8.8, 127.0.0.1"}).status_code, 429)
        # Another real address is not affected.
        other = {"X-Forwarded-For": "10.0.0.5, 203.0.113.9"}
        self.assertEqual(self.login("bad", headers=other).status_code, 401)
        self.assertEqual(self.login(PASSWORD, headers=other).status_code, 200)
        self.assertNotIn("203.0.113.9", main._login_failures)            # a success clears that address
        # Once the window has passed the first address may try again, and a success clears it.
        with main._login_lock:
            for addr in main._login_failures:
                main._login_failures[addr] = [t - main.LOGIN_WINDOW - 1 for t in main._login_failures[addr]]
        self.assertEqual(self.login(PASSWORD).status_code, 200)
        self.assertEqual(main._login_failures, {})

    def test_wrong_passwords_are_throttled_overall(self):
        with mock.patch.object(main, "LOGIN_MAX_FAILURES_TOTAL", 3):
            for i in range(3):
                self.assertEqual(self.login("bad", headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code, 401)
            r = self.login(PASSWORD, headers={"X-Forwarded-For": "198.51.100.99"})
        self.assertEqual(r.status_code, 429)

    def test_without_the_password_the_other_modes_are_unchanged(self):
        with mock.patch.object(main, "PASSWORD_MODE", False), mock.patch.object(main, "DASHBOARD_PASSWORD", ""):
            self.assertEqual(self.client.get("/api/config").get_json()["auth"], "open")
            self.assertEqual(self.client.get("/api/me").status_code, 200)
            r = self.client.post("/api/login", json={"password": "anything"})
            self.assertEqual((r.status_code, r.get_json()), (200, {"ok": True, "auth": "open"}))
            self.assertNotIn("Set-Cookie", r.headers)
            self.assertFalse(main.password_matches("anything"))
            self.assertFalse(main.password_matches(""))
        with mock.patch.object(main, "AUTH_DISABLED", False), mock.patch.object(main, "IAP_AUDIENCE", "/projects/x"):
            self.assertEqual(main.auth_mode(), "iap")
            self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_the_weekly_job_needs_no_password(self):
        """`python main.py remind` runs without a request, so nothing asks it for the cookie."""
        self.assertEqual(main.remind_latest(), {"reminded": []})
        self.assertEqual(main.declared_user(), "anonymous")


if __name__ == "__main__":
    unittest.main()
