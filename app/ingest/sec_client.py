import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

# Stay under the SEC fair-access limit of 10 requests per second.
DEFAULT_MIN_INTERVAL_SECONDS = 0.12


class SecClient:
    """HTTP client for EDGAR. Every request sends SEC_USER_AGENT and is throttled."""

    def __init__(
        self,
        user_agent: str,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
        timeout: float = 60.0,
        max_attempts: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        cleaned = user_agent.strip()
        if not cleaned:
            raise ValueError("SEC_USER_AGENT is required")
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._last_request_at = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": cleaned, "Accept": "*/*"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SecClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def company_facts(self, cik: str) -> dict:
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        return self.get_json(url)

    def submissions(self, cik: str) -> dict:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        return self.get_json(url)

    def filing_document(self, cik: str, accession: str, primary_document: str) -> str:
        cik_int = str(int(cik))
        accession_nodash = accession.replace("-", "")
        url = (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{cik_int}/{accession_nodash}/{primary_document}"
        )
        return self.get_text(url)

    def get_json(self, url: str) -> dict:
        response = self._get(url)
        payload = json.loads(response.text, parse_float=Decimal)
        if not isinstance(payload, dict):
            raise ValueError(f"expected JSON object from {url}")
        return payload

    def get_text(self, url: str) -> str:
        return self._get(url).text

    def _get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._throttle()
            try:
                response = self._client.get(url)
            except httpx.TransportError as exc:
                last_error = exc
                self._backoff(attempt, url, str(exc))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"SEC responded {response.status_code} for {url}",
                    request=response.request,
                    response=response,
                )
                self._backoff(attempt, url, str(response.status_code))
                continue
            response.raise_for_status()
            return response
        assert last_error is not None
        raise last_error

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self._min_interval:
            self._sleeper(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, url: str, reason: str) -> None:
        if attempt >= self._max_attempts:
            logger.error(
                "SEC request failed (%s) attempt %s/%s url=%s",
                reason,
                attempt,
                self._max_attempts,
                url,
            )
            return
        delay = min(2 ** (attempt - 1), 16)
        logger.warning(
            "SEC request failed (%s) attempt %s/%s url=%s; retrying in %ss",
            reason,
            attempt,
            self._max_attempts,
            url,
            delay,
        )
        self._sleeper(delay)
