"""Backward-compatible end-to-end tests for Lead Finder & Outreach CRM."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

import config
from src.email_drafter import save_email_drafts
from src.followup_scheduler import build_today_queue, populate_followup_dates
from src.lead_finder import DemoLeadFinder
from src.sheet_manager import load_leads_csv, merge_and_deduplicate, save_leads_csv


class TestLeadPipeline(unittest.TestCase):
    def test_end_to_end_demo_pipeline(self) -> None:
        existing = load_leads_csv(config.SAMPLE_DATA_DIR / "potential_leads_sample.csv")
        self.assertEqual(len(existing), 2)

        new_leads = DemoLeadFinder().find_leads(city="Austin", niche="dentist", limit=10)
        self.assertEqual(len(new_leads), 3)

        merged, added, skipped = merge_and_deduplicate(existing, new_leads)
        # Bright Smile Dental is a duplicate -> skipped=1, added=2, total=4
        self.assertEqual(skipped, 1)
        self.assertEqual(added, 2)
        self.assertEqual(len(merged), 4)

        populate_followup_dates(merged)

        bright = next(r for r in merged if r["Business Name"] == "Bright Smile Dental")
        self.assertEqual(bright["F #1"], "2026-11-20")
        self.assertEqual(bright["F #2"], "2026-11-22")
        self.assertEqual(bright["F #6"], "2026-12-09")

        oakwood = next(r for r in merged if r["Business Name"] == "Oakwood Wellness Clinic")
        self.assertEqual(oakwood["F #1"], "")

        queue = build_today_queue(merged, target_date=date(2026, 11, 20))
        self.assertEqual(len(queue), 3)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            out_csv = tmp_path / "leads_updated.csv"
            save_leads_csv(out_csv, merged)
            reloaded = load_leads_csv(out_csv)
            self.assertEqual(len(reloaded), 4)

            drafts = save_email_drafts(queue, output_dir=tmp_path / "drafts", sender_name="Alex")
            self.assertEqual(len(drafts), 3)
            for draft in drafts:
                self.assertTrue(draft.exists())
                text = draft.read_text(encoding="utf-8")
                self.assertIn("Subject:", text)
                self.assertIn("Alex", text)


if __name__ == "__main__":
    unittest.main()
