"""Low-level HTTP client for the NM Water Data API.

Handles:
  - JWT auth flow (POST /v1/auth/token, refresh on 401)
  - Rate limiting (default to 1000 req/min, well under the 1500 cap)
  - Retry with exponential backoff on transient failures
  - Pagination for list endpoints

This module knows nothing about the lake or canonical models —
it just returns JSON dicts.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.emnrd.nm.gov/wda"

_TRANSIENT_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


class NMWaterDataAuthError(RuntimeError):
    """Raised when credentials are missing or token exchange fails."""


class NMWaterDataClient:
    """Stateful client. Holds the access token and refreshes on demand."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        base_url: str | None = None,
        *,
        rate_limit_per_minute: int = 1000,
        timeout: float = 30.0,
    ) -> None:
        # Prefer explicit args, then Django settings, then env vars
        try:
            from django.conf import settings
            settings_username = getattr(settings, "NM_WDA_USERNAME", None)
            settings_password = getattr(settings, "NM_WDA_PASSWORD", None)
        except Exception:
            settings_username = None
            settings_password = None

        self.username = username or settings_username or os.getenv("NM_WDA_USERNAME") or ""
        self.password = password or settings_password or os.getenv("NM_WDA_PASSWORD") or ""
        self.base_url = (base_url or os.getenv("NM_WDA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._http = requests.Session()
        self._timeout = timeout
        self._min_interval = 60.0 / rate_limit_per_minute
        self._last_call_ts = 0.0

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _have_credentials(self) -> bool:
        return bool(self.username and self.password)

    def authenticate(self) -> None:
        if not self._have_credentials():
            raise NMWaterDataAuthError(
                "NM_WDA_USERNAME / NM_WDA_PASSWORD not set. Register at "
                "https://api.emnrd.nm.gov/wda/ and put creds in .env"
            )
        url = f"{self.base_url}/v1/auth/token"
        resp = self._http.post(
            url,
            json={"username": self.username, "password": self.password},
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise NMWaterDataAuthError(f"Token exchange failed: {resp.status_code} {resp.text}")
        body = resp.json()
        self._access_token = body["access_token"]
        self._refresh_token = body.get("refresh_token")

    def _ensure_authenticated(self) -> None:
        if self._access_token is None:
            self.authenticate()

    # ── Request plumbing ─────────────────────────────────────────────────────

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call_ts = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        self._ensure_authenticated()
        self._throttle()
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                headers = {"Authorization": f"Bearer {self._access_token}"}
                resp = self._http.get(url, params=params, headers=headers, timeout=self._timeout)
                if resp.status_code == 401:
                    # Token expired — re-auth once and retry
                    logger.info("NM WDA token expired, re-authenticating")
                    self.authenticate()
                    headers["Authorization"] = f"Bearer {self._access_token}"
                    resp = self._http.get(url, params=params, headers=headers, timeout=self._timeout)
                resp.raise_for_status()
                return resp
            except _TRANSIENT_ERRORS as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning("NM WDA _get attempt %d/4 failed: %s — retrying in %ds", attempt + 1, exc, wait)
                time.sleep(wait)
        raise last_exc  # type: ignore[misc]

    # ── Public endpoints ─────────────────────────────────────────────────────

    def list_wells(
        self,
        *,
        county: str | None = None,
        operator: str | None = None,
        page_size: int = 200,
    ) -> Iterator[dict]:
        """Stream well records matching the filter, paginated.

        Yields raw dicts directly (no source_url tuple — just the well data).
        Each dict contains at minimum: api14, operator, county, latitude, longitude.
        """
        page = 1
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": page_size}
            if county:
                params["county"] = county
            if operator:
                params["operator"] = operator
            resp = self._get("/v1/wells", params=params)
            body = resp.json()
            items = body.get("items", body if isinstance(body, list) else [])
            if not items:
                return
            for item in items:
                yield item
            if len(items) < page_size:
                return
            page += 1

    def healthcheck(self) -> bool:
        try:
            self._ensure_authenticated()
            return True
        except NMWaterDataAuthError:
            return False

    def close(self) -> None:
        self._http.close()
