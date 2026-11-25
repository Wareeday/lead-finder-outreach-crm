"""End-to-End Pipeline Orchestrator bridging discovery, enrichment, SQLite CRM, queue, and outreach."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from src.email_drafter import dispatch_outreach_batch, save_email_drafts
from src.enricher import WebsiteEnricher
from src.followup_scheduler import build_crm_queue, populate_record_followup_dates
from src.lead_finder import get_finder, rows_to_lead_records
from src.logging_config import get_logger
from src.models import LeadRecord, PipelineRunSummary
from src.repository import CRMRepository
from src.sheet_manager import (
    export_leads_json,
    export_markdown_report,
    load_leads_csv,
    save_leads_csv,
    sync_to_google_sheet,
)

logger = get_logger("pipeline")


@dataclass
class PipelineOptions:
    mode: str = "demo"
    city: str = config.DEFAULT_CITY
    niche: str = config.DEFAULT_NICHE
    limit: int = config.DEFAULT_LIMIT
    input_csv: Optional[Path] = None
    target_date: Optional[date] = None
    enrich: bool = False
    include_overdue: bool = True
    include_initial: bool = False
    draft_emails: bool = False
    write_html: bool = False
    send_emails: bool = False
    dry_run_send: bool = False
    sync_sheets: bool = False
    output_dir: Optional[Path] = None
    sender_name: Optional[str] = None


@dataclass
class PipelineResult:
    summary: PipelineRunSummary
    records: List[LeadRecord]
    queue: List[Dict[str, str]]
    draft_paths: List[Path]
    send_report: List[Dict[str, str]]
    updated_csv: Path
    extended_csv: Path
    queue_csv: Path
    json_export: Path
    report_md: Path


def run_pipeline(
    repo: CRMRepository,
    options: PipelineOptions,
) -> PipelineResult:
    """Execute the full CRM automation pipeline and persist results + exports."""
    start_ts = time.perf_counter()
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    ref_date = options.target_date or date.today()
    out_dir = options.output_dir or config.OUTPUT_DIR
    drafts_dir = out_dir / "email_drafts"

    summary = PipelineRunSummary(
        run_id=run_id,
        mode=options.mode,
        city=options.city,
        niche=options.niche,
    )

    try:
        # 1. Seed / merge existing CSV into SQLite CRM if provided
        if options.input_csv and options.input_csv.exists():
            seed_rows = load_leads_csv(options.input_csv)
            seed_records = rows_to_lead_records(seed_rows, niche=options.niche, source="csv_seed")
            repo.upsert_leads(seed_records, enrich_existing=True)

        # 2. Fetch candidate leads from requested source
        finder = get_finder(options.mode, input_csv=options.input_csv)
        raw_new = finder.find_leads(city=options.city, niche=options.niche, limit=options.limit)
        summary.fetched_count = len(raw_new)

        new_records = rows_to_lead_records(raw_new, niche=options.niche, source=options.mode)

        # 3. Optional website contact & signal enrichment before upsert
        if options.enrich and new_records:
            enricher = WebsiteEnricher()
            summary.enriched_count = enricher.enrich_batch(new_records)

        # 4. Deduplicate & upsert into SQLite CRM
        added, skipped, _ = repo.upsert_leads(new_records, enrich_existing=True)
        summary.added_count = added
        summary.skipped_count = skipped

        # 5. Recalculate F #1..F #6 schedule on all CRM records & persist any filled dates
        all_records = repo.list_leads(limit=10000)
        populate_record_followup_dates(all_records)
        for rec in all_records:
            if rec.outreach_date and rec.id is not None:
                repo.save_lead(rec)

        # 6. Build today's + overdue action queue
        sent_stages = repo.get_sent_stages_by_lead()
        queue = build_crm_queue(
            all_records,
            target_date=ref_date,
            include_overdue=options.include_overdue,
            include_initial=options.include_initial,
            sent_stages_by_lead=sent_stages,
        )
        summary.queued_count = len(queue)

        # 7. Draft emails if requested
        draft_paths: List[Path] = []
        if options.draft_emails:
            draft_paths = save_email_drafts(
                queue,
                output_dir=drafts_dir,
                sender_name=options.sender_name,
                write_html=options.write_html,
            )
            summary.drafted_count = len(draft_paths)

        # 8. Optional outbound dispatch / dry-run
        send_report: List[Dict[str, str]] = []
        if options.send_emails or options.dry_run_send:
            dispatched, _, send_report = dispatch_outreach_batch(
                repo,
                queue,
                dry_run=not options.send_emails,
                target_date=ref_date,
                sender_name=options.sender_name,
            )
            if options.send_emails:
                summary.sent_count = dispatched
                all_records = repo.list_leads(limit=10000)

        # 9. Export CSV, JSON, and Markdown executive report
        updated_csv = out_dir / "leads_updated.csv"
        extended_csv = out_dir / "leads_enriched_crm.csv"
        queue_csv = out_dir / "followups_today.csv"
        json_export = out_dir / "leads_export.json"
        report_md = out_dir / "pipeline_report.md"

        sheet_rows = [rec.to_sheet_row() for rec in all_records]
        extended_rows = [rec.to_extended_row() for rec in all_records]

        save_leads_csv(updated_csv, sheet_rows, columns=config.COLUMNS)
        save_leads_csv(extended_csv, extended_rows, columns=config.EXTENDED_COLUMNS)
        save_leads_csv(queue_csv, queue, columns=config.QUEUE_COLUMNS)
        export_leads_json(json_export, all_records)

        # 10. Optional Google Sheets sync
        if options.sync_sheets:
            analytics = repo.get_analytics()
            flat_metrics = {
                "Total Leads": analytics["total_leads"],
                "With Email": analytics["with_email"],
                "Email Coverage %": analytics["email_coverage_pct"],
                "Average Lead Score": analytics["average_score"],
                "Contacted": analytics["contacted_count"],
                "Positive Replies": analytics["positive_replies"],
                "Reply Rate %": analytics["reply_rate_pct"],
            }
            sync_to_google_sheet(
                sheet_rows,
                queue_rows=queue,
                analytics=flat_metrics,
            )

        summary.duration_ms = int((time.perf_counter() - start_ts) * 1000)
        summary.status = "success"
        export_markdown_report(report_md, summary, all_records, queue)
        repo.record_pipeline_run(summary)

        logger.info(
            "Pipeline %s finished in %dms (fetched=%d added=%d skipped=%d queue=%d drafts=%d)",
            run_id,
            summary.duration_ms,
            summary.fetched_count,
            summary.added_count,
            summary.skipped_count,
            summary.queued_count,
            summary.drafted_count,
        )

        return PipelineResult(
            summary=summary,
            records=all_records,
            queue=queue,
            draft_paths=draft_paths,
            send_report=send_report,
            updated_csv=updated_csv,
            extended_csv=extended_csv,
            queue_csv=queue_csv,
            json_export=json_export,
            report_md=report_md,
        )
    except Exception as exc:
        summary.duration_ms = int((time.perf_counter() - start_ts) * 1000)
        summary.status = "failed"
        summary.error_message = str(exc)
        repo.record_pipeline_run(summary)
        raise
