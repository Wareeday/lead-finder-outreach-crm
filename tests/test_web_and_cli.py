"""Integration tests for the Web CRM Dashboard REST API and CLI subcommands."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import main as cli_main
from src.repository import CRMRepository
from src.web_app import create_server


class TestWebDashboardAndAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "api_test.db"
        self.repo = CRMRepository(db_path=self.db_path)
        self.server = create_server(repo=self.repo, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmpdir.cleanup()

    def _req(self, path: str, method: str = "GET", payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8")

    def test_full_api_and_static_ui_lifecycle(self) -> None:
        # 1. Static HTML + Health
        status, _, html = self._req("/")
        self.assertEqual(status, 200)
        self.assertIn("Lead Finder &amp; Outreach CRM", html)

        status, _, health_raw = self._req("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(health_raw)["status"], "ok")

        # 2. Trigger demo pipeline via API (isolate outputs to tmpdir)
        from unittest.mock import patch
        with patch("config.OUTPUT_DIR", Path(self.tmpdir.name) / "out"):
            status, _, run_raw = self._req(
                "/api/pipeline/run",
                method="POST",
                payload={"mode": "demo", "date": "2026-11-20", "draft_emails": False},
            )
        self.assertEqual(status, 200)
        run_data = json.loads(run_raw)
        self.assertEqual(run_data["summary"]["status"], "success")
        self.assertEqual(run_data["queue_count"], 3)

        # 3. List & manually create lead
        _, _, leads_raw = self._req("/api/leads")
        leads = json.loads(leads_raw)["leads"]
        self.assertEqual(len(leads), 4)

        status, _, created_raw = self._req(
            "/api/leads",
            method="POST",
            payload={"business_name": "Cedar Park Dental", "email": "hi@cedar.org", "city": "Austin"},
        )
        self.assertEqual(status, 201)
        cedar_id = json.loads(created_raw)["lead"]["id"]

        # 4. Preview email, mark sent, and update status via PATCH
        _, _, preview_raw = self._req(f"/api/leads/{cedar_id}/preview-email?stage=initial")
        self.assertIn("Cedar Park Dental", json.loads(preview_raw)["subject"])

        _, _, sent_raw = self._req(
            f"/api/leads/{cedar_id}/mark-sent",
            method="POST",
            payload={"stage": "initial", "date": "2026-11-20"},
        )
        sent_lead = json.loads(sent_raw)["lead"]
        self.assertEqual(sent_lead["status"], "contacted")
        self.assertEqual(sent_lead["followups"]["F #1"], "2026-11-22")

        _, _, patch_raw = self._req(
            f"/api/leads/{cedar_id}",
            method="PATCH",
            payload={"status": "replied", "notes": "Interested in demo"},
        )
        self.assertEqual(json.loads(patch_raw)["lead"]["status"], "replied")

        # 5. Analytics & CSV export
        _, _, analytics_raw = self._req("/api/analytics")
        analytics = json.loads(analytics_raw)
        self.assertEqual(analytics["total_leads"], 5)
        self.assertEqual(analytics["positive_replies"], 1)

        _, headers, csv_body = self._req("/api/export/csv?kind=master")
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("Cedar Park Dental", csv_body)


class TestCLICommands(unittest.TestCase):
    def test_cli_run_stats_queue_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "cli.db"
            out_dir = tmp_path / "out"
            rc = cli_main.main(
                ["--mode", "demo", "--date", "2026-11-20", "--db", str(db_path), "--output-dir", str(out_dir)]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(cli_main.main(["stats", "--db", str(db_path)]), 0)
            self.assertEqual(
                cli_main.main(["queue", "--date", "2026-11-20", "--db", str(db_path), "--output-dir", str(out_dir)]),
                0,
            )
            self.assertEqual(
                cli_main.main(["send", "--date", "2026-11-20", "--include-initial", "--db", str(db_path)]),
                0,
            )
            self.assertEqual(
                cli_main.main(["lead-update", "--id", "1", "--status", "booked", "--db", str(db_path)]),
                0,
            )


if __name__ == "__main__":
    unittest.main()
