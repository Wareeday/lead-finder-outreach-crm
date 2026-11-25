"""Sheet Manager: CSV/JSON I/O, multi-key deduplication, reporting, and Google Sheets sync."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from src.logging_config import get_logger
from src.models import LeadRecord, PipelineRunSummary, ensure_sheet_columns

logger = get_logger("sheet_manager")

_LEGAL_SUFFIXES = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|ltd|limited|corp|corporation|co|company|plc|llp|pllc|pc|gmbh|pvt)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")

# Flexible CSV column alias mapping for importing arbitrary spreadsheets
COLUMN_ALIASES: Dict[str, List[str]] = {
    "Business Name": ["business name", "company", "company name", "name", "business", "account"],
    "Owner name": ["owner name", "owner", "contact", "contact name", "founder", "decision maker"],
    "Profile link": ["profile link", "linkedin", "social", "profile", "osm url"],
    "Website": ["website", "url", "web", "domain", "site", "homepage"],
    "Phone no": ["phone no", "phone", "telephone", "tel", "mobile", "phone number"],
    "Email": ["email", "e-mail", "email address", "mail", "contact email"],
    "Rating": ["rating", "stars", "google rating", "score"],
    "Reviews": ["reviews", "review count", "num reviews", "ratings count"],
    "map links": ["map links", "map link", "google maps", "maps url", "map"],
    "City": ["city", "location", "town", "area", "metro"],
    "Notes": ["notes", "note", "comments", "source", "description"],
    "Outreach": ["outreach", "outreach date", "first contact", "contacted date"],
}


def normalize_name(name: str) -> str:
    """Normalize a business name for exact-whitespace-insensitive matching."""
    return " ".join((name or "").strip().lower().split())


def canonical_business_name(name: str) -> str:
    """Normalize a business name by stripping punctuation and common legal suffixes."""
    cleaned = (name or "").strip().lower()
    cleaned = _LEGAL_SUFFIXES.sub(" ", cleaned)
    cleaned = _NON_ALNUM.sub(" ", cleaned)
    return " ".join(cleaned.split())


def normalize_domain(url_or_domain: str) -> str:
    """Extract canonical root domain (e.g. `https://www.Example.com/contact` -> `example.com`)."""
    raw = (url_or_domain or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_phone(phone: str) -> str:
    """Normalize a phone string to trailing 10 digits (or full digits if shorter) for dedup."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 7:
        return ""
    return digits[-10:] if len(digits) > 10 else digits


def compute_canonical_key(business_name: str, city: str = "", website: str = "", phone: str = "") -> str:
    """Produce a stable canonical identity key for a lead."""
    cname = canonical_business_name(business_name) or normalize_name(business_name)
    ccity = normalize_name(city)
    if cname and ccity:
        return f"{cname}::{ccity}"
    if cname:
        return cname
    domain = normalize_domain(website)
    if domain:
        return f"domain::{domain}"
    phone_key = normalize_phone(phone)
    if phone_key:
        return f"phone::{phone_key}"
    return ""


def _resolve_alias_header(header: str) -> Optional[str]:
    """Map an arbitrary CSV header to a canonical config.COLUMNS name if recognized."""
    if header in config.COLUMNS:
        return header
    if header in config.EXTENDED_COLUMNS:
        return header
    low = normalize_name(header)
    for canonical, aliases in COLUMN_ALIASES.items():
        if low == canonical.lower() or low in aliases:
            return canonical
    return None


def load_leads_csv(filepath: Path) -> List[Dict[str, str]]:
    """Read a leads CSV file (supporting standard or aliased columns) into aligned dicts."""
    if not filepath.exists():
        return []

    rows: List[Dict[str, str]] = []
    with filepath.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []

        header_map = {
            raw_col: (_resolve_alias_header(raw_col) or raw_col)
            for raw_col in reader.fieldnames
        }

        for raw_row in reader:
            remapped: Dict[str, str] = {}
            for raw_col, value in raw_row.items():
                if raw_col is None:
                    continue
                target_col = header_map.get(raw_col, raw_col)
                if value and not remapped.get(target_col):
                    remapped[target_col] = str(value).strip()

            cleaned = ensure_sheet_columns(remapped)
            if any(cleaned.values()):
                rows.append(cleaned)

    return rows


