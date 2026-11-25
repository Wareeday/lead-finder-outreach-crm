"""Email Drafter & Safe Outbound Dispatch Engine.

Generates personalized multi-stage drafts (.txt and optional .html), validates
recipients, respects opt-outs and daily send caps, and dispatches via configurable
SMTP (SSL 465 or STARTTLS 587) with audit-trail recording.
"""

from __future__ import annotations

import html
import os
import re
import smtplib
import time
from datetime import date
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from src.followup_scheduler import populate_record_followup_dates
from src.logging_config import get_logger
from src.models import LeadRecord, OutreachEvent
from src.repository import CRMRepository
from src.scorer import build_pain_point_hook

logger = get_logger("email_drafter")

_STRICT_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,24}$")


def _safe_filename(text: str) -> str:
    """Convert a string into a clean, OS-safe filename slug."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip().lower()).strip("_")
    return slug[:50] or "lead"


def is_valid_email(address: str) -> bool:
    """Validate email syntax and guard against header-injection characters."""
    raw = (address or "").strip()
    if not raw or "\r" in raw or "\n" in raw:
        return False
    _, parsed = parseaddr(raw)
    return bool(parsed and _STRICT_EMAIL_RE.match(parsed))


def _resolve_template(stage: str) -> Dict[str, str]:
    if stage == "initial":
        return config.EMAIL_TEMPLATES["initial"]
    if stage in config.STAGE_EMAIL_TEMPLATES:
        return config.STAGE_EMAIL_TEMPLATES[stage]
    return config.EMAIL_TEMPLATES["followup"]


def render_email(
    lead: LeadRecord | Dict[str, str],
    stage: str = "initial",
    sender_name: Optional[str] = None,
    sender_company: Optional[str] = None,
    include_unsubscribe: bool = True,
) -> Dict[str, str]:
    """Render personalized email subject, plaintext body, and HTML body for a lead."""
    template = _resolve_template(stage)
    resolved_sender = (sender_name or os.getenv("SENDER_NAME") or "Your Name").strip()
    resolved_company = (sender_company or os.getenv("SENDER_COMPANY") or "").strip()
    sender_title = os.getenv("SENDER_TITLE", "").strip()

    sig_parts = [p for p in (sender_title, resolved_company) if p]
    sender_signature = f"{' | '.join(sig_parts)}\n" if sig_parts else ""
    unsubscribe_note = (
        "\n---\nIf you'd prefer not to receive follow-ups, simply reply 'unsubscribe'.\n"
        if include_unsubscribe
        else ""
    )

    if isinstance(lead, LeadRecord):
        owner = lead.owner_name.strip()
        business = lead.business_name.strip() or "your team"
        city = lead.city.strip() or "your area"
        website = lead.website.strip()
        email = lead.email.strip()
        niche = lead.niche.strip() or "local business"
        rating = lead.rating.strip() or "great"
        reviews = lead.reviews.strip() or "several"
    else:
        owner = (lead.get("Owner name") or "").strip()
        business = (lead.get("Business Name") or "your team").strip()
        city = (lead.get("City") or "your area").strip()
        website = (lead.get("Website") or "").strip()
        email = (lead.get("Email") or "").strip()
        niche = (lead.get("Niche") or "local business").strip()
        rating = (lead.get("Rating") or "great").strip()
        reviews = (lead.get("Reviews") or "several").strip()

    pain_point_hook = build_pain_point_hook(lead)

    context = {
        "business_name": business,
        "owner_name": owner,
        "greeting_name": owner if owner else f"{business} team",
        "city": city,
        "website": website,
        "niche": niche,
        "rating": rating,
        "reviews": reviews,
        "pain_point_hook": pain_point_hook,
        "stage": stage,
        "sender_name": resolved_sender,
        "sender_company": resolved_company,
        "sender_signature": sender_signature,
        "unsubscribe_note": unsubscribe_note,
    }

    subject = template["subject"].format(**context).replace("\r", " ").replace("\n", " ").strip()
    body_text = template["body"].format(**context)

    paragraphs = [
        f"<p>{html.escape(block).replace(chr(10), '<br>')}</p>"
        for block in body_text.strip().split("\n\n")
    ]
    body_html = (
        "<!doctype html><html><body style='font-family:system-ui,-apple-system,sans-serif;"
        "line-height:1.55;color:#1e293b;max-width:620px;margin:0 auto;padding:16px;'>"
        + "\n".join(paragraphs)
        + "</body></html>"
    )

    return {
        "to": email,
        "subject": subject,
        "body": body_text,
        "html_body": body_html,
    }


def save_email_drafts(
    queue: List[Dict[str, str]],
    output_dir: Path | None = None,
    sender_name: str | None = None,
    *,
    write_html: bool = False,
) -> List[Path]:
    """Generate collision-safe personalized drafts for every lead in the action queue."""
    drafts_dir = output_dir or config.EMAIL_DRAFTS_DIR
    drafts_dir.mkdir(parents=True, exist_ok=True)
    sender = sender_name or os.getenv("SENDER_NAME", "Your Name")

    written_files: List[Path] = []
    used_names: Dict[str, int] = {}

    for item in queue:
        stage = item.get("Follow-Up Stage", "initial")
        email_data = render_email(item, stage=stage, sender_name=sender)
        slug = _safe_filename(item.get("Business Name", "lead"))
        stage_slug = _safe_filename(stage)
        base_stem = f"{slug}_{stage_slug}"

        count = used_names.get(base_stem, 0) + 1
        used_names[base_stem] = count
        final_stem = base_stem if count == 1 else f"{base_stem}_{count}"

        draft_path = drafts_dir / f"{final_stem}.txt"
        content = (
            f"To: {email_data['to'] or '[No email on file - enrich website or check phone]'}\n"
            f"Subject: {email_data['subject']}\n"
            f"Stage: {stage} | Score: {item.get('Lead Score', '-')}"
            f" ({item.get('Lead Grade', '-')})\n"
            f"\n{email_data['body']}"
        )
        draft_path.write_text(content, encoding="utf-8")
        written_files.append(draft_path)

        if write_html:
            html_path = drafts_dir / f"{final_stem}.html"
            html_path.write_text(email_data["html_body"], encoding="utf-8")

    return written_files


def send_via_smtp(
    to_address: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> None:
    """Send a single multipart email via configured SMTP server (supports SSL 465 or STARTTLS 587)."""
    if not is_valid_email(to_address):
        raise ValueError(f"Invalid recipient email address: '{to_address}'")

    smtp_user = (os.getenv("SMTP_USER") or os.getenv("GMAIL_ADDRESS") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD") or "").strip()
    smtp_host = (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    use_tls = os.getenv("SMTP_USE_TLS", "false").strip().lower() in {"1", "true", "yes"}

    if not smtp_user or not smtp_pass:
        raise ValueError(
            "Missing SMTP credentials. Configure GMAIL_ADDRESS + GMAIL_APP_PASSWORD "
            "(or SMTP_USER + SMTP_PASSWORD) in .env."
        )

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:{smtp_user}?subject=unsubscribe>"
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    if use_tls or smtp_port == 587:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(smtp_user, smtp_pass)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as smtp:
            smtp.login(smtp_user, smtp_pass)
            smtp.send_message(msg)


def send_via_gmail_smtp(to_address: str, subject: str, body: str) -> None:
    """Backward-compatible helper for sending via Gmail SMTP."""
    send_via_smtp(to_address=to_address, subject=subject, body_text=body)


def dispatch_outreach_batch(
    repo: CRMRepository,
    queue: List[Dict[str, str]],
    *,
    dry_run: bool = True,
    target_date: Optional[date] = None,
    daily_limit: Optional[int] = None,
    send_delay_seconds: Optional[float] = None,
    sender_name: Optional[str] = None,
) -> Tuple[int, int, List[Dict[str, str]]]:
    """Dispatch or dry-run queued emails with cap enforcement, dedup, and DB audit logging.

    Returns:
        (sent_or_previewed_count, skipped_count, report_items)
    """
    ref_date = target_date or date.today()
    ref_date_str = ref_date.strftime(config.DATE_FORMAT)
    cap = daily_limit if daily_limit is not None else int(os.getenv("DAILY_SEND_LIMIT", "25"))
    delay = (
        send_delay_seconds
        if send_delay_seconds is not None
        else float(os.getenv("SEND_DELAY_SECONDS", "3.0"))
    )

    already_sent_today = repo.count_sent_on_date(ref_date_str)
    remaining_quota = max(0, cap - already_sent_today)
    sent_stages_by_lead = repo.get_sent_stages_by_lead()

    dispatched = 0
    skipped = 0
    report: List[Dict[str, str]] = []

    for item in queue:
        lead_id_raw = item.get("Lead ID", "").strip()
        lead_id = int(lead_id_raw) if lead_id_raw.isdigit() else None
        stage = item.get("Follow-Up Stage", "initial")
        business_name = item.get("Business Name", "Unknown")

        lead = repo.get_lead(lead_id) if lead_id else None
        if lead and (lead.opted_out or lead.status in config.SUPPRESSED_STATUSES):
            skipped += 1
            report.append({"business": business_name, "stage": stage, "outcome": "skipped_suppressed"})
            continue

        if lead_id and stage in sent_stages_by_lead.get(lead_id, set()):
            skipped += 1
            report.append({"business": business_name, "stage": stage, "outcome": "skipped_already_sent"})
            continue

        recipient = (lead.email if lead else item.get("Email", "")).strip()
        if not is_valid_email(recipient):
            skipped += 1
            report.append({"business": business_name, "stage": stage, "outcome": "skipped_invalid_email"})
            continue

        if remaining_quota <= 0:
            skipped += 1
            report.append({"business": business_name, "stage": stage, "outcome": "skipped_daily_cap"})
            continue

        rendered = render_email(lead or item, stage=stage, sender_name=sender_name)

        if dry_run:
            dispatched += 1
            remaining_quota -= 1
            report.append(
                {
                    "business": business_name,
                    "stage": stage,
                    "to": recipient,
                    "subject": rendered["subject"],
                    "outcome": "dry_run_ready",
                }
            )
            continue

        try:
            send_via_smtp(
                to_address=recipient,
                subject=rendered["subject"],
                body_text=rendered["body"],
                body_html=rendered["html_body"],
            )
            dispatched += 1
            remaining_quota -= 1
            if lead is not None:
                if stage == "initial" and not lead.outreach_date:
                    lead.outreach_date = ref_date_str
                    populate_record_followup_dates([lead])
                if lead.status in {"new", "enriched"}:
                    lead.status = "contacted"
                lead.last_contacted = ref_date_str
                repo.save_lead(lead)
                repo.record_outreach_event(
                    OutreachEvent(
                        lead_id=lead.id,  # type: ignore[arg-type]
                        stage=stage,
                        channel="email",
                        status="sent",
                        subject=rendered["subject"],
                        body=rendered["body"],
                        recipient=recipient,
                    )
                )
            report.append(
                {
                    "business": business_name,
                    "stage": stage,
                    "to": recipient,
                    "subject": rendered["subject"],
                    "outcome": "sent",
                }
            )
            if delay > 0 and remaining_quota > 0:
                time.sleep(delay)
        except Exception as exc:
            logger.error("Failed sending %s to %s: %s", stage, recipient, exc)
            skipped += 1
            if lead is not None and lead.id is not None:
                repo.record_outreach_event(
                    OutreachEvent(
                        lead_id=lead.id,
                        stage=stage,
                        channel="email",
                        status="failed",
                        subject=rendered["subject"],
                        recipient=recipient,
                        error_message=str(exc),
                    )
                )
            report.append(
                {
                    "business": business_name,
                    "stage": stage,
                    "to": recipient,
                    "outcome": f"failed: {exc}",
                }
            )

    return dispatched, skipped, report
