"""chat-assistant/scripts/gchat.py: Google Chat from a Claude Code session, never sending without --confirm."""

import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "cloud_run"))
from google_apis import GoogleApiError  # noqa: E402

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "gchat.py")
_spec = importlib.util.spec_from_file_location("gchat", _PATH)
gchat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gchat)


class FakeChat:
    def __init__(self):
        self.sent = []
        self.spaces = [
            {"name": "spaces/AAA", "displayName": "STC team", "spaceType": "SPACE", "lastActiveTime": "2026-10-01T10:00:00Z"},
            {"name": "spaces/BBB", "displayName": "STC orders", "spaceType": "SPACE", "lastActiveTime": "2026-10-05T10:00:00Z"},
            {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE", "lastActiveTime": "2026-10-04T10:00:00Z"},
        ]

    def list_spaces(self):
        return list(self.spaces)

    def request(self, method, url, params=None, body=None):
        if url.endswith("spaces:findDirectMessage"):
            if params["name"] == "users/sydney@superhairpieces.com":
                return {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}
            raise GoogleApiError(404, '{"error": {"message": "not found"}}', url)
        return next(s for s in self.spaces if url.endswith(s["name"]))

    def reply_in_thread(self, space_name, thread_name, text):
        self.sent.append((space_name, text))
        return {"name": f"{space_name}/messages/1", "createTime": "2026-10-05T21:00:00Z"}


class GchatTests(unittest.TestCase):
    def setUp(self):
        self.chat, self.lines = FakeChat(), []

    def test_nothing_is_sent_without_confirm(self):
        gchat.cmd_send(self.chat, "Walker Tape arrives Thursday", to="sydney@superhairpieces.com", out=self.lines.append)
        self.assertEqual(self.chat.sent, [])
        self.assertIn("Walker Tape arrives Thursday", self.lines[0])
        self.assertIn("Not sent", self.lines[-1])
        gchat.cmd_send(self.chat, "Walker Tape arrives Thursday", to="sydney@superhairpieces.com", confirm=True,
                       out=self.lines.append)
        self.assertEqual(self.chat.sent, [("spaces/DM1", "Walker Tape arrives Thursday")])

    def test_spaces_by_exact_name_partial_name_or_id(self):
        self.assertEqual(gchat.find_space(self.chat, space="stc team")["name"], "spaces/AAA")
        self.assertEqual(gchat.find_space(self.chat, space="orders")["name"], "spaces/BBB")
        self.assertEqual(gchat.find_space(self.chat, space="spaces/BBB")["name"], "spaces/BBB")
        with self.assertRaises(gchat.Stop):  # "STC" is both spaces: never guess
            gchat.find_space(self.chat, space="STC")
        with self.assertRaises(gchat.Stop):
            gchat.find_space(self.chat, space="Nowhere")

    def test_no_direct_message_yet_is_explained(self):
        with self.assertRaises(gchat.Stop) as ctx:
            gchat.find_space(self.chat, to="new.person@superhairpieces.com")
        self.assertIn("no direct message", str(ctx.exception))

    def test_empty_or_too_long_messages_are_refused(self):
        for text in ("  ", "x" * (gchat.MAX_TEXT + 1)):
            with self.assertRaises(gchat.Stop):
                gchat.cmd_send(self.chat, text, space="spaces/AAA", confirm=True, out=self.lines.append)
        self.assertEqual(self.chat.sent, [])

    def test_list_is_newest_first_and_filters(self):
        gchat.cmd_list(self.chat, out=self.lines.append)
        self.assertTrue(self.lines[0].startswith("spaces/BBB"))
        self.assertEqual(self.lines[-1], "3 conversation(s).")
        self.lines.clear()
        gchat.cmd_list(self.chat, "team", out=self.lines.append)
        self.assertEqual(self.lines[-1], "1 conversation(s).")


if __name__ == "__main__":
    unittest.main()