def save_leads_csv(
    filepath: Path,
    rows: List[Dict[str, str]],
    columns: List[str] | None = None,
) -> None:
    """Atomically write a list of lead dictionaries to CSV."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = columns or config.COLUMNS

    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=".csv", dir=str(filepath.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, filepath)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _is_fuzzy_name_match(a: str, b: str, threshold: float = 0.92) -> bool:
    """Check high-confidence fuzzy similarity between two canonicalized business names."""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 6:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def find_matching_index(
    candidate: Dict[str, str],
    existing_rows: List[Dict[str, str]],
    domain_index: Dict[str, int],
    phone_index: Dict[str, int],
    exact_name_index: Dict[str, int],
    city_name_index: Dict[Tuple[str, str], int],
) -> Optional[int]:
    """Identify whether a candidate matches an existing row via domain, phone, or name+city."""
    domain = normalize_domain(candidate.get("Website", ""))
    if domain and domain in domain_index:
        return domain_index[domain]

    phone = normalize_phone(candidate.get("Phone no", ""))
    if phone and phone in phone_index:
        return phone_index[phone]

    raw_name = normalize_name(candidate.get("Business Name", ""))
    canon_name = canonical_business_name(candidate.get("Business Name", ""))
    city = normalize_name(candidate.get("City", ""))

    if city and canon_name and (canon_name, city) in city_name_index:
        return city_name_index[(canon_name, city)]

    if raw_name and raw_name in exact_name_index:
        idx = exact_name_index[raw_name]
        existing_city = normalize_name(existing_rows[idx].get("City", ""))
        if not city or not existing_city or city == existing_city:
            return idx

    # Fuzzy check only within the same city (or when city is omitted)
    if canon_name:
        for (existing_canon, existing_city), idx in city_name_index.items():
            if city and existing_city and city != existing_city:
                continue
            if _is_fuzzy_name_match(canon_name, existing_canon):
                return idx

    return None


def _backfill_blank_fields(target: Dict[str, str], source: Dict[str, str]) -> None:
    """Fill missing contact/metadata fields on an existing row without overwriting values."""
    enrichable_cols = (
        "Owner name",
        "Profile link",
        "Website",
        "Phone no",
        "Email",
        "Rating",
        "Reviews",
        "map links",
        "City",
    )
    for col in enrichable_cols:
        if not (target.get(col) or "").strip() and (source.get(col) or "").strip():
            target[col] = source[col].strip()


def merge_and_deduplicate(
    existing_leads: List[Dict[str, str]],
    new_leads: List[Dict[str, str]],
    *,
    enrich_existing: bool = True,
) -> Tuple[List[Dict[str, str]], int, int]:
    """Merge new leads using domain, phone, canonical name+city, and fuzzy matching.

    Returns:
        (merged_list, added_count, skipped_duplicates_count)
    """
    merged = [ensure_sheet_columns(row) for row in existing_leads]
    domain_index: Dict[str, int] = {}
    phone_index: Dict[str, int] = {}
    exact_name_index: Dict[str, int] = {}
    city_name_index: Dict[Tuple[str, str], int] = {}

    def _index_row(idx: int, row: Dict[str, str]) -> None:
        domain = normalize_domain(row.get("Website", ""))
        if domain and domain not in domain_index:
            domain_index[domain] = idx
        phone = normalize_phone(row.get("Phone no", ""))
        if phone and phone not in phone_index:
            phone_index[phone] = idx
        raw_name = normalize_name(row.get("Business Name", ""))
        if raw_name and raw_name not in exact_name_index:
            exact_name_index[raw_name] = idx
        canon = canonical_business_name(row.get("Business Name", ""))
        city = normalize_name(row.get("City", ""))
        if canon and (canon, city) not in city_name_index:
            city_name_index[(canon, city)] = idx

    for idx, row in enumerate(merged):
        _index_row(idx, row)

    added = 0
    skipped = 0

    for raw_lead in new_leads:
        lead = ensure_sheet_columns(raw_lead)
        if not lead["Business Name"]:
            continue

        match_idx = find_matching_index(
            lead,
            merged,
            domain_index,
            phone_index,
            exact_name_index,
            city_name_index,
        )
        if match_idx is not None:
            skipped += 1
            if enrich_existing:
                _backfill_blank_fields(merged[match_idx], lead)
                _index_row(match_idx, merged[match_idx])
            continue

        new_idx = len(merged)
        merged.append(lead)
        _index_row(new_idx, lead)
        added += 1

    return merged, added, skipped


def export_leads_json(filepath: Path, records: List[LeadRecord]) -> None:
    """Export CRM lead records to a formatted JSON file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    payload = [rec.to_api_dict() for rec in records]
    filepath.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def export_markdown_report(
    filepath: Path,
    summary: PipelineRunSummary,
    records: List[LeadRecord],
    queue: List[Dict[str, str]],
) -> None:
    """Write a concise Markdown executive summary for a pipeline run."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    email_count = 0
    for rec in records:
        grade_counts[rec.grade] = grade_counts.get(rec.grade, 0) + 1
        if rec.email.strip():
            email_count += 1

    top_leads = sorted(records, key=lambda r: r.score, reverse=True)[:5]
    lines = [
        f"# Pipeline Execution Report (`{summary.run_id}`)",
        "",
        f"- **Mode:** `{summary.mode}` | **Target:** `{summary.city}` / `{summary.niche}`",
        f"- **Duration:** {summary.duration_ms} ms | **Status:** `{summary.status}`",
        f"- **Leads Fetched:** {summary.fetched_count} | **Added:** {summary.added_count} | **Duplicates Skipped:** {summary.skipped_count}",
        f"- **Enriched Websites:** {summary.enriched_count} | **Queue Due:** {summary.queued_count} | **Drafts Created:** {summary.drafted_count} | **Emails Sent:** {summary.sent_count}",
        "",
        "## CRM Portfolio Breakdown",
        "",
        f"- **Total Leads in CRM:** {len(records)}",
        f"- **Leads with Email:** {email_count} ({(email_count / len(records) * 100):.1f}%)" if records else "- **Leads with Email:** 0",
        f"- **Grade Distribution:** A={grade_counts['A']}, B={grade_counts['B']}, C={grade_counts['C']}, D={grade_counts['D']}",
        f"- **Action Queue Items Due:** {len(queue)}",
        "",
        "## Top 5 Priority Leads (by Score)",
        "",
        "| Business Name | City | Grade | Score | Email | Status | Outreach Angle |",
        "| :--- | :--- | :---: | :---: | :--- | :--- | :--- |",
    ]
    for rec in top_leads:
        lines.append(
            f"| {rec.business_name} | {rec.city} | {rec.grade} | {rec.score} | "
            f"{rec.email or '—'} | {rec.status} | {rec.outreach_angle} |"
        )
    filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_to_google_sheet(
    rows: List[Dict[str, str]],
    sheet_id: str | None = None,
    worksheet_name: str = "Potential leads",
    credentials_path: str | None = None,
    queue_rows: List[Dict[str, str]] | None = None,
    analytics: Dict[str, Any] | None = None,
) -> int:
    """Push leads (and optional daily queue + metrics tabs) to Google Sheets via gspread."""
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "gspread is not installed. Run `pip install -r requirements.txt` to enable Google Sheets sync."
        ) from exc

    target_sheet_id = sheet_id or os.getenv("GOOGLE_SHEET_ID", "").strip()
    creds_file = credentials_path or os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json").strip()

    if not target_sheet_id:
        raise ValueError("Missing GOOGLE_SHEET_ID. Set it in your .env file.")
    if not Path(creds_file).exists():
        raise FileNotFoundError(
            f"Google service account JSON not found at '{creds_file}'. See README.md for setup."
        )

    client = gspread.service_account(filename=creds_file)
    spreadsheet = client.open_by_key(target_sheet_id)

    def _upsert_tab(tab_name: str, headers: List[str], tab_rows: List[Dict[str, str]]) -> None:
        try:
            ws = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(
                title=tab_name,
                rows=max(100, len(tab_rows) + 20),
                cols=max(10, len(headers)),
            )
        matrix = [headers] + [[str(r.get(col, "")) for col in headers] for r in tab_rows]
        ws.clear()
        ws.update(range_name="A1", values=matrix)

    _upsert_tab(worksheet_name, config.COLUMNS, rows)

    if queue_rows is not None:
        _upsert_tab("Follow up seq", config.QUEUE_COLUMNS, queue_rows)

    if analytics:
        metric_rows = [{"Metric": str(k), "Value": str(v)} for k, v in analytics.items()]
        _upsert_tab("CRM Metrics", ["Metric", "Value"], metric_rows)

    logger.info("Synced %d lead rows to Google Sheet '%s'", len(rows), target_sheet_id)
    return len(rows)
