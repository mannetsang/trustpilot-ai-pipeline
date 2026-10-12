"""An owner on plain Gmail (no Google Workspace): GOOGLE_CHAT=0 and no hosted-domain limit on sign-in."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

os.environ["STORE_BACKEND"] = "memory"  # as the other tests: no real database
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cloud_run")
sys.path.insert(0, SRC)
import tools  # noqa: E402
from store import MemoryStore  # noqa: E402
from tools import Toolset  # noqa: E402


def scopes_with(env):
    out = subprocess.run([sys.executable, "-c", "import json, google_apis; print(json.dumps(google_apis.ASSISTANT_SCOPES))"],
                         cwd=SRC, env={**os.environ, **env}, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class WithoutGoogleChatTests(unittest.TestCase):
    def test_no_chat_or_directory_permissions_are_asked_for(self):
        on, off = scopes_with({"GOOGLE_CHAT": "1"}), scopes_with({"GOOGLE_CHAT": "0"})
        self.assertTrue(any("/auth/chat." in s for s in on))
        self.assertFalse(any("/auth/chat." in s or "/auth/directory." in s for s in off))
        for kept in ("calendar.events", "gmail.readonly", "drive.readonly", "userinfo.email"):
            self.assertTrue(any(s.endswith(kept) for s in off), kept)

    def test_chat_tools_are_not_offered(self):
        names = lambda: {s["name"] for s in Toolset(MemoryStore(), None).specs()}  # noqa: E731
        self.assertIn("send_chat_message", names())
        with mock.patch.object(tools, "GOOGLE_CHAT", False):
            self.assertFalse(names() & tools.CHAT_TOOLS)
            self.assertIn("list_calendar", names())

    def test_the_hourly_run_skips_reading_chats(self):
        import main
        import worker

        with mock.patch.object(main, "GOOGLE_CHAT", False), \
                mock.patch.object(main.assistant, "run") as read_chats, \
                mock.patch.object(main.app, "config", {**main.app.config, "MAKE_GOOGLE": lambda: object()}), \
                mock.patch.object(main, "store", MemoryStore()), \
                mock.patch.object(worker, "work_due", return_value=[]), \
                mock.patch.object(main.integrations, "check_all", return_value={}), \
                mock.patch.object(main, "talk_service") as service:
            service.return_value.check_partners.return_value = {}
            body, status = main.do_run("schedule")
        self.assertEqual(status, 200)
        read_chats.assert_not_called()
        self.assertEqual(body["spaces_processed"], 0)

    def test_gmail_sign_in_has_no_hosted_domain(self):
        import main

        self.assertEqual(main.HOSTED_DOMAIN, {"hd": "superhairpieces.com"})  # the company copy's owner
        with mock.patch.dict(os.environ, {"OWNER_EMAIL": "talenttsang@gmail.com"}):
            out = subprocess.run([sys.executable, "-c", "import main; print(main.HOSTED_DOMAIN)"], cwd=SRC,
                                 env={**os.environ, "STORE_BACKEND": "memory"}, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "{}", out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
