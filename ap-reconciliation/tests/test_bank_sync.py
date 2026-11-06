"""The Drive import: statements put in the Bank Transactions folder (and its subfolders) are read by
`python main.py sync`, once per version, then their months are read for invoices and matched. Also
the job lease that keeps it, the weekly reminder and the dashboard's own steps from overlapping."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud_run"))

from test_pipeline import ApiTestCase, FakeDrive, build_xlsx, file_, folder, quiet, sample_rows  # noqa: E402
from test_statement_formats import EXPORT_LINES  # noqa: E402
import main  # noqa: E402
from drive import Drive, DriveError  # noqa: E402

GDOC, GSHEET = main.GOOGLE_DOC, main.GOOGLE_SHEET
NOW = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.timezone.utc)
EARLIER = "2026-11-02T12:00:00.000Z"          # three hours before NOW: settled
LATER = "2026-11-02T13:30:00.000Z"


class BankDrive(FakeDrive):
    """FakeDrive plus what the import uses: the real walk over list_children, and exports of
    Google Docs and Sheets (`exports`: {file id: bytes}); `fail`: {file id: exception} on read."""
    walk = Drive.walk

    def __init__(self, tree, exports=None, contents=None):
        super().__init__(tree, contents=contents)
        self.exports = exports or {}
        self.exported, self.fail = [], {}

    def export(self, file_id, mime_type):
        self._record("export")
        if file_id in self.fail:
            raise self.fail[file_id]
        self.exported.append((file_id, mime_type))
        return self.exports[file_id]

    def download(self, file):
        if file["id"] in self.fail:
            raise self.fail[file["id"]]
        return super().download(file)


def gdoc(fid, name, modified=EARLIER):
    return dict(file_(fid, name, GDOC, modified), webViewLink=f"https://docs.google.com/document/d/{fid}/edit")


def gsheet(fid, name, modified=EARLIER):
    return dict(file_(fid, name, GSHEET, modified), webViewLink=f"https://docs.google.com/spreadsheets/d/{fid}/edit")


class BankSyncTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.text = "\n".join(EXPORT_LINES).encode()
        self.tree = {
            "bank": [gdoc("doc_nfg", "NFG 7777 Apr 1-Oct 21 2026"), gsheet("sheet_rbc", "RBC export Sep"),
                     folder("sub", "US Chase"), file_("zip_1", "archive.zip", "application/zip", EARLIER)],
            "sub": [file_("csv_bad", "notes.csv", "text/csv", EARLIER)],
        }
        self.drive = BankDrive(self.tree, exports={"doc_nfg": self.text, "sheet_rbc": build_xlsx(sample_rows())},
                               contents={"csv_bad": b"hello,world\nfoo,bar\n"})
        main.set_clients(store=self.store, drive=self.drive)
        self.patches = [mock.patch.object(main, "BANK_FOLDER_ID", "bank"),
                        mock.patch.object(main, "LEASE_HOLDER", "job:test:1"),
                        mock.patch.object(main, "LEASE_GAP", 0), mock.patch.object(main, "LEASE_HOLD", 0)]
        for p in self.patches:
            p.start()
        self.drive.get = self._get

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def _get(self, file_id, fields=None):
        if file_id == "bank":
            return {"id": "bank", "name": "Bank Transactions", "mimeType": main.FOLDER}
        return FakeDrive.get(self.drive, file_id, fields)

    def sync(self, now=NOW):
        return main.sync_bank_folder(log=quiet, now=now)

    def sources(self):
        return {r["file_id"]: r for r in self.store.list_statement_sources()}


class FirstRunTests(BankSyncTestCase):
    def test_every_statement_in_the_folder_and_its_subfolders_is_read_once(self):
        result = self.sync()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sorted(result["imported"]), ["NFG 7777 Apr 1-Oct 21 2026", "RBC export Sep"])
        self.assertEqual(result["failed"], ["notes.csv"])
        src = self.sources()
        self.assertEqual(set(src), {"doc_nfg", "sheet_rbc", "csv_bad"})           # the .zip is left alone
        self.assertEqual(self.drive.exported, [("doc_nfg", "text/plain"), ("sheet_rbc", main.XLSX)])
        self.assertEqual((src["doc_nfg"]["status"], src["doc_nfg"]["new_count"]), ("imported", 6))
        self.assertEqual(src["csv_bad"]["status"], "failed")
        self.assertEqual(src["csv_bad"]["path"], "US Chase")
        self.assertIn("Could not recognise the columns", src["csv_bad"]["error"])
        # Each import is an upload in the dashboard, linked to its file in Drive, not copied.
        ups = {s["file_name"]: s for s in self.store.list_statements()}
        self.assertEqual(set(ups), {"NFG 7777 Apr 1-Oct 21 2026.txt", "RBC export Sep.xlsx"})
        nfg = ups["NFG 7777 Apr 1-Oct 21 2026.txt"]
        self.assertEqual(nfg["uploaded_by"], "Drive: Bank Transactions")
        self.assertEqual(nfg["drive_link"], "https://docs.google.com/document/d/doc_nfg/edit")
        self.assertEqual(self.drive.uploads, [])
        # Read and matched, nothing posted to Chat, nothing left to do.
        state = self.store.get_state("bank_sync")
        self.assertEqual((state["ok"], state["months_pending"], state["running"]), (True, [], False))
        self.assertEqual(state["last_import"]["name"], "RBC export Sep")
        self.assertEqual(self.store.notifications, [])
        self.assertIsNone(self.store.get_lease(main.LEASE_NAME)["holder"])

    def test_a_second_run_reads_nothing_again(self):
        self.sync()
        uploads = len(self.store.list_statements())
        self.drive.exported.clear()
        result = self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(result["status"], "nothing new")
        self.assertEqual(self.drive.exported, [])
        self.assertEqual(len(self.store.list_statements()), uploads)

    def test_the_months_are_read_and_matched_newest_first(self):
        calls = []
        with mock.patch.object(main, "index_month", side_effect=lambda y, m, **k: calls.append(("index", y, m)) or {}), \
                mock.patch.object(main, "match_month", side_effect=lambda y, m, **k: calls.append(("match", y, m)) or {}):
            self.sync()
        months = [(y, m) for kind, y, m in calls if kind == "match"]
        self.assertEqual(months, sorted(months, reverse=True))
        self.assertIn((2026, 9), months)
        self.assertIn((2026, 5), months)                    # the text export's first line


class VersionTests(BankSyncTestCase):
    def test_a_changed_file_is_read_again_and_adds_only_its_new_lines(self):
        self.sync()
        extra = '00000003,"4514xxxxxxxx7777",20261001,0,"NEW SUPPLIER","USD",000000099.00'
        self.drive.exports["doc_nfg"] = "\n".join(EXPORT_LINES[1:] + [extra]).encode()   # first line dropped
        self.tree["bank"][0]["modifiedTime"] = LATER
        self.sync()
        src = self.sources()["doc_nfg"]
        self.assertEqual((src["transaction_count"], src["new_count"]), (6, 1))
        self.assertIn("1 line of the version read before is no longer in the file", " ".join(src["warnings"]))
        nfg_uploads = [s for s in self.store.list_statements() if s["file_name"].startswith("NFG")]
        self.assertEqual(sorted(s["new_count"] for s in nfg_uploads), [1, 6])

    def test_a_new_version_without_new_lines_is_not_listed_as_an_upload(self):
        self.sync()
        uploads = len(self.store.list_statements())
        self.tree["bank"][0]["modifiedTime"] = LATER
        self.sync()
        self.assertEqual(len(self.store.list_statements()), uploads)
        self.assertEqual(self.sources()["doc_nfg"]["new_count"], 0)
        self.assertEqual(self.sources()["doc_nfg"]["modified_time"], LATER)

    def test_a_file_still_being_edited_waits_until_it_settles(self):
        self.tree["bank"][1]["modifiedTime"] = "2026-11-02T14:55:00.000Z"      # five minutes before NOW
        result = self.sync()
        self.assertEqual(result["settling"], ["RBC export Sep"])
        self.assertNotIn("sheet_rbc", self.sources())
        self.assertEqual(self.store.get_state("bank_sync")["settling"], ["RBC export Sep"])
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.sources()["sheet_rbc"]["status"], "imported")

    def test_a_file_that_cannot_be_read_waits_for_a_new_version(self):
        self.sync()
        self.drive.downloads.clear()
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.drive.downloads, [])                      # not read again as it is
        self.tree["sub"][0]["modifiedTime"] = LATER
        self.drive.contents["csv_bad"] = b"Date,Description,Amount\n2026-09-03,ANTHROPIC,150.00\n"
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertEqual(self.sources()["csv_bad"]["status"], "imported")

    def test_a_drive_failure_is_tried_again_then_given_up(self):
        self.drive.fail["doc_nfg"] = DriveError("Drive GET files/doc_nfg/export -> 500 backend error")
        for i, expected in enumerate([("retrying", 1), ("retrying", 2), ("failed", 3)]):
            self.sync(NOW + dt.timedelta(minutes=15 * i))
            src = self.sources()["doc_nfg"]
            self.assertEqual((src["status"], src["attempts"]), expected)
        self.assertIn("tried 3 times", src["error"])
        self.assertEqual(src["modified_time"], EARLIER)           # given up on this version only
        del self.drive.fail["doc_nfg"]
        self.sync(NOW + dt.timedelta(hours=1))
        self.assertEqual(self.sources()["doc_nfg"]["status"], "failed")       # until the file changes
        self.tree["bank"][0]["modifiedTime"] = LATER
        self.sync(NOW + dt.timedelta(hours=2))
        self.assertEqual(self.sources()["doc_nfg"]["status"], "imported")

    def test_a_retry_that_works_keeps_nothing_of_the_failure(self):
        self.drive.fail["doc_nfg"] = DriveError("Drive GET -> 503")
        self.sync()
        del self.drive.fail["doc_nfg"]
        self.sync(NOW + dt.timedelta(minutes=15))
        src = self.sources()["doc_nfg"]
        self.assertEqual((src["status"], src["attempts"], src["error"]), ("imported", 0, None))


class FolderAndLeaseTests(BankSyncTestCase):
    def test_a_folder_that_is_not_shared_says_what_to_do(self):
        def refuse(file_id, fields=None):
            raise DriveError("Drive GET files/bank -> 404 File not found: bank.")
        self.drive.get = refuse
        self.assertEqual(self.sync()["status"], "unreadable")
        state = self.store.get_state("bank_sync")
        self.assertFalse(state["ok"])
        self.assertIn("share it with ap@superhairpieces.com (Viewer)", state["error"])

    def test_off_without_a_folder(self):
        with mock.patch.object(main, "BANK_FOLDER_ID", ""):
            self.assertEqual(self.sync()["status"], "off")

    def test_the_run_waits_for_a_step_running_elsewhere(self):
        self.store.take_lease(main.LEASE_NAME, "service:rev-1:abcd", "match", 300)
        self.assertEqual(self.sync()["status"], "busy")
        self.assertEqual(self.sources(), {})
        self.assertTrue(self.store.get_state("bank_sync")["busy"])
        self.store.release_lease(main.LEASE_NAME, "service:rev-1:abcd")
        self.assertEqual(self.sync()["status"], "ok")

    def test_a_lease_left_by_a_dead_job_expires(self):
        self.store.take_lease(main.LEASE_NAME, "job:old:1", "drive-sync", -1)        # already expired
        self.assertEqual(self.sync()["status"], "ok")

    def test_months_a_run_did_not_finish_are_matched_by_the_next(self):
        with mock.patch.object(main, "match_month", side_effect=RuntimeError("cut short")):
            with self.assertRaises(RuntimeError):
                self.sync()
        pending = self.store.get_state("bank_sync")["months_pending"]
        self.assertTrue(pending)
        self.assertIsNone(self.store.get_lease(main.LEASE_NAME)["holder"])          # released all the same
        matched = []
        with mock.patch.object(main, "match_month", side_effect=lambda y, m, **k: matched.append([y, m]) or {}):
            self.assertEqual(self.sync(NOW + dt.timedelta(minutes=15))["status"], "ok")
        self.assertEqual(matched, pending)
        self.assertEqual(self.store.get_state("bank_sync")["months_pending"], [])


class ServiceLeaseTests(ApiTestCase):
    """The dashboard's own steps take the same lease."""

    def test_a_dashboard_step_waits_while_the_import_runs(self):
        self.store.take_lease(main.LEASE_NAME, "job:exec-1:1", "drive-sync", 300)
        r = self.client.post("/api/jobs?kind=match&year=2026&month=9")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["elsewhere"]["job"], "drive-sync")
        r = self.client.post("/api/match?year=2026&month=9")                  # the in-request kind too
        self.assertEqual(r.status_code, 409)
        self.store.release_lease(main.LEASE_NAME, "job:exec-1:1")
        r = self.client.post("/api/jobs?kind=match&year=2026&month=9")
        self.assertEqual(r.status_code, 202, r.get_json())

    def test_a_lease_an_earlier_dashboard_instance_left_is_taken_over(self):
        self.store.take_lease(main.LEASE_NAME, "service:rev-old:0000", "index", 300)
        r = self.client.post("/api/jobs?kind=match&year=2026&month=9")
        self.assertEqual(r.status_code, 202, r.get_json())
        self.wait_job(r.get_json()["job"]["id"])
        self.assertIsNone(self.store.get_lease(main.LEASE_NAME)["holder"])           # released after

    def test_the_store_unreachable_does_not_stop_the_dashboard(self):
        with mock.patch.object(self.store, "take_lease", side_effect=RuntimeError("supabase down")):
            r = self.client.post("/api/jobs?kind=match&year=2026&month=9")
        self.assertEqual(r.status_code, 202, r.get_json())
        self.wait_job(r.get_json()["job"]["id"])

    def test_the_weekly_reminder_takes_the_lease(self):
        seen = []
        with mock.patch.object(main, "LEASE_HOLDER", "job:remind:1"), \
                mock.patch.object(main, "_remind_latest", side_effect=lambda: seen.append(self.store.get_lease(main.LEASE_NAME)) or {}):
            main.remind_latest()
        self.assertEqual((seen[0]["holder"], seen[0]["job"]), ("job:remind:1", "remind"))
        self.assertIsNone(self.store.get_lease(main.LEASE_NAME)["holder"])

    def test_the_weekly_reminder_gives_up_when_the_lease_stays_taken(self):
        with mock.patch.object(main, "take_lease", return_value=None) as take:
            with self.assertRaises(SystemExit):
                main.remind_latest()
        take.assert_called_once_with("remind", wait=1500, fail_open=False)

    def wait_job(self, job_id):
        for _ in range(200):
            body = self.client.get(f"/api/jobs/current?id={job_id}").get_json()
            if not body["running"]:
                return body
            main.time.sleep(0.02)
        self.fail("job did not finish")


