"""Follow-Up Scheduler: calculates F #1..F #6 dates and builds stage-aware action queues."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

import config
from src.models import LeadRecord


def parse_date(date_text: str) -> date | None:
    """Parse a YYYY-MM-DD date string safely; return None if blank or invalid."""
    text = (date_text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, config.DATE_FORMAT).date()
    except ValueError:
        return None


def populate_followup_dates(leads: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Fill F #1 through F #6 columns whenever a valid Outreach date is present."""
    for row in leads:
        outreach_date = parse_date(row.get("Outreach", ""))
        if not outreach_date:
            continue
        for idx, day_offset in enumerate(config.FOLLOWUP_DAYS, start=1):
            col_name = f"F #{idx}"
            due_date = outreach_date + timedelta(days=day_offset)
            row[col_name] = due_date.strftime(config.DATE_FORMAT)
    return leads


def populate_record_followup_dates(records: List[LeadRecord]) -> List[LeadRecord]:
    """Populate `f1..f6` dates on typed LeadRecord objects whenever `outreach_date` is valid."""
    attrs = ("f1", "f2", "f3", "f4", "f5", "f6")
    for rec in records:
        outreach_dt = parse_date(rec.outreach_date)
        if not outreach_dt:
            continue
        for idx, day_offset in enumerate(config.FOLLOWUP_DAYS):
            if idx >= len(attrs):
                break
            due_date = outreach_dt + timedelta(days=day_offset)
            setattr(rec, attrs[idx], due_date.strftime(config.DATE_FORMAT))
    return records


def build_today_queue(
    leads: List[Dict[str, str]],
    target_date: date | None = None,
    *,
    include_overdue: bool = False,
) -> List[Dict[str, str]]:
    """Return leads with a follow-up (F #1..F #6) due on `target_date`.

    Defaults to exact `target_date` matching for CSV-pipeline predictability.
    Pass `include_overdue=True` to include earlier due dates that haven't been cleared.
    """
    ref_date = target_date or date.today()
    today_str = ref_date.strftime(config.DATE_FORMAT)
    queue: List[Dict[str, str]] = []

    for row in leads:
        status = (row.get("CRM Status") or "").strip().lower()
        if status in config.SUPPRESSED_STATUSES:
            continue

        for idx in range(1, len(config.FOLLOWUP_DAYS) + 1):
            stage = f"F #{idx}"
            due_dt = parse_date(row.get(stage, ""))
            if not due_dt:
                continue

            is_due = (due_dt <= ref_date) if include_overdue else (due_dt == ref_date)
            if is_due:
                overdue_days = max(0, (ref_date - due_dt).days)
                queue.append(
                    {
                        "Due Date": due_dt.strftime(config.DATE_FORMAT),
                        "Follow-Up Stage": stage,
                        "Overdue Days": str(overdue_days),
                        "Lead Score": row.get("Lead Score", ""),
                        "Lead Grade": row.get("Lead Grade", ""),
                        "CRM Status": status or "contacted",
                        "Business Name": row.get("Business Name", ""),
                        "Owner name": row.get("Owner name", ""),
                        "Email": row.get("Email", ""),
                        "Phone no": row.get("Phone no", ""),
                        "Website": row.get("Website", ""),
                        "City": row.get("City", ""),
                        "Outreach Angle": row.get("Outreach Angle", ""),
                        "Notes": row.get("Notes", ""),
                    }
                )
                if include_overdue:
                    # Avoid listing multiple overdue stages for the same lead on one day
                    break
    return queue


def build_crm_queue(
    records: List[LeadRecord],
    target_date: date | None = None,
    *,
    include_overdue: bool = True,
    include_initial: bool = False,
    sent_stages_by_lead: Optional[Dict[int, Set[str]]] = None,
) -> List[Dict[str, str]]:
    """Build an intelligent, de-duped action queue from persisted LeadRecords.

    - Automatically suppresses opted-out, replied, booked, won, lost, or bounced leads.
    - Skips any follow-up stage already logged as `sent` in `outreach_events`.
    - When `include_overdue=True`, selects the single most recent due/overdue unsent stage
      per lead so prospects aren't double-emailed on catch-up runs.
    - Optionally surfaces qualified `new`/`enriched` leads lacking an `outreach_date` as `initial`.
    """
    ref_date = target_date or date.today()
    sent_map = sent_stages_by_lead or {}
    queue: List[Dict[str, str]] = []
    stage_attrs = ("f1", "f2", "f3", "f4", "f5", "f6")

    for rec in records:
        if rec.opted_out or rec.status in config.SUPPRESSED_STATUSES:
            continue

        sent_stages = sent_map.get(rec.id or -1, set())
        outreach_dt = parse_date(rec.outreach_date)

        if not outreach_dt:
            if include_initial and "initial" not in sent_stages:
                queue.append(
                    {
                        "Lead ID": str(rec.id or ""),
                        "Due Date": ref_date.strftime(config.DATE_FORMAT),
                        "Follow-Up Stage": "initial",
                        "Overdue Days": "0",
                        "Lead Score": str(rec.score),
                        "Lead Grade": rec.grade,
                        "CRM Status": rec.status,
                        "Business Name": rec.business_name,
                        "Owner name": rec.owner_name,
                        "Email": rec.email,
                        "Phone no": rec.phone,
                        "Website": rec.website,
                        "City": rec.city,
                        "Rating": rec.rating,
                        "Reviews": rec.reviews,
                        "Niche": rec.niche,
                        "Outreach Angle": rec.outreach_angle,
                        "Notes": rec.notes,
                    }
                )
            continue

        # Find all due/overdue unsent follow-up stages, pick the most current stage due
        due_candidates: List[tuple[int, date, str]] = []
        for idx, attr in enumerate(stage_attrs, start=1):
            if idx > len(config.FOLLOWUP_DAYS):
                break
            stage = f"F #{idx}"
            if stage in sent_stages:
                continue
            due_dt = parse_date(getattr(rec, attr))
            if not due_dt:
                continue
            if (due_dt <= ref_date) if include_overdue else (due_dt == ref_date):
                due_candidates.append((idx, due_dt, stage))

        if not due_candidates:
            continue

        # Take the latest applicable stage (e.g. if both F #1 and F #2 are due, send F #2)
        _, chosen_due, chosen_stage = due_candidates[-1]
        overdue_days = max(0, (ref_date - chosen_due).days)

        queue.append(
            {
                "Lead ID": str(rec.id or ""),
                "Due Date": chosen_due.strftime(config.DATE_FORMAT),
                "Follow-Up Stage": chosen_stage,
                "Overdue Days": str(overdue_days),
                "Lead Score": str(rec.score),
                "Lead Grade": rec.grade,
                "CRM Status": rec.status,
                "Business Name": rec.business_name,
                "Owner name": rec.owner_name,
                "Email": rec.email,
                "Phone no": rec.phone,
                "Website": rec.website,
                "City": rec.city,
                "Rating": rec.rating,
                "Reviews": rec.reviews,
                "Niche": rec.niche,
                "Outreach Angle": rec.outreach_angle,
                "Notes": rec.notes,
            }
        )

    # Sort highest-priority leads first (score desc, due date asc)
    queue.sort(
        key=lambda item: (-int(item.get("Lead Score") or 0), item.get("Due Date", "")),
    )
    return queue
