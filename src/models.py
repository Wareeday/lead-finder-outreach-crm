"""Typed domain models for leads, enrichment signals, scoring, outreach events, and pipeline runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import config


@dataclass
class EnrichmentSignals:
    """Structured digital-maturity and contact signals discovered from a lead's website."""

    reachable: bool = False
    http_status: Optional[int] = None
    uses_https: bool = False
    page_title: str = ""
    emails_found: List[str] = field(default_factory=list)
    social_links: Dict[str, str] = field(default_factory=dict)
    cms_detected: str = ""
    has_booking_widget: bool = False
    has_live_chat: bool = False
    has_contact_form: bool = False
    mobile_viewport: bool = False
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "EnrichmentSignals":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            return cls(
                reachable=bool(data.get("reachable", False)),
                http_status=data.get("http_status"),
                uses_https=bool(data.get("uses_https", False)),
                page_title=str(data.get("page_title", "")),
                emails_found=list(data.get("emails_found", [])),
                social_links=dict(data.get("social_links", {})),
                cms_detected=str(data.get("cms_detected", "")),
                has_booking_widget=bool(data.get("has_booking_widget", False)),
                has_live_chat=bool(data.get("has_live_chat", False)),
                has_contact_form=bool(data.get("has_contact_form", False)),
                mobile_viewport=bool(data.get("mobile_viewport", False)),
                error=str(data.get("error", "")),
            )
        except (ValueError, TypeError):
            return cls()


@dataclass
class LeadScore:
    """Computed ICP qualification score and recommended outreach positioning."""

    score: int
    grade: str
    breakdown: Dict[str, int]
    outreach_angle: str


@dataclass
class LeadRecord:
    """Canonical CRM lead entity bridging SQLite storage and 18-column CSV/Sheet compatibility."""

    business_name: str
    owner_name: str = ""
    profile_link: str = ""
    website: str = ""
    phone: str = ""
    email: str = ""
    rating: str = ""
    reviews: str = ""
    map_link: str = ""
    city: str = ""
    notes: str = ""
    outreach_date: str = ""
    f1: str = ""
    f2: str = ""
    f3: str = ""
    f4: str = ""
    f5: str = ""
    f6: str = ""
    # Extended CRM metadata
    id: Optional[int] = None
    canonical_key: str = ""
    status: str = "new"
    score: int = 0
    grade: str = "C"
    outreach_angle: str = ""
    niche: str = ""
    source: str = ""
    opted_out: bool = False
    last_contacted: str = ""
    enrichment_json: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_sheet_row(self) -> Dict[str, str]:
        """Serialize to the canonical 18-column dictionary format."""
        return {
            "Business Name": self.business_name,
            "Owner name": self.owner_name,
            "Profile link": self.profile_link,
            "Website": self.website,
            "Phone no": self.phone,
            "Email": self.email,
            "Rating": self.rating,
            "Reviews": self.reviews,
            "map links": self.map_link,
            "City": self.city,
            "Notes": self.notes,
            "Outreach": self.outreach_date,
            "F #1": self.f1,
            "F #2": self.f2,
            "F #3": self.f3,
            "F #4": self.f4,
            "F #5": self.f5,
            "F #6": self.f6,
        }

    def to_extended_row(self) -> Dict[str, str]:
        """Serialize including CRM score, grade, status, and outreach angle."""
        row = self.to_sheet_row()
        row.update(
            {
                "CRM Status": self.status,
                "Lead Score": str(self.score),
                "Lead Grade": self.grade,
                "Outreach Angle": self.outreach_angle,
                "Niche": self.niche,
                "Source": self.source,
                "Last Contacted": self.last_contacted,
            }
        )
        return row

    def to_api_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-friendly dictionary for the REST API and dashboard."""
        return {
            "id": self.id,
            "canonical_key": self.canonical_key,
            "business_name": self.business_name,
            "owner_name": self.owner_name,
            "profile_link": self.profile_link,
            "website": self.website,
            "phone": self.phone,
            "email": self.email,
            "rating": self.rating,
            "reviews": self.reviews,
            "map_link": self.map_link,
            "city": self.city,
            "notes": self.notes,
            "outreach_date": self.outreach_date,
            "followups": {
                "F #1": self.f1,
                "F #2": self.f2,
                "F #3": self.f3,
                "F #4": self.f4,
                "F #5": self.f5,
                "F #6": self.f6,
            },
            "status": self.status,
            "score": self.score,
            "grade": self.grade,
            "outreach_angle": self.outreach_angle,
            "niche": self.niche,
            "source": self.source,
            "opted_out": self.opted_out,
            "last_contacted": self.last_contacted,
            "enrichment": asdict(EnrichmentSignals.from_json(self.enrichment_json)),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_sheet_row(
        cls,
        row: Dict[str, str],
        *,
        niche: str = "",
        source: str = "",
        status: str = "new",
    ) -> "LeadRecord":
        """Create a LeadRecord from a sheet-format dictionary."""
        return cls(
            business_name=(row.get("Business Name") or "").strip(),
            owner_name=(row.get("Owner name") or "").strip(),
            profile_link=(row.get("Profile link") or "").strip(),
            website=(row.get("Website") or "").strip(),
            phone=(row.get("Phone no") or "").strip(),
            email=(row.get("Email") or "").strip(),
            rating=(row.get("Rating") or "").strip(),
            reviews=(row.get("Reviews") or "").strip(),
            map_link=(row.get("map links") or "").strip(),
            city=(row.get("City") or "").strip(),
            notes=(row.get("Notes") or "").strip(),
            outreach_date=(row.get("Outreach") or "").strip(),
            f1=(row.get("F #1") or "").strip(),
            f2=(row.get("F #2") or "").strip(),
            f3=(row.get("F #3") or "").strip(),
            f4=(row.get("F #4") or "").strip(),
            f5=(row.get("F #5") or "").strip(),
            f6=(row.get("F #6") or "").strip(),
            status=(row.get("CRM Status") or status or "new").strip().lower(),
            niche=(row.get("Niche") or niche).strip(),
            source=(row.get("Source") or source).strip(),
        )


@dataclass
class OutreachEvent:
    """Audit event for drafted, sent, failed, or replied outreach interactions."""

    lead_id: int
    stage: str
    channel: str
    status: str
    subject: str = ""
    body: str = ""
    recipient: str = ""
    error_message: str = ""
    created_at: str = ""
    id: Optional[int] = None


@dataclass
class PipelineRunSummary:
    """Observability record for a single pipeline execution."""

    run_id: str
    mode: str
    city: str
    niche: str
    fetched_count: int = 0
    added_count: int = 0
    skipped_count: int = 0
    enriched_count: int = 0
    queued_count: int = 0
    drafted_count: int = 0
    sent_count: int = 0
    duration_ms: int = 0
    status: str = "success"
    error_message: str = ""
    started_at: str = ""


def ensure_sheet_columns(row: Dict[str, str]) -> Dict[str, str]:
    """Return a clean dict guaranteed to contain every key in config.COLUMNS."""
    return {col: str(row.get(col, "") or "").strip() for col in config.COLUMNS}
