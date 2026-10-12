"""Sign-in through Identity-Aware Proxy: the app trusts nothing but the signed
header IAP adds (ES256 JWT, IAP's keys, this service as audience) and still
applies its own domain and role rules on top of IAP's access list."""
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase  # noqa: E402  (sets the env and imports main)
import main  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from google.auth import jwt  # noqa: E402
from google.auth.crypt import es256  # noqa: E402

AUDIENCE = "/projects/304363458561/locations/us-central1/services/ap-reconciliation"


class IapAuthTests(ApiTestCase):
    @classmethod
    def setUpClass(cls):
        key = ec.generate_private_key(ec.SECP256R1())
        private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
        public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cls.private_pem, cls.public_pem = private_pem, public_pem
        cls.signer = es256.ES256Signer.from_string(private_pem, key_id="iap-test-key")
        cls.certs = {"iap-test-key": public_pem}

    def setUp(self):
        super().setUp()
        main._token_cache.clear()
        main._iap_keys.update({"fetched": 0.0, "keys": {}})
        self.fetches = mock.Mock(return_value=self.certs)
        self.patches = [mock.patch.object(main, "AUTH_DISABLED", False),
                        mock.patch.object(main, "IAP_AUDIENCE", AUDIENCE),
                        mock.patch.object(main, "fetch_iap_keys", self.fetches)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def assertion(self, email="manne@superhairpieces.com", **claims):
        now = int(time.time())
        payload = {"iss": "https://cloud.google.com/iap", "aud": AUDIENCE, "sub": "accounts.google.com:1",
                   "email": email, "hd": email.rsplit("@", 1)[-1], "iat": now, "exp": now + 600}
        payload.update(claims)
        return jwt.encode(self.signer, payload).decode()

    def me(self, token=None):
        headers = {"X-Goog-IAP-JWT-Assertion": token} if token else {}
        return self.client.get("/api/me", headers=headers)

    PAGE = {"X-Requested-With": "XMLHttpRequest"}     # what the page sends on every call

    def test_without_the_iap_header_nothing_but_the_page_and_config_answers(self):
        r = self.me()
        self.assertEqual((r.status_code, r.get_json()["error"]), (401, "sign in with your superhairpieces.com Google account"))
        self.assertEqual(self.client.get("/api/months").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 200)
        config = self.client.get("/api/config").get_json()
        self.assertEqual((config["auth"], config["auth_disabled"]), ("iap", False))
        self.assertNotIn("client_id", config)
        deep = self.client.get("/health?deep=1").get_json()
        self.assertEqual(deep["checks"]["auth"], {"mode": "iap", "audience": AUDIENCE})

    def test_signed_assertion_gives_the_role_from_the_email(self):
        r = self.me(self.assertion())
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()["email"], r.get_json()["role"]), ("manne@superhairpieces.com", "ap"))
        r = self.me(self.assertion("Yin.Wei@superhairpieces.com"))
        self.assertEqual((r.get_json()["email"], r.get_json()["role"], r.get_json()["name"]),
                         ("yin.wei@superhairpieces.com", "member", ""))

    def test_assertions_that_do_not_fit_are_rejected(self):
        bad = {
            "other audience": self.assertion(aud="/projects/1/locations/us-central1/services/other"),
            "other issuer": self.assertion(iss="https://accounts.google.com"),
            "expired": self.assertion(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600),
            "outside the domain": self.assertion("someone@gmail.com"),
            "no e-mail": self.assertion(email=""),
            "not a token": "x.y.z",
        }
        with mock.patch("builtins.print"):
            for why, token in bad.items():
                self.assertEqual(self.me(token).status_code, 401, why)
        # A key IAP does not publish: the signature cannot be checked.
        main._iap_keys.update({"fetched": 0.0, "keys": {}})
        with mock.patch.object(main, "fetch_iap_keys", return_value={}), mock.patch("builtins.print"):
            self.assertEqual(self.me(self.assertion()).status_code, 401)

    def test_verified_assertions_are_cached_until_they_expire(self):
        token = self.assertion()
        with mock.patch.object(main, "verify_iap_jwt", wraps=main.verify_iap_jwt) as verify:
            self.assertEqual(self.me(token).status_code, 200)
            self.assertEqual(self.me(token).status_code, 200)
            self.assertEqual(verify.call_count, 1)
        # Cached means cached with its role: an AP address stays AP without a second check.
        self.assertEqual(self.me(token).get_json()["role"], "ap")

    def test_iap_keys_are_fetched_once_and_refreshed_for_an_unknown_key_id(self):
        for _ in range(3):
            self.assertEqual(self.me(self.assertion()).status_code, 200)   # three distinct assertions
        self.assertEqual(self.fetches.call_count, 1)
        other = es256.ES256Signer.from_string(self.private_pem, key_id="iap-rotated-key")
        now = int(time.time())
        rotated = jwt.encode(other, {"iss": "https://cloud.google.com/iap", "aud": AUDIENCE, "sub": "s",
                                     "email": "manne@superhairpieces.com", "iat": now, "exp": now + 600}).decode()
        with mock.patch("builtins.print"):
            self.assertEqual(self.me(rotated).status_code, 401)      # unknown kid, and the cooldown holds
        self.assertEqual(self.fetches.call_count, 1)
        main._iap_keys["fetched"] -= main.IAP_KEYS_COOLDOWN + 1
        self.fetches.return_value = {**self.certs, "iap-rotated-key": self.public_pem}
        self.assertEqual(self.me(rotated).status_code, 200)          # refreshed early for the new key
        self.assertEqual(self.fetches.call_count, 2)

    def test_changes_behind_iap_must_come_from_the_page(self):
        ap = {"X-Goog-IAP-JWT-Assertion": self.assertion()}
        self.assertEqual(self.client.post("/api/index?year=2026&month=9", headers=ap).status_code, 403)
        self.assertEqual(self.client.post("/api/match?year=2026&month=9", headers=ap).status_code, 403)
        self.assertEqual(self.client.post("/api/index?year=2026&month=9", headers={**ap, **self.PAGE}).status_code, 200)
        self.assertEqual(self.client.get("/api/months", headers=ap).status_code, 200)   # reads need no header

    def test_without_an_audience_no_assertion_is_trusted(self):
        with mock.patch.object(main, "IAP_AUDIENCE", ""):
            self.assertEqual(self.me(self.assertion()).status_code, 401)

    def test_member_rules_still_apply_behind_iap(self):
        self.assertEqual(self.upload(query="?notify=0").status_code, 401)           # anonymous
        ap = {"X-Goog-IAP-JWT-Assertion": self.assertion()}
        member = {"X-Goog-IAP-JWT-Assertion": self.assertion("x@superhairpieces.com")}
        r = self.client.post("/api/statements?notify=0", headers={**ap, **self.PAGE},
                             data={"file": (__import__("io").BytesIO(self.xlsx), "Transaction Export.xlsx")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.client.post("/api/cards", headers={**member, **self.PAGE}, json={"last4": "1610", "label": "mine"}).status_code, 403)
        self.assertEqual(self.client.get("/api/months", headers=member).status_code, 200)


if __name__ == "__main__":
    unittest.main()
