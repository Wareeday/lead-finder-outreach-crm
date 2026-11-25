"""Tests for SSRF protection, website signal extraction, and mocked OSM/Google lead finders."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from src.enricher import WebsiteEnricher, extract_signals_from_html
from src.http_client import HTTPResponse, SSRFBlockedError, validate_public_http_url
from src.lead_finder import GooglePlacesFinder, OpenStreetMapFinder, resolve_osm_tag


class TestSSRFAndEnrichment(unittest.TestCase):
    def test_ssrf_blocks_private_and_loopback_addresses(self) -> None:
        for bad_url in (
            "http://127.0.0.1:8000/admin",
            "http://localhost/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
            "file:///etc/passwd",
        ):
            with self.assertRaises(SSRFBlockedError, msg=f"Should block {bad_url}"):
                validate_public_http_url(bad_url)

    def test_extract_signals_from_html(self) -> None:
        sample_html = """
        <html>
          <head>
            <title>Austin Premier Dental Care</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="/wp-content/themes/dental/style.css">
          </head>
          <body>
            <a href="mailto:care@austinpremier.com">Email Us</a>
            <a href="/contact">Contact</a>
            <a href="https://www.linkedin.com/company/austin-premier-dental">LinkedIn</a>
            <img src="ignore@example.com" alt="avatar@sentry.io">
            <script src="https://assets.calendly.com/assets/external/widget.js"></script>
            <form><input type="email"></form>
          </body>
        </html>
        """
        signals = extract_signals_from_html(sample_html, "https://austinpremier.com", 200)
        self.assertTrue(signals.reachable)
        self.assertTrue(signals.uses_https)
        self.assertEqual(signals.page_title, "Austin Premier Dental Care")
        self.assertEqual(signals.cms_detected, "WordPress")
        self.assertTrue(signals.has_booking_widget)
        self.assertFalse(signals.has_live_chat)
        self.assertTrue(signals.has_contact_form)
        self.assertTrue(signals.mobile_viewport)
        self.assertEqual(signals.emails_found, ["care@austinpremier.com"])
        self.assertIn("linkedin", signals.social_links)

    def test_enricher_follows_contact_subpage(self) -> None:
        mock_http = MagicMock()
        mock_http.get.side_effect = [
            HTTPResponse(
                status_code=200,
                url="https://clinic.example.org",
                headers={"content-type": "text/html"},
                body=b"<html><body><h1>Welcome</h1><a href='/contact'>Contact Us</a></body></html>",
            ),
            HTTPResponse(
                status_code=200,
                url="https://clinic.example.org/contact",
                headers={"content-type": "text/html"},
                body=b"<html><body>Reach us at <a href='mailto:frontdesk@clinic.org'>email</a></body></html>",
            ),
        ]
        enricher = WebsiteEnricher(http_client=mock_http, max_workers=1)
        signals = enricher.enrich_url("https://clinic.example.org")
        self.assertTrue(signals.reachable)
        self.assertEqual(signals.emails_found, ["frontdesk@clinic.org"])


class TestMockedAPIFinders(unittest.TestCase):
    def test_resolve_osm_tag_supports_aliases_and_raw_tags(self) -> None:
        self.assertEqual(resolve_osm_tag("dentist"), ("amenity", "dentist"))
        self.assertEqual(resolve_osm_tag("shop=florist"), ("shop", "florist"))

    def test_osm_finder_nominatim_plus_mirror_failover(self) -> None:
        mock_http = MagicMock()
        mock_http.get.return_value = HTTPResponse(
            status_code=200,
            url="https://nominatim.openstreetmap.org/search",
            headers={},
            body=json.dumps([{"osm_type": "relation", "osm_id": 113314, "category": "boundary"}]).encode(),
        )
        # First mirror fails, second mirror succeeds
        mock_http.post.side_effect = [
            RuntimeError("HTTP 504 Gateway Timeout"),
            HTTPResponse(
                status_code=200,
                url="https://overpass.kumi.systems/api/interpreter",
                headers={},
                body=json.dumps(
                    {
                        "elements": [
                            {
                                "type": "node",
                                "id": 999111,
                                "lat": 30.267,
                                "lon": -97.743,
                                "tags": {
                                    "name": "Riverside Dental",
                                    "website": "https://riversidedental.org",
                                    "contact:phone": "+1-512-555-0133",
                                    "addr:city": "Austin",
                                },
                            }
                        ]
                    }
                ).encode(),
            ),
        ]

        finder = OpenStreetMapFinder(http_client=mock_http)
        results = finder.find_leads(city='Austin "TX"', niche="dentist", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["Business Name"], "Riverside Dental")
        self.assertEqual(results[0]["Profile link"], "https://www.openstreetmap.org/node/999111")
        self.assertEqual(results[0]["Phone no"], "+1-512-555-0133")

    def test_google_places_finder_pagination_and_closed_filter(self) -> None:
        mock_http = MagicMock()
        mock_http.post.side_effect = [
            HTTPResponse(
                status_code=200,
                url=GooglePlacesFinder.ENDPOINT,
                headers={},
                body=json.dumps(
                    {
                        "places": [
                            {"displayName": {"text": "Closed Clinic"}, "businessStatus": "CLOSED_PERMANENTLY"},
                            {
                                "displayName": {"text": "Open Clinic"},
                                "businessStatus": "OPERATIONAL",
                                "websiteUri": "https://openclinic.org",
                                "internationalPhoneNumber": "+1 512 555 0100",
                                "rating": 4.8,
                                "userRatingCount": 95,
                                "googleMapsUri": "https://maps.google.com/?cid=1",
                            },
                        ],
                        "nextPageToken": "page2",
                    }
                ).encode(),
            ),
            HTTPResponse(
                status_code=200,
                url=GooglePlacesFinder.ENDPOINT,
                headers={},
                body=json.dumps(
                    {
                        "places": [
                            {
                                "displayName": {"text": "Second Page Clinic"},
                                "businessStatus": "OPERATIONAL",
                            }
                        ]
                    }
                ).encode(),
            ),
        ]
        finder = GooglePlacesFinder(api_key="test-key", http_client=mock_http)
        leads = finder.find_leads(city="Austin", niche="clinic", limit=5)
        self.assertEqual(len(leads), 2)
        self.assertEqual([l["Business Name"] for l in leads], ["Open Clinic", "Second Page Clinic"])


if __name__ == "__main__":
    unittest.main()