class StatusEndpointTests(BankSyncTestCase):
    def test_the_dashboard_line(self):
        self.sync()
        body = self.client.get("/api/bank-imports").get_json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["folder_url"], "https://drive.google.com/drive/folders/bank")
        self.assertEqual((body["state"]["ok"], body["state"]["folder_name"], body["state"]["running"]),
                         (True, "Bank Transactions", False))
        names = {f["name"]: f for f in body["files"]}
        self.assertEqual(names["notes.csv"]["status"], "failed")
        self.assertNotIn("transaction_ids", names["RBC export Sep"])
        self.store.take_lease(main.LEASE_NAME, "job:x:1", "drive-sync", 300)
        self.assertTrue(self.client.get("/api/bank-imports").get_json()["state"]["running"])

    def test_off_without_a_folder(self):
        with mock.patch.object(main, "BANK_FOLDER_ID", ""):
            self.assertEqual(self.client.get("/api/bank-imports").get_json(), {"enabled": False})


class WalkTests(unittest.TestCase):
    def test_subfolders_paths_depth_and_a_folder_reached_twice(self):
        tree = {"root": [folder("a", "A"), file_("f1", "one.csv", "text/csv")],
                "a": [folder("b", "B"), file_("f2", "two.csv", "text/csv")],
                "b": [file_("f3", "three.csv", "text/csv"), folder("a", "A again")]}
        d = BankDrive(tree)
        self.assertEqual({(f["id"], f["path"]) for f in d.walk("root")},
                         {("f1", ""), ("f2", "A"), ("f3", "A/B")})
        self.assertEqual({f["id"] for f in d.walk("root", max_depth=1)}, {"f1", "f2"})


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(BankSyncTestCase):
    """What the review of the first version found."""

    def test_lines_stored_under_an_earlier_reading_are_not_added_again(self):
        """Hand uploads read before a parser fix (Chase in CAD, True Top with signs turned round)
        carry other ids; the import recognises their lines and says how many read differently."""
        from test_statement_formats import EXPORT_LINES as lines
        import statements
        parsed = statements.parse_statement("x.txt", "\n".join(lines).encode())["transactions"]
        old = []
        for i, t in enumerate(parsed):
            row = dict(t, id=f"t_old_{i}", statement_id="st_old", created_at="2026-10-27T00:00:00Z",
                       invoice_status="missing", invoice_id=None, cost_center=None, usage=None, note=None)
            if i < 2:
                row.update(currency="CAD", amount=-t["amount"], description=t["description"].lower())
            old.append(row)
        self.store.insert_transactions(old)
        self.sync()
        src = self.sources()["doc_nfg"]
        self.assertEqual((src["status"], src["new_count"]), ("imported", 0))
        self.assertIn("6 lines were already in the dashboard from an earlier reading of this statement and not "
                      "added again; 2 read differently there", " ".join(src["warnings"]))
        self.assertEqual(len([t for t in self.store.transactions.values() if t["card_last4"] == "7777"]), 6)
        # A first read matches every month of the file, even with nothing new in it.
        self.assertEqual(self.store.get_state("bank_sync")["months_pending"], [])

    def test_a_first_read_with_nothing_new_still_matches_its_months(self):
        matched = []
        self.sync()                                                    # stores the lines
        self.store.statement_sources.clear()                           # as if the ledger write had been lost
        with mock.patch.object(main, "match_month", side_effect=lambda y, m, **k: matched.append((y, m)) or {}):
            self.sync(NOW + dt.timedelta(minutes=15))
        self.assertIn((2026, 5), matched)                              # the text export's months
        self.assertEqual(self.sources()["doc_nfg"]["new_count"], 0)

    def test_a_failed_insert_leaves_no_upload_behind(self):
        with mock.patch.object(self.store, "insert_transactions", side_effect=RuntimeError("timeout")):
            self.sync()
        self.assertEqual(self.sources()["doc_nfg"]["status"], "retrying")
        self.assertEqual([s["file_name"] for s in self.store.list_statements()], [])

    def test_a_failed_version_keeps_the_last_read_lines(self):
        self.sync()
        first = self.sources()["doc_nfg"]["transaction_ids"]
        self.tree["bank"][0]["modifiedTime"] = LATER
        self.drive.fail["doc_nfg"] = DriveError("Drive GET -> 500")
        self.sync(NOW + dt.timedelta(minutes=15))
        src = self.sources()["doc_nfg"]
        self.assertEqual((src["status"], src["transaction_ids"]), ("retrying", first))
        del self.drive.fail["doc_nfg"]
        self.drive.exports["doc_nfg"] = "\n".join(EXPORT_LINES[1:]).encode()
        self.sync(NOW + dt.timedelta(minutes=30))
        self.assertIn("1 line of the version read before is no longer in the file",
                      " ".join(self.sources()["doc_nfg"]["warnings"]))

    def test_removing_an_imported_upload_stops_its_file_being_read(self):
        self.sync()
        upload = next(s for s in self.store.list_statements() if s["file_name"].startswith("RBC"))
        r = self.client.delete(f"/api/statements/{upload['id']}")
        self.assertEqual(r.get_json()["file_excluded"], "RBC export Sep")
        self.assertEqual(r.get_json()["files_read_again"], ["NFG 7777 Apr 1-Oct 21 2026"])   # shares September
        self.assertEqual(self.sources()["sheet_rbc"]["status"], "removed")
        self.tree["bank"][1]["modifiedTime"] = LATER
        self.drive.exported.clear()
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.drive.exported, [("doc_nfg", "text/plain")])          # not the removed sheet
        self.assertEqual(self.sources()["sheet_rbc"]["status"], "removed")

    def test_files_known_by_type_without_an_extension(self):
        self.assertEqual(main.bank_reader({"name": "statement", "mimeType": "application/pdf"}), (None, "statement.pdf"))
        self.assertEqual(main.bank_reader({"name": "export", "mimeType": "text/csv"}), (None, "export.csv"))
        self.assertEqual(main.bank_reader({"name": "Visa 4894 Jan 2026.xls", "mimeType": "application/vnd.ms-excel"}),
                         (None, "Visa 4894 Jan 2026.xls"))
        self.assertEqual(main.bank_reader({"name": "Visa 4894 Jan 2026", "mimeType": "application/vnd.ms-excel"}),
                         (None, "Visa 4894 Jan 2026.xls"))
        self.assertIsNone(main.bank_reader({"name": "photo.heic", "mimeType": "image/heic"}))

    def test_a_file_reached_twice_is_read_once(self):
        self.tree["sub"].append(dict(self.tree["bank"][0]))            # the same Doc again, as a shortcut would give it
        self.sync()
        self.assertEqual([x for x in self.drive.exported if x[0] == "doc_nfg"], [("doc_nfg", "text/plain")])
        self.assertEqual(self.sources()["doc_nfg"]["path"], "")

    def test_a_busy_run_does_not_put_back_an_older_state(self):
        """Another run finishes while this one lists the folder (the slow part): this run, finding
        the lease taken, must not write back the state it would have read before the listing."""
        self.sync()
        self.store.set_state("bank_sync", dict(self.store.get_state("bank_sync"), months_pending=[[2026, 9]]))
        self.store.take_lease(main.LEASE_NAME, "job:other:1", "drive-sync", 300)
        listing = self.drive.walk

        def other_run_finishes_meanwhile(folder_id, max_depth=5):
            self.store.set_state("bank_sync", dict(self.store.get_state("bank_sync"), months_pending=[],
                                                   last_import={"name": "newer", "at": "x", "new": 1}))
            return listing(folder_id, max_depth)
        self.drive.walk = other_run_finishes_meanwhile
        self.assertEqual(self.sync(NOW + dt.timedelta(minutes=15))["status"], "nothing new")
        state = self.store.get_state("bank_sync")
        self.assertEqual((state["months_pending"], state["last_import"]["name"]), ([], "newer"))
        # The same with work to do and the lease taken: busy is written, nothing older comes back.
        self.tree["bank"][0]["modifiedTime"] = LATER
        self.assertEqual(self.sync(NOW + dt.timedelta(minutes=30))["status"], "busy")
        state = self.store.get_state("bank_sync")
        self.assertEqual((state["months_pending"], state["last_import"]["name"], state["busy"]), ([], "newer", True))

    def test_the_lease_is_let_go_between_months_and_a_waiting_step_gets_it(self):
        def dashboard_takes_it(seconds):
            self.store.take_lease(main.LEASE_NAME, "service:rev:1", "match", 300)
        with mock.patch.object(main.time, "sleep", side_effect=dashboard_takes_it):
            result = self.sync()
        self.assertGreater(result["months_left"], 0)
        self.assertEqual(self.store.get_lease(main.LEASE_NAME)["holder"], "service:rev:1")
        self.assertEqual(len(self.store.get_state("bank_sync")["months_pending"]), result["months_left"])
        self.store.release_lease(main.LEASE_NAME, "service:rev:1")
        self.assertEqual(self.sync(NOW + dt.timedelta(minutes=15))["months_left"], 0)

    def test_a_run_starts_no_month_after_its_budget(self):
        with mock.patch.object(main, "BANK_RUN_BUDGET", -1):
            result = self.sync()
        self.assertEqual(result["imported"], ["NFG 7777 Apr 1-Oct 21 2026"])        # one step, then out of time
        self.assertNotIn("sheet_rbc", self.sources())                                # read by the next run
        self.assertEqual(len(self.store.get_state("bank_sync")["months_pending"]), result["months_left"])
        self.assertGreater(result["months_left"], 0)
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.sources()["sheet_rbc"]["status"], "imported")
        self.assertEqual(self.store.get_state("bank_sync")["months_pending"], [])

    def test_months_left_are_matched_even_while_the_folder_cannot_be_read(self):
        self.store.set_state("bank_sync", {"months_pending": [[2026, 9]]})

        def refuse(file_id, fields=None):
            raise DriveError("Drive GET files/bank -> 404 File not found: bank.")
        self.drive.get = refuse
        matched = []
        with mock.patch.object(main, "match_month", side_effect=lambda y, m, **k: matched.append((y, m)) or {}):
            result = self.sync()
        self.assertEqual((result["status"], matched, result["months_left"]), ("unreadable", [(2026, 9)], 0))
        self.assertFalse(self.store.get_state("bank_sync")["ok"])

    def test_the_page_waits_on_jobs_current_while_a_job_holds_the_lease(self):
        self.store.take_lease(main.LEASE_NAME, "job:exec:1", "drive-sync", 300)
        body = self.client.get("/api/jobs/current").get_json()
        self.assertEqual(body["elsewhere"]["job"], "drive-sync")
        self.store.release_lease(main.LEASE_NAME, "job:exec:1")
        self.assertNotIn("elsewhere", self.client.get("/api/jobs/current").get_json())


