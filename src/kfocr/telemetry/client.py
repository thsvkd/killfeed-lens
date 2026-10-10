from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_TELEMETRY_HOSTS = frozenset({"telemetry-cdn.pubg.com"})


class PubgApiClient:
    """Small synchronous PUBG API client with a bounded retry budget.

    Authentication is attached per player request rather than configured on the
    HTTP client. This prevents credentials from reaching match, telemetry, or
    redirect destinations.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.pubg.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        max_retry_after: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        allowed_telemetry_hosts: frozenset[str] = _DEFAULT_TELEMETRY_HOSTS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_base < 0:
            raise ValueError("backoff_base cannot be negative")
        if max_retry_after <= 0:
            raise ValueError("max_retry_after must be positive")
        self.api_key = api_key if api_key is not None else os.getenv("PUBG_API_KEY")
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._jitter = jitter
        self.allowed_telemetry_hosts = allowed_telemetry_hosts

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PubgApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_player(self, shard: str, player_name: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("PUBG_API_KEY is required for player lookup")
        response = self._request(
            f"{self.base_url}/shards/{shard}/players",
            params={"filter[playerNames]": player_name},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("PUBG player response must be a JSON object")
        return payload

    def get_match(self, shard: str, match_id: str) -> dict[str, Any]:
        response = self._request(f"{self.base_url}/shards/{shard}/matches/{match_id}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("PUBG match response must be a JSON object")
        return payload

    def get_telemetry(self, telemetry_url: str) -> list[dict[str, Any]]:
        self._validate_telemetry_url(telemetry_url)
        response = self._request(
            telemetry_url,
            headers={"Accept-Encoding": "gzip"},
        )
        payload = response.json()
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("PUBG telemetry response must be an array of event objects")
        return payload

    def _request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {
            "Accept": "application/vnd.api+json",
            "User-Agent": "killfeed-lens/0.1",
        }
        if headers:
            request_headers.update(headers)

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            except httpx.RequestError:
                if attempt >= self.max_retries:
                    raise
                self._sleep(self._backoff(attempt))
                continue

            if response.is_redirect:
                raise httpx.HTTPStatusError(
                    "Redirects are disabled to keep request credentials origin-bound",
                    request=response.request,
                    response=response,
                )
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response
            if attempt >= self.max_retries:
                response.raise_for_status()
            self._sleep(self._retry_delay(response, attempt))

        raise AssertionError("bounded retry loop exhausted unexpectedly")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            parsed = _parse_retry_after(retry_after)
            if parsed is not None:
                if parsed > self.max_retry_after:
                    raise RuntimeError(
                        "Server Retry-After exceeds the bounded API wait budget"
                    )
                return parsed
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base * (2**attempt) + self.backoff_base * self._jitter(), 60.0)

    def _validate_telemetry_url(self, telemetry_url: str) -> None:
        parsed = urlparse(telemetry_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Telemetry URL must use HTTPS")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Telemetry URL may not include credentials or a non-standard port")
        if parsed.hostname.lower() not in self.allowed_telemetry_hosts:
            raise ValueError("Telemetry URL host is not an approved PUBG telemetry host")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
