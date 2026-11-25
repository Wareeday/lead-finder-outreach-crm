"""SQLite CRM Persistence Repository with migrations, audit logging, and analytics."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import config
from src.logging_config import get_logger
from src.models import LeadRecord, OutreachEvent, PipelineRunSummary
from src.scorer import score_lead_record
from src.sheet_manager import (
    canonical_business_name,
    compute_canonical_key,
    normalize_domain,
    normalize_name,
    normalize_phone,
)

logger = get_logger("repository")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    domain_key TEXT NOT NULL DEFAULT '',
    phone_key TEXT NOT NULL DEFAULT '',
    business_name TEXT NOT NULL,
    owner_name TEXT NOT NULL DEFAULT '',
    profile_link TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL DEFAULT '',
    reviews TEXT NOT NULL DEFAULT '',
    map_link TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    outreach_date TEXT NOT NULL DEFAULT '',
    f1 TEXT NOT NULL DEFAULT '',
    f2 TEXT NOT NULL DEFAULT '',
    f3 TEXT NOT NULL DEFAULT '',
    f4 TEXT NOT NULL DEFAULT '',
    f5 TEXT NOT NULL DEFAULT '',
    f6 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    score INTEGER NOT NULL DEFAULT 0,
    grade TEXT NOT NULL DEFAULT 'C',
    outreach_angle TEXT NOT NULL DEFAULT '',
    niche TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    opted_out INTEGER NOT NULL DEFAULT 0,
    last_contacted TEXT NOT NULL DEFAULT '',
    enrichment_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_grade ON leads(grade);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain_key);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_key);
CREATE INDEX IF NOT EXISTS idx_leads_city ON leads(city);

CREATE TABLE IF NOT EXISTS outreach_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'email',
    status TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outreach_lead_stage ON outreach_events(lead_id, stage, status);
CREATE INDEX IF NOT EXISTS idx_outreach_created ON outreach_events(created_at);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    niche TEXT NOT NULL DEFAULT '',
    fetched_count INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    enriched_count INTEGER NOT NULL DEFAULT 0,
    queued_count INTEGER NOT NULL DEFAULT 0,
    drafted_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL
);
"""


