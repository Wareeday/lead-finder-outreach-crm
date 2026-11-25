"""Tests for multi-key deduplication, scoring, SQLite repository, scheduler, and email dispatch."""

from __future__ import annotations

import smtplib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.email_drafter import dispatch_outreach_batch, is_valid_email, render_email, save_email_drafts
from src.followup_scheduler import build_crm_queue, populate_record_followup_dates
from src.models import EnrichmentSignals, LeadRecord, OutreachEvent
from src.repository import CRMRepository
from src.scorer import compute_lead_score
from src.sheet_manager import load_leads_csv, merge_and_deduplicate


class TestDeduplicationAndCsvAliases(unittest.TestCase):
    def test_multi_key_dedup_domain_phone_and_legal_suffix(self) -> None:
        existing = [
            {
                "Business Name": "Bright Smile Dental LLC",
                "Website": "https://www.brightsmile.example.com/home",
                "Phone no": "+1 (512) 555-0101",
                "City": "Austin",
                "Email": "",
            },
            {
                "Business Name": "Downtown Dental",
                "Website": "https://downtowndental-austin.example.com",
                "Phone no": "512-555-0188",
                "City": "Austin",
            },
        ]
        incoming = [
            # Matches existing #1 via normalized domain + backfills blank Email
            {
                "Business Name": "Bright Smile Dentistry",
                "Website": "http://brightsmile.example.com",
                "Email": "hello@brightsmile.example.com",
                "City": "Austin",
            },
            # Matches existing #2 via normalized phone
            {
                "Business Name": "DT Dental Clinic",
                "Phone no": "+1-512-555-0188",
                "City": "Austin",
            },
            # Same name in a DIFFERENT city -> must NOT be falsely deduplicated
            {
                "Business Name": "Downtown Dental",
                "Website": "https://downtowndental-dallas.example.com",
                "Phone no": "214-555-0199",
                "City": "Dallas",
            },
        ]

        merged, added, skipped = merge_and_deduplicate(existing, incoming, enrich_existing=True)
        self.assertEqual(added, 1)
        self.assertEqual(skipped, 2)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["Email"], "hello@brightsmile.example.com")

    def test_csv_alias_loading(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "external.csv"
            csv_path.write_text(
                "Company Name,Contact,Tel,E-mail,URL,Location\n"
                "Summit Orthodontics,Dr. Patel,512-555-0177,info@summit.example.com,https://summit.example.com,Austin\n",
                encoding="utf-8",
            )
            rows = load_leads_csv(csv_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Business Name"], "Summit Orthodontics")
            self.assertEqual(rows[0]["Owner name"], "Dr. Patel")
            self.assertEqual(rows[0]["Phone no"], "512-555-0177")
            self.assertEqual(rows[0]["Email"], "info@summit.example.com")
            self.assertEqual(rows[0]["Website"], "https://summit.example.com")
            self.assertEqual(rows[0]["City"], "Austin")


class TestScoringAndRepository(unittest.TestCase):
    def test_lead_scoring_grades(self) -> None:
        high_value = LeadRecord(
            business_name="Prime Dental Studio",
            owner_name="Dr. Maya Rao",
            email="maya@primedental.example.com",
            phone="+1-512-555-0123",
            website="https://primedental.example.com",
            rating="4.9",
            reviews="180",
            enrichment_json=EnrichmentSignals(
                reachable=True,
                uses_https=True,
                has_booking_widget=False,
                has_live_chat=False,
            ).to_json(),
        )
        score = compute_lead_score(high_value)
        self.assertGreaterEqual(score.score, 85)
        self.assertEqual(score.grade, "A")
        self.assertIn("online booking", score.outreach_angle.lower())

        low_value = LeadRecord(business_name="Mystery Shop", website="https://mystery.example.com")
        low_score = compute_lead_score(low_value)
        self.assertEqual(low_score.grade, "D")

    def test_repository_upsert_events_and_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = CRMRepository(db_path=Path(tmpdir) / "crm.db")
            records = [
                LeadRecord(
                    business_name="Alpha Dental",
                    email="alpha@example.com",
                    city="Austin",
                    outreach_date="2026-11-18",
                    status="contacted",
                ),
                LeadRecord(
                    business_name="Beta Dental",
                    email="beta@example.com",
                    city="Austin",
                    outreach_date="2026-11-18",
                    status="replied",
                ),
                LeadRecord(
                    business_name="Gamma Dental",
                    email="gamma@example.com",
                    city="Austin",
                    status="new",
                ),
            ]
            populate_record_followup_dates(records)
            added, skipped, touched = repo.upsert_leads(records)
            self.assertEqual(added, 3)
            self.assertEqual(skipped, 0)

            all_leads = repo.list_leads()
            queue = build_crm_queue(
                all_leads,
                target_date=date(2026, 11, 20),
                include_overdue=True,
                include_initial=True,
                sent_stages_by_lead=repo.get_sent_stages_by_lead(),
            )
            # Beta Dental is suppressed ('replied'); Alpha gets F #1; Gamma gets initial
            names_and_stages = {(q["Business Name"], q["Follow-Up Stage"]) for q in queue}
            self.assertEqual(
                names_and_stages,
                {("Alpha Dental", "F #1"), ("Gamma Dental", "initial")},
            )

            # Mark Alpha F #1 as sent and verify it disappears from queue
            alpha = next(l for l in all_leads if l.business_name == "Alpha Dental")
            repo.record_outreach_event(
                OutreachEvent(
                    lead_id=alpha.id,  # type: ignore[arg-type]
                    stage="F #1",
                    channel="email",
                    status="sent",
                    recipient=alpha.email,
                )
            )
            queue_after = build_crm_queue(
                repo.list_leads(),
                target_date=date(2026, 11, 20),
                include_overdue=True,
                include_initial=False,
                sent_stages_by_lead=repo.get_sent_stages_by_lead(),
            )
            self.assertEqual(len(queue_after), 0)

            analytics = repo.get_analytics()
            self.assertEqual(analytics["total_leads"], 3)
            self.assertEqual(analytics["positive_replies"], 1)


class TestEmailValidationAndDispatch(unittest.TestCase):
    def test_email_validation_blocks_header_injection(self) -> None:
        self.assertTrue(is_valid_email("owner@business.com"))
        self.assertFalse(is_valid_email("owner@business.com\r\nBcc: evil@attacker.com"))
        self.assertFalse(is_valid_email("not-an-email"))

    def test_collision_safe_drafts_and_html(self) -> None:
        queue = [
            {"Business Name": "Smile Studio", "Follow-Up Stage": "F #1", "Email": "a@example.com"},
            {"Business Name": "Smile Studio!", "Follow-Up Stage": "F #1", "Email": "b@example.com"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = save_email_drafts(queue, output_dir=Path(tmpdir), sender_name="Alex", write_html=True)
            self.assertEqual(len(paths), 2)
            self.assertNotEqual(paths[0].name, paths[1].name)
            self.assertTrue(paths[0].with_suffix(".html").exists())

    def test_dry_run_and_mocked_smtp_dispatch_with_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = CRMRepository(db_path=Path(tmpdir) / "crm.db")
            repo.upsert_leads(
                [
                    LeadRecord(business_name="Lead One", email="one@business.com", city="Austin"),
                    LeadRecord(business_name="Lead Two", email="two@business.com", city="Austin"),
                ]
            )
            queue = build_crm_queue(repo.list_leads(), target_date=date(2026, 11, 25), include_initial=True)

            # Dry run makes zero SMTP calls and zero DB status changes
            count, skipped, report = dispatch_outreach_batch(
                repo, queue, dry_run=True, target_date=date(2026, 11, 25), daily_limit=10
            )
            self.assertEqual(count, 2)
            self.assertEqual(skipped, 0)
            self.assertEqual(repo.count_sent_on_date("2026-11-25"), 0)

            # Live dispatch with mocked SMTP and daily_limit=1
            mock_smtp = MagicMock()
            with (
                patch.dict("os.environ", {"GMAIL_ADDRESS": "sender@example.com", "GMAIL_APP_PASSWORD": "secret"}),
                patch.object(smtplib, "SMTP_SSL", return_value=mock_smtp),
            ):
                sent, skipped_live, _ = dispatch_outreach_batch(
                    repo,
                    queue,
                    dry_run=False,
                    target_date=date(2026, 11, 25),
                    daily_limit=1,
                    send_delay_seconds=0.0,
                )
            self.assertEqual(sent, 1)
            self.assertEqual(skipped_live, 1)
            self.assertEqual(repo.count_sent_on_date("2026-11-25"), 1)


if __name__ == "__main__":
    unittest.main()
