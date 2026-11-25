"""Lead Finder & Outreach CRM — Unified Production CLI & Dashboard Launcher.

Backward-compatible quick pipeline:
    python3 main.py --mode demo --date 2026-11-20 --draft-emails
    python3 main.py --mode osm --city "Austin" --niche "dentist" --limit 15 --enrich --draft-emails

Modern subcommands:
    python3 main.py run --mode demo --date 2026-11-20 --draft-emails
    python3 main.py enrich --limit 25
    python3 main.py queue --date 2026-11-20 --include-initial
    python3 main.py send --dry-run
    python3 main.py stats
    python3 main.py lead-update --id 1 --status replied --notes "Booked discovery call"
    python3 main.py dashboard --port 8000
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import config
from src.email_drafter import dispatch_outreach_batch, save_email_drafts
from src.enricher import WebsiteEnricher
from src.followup_scheduler import build_crm_queue, populate_record_followup_dates
from src.logging_config import configure_logging
from src.pipeline import PipelineOptions, run_pipeline
from src.repository import CRMRepository
from src.sheet_manager import export_leads_json, save_leads_csv, sync_to_google_sheet
from src.web_app import serve_dashboard

SUBCOMMANDS = {
    "run",
    "enrich",
    "queue",
    "send",
    "lead-update",
    "stats",
    "export",
    "sync",
    "dashboard",
}


def _parse_date_arg(raw: str) -> date:
    if not raw:
        return date.today()
    return datetime.strptime(raw.strip(), config.DATE_FORMAT).date()


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=["demo", "osm", "google", "csv"],
        default="demo",
        help="Lead source: 'demo' (offline), 'osm' (free Overpass API), 'google' (Places API), or 'csv'.",
    )
    parser.add_argument("--city", default=config.DEFAULT_CITY, help="Target city (e.g. 'Austin').")
    parser.add_argument(
        "--niche",
        default=config.DEFAULT_NICHE,
        help="Business niche or raw OSM tag key=value (e.g. 'dentist', 'cafe', 'shop=bakery').",
    )
    parser.add_argument("--limit", type=int, default=config.DEFAULT_LIMIT, help="Max new leads to fetch.")
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=config.SAMPLE_DATA_DIR / "potential_leads_sample.csv",
        help="Path to seed/import CSV file.",
    )
    parser.add_argument("--date", default="", help="Override reference date (YYYY-MM-DD).")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Inspect lead websites for emails, social links, and booking/chat signals.",
    )
    parser.add_argument(
        "--include-initial",
        action="store_true",
        help="Include uncontacted leads as 'initial' stage in today's action queue.",
    )
    parser.add_argument(
        "--no-overdue",
        action="store_true",
        help="Only include follow-ups strictly matching --date (exclude earlier overdue stages).",
    )
    parser.add_argument(
        "--draft-emails",
        action="store_true",
        help="Generate personalized outreach .txt drafts in output/email_drafts/.",
    )
    parser.add_argument(
        "--write-html",
        action="store_true",
        help="Also generate .html previews alongside .txt drafts.",
    )
    parser.add_argument(
        "--dry-run-send",
        action="store_true",
        help="Preview outbound SMTP dispatch counts and validation without sending emails.",
    )
    parser.add_argument(
        "--send-emails",
        action="store_true",
        help="Live-dispatch valid queued emails via SMTP (enforces DAILY_SEND_LIMIT).",
    )
    parser.add_argument(
        "--sync-sheets",
        action="store_true",
        help="Sync leads, queue, and metrics to Google Sheets.",
    )
    parser.add_argument("--db", type=Path, default=None, help="Custom SQLite CRM database path.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Custom output artifacts directory.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lead-crm",
        description="Lead Finder & Outreach CRM: discovery, website enrichment, scoring, scheduling & dashboard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the full discovery -> enrich -> score -> queue -> outreach pipeline.")
    _add_run_arguments(run_p)

    enrich_p = sub.add_parser("enrich", help="Enrich persisted leads that have websites.")
    enrich_p.add_argument("--limit", type=int, default=50, help="Max leads to inspect.")
    enrich_p.add_argument("--force", action="store_true", help="Re-enrich leads already inspected.")
    enrich_p.add_argument("--db", type=Path, default=None)

    queue_p = sub.add_parser("queue", help="Inspect and export today's follow-up action queue.")
    queue_p.add_argument("--date", default="", help="Reference date (YYYY-MM-DD).")
    queue_p.add_argument("--include-initial", action="store_true")
    queue_p.add_argument("--no-overdue", action="store_true")
    queue_p.add_argument("--draft-emails", action="store_true")
    queue_p.add_argument("--output-dir", type=Path, default=None)
    queue_p.add_argument("--db", type=Path, default=None)

    send_p = sub.add_parser("send", help="Dry-run or dispatch queued emails via SMTP with safety caps.")
    send_p.add_argument("--date", default="", help="Reference date (YYYY-MM-DD).")
    send_p.add_argument("--include-initial", action="store_true")
    send_p.add_argument("--limit", type=int, default=None, help="Override DAILY_SEND_LIMIT for this run.")
    send_p.add_argument("--delay", type=float, default=None, help="Delay seconds between sends.")
    send_p.add_argument(
        "--confirm-send",
        action="store_true",
        help="Actually transmit emails over SMTP (omit for safe dry-run preview).",
    )
    send_p.add_argument("--db", type=Path, default=None)

    upd_p = sub.add_parser("lead-update", help="Update a lead's CRM status, contact info, or notes.")
    upd_p.add_argument("--id", type=int, required=True, help="Lead database ID.")
    upd_p.add_argument("--status", choices=config.CRM_STATUSES, default=None)
    upd_p.add_argument("--email", default=None)
    upd_p.add_argument("--owner", default=None)
    upd_p.add_argument("--notes", default=None)
    upd_p.add_argument("--outreach-date", default=None)
    upd_p.add_argument("--db", type=Path, default=None)

    stats_p = sub.add_parser("stats", help="Print CRM pipeline analytics and conversion metrics.")
    stats_p.add_argument("--db", type=Path, default=None)

    exp_p = sub.add_parser("export", help="Export CRM leads to CSV, extended CSV, or JSON.")
    exp_p.add_argument("--format", choices=["csv", "extended-csv", "json"], default="extended-csv")
    exp_p.add_argument("--out", type=Path, default=None)
    exp_p.add_argument("--db", type=Path, default=None)

    sync_p = sub.add_parser("sync", help="Push persisted CRM leads & queue to Google Sheets.")
    sync_p.add_argument("--date", default="")
    sync_p.add_argument("--db", type=Path, default=None)

    dash_p = sub.add_parser("dashboard", help="Launch the interactive Web CRM Dashboard & REST API.")
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8000)
    dash_p.add_argument("--db", type=Path, default=None)

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    target_date = _parse_date_arg(args.date)

    print(f"[1/6] Initialized SQLite CRM store: {repo.db_path}")
    print(f"[2/6] Running discovery mode='{args.mode}' (city='{args.city}', niche='{args.niche}', limit={args.limit})")

    result = run_pipeline(
        repo,
        PipelineOptions(
            mode=args.mode,
            city=args.city,
            niche=args.niche,
            limit=args.limit,
            input_csv=args.input_csv,
            target_date=target_date,
            enrich=args.enrich,
            include_overdue=not args.no_overdue,
            include_initial=args.include_initial,
            draft_emails=args.draft_emails,
            write_html=args.write_html,
            send_emails=args.send_emails,
            dry_run_send=args.dry_run_send,
            sync_sheets=args.sync_sheets,
            output_dir=args.output_dir,
        ),
    )
    s = result.summary
    print(
        f"[3/6] Fetched {s.fetched_count} | Added {s.added_count} | "
        f"Duplicates skipped {s.skipped_count} | Total in CRM: {len(result.records)}"
    )
    if args.enrich:
        print(f"      Enriched {s.enriched_count} websites with contact & maturity signals.")
    print(f"[4/6] Scheduled F #1-F #6 | {len(result.queue)} follow-ups due for {target_date}")
    print(f"      Master CSV   -> {result.updated_csv}")
    print(f"      Extended CRM -> {result.extended_csv}")
    print(f"      Daily Queue  -> {result.queue_csv}")
    print(f"      Report       -> {result.report_md}")

    if args.draft_emails:
        draft_target = (args.output_dir / "email_drafts") if args.output_dir else config.EMAIL_DRAFTS_DIR
        print(f"[5/6] Generated {len(result.draft_paths)} personalized email drafts in {draft_target}")
    else:
        print("[5/6] Email drafting skipped (pass --draft-emails to write personalized drafts).")

    if result.send_report:
        print(f"[6/6] Dispatch summary: {len(result.send_report)} processed.")
    elif args.sync_sheets:
        print("[6/6] Synced CRM tabs to Google Sheets.")
    else:
        print(f"[6/6] Pipeline completed in {s.duration_ms} ms. Run `python3 main.py dashboard` to view UI.")
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    leads = repo.list_leads(limit=args.limit)
    enricher = WebsiteEnricher()
    count = enricher.enrich_batch(leads, force=args.force)
    for lead in leads:
        if lead.id is not None:
            repo.save_lead(lead)
    print(f"Enriched {count} websites across {len(leads)} inspected leads.")
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    ref_date = _parse_date_arg(args.date)
    records = repo.list_leads(limit=10000)
    queue = build_crm_queue(
        records,
        target_date=ref_date,
        include_overdue=not args.no_overdue,
        include_initial=args.include_initial,
        sent_stages_by_lead=repo.get_sent_stages_by_lead(),
    )
    out_dir = args.output_dir or config.OUTPUT_DIR
    queue_csv = out_dir / "followups_today.csv"
    save_leads_csv(queue_csv, queue, columns=config.QUEUE_COLUMNS)
    print(f"Queue for {ref_date}: {len(queue)} items -> {queue_csv}")
    for item in queue[:20]:
        print(
            f"  [{item['Follow-Up Stage']:7s}] {item['Business Name']} "
            f"(Grade {item['Lead Grade']} / {item['Lead Score']}) -> {item['Email'] or 'No email'}"
        )
    if args.draft_emails:
        drafts = save_email_drafts(queue, output_dir=out_dir / "email_drafts")
        print(f"Saved {len(drafts)} email drafts to {out_dir / 'email_drafts'}")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    ref_date = _parse_date_arg(args.date)
    records = repo.list_leads(limit=10000)
    queue = build_crm_queue(
        records,
        target_date=ref_date,
        include_overdue=True,
        include_initial=args.include_initial,
        sent_stages_by_lead=repo.get_sent_stages_by_lead(),
    )
    dry_run = not args.confirm_send
    processed, skipped, report = dispatch_outreach_batch(
        repo,
        queue,
        dry_run=dry_run,
        target_date=ref_date,
        daily_limit=args.limit,
        send_delay_seconds=args.delay,
    )
    mode_label = "DRY RUN" if dry_run else "LIVE SEND"
    print(f"[{mode_label}] Processed: {processed} | Skipped: {skipped}")
    for row in report:
        print(f"  - {row['business']} [{row['stage']}]: {row['outcome']}")
    return 0


def _cmd_lead_update(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    updates = {}
    if args.status is not None:
        updates["status"] = args.status
    if args.email is not None:
        updates["email"] = args.email
    if args.owner is not None:
        updates["owner_name"] = args.owner
    if args.notes is not None:
        updates["notes"] = args.notes
    if args.outreach_date is not None:
        updates["outreach_date"] = args.outreach_date

    updated = repo.update_lead_fields(args.id, updates)
    if not updated:
        print(f"Lead ID {args.id} not found.", file=sys.stderr)
        return 1
    if updated.outreach_date:
        populate_record_followup_dates([updated])
        updated = repo.save_lead(updated)
    print(
        f"Updated lead #{updated.id}: {updated.business_name} | "
        f"status={updated.status} | score={updated.score} ({updated.grade})"
    )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    stats = repo.get_analytics()
    print("=== Lead Finder & Outreach CRM Analytics ===")
    print(f"Database:       {repo.db_path}")
    print(f"Total Leads:    {stats['total_leads']}")
    print(f"Email Coverage: {stats['with_email']} ({stats['email_coverage_pct']}%)")
    print(f"Phone Coverage: {stats['with_phone']}")
    print(f"Average Score:  {stats['average_score']} / 100")
    print(f"Contacted+:     {stats['contacted_count']}")
    print(f"Positive Reply: {stats['positive_replies']} ({stats['reply_rate_pct']}%)")
    print(f"By Status:      {stats['by_status']}")
    print(f"By Grade:       {stats['by_grade']}")
    print(f"By Source:      {stats['by_source']}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    records = repo.list_leads(limit=10000)
    if args.format == "json":
        target = args.out or (config.OUTPUT_DIR / "leads_export.json")
        export_leads_json(target, records)
    elif args.format == "extended-csv":
        target = args.out or (config.OUTPUT_DIR / "leads_enriched_crm.csv")
        save_leads_csv(target, [r.to_extended_row() for r in records], columns=config.EXTENDED_COLUMNS)
    else:
        target = args.out or (config.OUTPUT_DIR / "leads_updated.csv")
        save_leads_csv(target, [r.to_sheet_row() for r in records], columns=config.COLUMNS)
    print(f"Exported {len(records)} leads to {target}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    repo = CRMRepository(db_path=args.db)
    ref_date = _parse_date_arg(args.date)
    records = repo.list_leads(limit=10000)
    queue = build_crm_queue(
        records,
        target_date=ref_date,
        include_overdue=True,
        sent_stages_by_lead=repo.get_sent_stages_by_lead(),
    )
    count = sync_to_google_sheet([r.to_sheet_row() for r in records], queue_rows=queue)
    print(f"Synced {count} leads and {len(queue)} queued follow-ups to Google Sheets.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    config.load_env_file()
    configure_logging()

    raw_args = list(sys.argv[1:] if argv is None else argv)
    # Backward compatibility: default to `run` when no subcommand is specified
    if not raw_args or (raw_args[0] not in SUBCOMMANDS and raw_args[0] not in {"-h", "--help"}):
        raw_args = ["run", *raw_args]

    parser = build_parser()
    args = parser.parse_args(raw_args)

    handlers = {
        "run": _cmd_run,
        "enrich": _cmd_enrich,
        "queue": _cmd_queue,
        "send": _cmd_send,
        "lead-update": _cmd_lead_update,
        "stats": _cmd_stats,
        "export": _cmd_export,
        "sync": _cmd_sync,
        "dashboard": lambda a: serve_dashboard(host=a.host, port=a.port, db_path=a.db) or 0,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
