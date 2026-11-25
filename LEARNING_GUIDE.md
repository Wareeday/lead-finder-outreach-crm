# Architecture & Customization Guide

This guide walks through how the v2.0 `lead-finder-outreach-crm` pipeline operates end-to-end and how to customize schemas, discovery niches, scoring weights, follow-up cadences, email copy, and integrations without rewriting core modules.

---

## 1. End-to-End Architecture

Every run—whether triggered from the CLI (`main.py`) or the Web Dashboard (`src/web_app.py`)—flows through `src/pipeline.py`:

1. **Discovery (`src/lead_finder.py`)** — Fetches businesses from `demo`, `osm` (Nominatim geocoding + Overpass QL with multi-mirror fallback), `google` (Places API New with pagination & closure filtering), or `csv` (flexible column-alias importer) and normalizes them into `LeadRecord` objects.
2. **Website Enrichment (`src/enricher.py`)** — Safely inspects public lead websites using `ResilientHTTPClient` (`src/http_client.py`) with SSRF validation, extracting public emails, social profiles, HTTPS status, CMS signatures, booking widgets, and live chat signals.
3. **Deduplication & Scoring (`src/sheet_manager.py`, `src/scorer.py`, `src/repository.py`)** — Resolves duplicates by normalized root domain, normalized phone digits, canonicalized business name + city (ignoring legal suffixes like `LLC`/`Inc.`), and fuzzy similarity, then computes a `0–100` ICP qualification score (`A`–`D`) and tailored `Outreach Angle`.
4. **SQLite CRM Persistence (`src/repository.py`)** — Stores leads, `outreach_events`, and `pipeline_runs` in `output/crm.db` (SQLite WAL mode) with schema migration tracking and funnel analytics.
5. **Stage-Aware Scheduling (`src/followup_scheduler.py`)** — Calculates `F #1`–`F #6` (`+2, +4, +7, +10, +14, +21` days after `Outreach`), detects due and overdue stages without double-queuing, skips stages already logged as `sent`, and automatically suppresses `replied`, `booked`, `won`, `lost`, `bounced`, and `unsubscribed` leads.
6. **Outreach & Exports (`src/email_drafter.py`, `src/sheet_manager.py`)** — Writes collision-safe `.txt`/`.html` drafts, optionally dispatches via rate-limited SMTP with daily send caps, writes atomic CSV/JSON/Markdown exports, and optionally syncs tabs to Google Sheets.

---

## 2. Customization Cookbook

### A. Customize Follow-Up Cadence
Edit `FOLLOWUP_DAYS` in `config.py`:
```python
FOLLOWUP_DAYS = [2, 4, 7, 10, 14, 21]
```

### B. Add New OpenStreetMap Niches
Add entries to `OSM_NICHE_TAGS` in `config.py` using [OpenStreetMap Map Features](https://wiki.openstreetmap.org/wiki/Map_features):
```python
OSM_NICHE_TAGS["bakery"] = ("shop", "bakery")
```
Or pass any raw tag on the CLI at runtime:
```bash
python3 main.py run --mode osm --city "Austin" --niche "shop=bakery" --limit 25
```

### C. Tune Lead Scoring Weights
Adjust `SCORING_WEIGHTS` in `config.py` (`has_email`, `has_phone`, `has_owner_name`, `has_website`, `high_rating`, `review_volume`, `automation_opportunity`) to prioritize signals that match your ideal client profile.

### D. Customize Multi-Stage Outreach Copy
Edit `EMAIL_TEMPLATES` and `STAGE_EMAIL_TEMPLATES` in `config.py`. Available placeholders:
`{business_name}`, `{owner_name}`, `{greeting_name}`, `{city}`, `{website}`, `{niche}`, `{rating}`, `{reviews}`, `{pain_point_hook}`, `{stage}`, `{sender_name}`, `{sender_company}`, `{sender_signature}`, and `{unsubscribe_note}`.

### E. Connect Live Google Sheets Sync
1. Create a Google Cloud Service Account and download `credentials.json`.
2. Share your spreadsheet with the service account email (`...@...iam.gserviceaccount.com`) as **Editor**.
3. Copy `.env.example` to `.env`, set `GOOGLE_SHEET_ID`, install optional extras (`pip install -e ".[integrations]"`), and run:
```bash
python3 main.py sync
```