class CRMRepository:
    """Thread-safe SQLite persistence layer for leads, outreach events, and pipeline runs."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or config.get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_V1)
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current_version = row["v"] if row and row["v"] is not None else 0
            if current_version < 1:
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (1, _utc_now()),
                )

    @staticmethod
    def _row_to_lead(row: sqlite3.Row) -> LeadRecord:
        return LeadRecord(
            id=row["id"],
            canonical_key=row["canonical_key"],
            business_name=row["business_name"],
            owner_name=row["owner_name"],
            profile_link=row["profile_link"],
            website=row["website"],
            phone=row["phone"],
            email=row["email"],
            rating=row["rating"],
            reviews=row["reviews"],
            map_link=row["map_link"],
            city=row["city"],
            notes=row["notes"],
            outreach_date=row["outreach_date"],
            f1=row["f1"],
            f2=row["f2"],
            f3=row["f3"],
            f4=row["f4"],
            f5=row["f5"],
            f6=row["f6"],
            status=row["status"],
            score=row["score"],
            grade=row["grade"],
            outreach_angle=row["outreach_angle"],
            niche=row["niche"],
            source=row["source"],
            opted_out=bool(row["opted_out"]),
            last_contacted=row["last_contacted"],
            enrichment_json=row["enrichment_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _find_duplicate_row(self, conn: sqlite3.Connection, lead: LeadRecord) -> Optional[sqlite3.Row]:
        """Match an incoming lead by domain, phone, canonical_key, or same-city business name."""
        domain_key = normalize_domain(lead.website)
        if domain_key:
            hit = conn.execute(
                "SELECT * FROM leads WHERE domain_key = ? LIMIT 1", (domain_key,)
            ).fetchone()
            if hit:
                return hit

        phone_key = normalize_phone(lead.phone)
        if phone_key:
            hit = conn.execute(
                "SELECT * FROM leads WHERE phone_key = ? LIMIT 1", (phone_key,)
            ).fetchone()
            if hit:
                return hit

        canon_key = compute_canonical_key(lead.business_name, lead.city, lead.website, lead.phone)
        if canon_key:
            hit = conn.execute(
                "SELECT * FROM leads WHERE canonical_key = ? LIMIT 1", (canon_key,)
            ).fetchone()
            if hit:
                return hit

        # Match same business name across blank vs populated city
        target_canon = canonical_business_name(lead.business_name)
        target_city = normalize_name(lead.city)
        if target_canon:
            candidates = conn.execute(
                "SELECT * FROM leads WHERE LOWER(business_name) = ? OR canonical_key LIKE ?",
                (normalize_name(lead.business_name), f"{target_canon}::%"),
            ).fetchall()
            for cand in candidates:
                cand_city = normalize_name(cand["city"])
                if not target_city or not cand_city or target_city == cand_city:
                    return cand

        return None

    def upsert_leads(
        self,
        records: List[LeadRecord],
        *,
        enrich_existing: bool = True,
    ) -> Tuple[int, int, List[LeadRecord]]:
        """Insert genuinely new leads and optionally backfill blank fields on existing duplicates."""
        now = _utc_now()
        added = 0
        skipped = 0
        touched: List[LeadRecord] = []

        with self._connect() as conn:
            for raw_rec in records:
                if not raw_rec.business_name.strip():
                    continue

                rec = LeadRecord(
                    **{
                        **raw_rec.__dict__,
                        "canonical_key": raw_rec.canonical_key
                        or compute_canonical_key(
                            raw_rec.business_name, raw_rec.city, raw_rec.website, raw_rec.phone
                        ),
                    }
                )
                score_lead_record(rec)
                if rec.outreach_date and rec.status == "new":
                    rec.status = "contacted"

                existing = self._find_duplicate_row(conn, rec)
                if existing is not None:
                    skipped += 1
                    if enrich_existing:
                        merged_rec = self._row_to_lead(existing)
                        changed = False
                        for attr in (
                            "owner_name",
                            "profile_link",
                            "website",
                            "phone",
                            "email",
                            "rating",
                            "reviews",
                            "map_link",
                            "city",
                            "outreach_date",
                            "f1",
                            "f2",
                            "f3",
                            "f4",
                            "f5",
                            "f6",
                            "niche",
                            "source",
                        ):
                            cur_val = getattr(merged_rec, attr)
                            new_val = getattr(rec, attr)
                            if not cur_val and new_val:
                                setattr(merged_rec, attr, new_val)
                                changed = True
                        if rec.enrichment_json and not merged_rec.enrichment_json:
                            merged_rec.enrichment_json = rec.enrichment_json
                            changed = True
                        if changed:
                            if merged_rec.outreach_date and merged_rec.status == "new":
                                merged_rec.status = "contacted"
                            score_lead_record(merged_rec)
                            self._update_record_in_conn(conn, merged_rec, now)
                        touched.append(merged_rec)
                    continue

                domain_key = normalize_domain(rec.website)
                phone_key = normalize_phone(rec.phone)
                canon_key = rec.canonical_key or compute_canonical_key(
                    rec.business_name, rec.city, rec.website, rec.phone
                )
                # Ensure canonical_key uniqueness even if business name collides with different city/domain
                if conn.execute(
                    "SELECT 1 FROM leads WHERE canonical_key = ?", (canon_key,)
                ).fetchone():
                    canon_key = f"{canon_key}::{domain_key or phone_key or now}"

                cursor = conn.execute(
                    """
                    INSERT INTO leads (
                        canonical_key, domain_key, phone_key, business_name, owner_name,
                        profile_link, website, phone, email, rating, reviews, map_link,
                        city, notes, outreach_date, f1, f2, f3, f4, f5, f6,
                        status, score, grade, outreach_angle, niche, source,
                        opted_out, last_contacted, enrichment_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canon_key,
                        domain_key,
                        phone_key,
                        rec.business_name,
                        rec.owner_name,
                        rec.profile_link,
                        rec.website,
                        rec.phone,
                        rec.email,
                        rec.rating,
                        rec.reviews,
                        rec.map_link,
                        rec.city,
                        rec.notes,
                        rec.outreach_date,
                        rec.f1,
                        rec.f2,
                        rec.f3,
                        rec.f4,
                        rec.f5,
                        rec.f6,
                        rec.status,
                        rec.score,
                        rec.grade,
                        rec.outreach_angle,
                        rec.niche,
                        rec.source,
                        1 if rec.opted_out else 0,
                        rec.last_contacted,
                        rec.enrichment_json,
                        now,
                        now,
                    ),
                )
                rec.id = cursor.lastrowid
                rec.canonical_key = canon_key
                rec.created_at = now
                rec.updated_at = now
                touched.append(rec)
                added += 1

        return added, skipped, touched

    def _update_record_in_conn(self, conn: sqlite3.Connection, rec: LeadRecord, now: str) -> None:
        conn.execute(
            """
            UPDATE leads SET
                domain_key = ?, phone_key = ?, business_name = ?, owner_name = ?,
                profile_link = ?, website = ?, phone = ?, email = ?, rating = ?,
                reviews = ?, map_link = ?, city = ?, notes = ?, outreach_date = ?,
                f1 = ?, f2 = ?, f3 = ?, f4 = ?, f5 = ?, f6 = ?,
                status = ?, score = ?, grade = ?, outreach_angle = ?, niche = ?,
                source = ?, opted_out = ?, last_contacted = ?, enrichment_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalize_domain(rec.website),
                normalize_phone(rec.phone),
                rec.business_name,
                rec.owner_name,
                rec.profile_link,
                rec.website,
                rec.phone,
                rec.email,
                rec.rating,
                rec.reviews,
                rec.map_link,
                rec.city,
                rec.notes,
                rec.outreach_date,
                rec.f1,
                rec.f2,
                rec.f3,
                rec.f4,
                rec.f5,
                rec.f6,
                rec.status,
                rec.score,
                rec.grade,
                rec.outreach_angle,
                rec.niche,
                rec.source,
                1 if rec.opted_out else 0,
                rec.last_contacted,
                rec.enrichment_json,
                now,
                rec.id,
            ),
        )

    def save_lead(self, rec: LeadRecord) -> LeadRecord:
        """Persist updates to an existing LeadRecord."""
        if rec.id is None:
            raise ValueError("Cannot update lead without an ID")
        score_lead_record(rec)
        now = _utc_now()
        with self._connect() as conn:
            self._update_record_in_conn(conn, rec, now)
        rec.updated_at = now
        return rec

    def list_leads(
        self,
        *,
        status: Optional[str] = None,
        grade: Optional[str] = None,
        city: Optional[str] = None,
        niche: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[LeadRecord]:
        clauses: List[str] = []
        params: List[Any] = []

        if status:
            clauses.append("status = ?")
            params.append(status.strip().lower())
        if grade:
            clauses.append("grade = ?")
            params.append(grade.strip().upper())
        if city:
            clauses.append("LOWER(city) = ?")
            params.append(city.strip().lower())
        if niche:
            clauses.append("LOWER(niche) = ?")
            params.append(niche.strip().lower())
        if search:
            like = f"%{search.strip()}%"
            clauses.append(
                "(business_name LIKE ? OR owner_name LIKE ? OR email LIKE ? OR city LIKE ? OR notes LIKE ?)"
            )
            params.extend([like, like, like, like, like])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM leads {where_sql} ORDER BY score DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_lead(r) for r in rows]

    def get_lead(self, lead_id: int) -> Optional[LeadRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return self._row_to_lead(row) if row else None

    def update_lead_fields(self, lead_id: int, updates: Dict[str, Any]) -> Optional[LeadRecord]:
        """Safely update editable CRM fields on a lead."""
        lead = self.get_lead(lead_id)
        if not lead:
            return None

        allowed = {
            "business_name",
            "owner_name",
            "profile_link",
            "website",
            "phone",
            "email",
            "rating",
            "reviews",
            "map_link",
            "city",
            "notes",
            "outreach_date",
            "f1",
            "f2",
            "f3",
            "f4",
            "f5",
            "f6",
            "status",
            "niche",
            "source",
            "opted_out",
            "last_contacted",
            "enrichment_json",
        }
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "status":
                status_val = str(value).strip().lower()
                if status_val not in config.CRM_STATUSES:
                    raise ValueError(f"Invalid status '{status_val}'. Allowed: {config.CRM_STATUSES}")
                setattr(lead, key, status_val)
                if status_val == "unsubscribed":
                    lead.opted_out = True
            elif key == "opted_out":
                lead.opted_out = bool(value)
                if lead.opted_out and lead.status not in config.SUPPRESSED_STATUSES:
                    lead.status = "unsubscribed"
            else:
                setattr(lead, key, str(value or "").strip())

        return self.save_lead(lead)

    def delete_lead(self, lead_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            return cursor.rowcount > 0

    def record_outreach_event(self, event: OutreachEvent) -> int:
        now = event.created_at or _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO outreach_events (
                    lead_id, stage, channel, status, subject, body,
                    recipient, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.lead_id,
                    event.stage,
                    event.channel,
                    event.status,
                    event.subject,
                    event.body,
                    event.recipient,
                    event.error_message,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_lead_events(self, lead_id: int) -> List[OutreachEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outreach_events WHERE lead_id = ? ORDER BY id DESC",
                (lead_id,),
            ).fetchall()
            return [
                OutreachEvent(
                    id=r["id"],
                    lead_id=r["lead_id"],
                    stage=r["stage"],
                    channel=r["channel"],
                    status=r["status"],
                    subject=r["subject"],
                    body=r["body"],
                    recipient=r["recipient"],
                    error_message=r["error_message"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def get_sent_stages_by_lead(self) -> Dict[int, Set[str]]:
        """Return mapping of lead_id -> set of stages already marked sent."""
        result: Dict[int, Set[str]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT lead_id, stage FROM outreach_events WHERE status = 'sent'"
            ).fetchall()
            for r in rows:
                result.setdefault(r["lead_id"], set()).add(r["stage"])
        return result

    def count_sent_on_date(self, date_prefix: str) -> int:
        """Count outbound emails actually dispatched on a given YYYY-MM-DD date."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM outreach_events WHERE status = 'sent' AND created_at LIKE ?",
                (f"{date_prefix}%",),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def record_pipeline_run(self, summary: PipelineRunSummary) -> None:
        started = summary.started_at or _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_runs (
                    run_id, mode, city, niche, fetched_count, added_count,
                    skipped_count, enriched_count, queued_count, drafted_count,
                    sent_count, duration_ms, status, error_message, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.run_id,
                    summary.mode,
                    summary.city,
                    summary.niche,
                    summary.fetched_count,
                    summary.added_count,
                    summary.skipped_count,
                    summary.enriched_count,
                    summary.queued_count,
                    summary.drafted_count,
                    summary.sent_count,
                    summary.duration_ms,
                    summary.status,
                    summary.error_message,
                    started,
                ),
            )

    def list_pipeline_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_analytics(self) -> Dict[str, Any]:
        """Compute aggregate CRM funnel, scoring, and outreach telemetry."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            with_email = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE TRIM(email) != ''"
            ).fetchone()[0]
            with_phone = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE TRIM(phone) != ''"
            ).fetchone()[0]
            avg_score_row = conn.execute("SELECT AVG(score) FROM leads").fetchone()
            avg_score = round(float(avg_score_row[0] or 0.0), 1)

            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM leads GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["cnt"] for r in status_rows}

            grade_rows = conn.execute(
                "SELECT grade, COUNT(*) AS cnt FROM leads GROUP BY grade"
            ).fetchall()
            by_grade = {r["grade"]: r["cnt"] for r in grade_rows}

            source_rows = conn.execute(
                "SELECT COALESCE(NULLIF(source, ''), 'imported') AS src, COUNT(*) AS cnt "
                "FROM leads GROUP BY src"
            ).fetchall()
            by_source = {r["src"]: r["cnt"] for r in source_rows}

            event_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM outreach_events GROUP BY status"
            ).fetchall()
            events_by_status = {r["status"]: r["cnt"] for r in event_rows}

            contacted_plus = sum(
                by_status.get(s, 0)
                for s in ("contacted", "replied", "booked", "won")
            )
            positive_replies = sum(
                by_status.get(s, 0) for s in ("replied", "booked", "won")
            )
            reply_rate = round((positive_replies / contacted_plus) * 100, 1) if contacted_plus else 0.0

            return {
                "total_leads": total,
                "with_email": with_email,
                "email_coverage_pct": round((with_email / total) * 100, 1) if total else 0.0,
                "with_phone": with_phone,
                "average_score": avg_score,
                "contacted_count": contacted_plus,
                "positive_replies": positive_replies,
                "reply_rate_pct": reply_rate,
                "by_status": by_status,
                "by_grade": by_grade,
                "by_source": by_source,
                "outreach_events": events_by_status,
            }
