"""Central configuration for the Lead Finder & Outreach CRM.

Provides defaults, CRM column schemas, follow-up cadence, OpenStreetMap tag
mappings, multi-stage outreach templates, lead-scoring weights, and a built-in
`.env` loader that works with or without `python-dotenv`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

# Base paths (all relative to project root so the repo works anywhere)
BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DATA_DIR = BASE_DIR / "sample_data"
OUTPUT_DIR = BASE_DIR / "output"
EMAIL_DRAFTS_DIR = OUTPUT_DIR / "email_drafts"
LOGS_DIR = OUTPUT_DIR / "logs"
DEFAULT_DB_PATH = OUTPUT_DIR / "crm.db"

# Canonical 18-column schema matching the Google Sheet ("Potential leads" tab)
COLUMNS: List[str] = [
    "Business Name",
    "Owner name",
    "Profile link",
    "Website",
    "Phone no",
    "Email",
    "Rating",
    "Reviews",
    "map links",
    "City",
    "Notes",
    "Outreach",
    "F #1",
    "F #2",
    "F #3",
    "F #4",
    "F #5",
    "F #6",
]

EXTENDED_COLUMNS: List[str] = COLUMNS + [
    "CRM Status",
    "Lead Score",
    "Lead Grade",
    "Outreach Angle",
    "Niche",
    "Source",
    "Last Contacted",
]

QUEUE_COLUMNS: List[str] = [
    "Due Date",
    "Follow-Up Stage",
    "Overdue Days",
    "Lead Score",
    "Lead Grade",
    "CRM Status",
    "Business Name",
    "Owner name",
    "Email",
    "Phone no",
    "Website",
    "City",
    "Outreach Angle",
    "Notes",
]

# Follow-up schedule in days after the initial Outreach date
FOLLOWUP_DAYS: List[int] = [2, 4, 7, 10, 14, 21]

# ISO-8601 date format used across the sheet, database, and CLI
DATE_FORMAT = "%Y-%m-%d"

# Default lead-search settings (override via CLI flags)
DEFAULT_CITY = "Austin"
DEFAULT_NICHE = "dentist"
DEFAULT_LIMIT = 20

# CRM lifecycle statuses
CRM_STATUSES: Tuple[str, ...] = (
    "new",
    "enriched",
    "contacted",
    "replied",
    "booked",
    "won",
    "lost",
    "unsubscribed",
    "bounced",
)

# Leads in these statuses are automatically suppressed from outbound follow-ups
SUPPRESSED_STATUSES: frozenset[str] = frozenset(
    {"replied", "booked", "won", "lost", "unsubscribed", "bounced"}
)

# Mapping from everyday niche terms to OpenStreetMap (OSM) tags.
# Users can also pass raw `key=value` strings (e.g., `--niche "shop=bakery"`).
# Reference: https://wiki.openstreetmap.org/wiki/Map_features
OSM_NICHE_TAGS: Dict[str, Tuple[str, str]] = {
    "dentist": ("amenity", "dentist"),
    "dental": ("amenity", "dentist"),
    "clinic": ("amenity", "clinic"),
    "doctors": ("amenity", "doctors"),
    "pharmacy": ("amenity", "pharmacy"),
    "veterinary": ("amenity", "veterinary"),
    "vet": ("amenity", "veterinary"),
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "coffee": ("amenity", "cafe"),
    "bakery": ("shop", "bakery"),
    "gym": ("leisure", "fitness_centre"),
    "fitness": ("leisure", "fitness_centre"),
    "salon": ("shop", "hairdresser"),
    "barber": ("shop", "hairdresser"),
    "spa": ("leisure", "spa"),
    "plumber": ("craft", "plumber"),
    "electrician": ("craft", "electrician"),
    "hvac": ("craft", "hvac"),
    "car_repair": ("shop", "car_repair"),
    "auto_repair": ("shop", "car_repair"),
    "real_estate": ("office", "estate_agent"),
    "lawyer": ("office", "lawyer"),
    "accounting": ("office", "accountant"),
    "architect": ("office", "architect"),
    "insurance": ("office", "insurance"),
    "it": ("office", "it"),
    "coworking": ("amenity", "coworking_space"),
    "hotel": ("tourism", "hotel"),
    "optician": ("shop", "optician"),
}

# Weighted scoring configuration (sum of positive potential max = 100)
SCORING_WEIGHTS: Dict[str, int] = {
    "has_email": 28,
    "has_phone": 14,
    "has_owner_name": 10,
    "has_website": 10,
    "high_rating": 10,          # rating >= 4.3
    "review_volume": 8,         # reviews >= 25
    "automation_opportunity": 20,  # missing booking/chat widget or outdated site
}

# Email templates for initial outreach and multi-stage follow-ups.
# Available placeholders:
#   {business_name}, {owner_name}, {greeting_name}, {city}, {website},
#   {niche}, {rating}, {reviews}, {pain_point_hook}, {stage},
#   {sender_name}, {sender_company}, {unsubscribe_note}
EMAIL_TEMPLATES: Dict[str, Dict[str, str]] = {
    "initial": {
        "subject": "Quick idea to help {business_name} save time in {city}",
        "body": (
            "Hi {greeting_name},\n\n"
            "I came across {business_name} while researching local businesses in {city}. "
            "{pain_point_hook}\n\n"
            "I build lightweight Python and AI automations that help small teams eliminate "
            "repetitive admin work and capture more inbound leads.\n\n"
            "Would you be open to a quick 10-minute chat this week to see if a simple "
            "workflow could save your team a few hours?\n\n"
            "Best regards,\n"
            "{sender_name}\n"
            "{sender_signature}"
            "{unsubscribe_note}"
        ),
    },
    "followup": {
        "subject": "Re: Quick idea for {business_name} (Follow-up {stage})",
        "body": (
            "Hi {greeting_name},\n\n"
            "Just floating this back to the top of your inbox in case it got buried. "
            "I'd love to share a quick 2-minute example of how we automate lead tracking "
            "and client follow-ups for businesses like {business_name}.\n\n"
            "Let me know if you'd like me to send it over.\n\n"
            "Best regards,\n"
            "{sender_name}\n"
            "{sender_signature}"
            "{unsubscribe_note}"
        ),
    },
}

# Optional stage-specific follow-up variations for richer multi-touch sequences
STAGE_EMAIL_TEMPLATES: Dict[str, Dict[str, str]] = {
    "F #1": {
        "subject": "Re: Quick idea for {business_name}",
        "body": (
            "Hi {greeting_name},\n\n"
            "Quick bump on my note from a couple of days ago. {pain_point_hook}\n\n"
            "Happy to record a 90-second walkthrough tailored specifically to {business_name} "
            "so you can review it whenever you have a spare minute.\n\n"
            "Best regards,\n"
            "{sender_name}\n"
            "{sender_signature}"
            "{unsubscribe_note}"
        ),
    },
    "F #2": {
        "subject": "2-minute workflow example for {business_name}",
        "body": (
            "Hi {greeting_name},\n\n"
            "Most teams in {city} we speak with lose leads simply because follow-ups fall "
            "through the cracks during busy hours.\n\n"
            "I put together a short checklist of the 3 highest-ROI automations for local "
            "businesses—shall I send the link over?\n\n"
            "Best regards,\n"
            "{sender_name}\n"
            "{sender_signature}"
            "{unsubscribe_note}"
        ),
    },
    "F #3": {
        "subject": "Worth exploring for {business_name}?",
        "body": (
            "Hi {greeting_name},\n\n"
            "Just checking in once more—are you currently looking at ways to streamline "
            "inquiry follow-ups and booking workflows at {business_name}, or is everything "
            "already handled in-house?\n\n"
            "A quick 'yes' or 'not right now' helps me know whether to follow up.\n\n"
            "Best regards,\n"
            "{sender_name}\n"
            "{sender_signature}"
            "{unsubscribe_note}"
        ),
    },
    "F #6": {
        "subject": "Closing the loop — {business_name}",
        "body": (
            "Hi {greeting_name},\n\n"
            "I know things get busy, so I'll close the loop on my end for now. "
            "If automating lead capture or follow-ups ever becomes a priority for {business_name}, "
            "feel free to reply anytime.\n\n"
            "Wishing you and the team continued success in {city}!\n\n"
            "Best regards,\n"
            "{sender_name}\n"
            "{sender_signature}"
            "{unsubscribe_note}"
        ),
    },
}


def load_env_file(env_path: Path | None = None) -> None:
    """Load environment variables from `.env` via python-dotenv or built-in parser."""
    target = env_path or (BASE_DIR / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=target)
        return
    except ImportError:
        pass

    if not target.exists():
        return

    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_db_path() -> Path:
    """Return configured SQLite database path."""
    raw = os.getenv("CRM_DB_PATH", "").strip()
    if not raw:
        return DEFAULT_DB_PATH
    path = Path(raw)
    return path if path.is_absolute() else (BASE_DIR / path)
