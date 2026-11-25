"""Resilient HTTP client with rate limiting, exponential backoff, and SSRF protection."""

from __future__ import annotations

import ipaddress
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

from src.logging_config import get_logger

logger = get_logger("http_client")

DEFAULT_USER_AGENT = "LeadFinderOutreachCRM/2.0 (+https://github.com/example/lead-finder-outreach-crm)"
RETRYABLE_STATUS_CODES: Set[int] = {429, 500, 502, 503, 504}
MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap


class SSRFBlockedError(ValueError):
    """Raised when a URL points to a disallowed scheme or private/internal network address."""


@dataclass
class HTTPResponse:
    """Simplified HTTP response payload."""

    status_code: int
    url: str
    headers: Dict[str, str]
    body: bytes

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


def is_public_ip(ip_str: str) -> bool:
    """Return True only if an IP address is globally routable (not loopback/private/reserved)."""
    addr = ipaddress.ip_address(ip_str)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_public_http_url(url: str) -> str:
    """Validate scheme, host, and resolved IP addresses to prevent SSRF attacks."""
    candidate = (url or "").strip()
    if not candidate:
        raise SSRFBlockedError("Empty URL")

    if "://" not in candidate:
        # Reject dangerous pseudo-schemes like javascript: or data:
        if ":" in candidate.split("/", 1)[0]:
            scheme_prefix = candidate.split(":", 1)[0].lower()
            if scheme_prefix not in {"http", "https"} and not scheme_prefix.endswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
                raise SSRFBlockedError(f"Unsupported URL scheme: {scheme_prefix}")
        candidate = f"https://{candidate}"

    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise SSRFBlockedError(f"Unsupported URL scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlockedError("Missing hostname")
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise SSRFBlockedError("Loopback hostname is not permitted")

    try:
        # Check if literal IP first
        ipaddress.ip_address(hostname)
        if not is_public_ip(hostname):
            raise SSRFBlockedError(f"Private or non-routable IP blocked: {hostname}")
    except ValueError:
        # Resolve DNS and verify every returned IP is public
        try:
            infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise ConnectionError(f"DNS resolution failed for {hostname}: {exc}") from exc

        for info in infos:
            ip_addr = info[4][0]
            # Strip IPv6 scope id if present
            ip_addr = ip_addr.split("%", 1)[0]
            if not is_public_ip(ip_addr):
                raise SSRFBlockedError(f"Host {hostname} resolved to blocked IP {ip_addr}")

    return candidate


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ensure HTTP redirects cannot bounce to internal/private network destinations."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute_target = urllib.parse.urljoin(req.full_url, newurl)
        validate_public_http_url(absolute_target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HostRateLimiter:
    """Thread-safe per-hostname minimum interval limiter."""

    def __init__(self, min_interval_seconds: float = 1.0) -> None:
        self.min_interval = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._last_request: Dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self.min_interval <= 0:
            return
        host = urllib.parse.urlparse(url).netloc.lower()
        with self._lock:
            now = time.monotonic()
            previous = self._last_request.get(host)
            if previous is not None:
                elapsed = now - previous
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
            self._last_request[host] = time.monotonic()


class ResilientHTTPClient:
    """HTTP client featuring per-host rate limiting, retries, and SSRF validation."""

    def __init__(
        self,
        *,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: int = 20,
        min_host_interval: float = 0.5,
        user_agent: Optional[str] = None,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.rate_limiter = HostRateLimiter(min_interval_seconds=min_host_interval)
        self.user_agent = user_agent or os.getenv("HTTP_USER_AGENT", DEFAULT_USER_AGENT)
        self._safe_opener = urllib.request.build_opener(_SafeRedirectHandler())

    def request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Dict[str, str] | None = None,
        timeout: int | None = None,
        ssrf_protect: bool = False,
        max_bytes: int = MAX_HTML_BYTES,
    ) -> HTTPResponse:
        target_url = validate_public_http_url(url) if ssrf_protect else url
        req_headers = {"User-Agent": self.user_agent}
        if headers:
            req_headers.update(headers)

        req_timeout = timeout or self.timeout
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self.rate_limiter.wait(target_url)
            req = urllib.request.Request(
                target_url,
                data=data,
                headers=req_headers,
                method=method.upper(),
            )
            try:
                opener = self._safe_opener if ssrf_protect else urllib.request.build_opener()
                with opener.open(req, timeout=req_timeout) as resp:
                    raw = resp.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raw = raw[:max_bytes]
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    return HTTPResponse(
                        status_code=getattr(resp, "status", 200),
                        url=resp.geturl(),
                        headers=resp_headers,
                        body=raw,
                    )
            except urllib.error.HTTPError as exc:
                last_exc = exc
                status = exc.code
                if status in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = self._compute_delay(attempt, retry_after)
                    logger.warning(
                        "HTTP %s from %s (attempt %d/%d); retrying in %.1fs",
                        status,
                        target_url,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                body_snippet = ""
                try:
                    body_snippet = exc.read(512).decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise RuntimeError(
                    f"HTTP {status} from {target_url}: {body_snippet or exc.reason}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self._compute_delay(attempt, None)
                    logger.warning(
                        "Network error on %s (%s); retrying in %.1fs",
                        target_url,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Network request failed for {target_url}: {exc}") from exc

        raise RuntimeError(f"Exhausted retries for {target_url}: {last_exc}")

    def get(self, url: str, **kwargs) -> HTTPResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> HTTPResponse:
        return self.request("POST", url, **kwargs)

    def _compute_delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        jitter = random.uniform(0.1, 0.5)
        return min((self.backoff_base * (2**attempt)) + jitter, 20.0)
