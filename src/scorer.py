"""Lead Scoring & Qualification Engine.

Scores each lead from 0 to 100 across contactability, market reputation, and
automation opportunity signals, assigning an actionable tier (A-D) and a
tailored outreach angle.
"""

from __future__ import annotations

import re
from typing import Dict

import config
from src.models import EnrichmentSignals, LeadRecord, LeadScore

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_float(val: str) -> float:
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: str) -> int:
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return 0


def compute_lead_score(
    lead: LeadRecord | Dict[str, str],
    signals: EnrichmentSignals | None = None,
) -> LeadScore:
    """Compute a deterministic 0-100 qualification score, grade, and outreach angle."""
    if isinstance(lead, LeadRecord):
        email = lead.email.strip()
        phone = lead.phone.strip()
        owner = lead.owner_name.strip()
        website = lead.website.strip()
        rating = _safe_float(lead.rating)
        reviews = _safe_int(lead.reviews)
        enrichment = signals or EnrichmentSignals.from_json(lead.enrichment_json)
    else:
        email = (lead.get("Email") or "").strip()
        phone = (lead.get("Phone no") or "").strip()
        owner = (lead.get("Owner name") or "").strip()
        website = (lead.get("Website") or "").strip()
        rating = _safe_float(lead.get("Rating", ""))
        reviews = _safe_int(lead.get("Reviews", ""))
        enrichment = signals or EnrichmentSignals()

    weights = config.SCORING_WEIGHTS
    breakdown: Dict[str, int] = {}

    breakdown["email"] = weights["has_email"] if _EMAIL_RE.match(email) else 0
    breakdown["phone"] = weights["has_phone"] if len(re.sub(r"\D", "", phone)) >= 7 else 0
    breakdown["owner"] = weights["has_owner_name"] if owner else 0
    breakdown["website"] = weights["has_website"] if website else 0

    if rating >= 4.3:
        breakdown["rating"] = weights["high_rating"]
    elif rating >= 3.8:
        breakdown["rating"] = max(1, weights["high_rating"] // 2)
    else:
        breakdown["rating"] = 0

    if reviews >= 25:
        breakdown["reviews"] = weights["review_volume"]
    elif reviews >= 5:
        breakdown["reviews"] = max(1, weights["review_volume"] // 2)
    else:
        breakdown["reviews"] = 0

    # Digital maturity / automation gap scoring
    opp_max = weights["automation_opportunity"]
    if not website:
        breakdown["opportunity"] = opp_max
        angle = "No website listed — pitch automated intake page, Google/map presence & lead capture"
    elif enrichment.reachable:
        if not enrichment.has_booking_widget and not enrichment.has_live_chat:
            breakdown["opportunity"] = int(opp_max * 0.85)
            angle = "Active site lacks online booking & instant chat — pitch 24/7 lead capture automation"
        elif not enrichment.uses_https:
            breakdown["opportunity"] = int(opp_max * 0.75)
            angle = "Unencrypted HTTP site — pitch modern conversion flow, HTTPS upgrade & auto follow-ups"
        elif not enrichment.has_booking_widget:
            breakdown["opportunity"] = int(opp_max * 0.65)
            angle = "No automated booking widget detected — pitch calendar scheduling & reminders"
        else:
            breakdown["opportunity"] = int(opp_max * 0.35)
            angle = "Modern web presence — pitch CRM follow-up sequencing & reactivation workflows"
    else:
        breakdown["opportunity"] = int(opp_max * 0.45)
        angle = "Pitch automated lead follow-up & review-to-booking workflow"

    total = min(100, sum(breakdown.values()))
    if total >= 75:
        grade = "A"
    elif total >= 55:
        grade = "B"
    elif total >= 35:
        grade = "C"
    else:
        grade = "D"

    return LeadScore(score=total, grade=grade, breakdown=breakdown, outreach_angle=angle)


def build_pain_point_hook(lead: LeadRecord | Dict[str, str], signals: EnrichmentSignals | None = None) -> str:
    """Return a natural 1-sentence personalization hook for outreach templates."""
    if isinstance(lead, LeadRecord):
        website = lead.website.strip()
        rating = _safe_float(lead.rating)
        reviews = _safe_int(lead.reviews)
        enrichment = signals or EnrichmentSignals.from_json(lead.enrichment_json)
        name = lead.business_name.strip() or "your team"
    else:
        website = (lead.get("Website") or "").strip()
        rating = _safe_float(lead.get("Rating", ""))
        reviews = _safe_int(lead.get("Reviews", ""))
        enrichment = signals or EnrichmentSignals()
        name = (lead.get("Business Name") or "").strip() or "your team"

    if rating >= 4.5 and reviews >= 20:
        reputation_phrase = f"Your {rating:.1f}-star reputation across {reviews} reviews really stands out."
    elif rating >= 4.0:
        reputation_phrase = f"It's great to see {name}'s strong {rating:.1f}-star rating."
    else:
        reputation_phrase = ""

    if not website:
        gap_phrase = (
            "I noticed there isn't a direct website link on your map listing yet, which often "
            "causes high-intent visitors to drop off before booking."
        )
    elif enrichment.reachable and not enrichment.has_booking_widget:
        gap_phrase = (
            "I took a quick look at your site and noticed visitors can't book or trigger an "
            "instant callback directly online without manual back-and-forth."
        )
    elif enrichment.reachable and not enrichment.uses_https:
        gap_phrase = (
            "I noticed your site is still loading over standard HTTP rather than HTTPS and lacks "
            "automated inquiry routing."
        )
    else:
        gap_phrase = (
            "With busy local teams, it's easy for warm inquiries to slip through the cracks "
            "without instant follow-up."
        )

    return f"{reputation_phrase} {gap_phrase}".strip()


def score_lead_record(record: LeadRecord) -> LeadRecord:
    """Score a LeadRecord in place and return it."""
    signals = EnrichmentSignals.from_json(record.enrichment_json)
    result = compute_lead_score(record, signals)
    record.score = result.score
    record.grade = result.grade
    record.outreach_angle = result.outreach_angle
    return record
