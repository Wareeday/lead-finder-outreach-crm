"""Lead Finder module supporting Demo, OpenStreetMap (Nominatim + Overpass), Google Places, and CSV import.

Every finder normalizes results into the canonical 18-column schema (`config.COLUMNS`)
so downstream scoring, deduplication, enrichment, and outreach work identically across sources.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from src.http_client import ResilientHTTPClient
from src.logging_config import get_logger
from src.models import LeadRecord
from src.sheet_manager import load_leads_csv

logger = get_logger("lead_finder")


def empty_lead_row() -> Dict[str, str]:
    """Return a blank lead dictionary containing all expected columns."""
    return {col: "" for col in config.COLUMNS}


def _escape_overpass(value: str) -> str:
    """Escape quotes and backslashes for safe Overpass QL string literals."""
    return value.replace("\\", "\\\\").replace('"', '\\"').strip()


def resolve_osm_tag(niche: str) -> Tuple[str, str]:
    """Resolve a human niche name or raw `key=value` string into an OSM tag tuple."""
    cleaned = (niche or "").strip()
    if "=" in cleaned:
        key, val = cleaned.split("=", 1)
        key = key.strip().lower()
        val = val.strip().lower()
        if key and val:
            return key, val
    niche_key = cleaned.lower().replace(" ", "_")
    return config.OSM_NICHE_TAGS.get(niche_key, ("amenity", niche_key))


class DemoLeadFinder:
    """Offline lead finder that reads sample JSON data for safe testing and instant demos."""

    def __init__(self, sample_file: Path | None = None) -> None:
        self.sample_file = sample_file or (config.SAMPLE_DATA_DIR / "demo_api_leads.json")

    def find_leads(self, city: str, niche: str, limit: int = 20) -> List[Dict[str, str]]:
        """Load mock leads from sample_data/demo_api_leads.json."""
        if not self.sample_file.exists():
            raise FileNotFoundError(f"Demo data file not found: {self.sample_file}")

        raw_items = json.loads(self.sample_file.read_text(encoding="utf-8"))
        leads: List[Dict[str, str]] = []

        for item in raw_items[:limit]:
            row = empty_lead_row()
            row["Business Name"] = str(item.get("name", "")).strip()
            row["Owner name"] = str(item.get("owner", "")).strip()
            row["Profile link"] = str(item.get("profile_link", "")).strip()
            row["Website"] = str(item.get("website", "")).strip()
            row["Phone no"] = str(item.get("phone", "")).strip()
            row["Email"] = str(item.get("email", "")).strip()
            row["Rating"] = str(item.get("rating", ""))
            row["Reviews"] = str(item.get("reviews", ""))
            row["map links"] = str(item.get("map_link", "")).strip()
            row["City"] = str(item.get("city", city)).strip()
            row["Notes"] = str(item.get("notes", f"Source: Demo ({niche})")).strip()
            row["Outreach"] = str(item.get("outreach", "")).strip()
            if row["Business Name"]:
                leads.append(row)

        return leads


class OpenStreetMapFinder:
    """100% FREE lead finder powered by Nominatim geocoding + OpenStreetMap Overpass API.

    Resolves ambiguous city names via Nominatim first to obtain an exact OSM area ID
    or bounding box, then queries Overpass with multi-mirror fallback and retries.
    """

    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    OVERPASS_MIRRORS = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    )

    def __init__(self, http_client: ResilientHTTPClient | None = None) -> None:
        self.http = http_client or ResilientHTTPClient(
            max_retries=2,
            backoff_base=1.5,
            timeout=30,
            min_host_interval=1.1,  # Respect Nominatim/Overpass 1 req/s etiquette
        )

    def _resolve_city_scope(self, city: str) -> Tuple[Optional[int], Optional[Tuple[float, float, float, float]]]:
        """Resolve city name via Nominatim into an Overpass area ID or bounding box."""
        params = urllib.parse.urlencode(
            {"q": city, "format": "jsonv2", "limit": 5, "addressdetails": 1}
        )
        url = f"{self.NOMINATIM_URL}?{params}"
        try:
            resp = self.http.get(url, timeout=15)
            candidates = json.loads(resp.text())
        except Exception as exc:
            logger.debug("Nominatim lookup failed for '%s', falling back to name filter: %s", city, exc)
            return None, None

        if not isinstance(candidates, list) or not candidates:
            return None, None

        # Prefer administrative boundary relations/ways over point nodes
        best = None
        for cand in candidates:
            if cand.get("osm_type") == "relation" and cand.get("category") == "boundary":
                best = cand
                break
        if best is None:
            best = candidates[0]

        osm_type = best.get("osm_type")
        osm_id = best.get("osm_id")
        if osm_type == "relation" and isinstance(osm_id, int):
            return 3_600_000_000 + osm_id, None
        if osm_type == "way" and isinstance(osm_id, int):
            return 2_400_000_000 + osm_id, None

        bbox = best.get("boundingbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                # Nominatim returns [south, north, west, east]
                south, north, west, east = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                return None, (south, west, north, east)
            except (ValueError, TypeError):
                pass
        return None, None

    def _build_overpass_query(
        self,
        city: str,
        osm_key: str,
        osm_val: str,
        limit: int,
        area_id: Optional[int],
        bbox: Optional[Tuple[float, float, float, float]],
    ) -> str:
        safe_key = _escape_overpass(osm_key)
        safe_val = _escape_overpass(osm_val)

        if area_id is not None:
            return f"""
            [out:json][timeout:25];
            area({area_id})->.searchArea;
            (
              node["{safe_key}"="{safe_val}"](area.searchArea);
              way["{safe_key}"="{safe_val}"](area.searchArea);
              relation["{safe_key}"="{safe_val}"](area.searchArea);
            );
            out center {limit};
            """
        if bbox is not None:
            s, w, n, e = bbox
            return f"""
            [out:json][timeout:25];
            (
              node["{safe_key}"="{safe_val}"]({s},{w},{n},{e});
              way["{safe_key}"="{safe_val}"]({s},{w},{n},{e});
              relation["{safe_key}"="{safe_val}"]({s},{w},{n},{e});
            );
            out center {limit};
            """

        safe_city = _escape_overpass(city)
        return f"""
        [out:json][timeout:25];
        area["name"="{safe_city}"]["boundary"="administrative"]->.searchArea;
        (
          node["{safe_key}"="{safe_val}"](area.searchArea);
          way["{safe_key}"="{safe_val}"](area.searchArea);
        );
        out center {limit};
        """

    def find_leads(self, city: str, niche: str, limit: int = 20) -> List[Dict[str, str]]:
        """Query OpenStreetMap for businesses matching `niche` inside `city`."""
        osm_key, osm_val = resolve_osm_tag(niche)
        area_id, bbox = self._resolve_city_scope(city)
        query = self._build_overpass_query(city, osm_key, osm_val, limit, area_id, bbox)
        data = urllib.parse.urlencode({"data": query}).encode("utf-8")

        payload = None
        last_err: Exception | None = None
        for mirror_url in self.OVERPASS_MIRRORS:
            try:
                resp = self.http.post(mirror_url, data=data, timeout=30)
                payload = json.loads(resp.text())
                break
            except Exception as exc:
                last_err = exc
                logger.warning("Overpass mirror %s failed: %s", mirror_url, exc)

        if payload is None:
            raise RuntimeError(f"Could not reach any Overpass API mirror: {last_err}") from last_err

        leads: List[Dict[str, str]] = []
        for elem in payload.get("elements", [])[:limit]:
            tags = elem.get("tags", {})
            name = (tags.get("name") or tags.get("name:en") or "").strip()
            if not name:
                continue

            lat = elem.get("lat") or elem.get("center", {}).get("lat")
            lon = elem.get("lon") or elem.get("center", {}).get("lon")
            map_link = (
                f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
                if lat and lon
                else ""
            )

            elem_type = elem.get("type", "node")
            elem_id = elem.get("id")
            osm_profile = (
                f"https://www.openstreetmap.org/{elem_type}/{elem_id}"
                if elem_id
                else ""
            )
            social_profile = (
                tags.get("contact:linkedin")
                or tags.get("contact:instagram")
                or tags.get("contact:facebook")
                or tags.get("facebook")
                or osm_profile
            )

            addr_parts = [
                tags.get("addr:housenumber", "").strip(),
                tags.get("addr:street", "").strip(),
                tags.get("addr:postcode", "").strip(),
            ]
            street_addr = " ".join(p for p in addr_parts if p)
            notes_parts = [f"Source: OpenStreetMap ({osm_key}={osm_val})"]
            if street_addr:
                notes_parts.append(f"Address: {street_addr}")
            if tags.get("opening_hours"):
                notes_parts.append(f"Hours: {tags['opening_hours']}")

            row = empty_lead_row()
            row["Business Name"] = name
            row["Profile link"] = social_profile
            row["Website"] = (tags.get("website") or tags.get("contact:website") or "").strip()
            row["Phone no"] = (
                tags.get("phone") or tags.get("contact:phone") or tags.get("mobile") or ""
            ).strip()
            row["Email"] = (tags.get("email") or tags.get("contact:email") or "").strip()
            row["map links"] = map_link
            row["City"] = (tags.get("addr:city") or city).strip()
            row["Notes"] = " | ".join(notes_parts)
            leads.append(row)

        logger.info("OSM returned %d leads for city='%s' niche='%s'", len(leads), city, niche)
        return leads


class GooglePlacesFinder:
    """Lead finder using Google Places API (New) Text Search with pagination & status filtering.

    Requires GOOGLE_PLACES_API_KEY in environment / .env.
    Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
    """

    ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

    def __init__(self, api_key: str | None = None, http_client: ResilientHTTPClient | None = None) -> None:
        self.api_key = api_key or os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError(
                "Missing GOOGLE_PLACES_API_KEY. Add it to your .env file or use --mode osm (100% free)."
            )
        self.http = http_client or ResilientHTTPClient(max_retries=2, timeout=25)

    def find_leads(self, city: str, niche: str, limit: int = 20) -> List[Dict[str, str]]:
        """Search Google Places API (New) for operational `niche` businesses in `city`."""
        field_mask = (
            "places.displayName,places.formattedAddress,places.websiteUri,"
            "places.nationalPhoneNumber,places.internationalPhoneNumber,"
            "places.rating,places.userRatingCount,places.googleMapsUri,"
            "places.businessStatus,nextPageToken"
        )
        leads: List[Dict[str, str]] = []
        page_token: Optional[str] = None

        while len(leads) < limit:
            page_size = min(20, limit - len(leads))
            payload_body: Dict[str, object] = {
                "textQuery": f"{niche} in {city}",
                "maxResultCount": page_size,
            }
            if page_token:
                payload_body["pageToken"] = page_token

            resp = self.http.post(
                self.ENDPOINT,
                data=json.dumps(payload_body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": field_mask,
                },
            )
            payload = json.loads(resp.text())
            places = payload.get("places", [])
            if not places:
                break

            for place in places:
                if place.get("businessStatus") == "CLOSED_PERMANENTLY":
                    continue
                name = place.get("displayName", {}).get("text", "").strip()
                if not name:
                    continue

                notes = f"Source: Google Places ({niche})"
                if place.get("formattedAddress"):
                    notes += f" | Address: {place['formattedAddress']}"

                row = empty_lead_row()
                row["Business Name"] = name
                row["Website"] = place.get("websiteUri", "").strip()
                row["Phone no"] = (
                    place.get("internationalPhoneNumber")
                    or place.get("nationalPhoneNumber")
                    or ""
                ).strip()
                row["Rating"] = str(place.get("rating", ""))
                row["Reviews"] = str(place.get("userRatingCount", ""))
                row["map links"] = place.get("googleMapsUri", "").strip()
                row["City"] = city
                row["Notes"] = notes
                leads.append(row)
                if len(leads) >= limit:
                    break

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        logger.info("Google Places returned %d leads for city='%s' niche='%s'", len(leads), city, niche)
        return leads


class CsvImportFinder:
    """Import and normalize external CSV lead lists using flexible column-alias mapping."""

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = csv_path

    def find_leads(self, city: str = "", niche: str = "", limit: int = 1000) -> List[Dict[str, str]]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Import CSV not found: {self.csv_path}")
        rows = load_leads_csv(self.csv_path)[:limit]
        for row in rows:
            if city and not row.get("City"):
                row["City"] = city
            if niche and not row.get("Notes"):
                row["Notes"] = f"Imported CSV ({niche})"
        return rows


def get_finder(mode: str, *, input_csv: Path | None = None):
    """Factory returning the requested lead finder instance."""
    mode_clean = mode.strip().lower()
    if mode_clean == "demo":
        return DemoLeadFinder()
    if mode_clean == "osm":
        return OpenStreetMapFinder()
    if mode_clean == "google":
        return GooglePlacesFinder()
    if mode_clean == "csv":
        if not input_csv:
            raise ValueError("CSV import mode requires --input-csv <path>.")
        return CsvImportFinder(input_csv)
    raise ValueError(f"Unsupported mode '{mode}'. Choose from: demo, osm, google, csv.")


def rows_to_lead_records(
    rows: List[Dict[str, str]],
    *,
    niche: str = "",
    source: str = "",
) -> List[LeadRecord]:
    """Convert raw sheet-format dicts into typed LeadRecord instances."""
    return [
        LeadRecord.from_sheet_row(row, niche=niche, source=source)
        for row in rows
        if (row.get("Business Name") or "").strip()
    ]
