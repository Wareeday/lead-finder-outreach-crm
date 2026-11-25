"""Interactive Web CRM Dashboard & JSON REST API powered by Python's ThreadingHTTPServer."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import urllib.parse
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import config
from src.email_drafter import render_email
from src.followup_scheduler import build_crm_queue, populate_record_followup_dates
from src.logging_config import get_logger
from src.models import LeadRecord, OutreachEvent
from src.pipeline import PipelineOptions, run_pipeline
from src.repository import CRMRepository

logger = get_logger("web_app")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _parse_target_date(raw: str | None) -> date:
    if not raw:
        return date.today()
    return datetime.strptime(raw.strip(), config.DATE_FORMAT).date()


class CRMRequestHandler(BaseHTTPRequestHandler):
    """REST API & SPA static asset handler bound to a CRMRepository."""

    server_version = "LeadCRM/2.0"

    @property
    def repo(self) -> CRMRepository:
        return self.server.repo  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("HTTP %s - %s", self.address_string(), format % args)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_csv(self, filename: str, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        raw = buf.getvalue().encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ValueError("Payload too large")
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        static_root = STATIC_DIR.resolve()
        target = (STATIC_DIR / clean).resolve()
        if not target.is_relative_to(static_root) or not target.is_file():
            self._send_json({"error": "Not found"}, status=404)
            return

        content_type, _ = mimetypes.guess_type(str(target))
        raw = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/api/health":
                self._send_json({"status": "ok", "db_path": str(self.repo.db_path)})
                return

            if path == "/api/analytics":
                analytics = self.repo.get_analytics()
                runs = self.repo.list_pipeline_runs(limit=15)
                sent_stages = self.repo.get_sent_stages_by_lead()
                all_leads = self.repo.list_leads(limit=5000)
                due_queue = build_crm_queue(
                    all_leads,
                    target_date=date.today(),
                    include_overdue=True,
                    include_initial=False,
                    sent_stages_by_lead=sent_stages,
                )
                analytics["due_today_count"] = len(due_queue)
                analytics["recent_runs"] = runs
                self._send_json(analytics)
                return

            if path == "/api/leads":
                leads = self.repo.list_leads(
                    status=query.get("status", [None])[0],
                    grade=query.get("grade", [None])[0],
                    city=query.get("city", [None])[0],
                    niche=query.get("niche", [None])[0],
                    search=query.get("search", [None])[0],
                    limit=int(query.get("limit", ["500"])[0]),
                    offset=int(query.get("offset", ["0"])[0]),
                )
                self._send_json({"leads": [l.to_api_dict() for l in leads], "total": len(leads)})
                return

            if path.startswith("/api/leads/") and path.endswith("/preview-email"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[2].isdigit():
                    lead = self.repo.get_lead(int(parts[2]))
                    if not lead:
                        self._send_json({"error": "Lead not found"}, status=404)
                        return
                    stage = query.get("stage", ["initial"])[0]
                    rendered = render_email(lead, stage=stage)
                    self._send_json(rendered)
                    return

            if path.startswith("/api/leads/"):
                lead_id_str = path.rstrip("/").rsplit("/", 1)[-1]
                if not lead_id_str.isdigit():
                    self._send_json({"error": "Invalid lead ID"}, status=400)
                    return
                lead = self.repo.get_lead(int(lead_id_str))
                if not lead:
                    self._send_json({"error": "Lead not found"}, status=404)
                    return
                events = self.repo.get_lead_events(int(lead_id_str))
                data = lead.to_api_dict()
                data["events"] = [e.__dict__ for e in events]
                self._send_json(data)
                return

            if path == "/api/queue":
                ref_date = _parse_target_date(query.get("date", [None])[0])
                include_initial = query.get("include_initial", ["false"])[0].lower() == "true"
                include_overdue = query.get("include_overdue", ["true"])[0].lower() == "true"
                records = self.repo.list_leads(limit=5000)
                sent_stages = self.repo.get_sent_stages_by_lead()
                queue = build_crm_queue(
                    records,
                    target_date=ref_date,
                    include_overdue=include_overdue,
                    include_initial=include_initial,
                    sent_stages_by_lead=sent_stages,
                )
                self._send_json({"date": ref_date.strftime(config.DATE_FORMAT), "queue": queue})
                return

            if path == "/api/export/csv":
                kind = query.get("kind", ["master"])[0]
                records = self.repo.list_leads(limit=10000)
                if kind == "extended":
                    self._send_csv(
                        "leads_enriched_crm.csv",
                        config.EXTENDED_COLUMNS,
                        [r.to_extended_row() for r in records],
                    )
                elif kind == "queue":
                    sent_stages = self.repo.get_sent_stages_by_lead()
                    queue = build_crm_queue(
                        records,
                        target_date=date.today(),
                        include_overdue=True,
                        include_initial=False,
                        sent_stages_by_lead=sent_stages,
                    )
                    self._send_csv("followups_today.csv", config.QUEUE_COLUMNS, queue)
                else:
                    self._send_csv(
                        "leads_updated.csv",
                        config.COLUMNS,
                        [r.to_sheet_row() for r in records],
                    )
                return

            # Static UI routes
            if path in {"/", "/index.html"}:
                self._serve_static("index.html")
                return
            self._serve_static(path)
        except Exception as exc:
            logger.exception("GET %s failed: %s", path, exc)
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()

            if path == "/api/leads":
                name = str(body.get("business_name", "")).strip()
                if not name:
                    self._send_json({"error": "business_name is required"}, status=400)
                    return
                rec = LeadRecord(
                    business_name=name,
                    owner_name=str(body.get("owner_name", "")).strip(),
                    profile_link=str(body.get("profile_link", "")).strip(),
                    website=str(body.get("website", "")).strip(),
                    phone=str(body.get("phone", "")).strip(),
                    email=str(body.get("email", "")).strip(),
                    city=str(body.get("city", "")).strip(),
                    notes=str(body.get("notes", "")).strip(),
                    niche=str(body.get("niche", "")).strip(),
                    source="manual",
                )
                added, skipped, touched = self.repo.upsert_leads([rec], enrich_existing=True)
                self._send_json(
                    {
                        "added": added,
                        "skipped": skipped,
                        "lead": touched[0].to_api_dict() if touched else None,
                    },
                    status=201 if added else 200,
                )
                return

            if path.startswith("/api/leads/") and path.endswith("/mark-sent"):
                parts = path.strip("/").split("/")
                if len(parts) != 4 or not parts[2].isdigit():
                    self._send_json({"error": "Invalid lead ID"}, status=400)
                    return
                lead = self.repo.get_lead(int(parts[2]))
                if not lead:
                    self._send_json({"error": "Lead not found"}, status=404)
                    return
                stage = str(body.get("stage", "initial")).strip()
                sent_date = str(body.get("date", date.today().strftime(config.DATE_FORMAT))).strip()
                rendered = render_email(lead, stage=stage)
                if stage == "initial" and not lead.outreach_date:
                    lead.outreach_date = sent_date
                    populate_record_followup_dates([lead])
                if lead.status in {"new", "enriched"}:
                    lead.status = "contacted"
                lead.last_contacted = sent_date
                updated = self.repo.save_lead(lead)
                self.repo.record_outreach_event(
                    OutreachEvent(
                        lead_id=lead.id,  # type: ignore[arg-type]
                        stage=stage,
                        channel="email",
                        status="sent",
                        subject=rendered["subject"],
                        body=rendered["body"],
                        recipient=lead.email,
                    )
                )
                self._send_json({"lead": updated.to_api_dict(), "stage": stage})
                return

            if path == "/api/pipeline/run":
                mode = str(body.get("mode", "demo")).strip().lower()
                if mode not in {"demo", "osm", "google"}:
                    self._send_json({"error": "mode must be demo, osm, or google"}, status=400)
                    return
                opts = PipelineOptions(
                    mode=mode,
                    city=str(body.get("city") or config.DEFAULT_CITY),
                    niche=str(body.get("niche") or config.DEFAULT_NICHE),
                    limit=max(1, min(100, int(body.get("limit", 15)))),
                    target_date=_parse_target_date(body.get("date")),
                    enrich=bool(body.get("enrich", False)),
                    draft_emails=bool(body.get("draft_emails", True)),
                    include_initial=bool(body.get("include_initial", False)),
                    input_csv=(
                        config.SAMPLE_DATA_DIR / "potential_leads_sample.csv"
                        if mode == "demo"
                        else None
                    ),
                )
                result = run_pipeline(self.repo, opts)
                self._send_json(
                    {
                        "summary": result.summary.__dict__,
                        "queue_count": len(result.queue),
                        "drafts_count": len(result.draft_paths),
                    }
                )
                return

            self._send_json({"error": "Endpoint not found"}, status=404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("POST %s failed: %s", path, exc)
            self._send_json({"error": str(exc)}, status=500)

    def do_PATCH(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            if not path.startswith("/api/leads/"):
                self._send_json({"error": "Endpoint not found"}, status=404)
                return
            lead_id_str = path.rstrip("/").rsplit("/", 1)[-1]
            if not lead_id_str.isdigit():
                self._send_json({"error": "Invalid lead ID"}, status=400)
                return

            body = self._read_json_body()
            updated = self.repo.update_lead_fields(int(lead_id_str), body)
            if not updated:
                self._send_json({"error": "Lead not found"}, status=404)
                return
            if updated.outreach_date:
                populate_record_followup_dates([updated])
                updated = self.repo.save_lead(updated)
            self._send_json({"lead": updated.to_api_dict()})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("PATCH %s failed: %s", path, exc)
            self._send_json({"error": str(exc)}, status=500)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/leads/"):
            self._send_json({"error": "Endpoint not found"}, status=404)
            return
        lead_id_str = path.rstrip("/").rsplit("/", 1)[-1]
        if not lead_id_str.isdigit():
            self._send_json({"error": "Invalid lead ID"}, status=400)
            return
        deleted = self.repo.delete_lead(int(lead_id_str))
        if not deleted:
            self._send_json({"error": "Lead not found"}, status=404)
            return
        self._send_json({"deleted": True})


class CRMHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer holding a shared CRMRepository reference."""

    def __init__(self, server_address: Tuple[str, int], repo: CRMRepository) -> None:
        self.repo = repo
        super().__init__(server_address, CRMRequestHandler)


def create_server(
    repo: CRMRepository | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> CRMHTTPServer:
    """Create a configured CRM HTTP server instance (useful for testing & CLI startup)."""
    return CRMHTTPServer((host, port), repo or CRMRepository())


def serve_dashboard(host: str = "127.0.0.1", port: int = 8000, db_path: Optional[Path] = None) -> None:
    """Run the Web CRM Dashboard blocking until interrupted."""
    repo = CRMRepository(db_path=db_path)
    server = create_server(repo=repo, host=host, port=port)
    actual_port = server.server_address[1]
    print(f"Lead Finder & Outreach CRM Dashboard running at http://{host}:{actual_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down CRM server...")
    finally:
        server.server_close()
