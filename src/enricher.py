"""Website Contact & Digital-Signal Enricher.

Safely fetches public lead websites (with SSRF protection, size limits, and
concurrent worker pooling) to extract contact emails, social profiles, CMS
footprints, and conversion/booking gaps for scoring and outreach personalization.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple

from src.http_client import ResilientHTTPClient, SSRFBlockedError
from src.logging_config import get_logger
from src.models import EnrichmentSignals, LeadRecord
from src.scorer import score_lead_record

logger = get_logger("enricher")

_EMAIL_REGEX = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,24}\b")
_JUNK_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "sentry.io",
    "wixpress.com",
    "w3.org",
    "schema.org",
    "domain.com",
    "yourdomain.com",
}
_JUNK_EMAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_JUNK_LOCAL_PARTS = ("noreply", "no-reply", "donotreply", "mailer-daemon", "webmaster@localhost")

_SOCIAL_PATTERNS: Dict[str, re.Pattern[str]] = {
    "linkedin": re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[^\s\"'<>]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[^\s\"'<>]+", re.I),
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[^\s\"'<>]+", re.I),
    "x": re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s\"'<>]+", re.I),
}

_BOOKING_HINTS = (
    "calendly.com",
    "acuityscheduling.com",
    "janeapp.com",
    "zocdoc.com",
    "fresha.com",
    "booksy.com",
    "mindbodyonline.com",
    "setmore.com",
    "squareup.com/appointments",
    "vagaro.com",
    "opentable.com",
    "resy.com",
    "book now",
    "book appointment",
    "schedule online",
)

_CHAT_HINTS = (
    "intercom.io",
    "widget.intercom.io",
    "js.driftt.com",
    "tidio.co",
    "zdassets.com",
    "tawk.to",
    "crisp.chat",
    "livechatinc.com",
    "wa.me/",
    "api.whatsapp.com",
)

_CMS_SIGNATURES: Tuple[Tuple[str, str], ...] = (
    ("wp-content/", "WordPress"),
    ("cdn.shopify.com", "Shopify"),
    ("wixstatic.com", "Wix"),
    ("squarespace.com", "Squarespace"),
    ("webflow.com", "Webflow"),
    ("ghost.io", "Ghost"),
)


class _HTMLSignalParser(HTMLParser):
    """Lightweight stdlib HTML parser extracting title, links, mailtos, forms, and viewport."""

    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_chunks: List[str] = []
        self.links: List[str] = []
        self.mailto_emails: List[str] = []
        self.has_form = False
        self.has_viewport = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        t = tag.lower()
        if t == "title":
            self.in_title = True
        elif t == "form":
            self.has_form = True
        elif t == "meta" and attr_map.get("name", "").lower() == "viewport":
            self.has_viewport = True
        elif t == "a":
            href = attr_map.get("href", "").strip()
            if not href:
                return
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?", 1)[0].strip()
                if addr:
                    self.mailto_emails.append(addr)
            else:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_chunks.append(data)


def _filter_valid_emails(candidates: List[str]) -> List[str]:
    cleaned: List[str] = []
    seen: Set[str] = set()
    for raw in candidates:
        email = raw.strip().lower().strip(".,;:()<>[]\"'")
        if not email or "@" not in email:
            continue
        local, domain = email.rsplit("@", 1)
        if domain in _JUNK_EMAIL_DOMAINS:
            continue
        if any(email.endswith(ext) for ext in _JUNK_EMAIL_EXTENSIONS):
            continue
        if any(junk in local for junk in _JUNK_LOCAL_PARTS):
            continue
        if len(email) > 120:
            continue
        if email not in seen:
            seen.add(email)
            cleaned.append(email)
    return cleaned


def extract_signals_from_html(html: str, final_url: str, status_code: int = 200) -> EnrichmentSignals:
    """Analyze HTML markup and return structured contact & maturity signals."""
    parser = _HTMLSignalParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    lower_html = html.lower()
    raw_emails = list(parser.mailto_emails) + _EMAIL_REGEX.findall(html)
    emails = _filter_valid_emails(raw_emails)

    social_links: Dict[str, str] = {}
    for network, pattern in _SOCIAL_PATTERNS.items():
        match = pattern.search(html)
        if match:
            social_links[network] = match.group(0).rstrip(".,;)\"'")

    cms = ""
    for sig, label in _CMS_SIGNATURES:
        if sig in lower_html:
            cms = label
            break

    return EnrichmentSignals(
        reachable=True,
        http_status=status_code,
        uses_https=final_url.lower().startswith("https://"),
        page_title=" ".join("".join(parser.title_chunks).split())[:140],
        emails_found=emails,
        social_links=social_links,
        cms_detected=cms,
        has_booking_widget=any(h in lower_html for h in _BOOKING_HINTS),
        has_live_chat=any(h in lower_html for h in _CHAT_HINTS),
        has_contact_form=parser.has_form,
        mobile_viewport=parser.has_viewport,
    )


def _find_contact_subpages(base_url: str, links: List[str], limit: int = 2) -> List[str]:
    """Identify up to `limit` internal contact/about pages to check when homepage lacks an email."""
    base_host = urllib.parse.urlparse(base_url).netloc.lower()
    candidates: List[str] = []
    seen: Set[str] = {base_url}
    for href in links:
        low = href.lower()
        if not any(k in low for k in ("contact", "about", "team", "book", "reach")):
            continue
        abs_url = urllib.parse.urljoin(base_url, href).split("#", 1)[0]
        parsed = urllib.parse.urlparse(abs_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() == base_host:
            if abs_url not in seen:
                seen.add(abs_url)
                candidates.append(abs_url)
                if len(candidates) >= limit:
                    break
    return candidates


class WebsiteEnricher:
    """Concurrent, SSRF-safe website enricher for B2B leads."""

    def __init__(
        self,
        http_client: ResilientHTTPClient | None = None,
        max_workers: int | None = None,
    ) -> None:
        timeout = int(os.getenv("ENRICH_TIMEOUT_SECONDS", "10"))
        self.http = http_client or ResilientHTTPClient(
            max_retries=1,
            backoff_base=0.5,
            timeout=timeout,
            min_host_interval=0.25,
        )
        self.max_workers = max_workers or int(os.getenv("ENRICH_MAX_WORKERS", "4"))

    def enrich_url(self, website_url: str) -> EnrichmentSignals:
        """Fetch a business website (plus contact page if needed) and extract signals."""
        try:
            resp = self.http.get(website_url, ssrf_protect=True)
        except SSRFBlockedError as exc:
            return EnrichmentSignals(reachable=False, error=f"Blocked unsafe URL: {exc}")
        except Exception as exc:
            return EnrichmentSignals(reachable=False, error=str(exc))

        content_type = resp.headers.get("content-type", "")
        if content_type and "html" not in content_type.lower() and "text" not in content_type.lower():
            return EnrichmentSignals(
                reachable=True,
                http_status=resp.status_code,
                uses_https=resp.url.lower().startswith("https://"),
                error=f"Non-HTML content-type: {content_type}",
            )

        html = resp.text()
        signals = extract_signals_from_html(html, resp.url, resp.status_code)

        # Follow internal contact/about links if homepage didn't expose an email
        if not signals.emails_found:
            parser = _HTMLSignalParser()
            try:
                parser.feed(html)
            except Exception:
                pass
            for sub_url in _find_contact_subpages(resp.url, parser.links, limit=2):
                try:
                    sub_resp = self.http.get(sub_url, ssrf_protect=True)
                    sub_signals = extract_signals_from_html(
                        sub_resp.text(), sub_resp.url, sub_resp.status_code
                    )
                    for email in sub_signals.emails_found:
                        if email not in signals.emails_found:
                            signals.emails_found.append(email)
                    for net, link in sub_signals.social_links.items():
                        signals.social_links.setdefault(net, link)
                    signals.has_booking_widget = signals.has_booking_widget or sub_signals.has_booking_widget
                    signals.has_live_chat = signals.has_live_chat or sub_signals.has_live_chat
                    signals.has_contact_form = signals.has_contact_form or sub_signals.has_contact_form
                    if signals.emails_found:
                        break
                except Exception:
                    continue

        return signals

    def enrich_lead(self, lead: LeadRecord) -> Tuple[LeadRecord, bool]:
        """Enrich a single LeadRecord in place. Returns `(lead, enriched_successfully)`."""
        if not lead.website.strip():
            score_lead_record(lead)
            return lead, False

        signals = self.enrich_url(lead.website)
        lead.enrichment_json = signals.to_json()

        if signals.emails_found and not lead.email.strip():
            lead.email = signals.emails_found[0]

        preferred_social = (
            signals.social_links.get("linkedin")
            or signals.social_links.get("instagram")
            or signals.social_links.get("facebook")
        )
        if preferred_social and (
            not lead.profile_link.strip() or "openstreetmap.org" in lead.profile_link
        ):
            lead.profile_link = preferred_social

        if signals.reachable and lead.status == "new":
            lead.status = "enriched"

        score_lead_record(lead)
        return lead, signals.reachable

    def enrich_batch(self, leads: List[LeadRecord], *, force: bool = False) -> int:
        """Concurrently enrich leads that have websites and haven't been enriched yet."""
        targets = [
            lead
            for lead in leads
            if lead.website.strip() and (force or not lead.enrichment_json)
        ]
        if not targets:
            return 0

        enriched_count = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.enrich_lead, lead): lead for lead in targets}
            for future in as_completed(futures):
                try:
                    _, ok = future.result()
                    if ok:
                        enriched_count += 1
                except Exception as exc:
                    logger.warning("Enrichment worker failed: %s", exc)

        logger.info("Enriched %d/%d websites", enriched_count, len(targets))
        return enriched_count
