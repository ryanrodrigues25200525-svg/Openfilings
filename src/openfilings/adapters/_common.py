"""Small, dependency-free helpers shared by public-market adapters."""

from __future__ import annotations

import asyncio
import random
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any

import httpx

from openfilings.exceptions import SourceError
from openfilings.limits import MAX_TAGGED_DOCUMENT_BYTES


async def bounded_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int = MAX_TAGGED_DOCUMENT_BYTES,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request without allowing HTTPX to buffer an unbounded body."""

    follow_redirects = kwargs.pop("follow_redirects", None)
    request = client.build_request(method, url, **kwargs)
    send_options = (
        {"follow_redirects": follow_redirects} if follow_redirects is not None else {}
    )
    response = await client.send(
        request,
        stream=True,
        **send_options,
    )
    declared = response.headers.get("content-length")
    try:
        if declared is not None and int(declared) > max_bytes:
            await response.aclose()
            raise SourceError(f"Response exceeds the {max_bytes} byte limit.")
    except ValueError:
        pass
    # Mock transports may provide an already-materialized response. Production
    # requests reach this method with an unconsumed stream.
    if response.is_stream_consumed:
        if len(response.content) > max_bytes:
            raise SourceError(f"Response exceeds the {max_bytes} byte limit.")
        return response
    data = bytearray()
    try:
        # ``aiter_bytes`` retains HTTPX's normal content decoding. Using raw
        # chunks here would leave gzip-compressed bytes in ``response.content``
        # after we materialize the bounded stream, breaking JSON and XML
        # consumers for regulators that compress API replies.
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > max_bytes:
                raise SourceError(f"Response exceeds the {max_bytes} byte limit.")
    except Exception:
        await response.aclose()
        raise
    response._content = bytes(data)  # HTTPX has consumed the stream above.
    return response


def _jittered(delay: float) -> float:
    """Add +/-25% proportional jitter to a backoff delay.

    A fixed exponential schedule retries every failing adapter in lockstep -
    source="all" fans out to 20+ clients at once, so a shared upstream hiccup
    (a regulator's load balancer briefly rejecting connections) has every
    client retrying at the exact same instant, which is both impolite to a
    free public endpoint and self-defeating (the retries collide again).
    """

    return delay * random.uniform(0.75, 1.25)


class RetryingClient:
    """Bounded HTTP wrapper with consistent regulator error handling."""

    def __init__(
        self,
        source_label: str,
        *,
        timeout_seconds: float,
        max_retries: int,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._source_label = source_label
        self._max_retries = max_retries
        self._owns_client = client is None
        self._default_headers = headers or {}
        self._client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers=headers,
        )

    async def __aenter__(self) -> RetryingClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return_redirects = bool(kwargs.pop("_return_redirects", False))
        request_headers = {
            **self._default_headers,
            **dict(kwargs.pop("headers", {}) or {}),
        }
        kwargs["headers"] = request_headers
        for attempt in range(self._max_retries + 1):
            try:
                response = await bounded_request(self._client, method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    detail = str(exc).strip() or type(exc).__name__
                    raise SourceError(
                        f"{self._source_label} request failed: {detail}"
                    ) from exc
                await asyncio.sleep(_jittered(0.25 * (2**attempt)))
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                if return_redirects and response.is_redirect:
                    return response
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = response.text.strip()[:300] or response.reason_phrase
                    message = (
                        f"{self._source_label} returned "
                        f"{response.status_code}: {detail}"
                    )
                    raise SourceError(message) from exc
                return response
            if attempt >= self._max_retries:
                detail = response.text.strip()[:300] or response.reason_phrase
                raise SourceError(
                    f"{self._source_label} returned {response.status_code}: {detail}"
                )
            retry_after = response.headers.get("retry-after")
            try:
                delay = (
                    min(float(retry_after), 30.0)
                    if retry_after
                    else _jittered(0.5 * 2**attempt)
                )
            except ValueError:
                delay = _jittered(0.5 * 2**attempt)
            await asyncio.sleep(delay)
        raise AssertionError("retry loop exited unexpectedly")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^\w]+", " ", plain).split())


def slug(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", normalize_text(value))) or "document"


# Latin letters that NFKD does not decompose, because they are independent
# letters rather than a base plus a combining mark. Without these, a plain
# ASCII query can never substring-match the registered name: "orsted" is not
# a substring of "ørsted a s". Applied only when matching a search query -
# normalize_text itself also feeds slug(), which builds real KAP company
# URLs, so widening it there would change working document links.
_MATCH_TRANSLITERATIONS = str.maketrans(
    {
        "ø": "o",
        "æ": "ae",
        "œ": "oe",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ß": "ss",
        "ı": "i",  # noqa: RUF001 - Turkish dotless i, deliberate
    }
)


def match_text(value: str) -> str:
    """Fold text for search comparison, including non-decomposable letters."""

    return " ".join(normalize_text(value).translate(_MATCH_TRANSLITERATIONS).split())


def utc_midnight(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=UTC)


def ranked_matches(
    query: str,
    records: list[tuple[tuple[str, ...], Any]],
    *,
    limit: int,
) -> list[Any]:
    normalized_query = match_text(query)
    if not normalized_query:
        return []
    ranked: list[tuple[int, int, str, Any]] = []
    for fields, record in records:
        normalized_fields = tuple(match_text(field) for field in fields)
        if any(normalized_query in field for field in normalized_fields):
            if normalized_query in normalized_fields:
                rank = 0
            elif any(field.startswith(normalized_query) for field in normalized_fields):
                rank = 1
            else:
                rank = 2
            ranked.append((rank, 0, normalized_fields[-1], record))
            continue
        # Extra-token tolerance (issue #10): a query like "Ford Otosan"
        # is never a substring of the registered name ("ford otomotiv
        # sanayi a s"), so the strict check above turns a strong partial
        # match into no match at all. Fall back to token-subset scoring:
        # every significant query token that appears in a name field
        # counts, and at least half of them must hit. Single-token
        # queries are unaffected (one token either matches or it does
        # not), and tokens shorter than 3 characters are ignored so
        # stopwords like "a" or "s" cannot match every record. This does
        # NOT resolve true aliases ("PKO" appears nowhere in the legal
        # name) - those need per-source ticker/alias data, deferred.
        tokens = [token for token in normalized_query.split() if len(token) >= 3]
        if not tokens:
            continue
        matched = sum(
            1 for token in tokens if any(token in field for field in normalized_fields)
        )
        if matched * 2 < len(tokens):
            continue
        ranked.append((3, len(tokens) - matched, normalized_fields[-1], record))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [record for _, _, _, record in ranked[: max(1, limit)]]