class SecondReviewTests(BankSyncTestCase):
    """What the second review found."""

    def store_earlier_reading(self, lines, skip=(), currency="CAD"):
        import statements
        parsed = statements.parse_statement("x.txt", "\n".join(lines).encode())["transactions"]
        rows = [dict(t, id=f"t_old_{i}", statement_id="st_old", created_at="2026-10-27T00:00:00Z", currency=currency,
                     invoice_status="missing" if t["type"] == "purchase" else "not_required", invoice_id=None,
                     cost_center=None, usage=None, note=None)
                for i, t in enumerate(parsed) if t["description"] not in skip]
        self.store.insert_transactions(rows)
        return parsed

    def test_a_close_match_is_not_taken_by_a_looser_one_first(self):
        """Two same-day 7.27 Amazon lines; the earlier reading kept only the second: the second
        claims it, the first is added (not the other way round)."""
        self.store_earlier_reading(EXPORT_LINES, skip=("AMAZON MKTPL*AAA111",))
        self.sync()
        june = [t["description"] for t in self.store.transactions.values() if t["txn_date"] == "2026-06-01"]
        self.assertEqual(sorted(june), ["AMAZON MKTPL*AAA111", "AMAZON MKTPL*BBB222"])

    def test_a_refund_listed_before_its_sale_does_not_take_the_sales_place(self):
        lines = ['00000003,"4514xxxxxxxx7777",20260904,1,"ANCHOR YACHT CHARTERS","USD",000002061.98',
                 '00000003,"4514xxxxxxxx7777",20260904,0,"ANCHOR YACHT CHARTERS","USD",000002061.98']
        self.drive.exports["doc_nfg"] = "\n".join(lines).encode()
        self.store_earlier_reading(lines[1:])                         # the sale only, in CAD
        self.sync()
        sept = sorted((t["type"], t["amount"]) for t in self.store.transactions.values() if t["card_last4"] == "7777")
        self.assertEqual(sept, [("credit", -2061.98), ("purchase", 2061.98)])

    def test_gemini_signs_turned_round_still_pair_by_type(self):
        """True Top read by Gemini: purchases stored negative. Each line pairs with its own row."""
        parsed = self.store_earlier_reading(EXPORT_LINES, currency="USD")
        for t in self.store.transactions.values():
            t["amount"] = -t["amount"]
        self.sync()
        self.assertEqual(self.sources()["doc_nfg"]["new_count"], 0)
        self.assertEqual(len(self.store.transactions) - 10, len(parsed))     # 10: the RBC sheet's own lines

    def test_deleting_a_hand_upload_has_the_import_read_its_months_again(self):
        self.store_earlier_reading(EXPORT_LINES)
        self.store.statements.append({"id": "st_old", "file_name": "by hand.docx", "months": ["2026 05", "2026 06"],
                                      "uploaded_at": "2026-10-27T00:00:00Z", "drive_file_id": "copy_1"})
        self.sync()
        self.assertEqual(self.sources()["doc_nfg"]["new_count"], 0)
        r = self.client.delete("/api/statements/st_old")
        self.assertEqual(r.get_json()["files_read_again"], ["NFG 7777 Apr 1-Oct 21 2026"])
        self.sync(NOW + dt.timedelta(minutes=15))
        self.assertEqual(self.sources()["doc_nfg"]["new_count"], 6)
        self.assertEqual(len([t for t in self.store.transactions.values() if t["card_last4"] == "7777"]), 6)

    def test_a_reread_matches_the_months_of_lines_it_had_not_seen(self):
        self.sync()
        extra = '00000003,"4514xxxxxxxx7777",20261005,0,"NEW SUPPLIER","USD",000000099.00'
        self.drive.exports["doc_nfg"] = "\n".join(EXPORT_LINES + [extra]).encode()
        self.tree["bank"][0]["modifiedTime"] = LATER
        with mock.patch.object(self.store, "save_statement_source", side_effect=RuntimeError("503")):
            with self.assertRaises(RuntimeError):
                self.sync(NOW + dt.timedelta(minutes=15))                # the line is stored, the row is not
        matched = []
        with mock.patch.object(main, "match_month", side_effect=lambda y, m, **k: matched.append((y, m)) or {}):
            self.sync(NOW + dt.timedelta(minutes=30))
        self.assertIn((2026, 10), matched)

    def test_the_run_counts_as_running_between_its_steps(self):
        seen = []

        def look(seconds):
            seen.append(self.client.get("/api/bank-imports").get_json()["state"]["running"])
        with mock.patch.object(main.time, "sleep", side_effect=look):
            self.sync()
        self.assertTrue(seen and all(seen))
        self.assertFalse(self.client.get("/api/bank-imports").get_json()["state"]["running"])

    def test_a_line_of_credit_is_not_a_card(self):
        import statements
        data = (b"Account Type,Account Number,Transaction Date,Cheque Number,Description 1,Description 2,CAD$,USD$\n"
                b"Line of Credit,00102-7654321,2/17/2026,,TRANSFER TO CHQ,,-2500.00,\n"
                b"Visa,4516070100670000,2/19/2026,,PETRO-CANADA 00453,,-109.98,\n")
        r = statements.parse_statement("x.csv", data)
        self.assertEqual([t["card_last4"] for t in r["transactions"]], ["0000"])
