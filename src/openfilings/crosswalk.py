"""Ticker/ISIN-to-LEI crosswalk for markets whose sources key on LEIs.

UK FCA NSM and the ESEF jurisdictions identify companies by LEI, so a caller
holding only a ticker or an ISIN needs a bridge: OpenFIGI maps the identifier
to an issuer name, GLEIF maps that name to an LEI, and the market's own
company-ID construction (``uk_lei_<LEI>``, ``nl_lei_<LEI>``, ...) completes
the answer. Both upstreams are keyless.

OpenFIGI's keyless tier allows 25 mapping requests per 60-second window, so
POSTs are paced one per ``_MIN_POST_INTERVAL_SECONDS`` per event loop, and a
``ratelimit-remaining: 0`` reply parks the next POST until
``ratelimit-reset`` (both headers confirmed live: ``25;w=60``, reset counts
down in seconds). Live probing on 2026-10-10 also confirmed that OpenFIGI
rejects explicit ``null`` job fields (``"exchCode": null`` is an error), that
tickers are case-sensitive, that a miss is ``{"warning": "No identifier
found."}``, and that the free tier answers without an API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from openfilings._version import __version__
from openfilings.adapters._common import RetryingClient
from openfilings.config import Settings
from openfilings.exceptions import ConfigurationError, OpenFilingsError, SourceError

_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
_LEI_RECORDS_URL = "https://api.gleif.org/api/v1/lei-records"
_MIN_POST_INTERVAL_SECONDS = 2.4
_MAX_JOBS_PER_REQUEST = 10
_MAX_VERIFIED_CANDIDATES = 3
# LEIs are 20 alphanumerics starting with a 4-character LOU prefix; ISINs are
# a 2-letter country, a 9-character NSIN, and one check digit.
_LEI_PATTERN = re.compile(r"^[A-Z0-9]{20}$")
_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# Legal-name words for tokenizing company names ("ASML Holding N.V." ->
# asml/holding/n/v; a bare "shel" is not a word of "SHELL INTERNATIONAL").
_NAME_WORD = re.compile(r"[^\W_]+")
# Corporate-form words dropped from the tail of a GLEIF search term.
_NAME_SUFFIXES = frozenset(
    {
        "ab",
        "ag",
        "as",
        "asa",
        "bv",
        "co",
        "corp",
        "corporation",
        "gmbh",
        "inc",
        "incorporated",
        "jsc",
        "kk",
        "ltd",
        "limited",
        "llc",
        "nv",
        "oao",
        "oyj",
        "pao",
        "pcl",
        "plc",
        "pte",
        "sa",
        "se",
        "spa",
        "srl",
    }
)


@dataclass(frozen=True, slots=True)
class MarketHint:
    """OpenFIGI/GLEIF parameters for one LEI-keyed filing market."""

    market: str
    exchange_codes: tuple[str, ...]
    jurisdiction: str
    id_prefix: str
    source: str
    country_name: str


# Exchange codes were verified live against OpenFIGI on 2026-10-10 with one
# listed equity per market: ASML@NA, TSCO@LN, AI@FP, ITX@SM, ENI@IM,
# VOLVB@SS, NOVOB@DC, NOKIA@FH, EQNR@NO, PKO@PW, GALP@PL, ABI@BB, EBS@AV,
# REIN@LX. The codes follow a Bloomberg-style two-letter taxonomy rather
# than ISO country codes, which is why the easy ones to swap are wrong:
# "PW" is Warsaw (Poland), "PL" is Euronext Lisbon (Portugal), and "LX" is
# the Luxembourg Stock Exchange ("LU" is not an OpenFIGI exchange code).
# London is messier: symbols that carry a dot on the LSE use a slash in
# OpenFIGI ("BP." -> "BP/", "BT.A" -> "BT/A"), so a plain "BP" is a miss
# there; no variant expansion is attempted, only dot-free tickers like TSCO
# resolve as written. German market codes would be ("GR", "GF", "GY", ...)
# if a DE market is ever added.
MARKET_HINTS: dict[str, MarketHint] = {
    "uk": MarketHint(
        market="uk",
        exchange_codes=("LN",),
        jurisdiction="GB",
        id_prefix="uk",
        source="fca_nsm",
        country_name="United Kingdom",
    ),
    "nl": MarketHint(
        market="nl",
        exchange_codes=("NA",),
        jurisdiction="NL",
        id_prefix="nl",
        source="esef",
        country_name="Netherlands",
    ),
    "fr": MarketHint(
        market="fr",
        exchange_codes=("FP",),
        jurisdiction="FR",
        id_prefix="fr",
        source="esef",
        country_name="France",
    ),
    "es": MarketHint(
        market="es",
        exchange_codes=("SM",),
        jurisdiction="ES",
        id_prefix="es",
        source="esef",
        country_name="Spain",
    ),
    "it": MarketHint(
        market="it",
        exchange_codes=("IM",),
        jurisdiction="IT",
        id_prefix="it",
        source="esef",
        country_name="Italy",
    ),
    "dk": MarketHint(
        market="dk",
        exchange_codes=("DC",),
        jurisdiction="DK",
        id_prefix="dk",
        source="esef",
        country_name="Denmark",
    ),
    "se": MarketHint(
        market="se",
        exchange_codes=("SS",),
        jurisdiction="SE",
        id_prefix="se",
        source="esef",
        country_name="Sweden",
    ),
    "fi": MarketHint(
        market="fi",
        exchange_codes=("FH",),
        jurisdiction="FI",
        id_prefix="fi",
        source="esef",
        country_name="Finland",
    ),
    "no": MarketHint(
        market="no",
        exchange_codes=("NO",),
        jurisdiction="NO",
        id_prefix="no",
        source="esef",
        country_name="Norway",
    ),
    "pl": MarketHint(
        market="pl",
        exchange_codes=("PW",),
        jurisdiction="PL",
        id_prefix="pl",
        source="esef",
        country_name="Poland",
    ),
    "pt": MarketHint(
        market="pt",
        exchange_codes=("PL",),
        jurisdiction="PT",
        id_prefix="pt",
        source="esef",
        country_name="Portugal",
    ),
    "be": MarketHint(
        market="be",
        exchange_codes=("BB",),
        jurisdiction="BE",
        id_prefix="be",
        source="esef",
        country_name="Belgium",
    ),
    "at": MarketHint(
        market="at",
        exchange_codes=("AV",),
        jurisdiction="AT",
        id_prefix="at",
        source="esef",
        country_name="Austria",
    ),
    "lu": MarketHint(
        market="lu",
        exchange_codes=("LX",),
        jurisdiction="LU",
        id_prefix="lu",
        source="esef",
        country_name="Luxembourg",
    ),
}

#: Reverse view of ``MARKET_HINTS``: exchange code -> owning market hint.
#: Used to read a market off an OpenFIGI mapping row when the caller gave
#: no market of its own. First hint wins if codes ever collide.
_HINT_BY_EXCHANGE: dict[str, MarketHint] = {}
for _market_hint in MARKET_HINTS.values():
    for _exchange_code in _market_hint.exchange_codes:
        _HINT_BY_EXCHANGE.setdefault(_exchange_code.upper(), _market_hint)


@dataclass(slots=True)
class _PacingState:
    lock: asyncio.Lock
    last_post_at: float | None = None
    blocked_until: float = 0.0


# Keyed by the running event loop: a bare module-level lock binds to the
# first loop that awaits it and would then raise "attached to a different
# loop" for every later loop in the same process (tests, or a second
# asyncio.run). Within one loop the state is shared by every client.
_PACING_STATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _PacingState] = (
    weakref.WeakKeyDictionary()
)


def _pacing_state() -> _PacingState:
    loop = asyncio.get_running_loop()
    state = _PACING_STATES.get(loop)
    if state is None:
        state = _PacingState(lock=asyncio.Lock())
        _PACING_STATES[loop] = state
    return state


async def _wait_for_post_slot() -> None:
    """Serialize paced POST starts across coroutines on the running loop."""

    state = _pacing_state()
    async with state.lock:
        now = time.monotonic()
        delay = max(state.blocked_until - now, 0.0)
        if state.last_post_at is not None:
            delay = max(delay, state.last_post_at + _MIN_POST_INTERVAL_SECONDS - now)
        if delay > 0:
            await asyncio.sleep(delay)
        state.last_post_at = time.monotonic()


def _note_rate_limit(response: httpx.Response) -> None:
    """Park the next POST until the window resets once the budget is spent."""

    remaining = (response.headers.get("ratelimit-remaining") or "").strip()
    reset = (response.headers.get("ratelimit-reset") or "").strip()
    if remaining != "0" or not reset:
        return
    try:
        seconds = float(reset)
    except ValueError:
        return
    state = _pacing_state()
    state.blocked_until = max(state.blocked_until, time.monotonic() + max(seconds, 0.0))


def _json_payload(response: httpx.Response, *, label: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"The {label} API returned invalid JSON.") from exc


def _equity_rows(entry: Any) -> list[dict[str, Any]]:
    """Every Equity row of one mapping result, in response order."""

    if not isinstance(entry, dict):
        return []
    rows = entry.get("data")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("marketSector") == "Equity"
    ]


def _equity_row(entry: Any) -> dict[str, Any] | None:
    """Keep the first Equity row of one mapping result; None means no match."""

    rows = _equity_rows(entry)
    return rows[0] if rows else None


class OpenFigiClient(RetryingClient):
    """Keyless OpenFIGI identifier mapping with batched, paced POSTs."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            "OpenFIGI",
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            headers={"User-Agent": f"openfilings/{__version__}"},
            client=client,
        )

    async def map_ids(
        self,
        jobs: Sequence[tuple[str, str | None]],
        *,
        id_type: str = "TICKER",
    ) -> list[dict[str, Any] | None]:
        """Map identifiers to FIGI rows, one result per job, positionally aligned."""

        entries = await self._map_entries(jobs, id_type=id_type)
        return [_equity_row(entry) for entry in entries]

    async def map_all_equity_rows(
        self,
        jobs: Sequence[tuple[str, str | None]],
        *,
        id_type: str = "TICKER",
    ) -> list[dict[str, Any]]:
        """Every Equity row of every job, flattened in response order.

        An exchange-less job answers with one row per exchange listing, and
        those rows are what a caller without a market needs: each carries
        the ``exchCode`` that identifies the listing market.
        """

        rows: list[dict[str, Any]] = []
        for entry in await self._map_entries(jobs, id_type=id_type):
            rows.extend(_equity_rows(entry))
        return rows

    async def _map_entries(
        self,
        jobs: Sequence[tuple[str, str | None]],
        *,
        id_type: str,
    ) -> list[Any]:
        results: list[Any] = []
        for start in range(0, len(jobs), _MAX_JOBS_PER_REQUEST):
            batch = jobs[start : start + _MAX_JOBS_PER_REQUEST]
            body = []
            for value, exchange in batch:
                job: dict[str, Any] = {
                    "idType": id_type,
                    # OpenFIGI tickers are case-sensitive (verified live),
                    # and an explicit null field is a per-job error there,
                    # so exchCode is only sent when it is actually set.
                    "idValue": value.strip().upper(),
                }
                if exchange and exchange.strip():
                    job["exchCode"] = exchange.strip().upper()
                body.append(job)
            await _wait_for_post_slot()
            response = await self._request("POST", _MAPPING_URL, json=body)
            _note_rate_limit(response)
            payload = _json_payload(response, label="OpenFIGI")
            if not isinstance(payload, list):
                raise SourceError("The OpenFIGI API returned an unexpected response.")
            for index in range(len(batch)):
                results.append(payload[index] if index < len(payload) else None)
        return results


@dataclass(frozen=True, slots=True)
class GleifRecord:
    """One GLEIF LEI record, reduced to the fields the crosswalk needs."""

    lei: str
    legal_name: str
    jurisdiction: str
    status: str


def _gleif_record(row: Any) -> GleifRecord | None:
    if not isinstance(row, dict):
        return None
    attributes = row.get("attributes")
    if not isinstance(attributes, dict):
        return None
    entity = attributes.get("entity")
    if not isinstance(entity, dict):
        return None
    lei = str(attributes.get("lei") or "").strip()
    legal_name = entity.get("legalName")
    name = (
        str(legal_name.get("name") or "").strip()
        if isinstance(legal_name, dict)
        else ""
    )
    if not lei or not name:
        return None
    return GleifRecord(
        lei=lei,
        legal_name=name,
        jurisdiction=str(entity.get("jurisdiction") or "").strip(),
        status=str(entity.get("status") or "").strip(),
    )


class GleifClient(RetryingClient):
    """Keyless GLEIF legal-name search."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            "GLEIF",
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            headers={
                "User-Agent": f"openfilings/{__version__}",
                "Accept": "application/vnd.api+json",
            },
            client=client,
        )

    async def search_legal_name(
        self,
        name: str,
        *,
        jurisdiction: str | None = None,
        limit: int = 5,
    ) -> list[GleifRecord]:
        """Search LEI records by legal name, exact-name matches first.

        GLEIF's name filter is a literal contains-match, so the full name
        can miss while its head hits: OpenFIGI spells "ASML HOLDING NV"
        where the register holds "ASML Holding N.V.". Querying the term
        returns the real record alongside the ADR funds whose names merely
        start with it; the exact-name check then ranks - and the caller's
        verifier discards - the rest.
        """

        clean_name = name.strip()
        if not clean_name:
            return []
        target = _normalized_name(clean_name)
        term = _search_term(clean_name)
        records = await self._search_name(term, jurisdiction, limit)
        if term != clean_name and not any(
            _normalized_name(record.legal_name) == target for record in records
        ):
            # The term restated the name wrongly (a punctuation-only
            # spelling, a suffix GLEIF keeps): retry the name as written so
            # a miss here is a miss upstream, not a missing fallback.
            records = _merged_records(
                records, await self._search_name(clean_name, jurisdiction, limit)
            )
        records.sort(
            key=lambda record: (
                _normalized_name(record.legal_name) != target,
                record.status != "ACTIVE",
            )
        )
        return records

    async def _search_name(
        self,
        name: str,
        jurisdiction: str | None,
        limit: int,
    ) -> list[GleifRecord]:
        params: dict[str, Any] = {
            "filter[entity.legalName]": name,
            "page[size]": max(1, min(limit, 100)),
        }
        if jurisdiction and jurisdiction.strip():
            params["filter[entity.jurisdiction]"] = jurisdiction.strip().upper()
        response = await self._request("GET", _LEI_RECORDS_URL, params=params)
        payload = _json_payload(response, label="GLEIF")
        if not isinstance(payload, dict):
            raise SourceError("The GLEIF API returned an unexpected response.")
        rows = payload.get("data")
        records = []
        if isinstance(rows, list):
            for row in rows:
                record = _gleif_record(row)
                if record is not None:
                    records.append(record)
        return records


def _search_term(text: str) -> str:
    """The distinctive leading words of a name, for a GLEIF contains-match."""

    words = _NAME_WORD.findall(text)
    while len(words) > 1 and words[-1].casefold() in _NAME_SUFFIXES:
        words.pop()
    return " ".join(words[:3]).upper() or text.strip()


def _normalized_name(text: str) -> str:
    """Casefolded letters and digits only: punctuation never blocks a match."""

    return "".join(_NAME_WORD.findall(text)).casefold()


def _merged_records(
    records: list[GleifRecord], extra: list[GleifRecord]
) -> list[GleifRecord]:
    """Both record lists in order, first mention of an LEI winning."""

    seen = {record.lei for record in records}
    return [*records, *(record for record in extra if record.lei not in seen)]


@dataclass(frozen=True, slots=True)
class CrosswalkHit:
    """Resolution outcome for one crosswalk query."""

    query: str
    market: str | None
    ticker: str | None
    name: str | None
    isin: str | None
    lei: str | None
    company_id: str | None
    method: str
    confidence: str
    candidates: tuple[str, ...] = ()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cached_hit(key: str, query: str, record: dict[str, Any]) -> CrosswalkHit:
    market, _, _ = key.partition(":")
    lei = _optional_str(record.get("lei"))
    return CrosswalkHit(
        query=query,
        market=None if market == "-" else market,
        ticker=_optional_str(record.get("ticker")),
        name=_optional_str(record.get("name")),
        isin=_optional_str(record.get("isin")),
        lei=lei,
        company_id=_optional_str(record.get("company_id")),
        method=_optional_str(record.get("method")) or "ticker",
        # A cached mapping was never re-verified against the live source.
        confidence="candidate",
        candidates=(lei,) if lei else (),
    )


class Crosswalk:
    """Bridge tickers, ISINs, and LEIs to LEI-keyed company IDs."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        if data_dir is not None:
            resolved_dir = Path(data_dir)
        else:
            resolved_dir = (settings or Settings.from_env()).data_dir
        self._settings = settings
        self._data_dir = Path(resolved_dir)
        self._path = self._data_dir / "crosswalk.json"
        self._entries: dict[str, dict[str, Any]] | None = None
        self._openfigi: OpenFigiClient | None = None
        self._gleif: GleifClient | None = None

    def _resolved_settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings.from_env()
        return self._settings

    def _openfigi_client(self) -> OpenFigiClient:
        if self._openfigi is None:
            settings = self._resolved_settings()
            self._openfigi = OpenFigiClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            )
        return self._openfigi

    def _gleif_client(self) -> GleifClient:
        if self._gleif is None:
            settings = self._resolved_settings()
            self._gleif = GleifClient(
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
            )
        return self._gleif

    async def resolve(
        self,
        query: str,
        *,
        market: str | None = None,
        verifier: Callable[[str], Awaitable[bool]] | None = None,
    ) -> CrosswalkHit:
        """Resolve a ticker, ISIN, or LEI to an LEI-keyed company ID."""

        clean_query = query.strip()
        if not clean_query:
            raise ConfigurationError("A non-empty crosswalk query is required.")
        normalized_market = (
            market.strip().casefold() if market and market.strip() else None
        )
        hint = (
            MARKET_HINTS.get(normalized_market)
            if normalized_market is not None
            else None
        )
        if normalized_market is not None and hint is None:
            supported = ", ".join(sorted(MARKET_HINTS))
            raise ConfigurationError(
                f"Unknown market '{market}'. Supported markets: {supported}."
            )

        key = f"{normalized_market or '-'}:{clean_query.upper()}"
        record = self._load().get(key)
        if record is not None:
            return _cached_hit(key, clean_query, record)

        upper = clean_query.upper()
        if _LEI_PATTERN.fullmatch(upper):
            return await self._resolve_lei(key, clean_query, upper, hint, verifier)
        is_isin = _ISIN_PATTERN.fullmatch(upper) is not None
        return await self._resolve_identifier(
            key, clean_query, upper, hint, verifier, isin=is_isin
        )

    async def _resolve_lei(
        self,
        key: str,
        query: str,
        lei: str,
        hint: MarketHint | None,
        verifier: Callable[[str], Awaitable[bool]] | None,
    ) -> CrosswalkHit:
        if hint is None:
            # Without a market there is no company-id prefix to build, but
            # the LEI the caller supplied is still the answer.
            return CrosswalkHit(
                query=query,
                market=None,
                ticker=None,
                name=None,
                isin=None,
                lei=lei,
                company_id=None,
                method="lei",
                confidence="candidate",
            )
        return await self._finalize(
            key,
            query=query,
            hint=hint,
            ticker=None,
            name=None,
            isin=None,
            method="lei",
            candidates=(lei,),
            verifier=verifier,
        )

    async def _resolve_identifier(
        self,
        key: str,
        query: str,
        value: str,
        hint: MarketHint | None,
        verifier: Callable[[str], Awaitable[bool]] | None,
        *,
        isin: bool,
    ) -> CrosswalkHit:
        method = "isin" if isin else "ticker"
        id_type = "ID_ISIN" if isin else "TICKER"
        if hint is None:
            # No market was supplied: the mapping response's own exchange
            # codes say where the security lists, so read the answer off
            # those rows instead of guessing an exchange up front.
            try:
                all_rows = await self._openfigi_client().map_all_equity_rows(
                    [(value, None)], id_type=id_type
                )
            except SourceError:
                all_rows = []
            return await self._resolve_from_rows(
                query, value, all_rows, verifier=verifier, isin=isin
            )
        # An ISIN is globally unique; the hint's exchange code would only
        # narrow the listing list, never add information.
        jobs: list[tuple[str, str | None]] = (
            [(value, None)] if isin else [(value, code) for code in hint.exchange_codes]
        )
        try:
            rows = await self._openfigi_client().map_ids(jobs, id_type=id_type)
        except SourceError:
            rows = []
        row = next((item for item in rows if item is not None), None)
        ticker = _optional_str(row.get("ticker")) if row else None
        name = _optional_str(row.get("name")) if row else None

        if row is None:
            if hint is not None and not isin:
                # OpenFIGI cannot map the ticker, but the raw query may be a
                # legal name that GLEIF knows.
                candidates = await self._name_candidates(query, hint)
                if candidates:
                    return await self._finalize(
                        key,
                        query=query,
                        hint=hint,
                        ticker=value,
                        name=None,
                        isin=None,
                        method="name",
                        candidates=candidates,
                        verifier=verifier,
                    )
            return CrosswalkHit(
                query=query,
                market=hint.market if hint is not None else None,
                ticker=ticker or (None if isin else value),
                name=None,
                isin=value if isin else None,
                lei=None,
                company_id=None,
                method=method,
                confidence="candidate",
            )

        candidates = await self._name_candidates(name, hint)
        if hint is None:
            # An ISIN can still yield an LEI without a market, but only the
            # market supplies the company-id prefix the source adapters use.
            return CrosswalkHit(
                query=query,
                market=None,
                ticker=ticker,
                name=name,
                isin=value,
                lei=candidates[0] if candidates else None,
                company_id=None,
                method=method,
                confidence="candidate",
                candidates=candidates,
            )
        return await self._finalize(
            key,
            query=query,
            hint=hint,
            ticker=ticker or (None if isin else value),
            name=name,
            isin=value if isin else None,
            method=method,
            candidates=candidates,
            verifier=verifier,
        )

    async def _resolve_from_rows(
        self,
        query: str,
        value: str,
        rows: Sequence[dict[str, Any] | None],
        *,
        verifier: Callable[[str], Awaitable[bool]] | None,
        isin: bool,
    ) -> CrosswalkHit:
        """Derive the market from the mapping rows' own exchange codes.

        A caller without a market hint still gets a full answer: OpenFIGI's
        response says on which exchanges the security lists, and each of
        these markets owns a fixed exchange code. Every supported exchange
        found is tried in row order; a verified candidate wins, otherwise
        the first supported market's candidate is returned.
        """

        method = "isin" if isin else "ticker"
        fallback_name: str | None = None
        fallback_ticker: str | None = None
        attempted: list[CrosswalkHit] = []
        seen_markets: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            exchange = (_optional_str(row.get("exchCode")) or "").upper()
            name = _optional_str(row.get("name"))
            ticker = _optional_str(row.get("ticker"))
            fallback_name = fallback_name or name
            fallback_ticker = fallback_ticker or ticker
            hint = _HINT_BY_EXCHANGE.get(exchange)
            if hint is None or hint.market in seen_markets:
                continue
            seen_markets.add(hint.market)
            candidates = await self._name_candidates(name, hint)
            hit = await self._finalize(
                f"{hint.market}:{value.upper()}",
                query=query,
                hint=hint,
                ticker=ticker or (None if isin else value),
                name=name,
                isin=value if isin else None,
                method=method,
                candidates=candidates,
                verifier=verifier,
            )
            if hit.confidence == "verified":
                return hit
            attempted.append(hit)
        if attempted:
            return attempted[0]
        candidates = await self._name_candidates(fallback_name, None) if isin else ()
        return CrosswalkHit(
            query=query,
            market=None,
            ticker=fallback_ticker or (None if isin else value),
            name=fallback_name,
            isin=value if isin else None,
            lei=candidates[0] if candidates else None,
            company_id=None,
            method=method,
            confidence="candidate",
            candidates=candidates,
        )

    async def _name_candidates(
        self, name: str | None, hint: MarketHint | None
    ) -> tuple[str, ...]:
        """Candidate LEIs for a name, scoped to the market's jurisdiction first."""

        if not name or not name.strip():
            return ()
        gleif = self._gleif_client()
        records: list[GleifRecord] = []
        try:
            if hint is not None:
                records = await gleif.search_legal_name(
                    name, jurisdiction=hint.jurisdiction, limit=25
                )
            if not records:
                records = await gleif.search_legal_name(name, limit=25)
        except SourceError:
            return ()
        return tuple(dict.fromkeys(record.lei for record in records))

    async def _finalize(
        self,
        key: str,
        *,
        query: str,
        hint: MarketHint,
        ticker: str | None,
        name: str | None,
        isin: str | None,
        method: str,
        candidates: tuple[str, ...],
        verifier: Callable[[str], Awaitable[bool]] | None,
    ) -> CrosswalkHit:
        if not candidates:
            return CrosswalkHit(
                query=query,
                market=hint.market,
                ticker=ticker,
                name=name,
                isin=isin,
                lei=None,
                company_id=None,
                method=method,
                confidence="candidate",
            )
        selected = candidates[0]
        confidence = "candidate"
        if verifier is not None:
            for candidate in candidates[:_MAX_VERIFIED_CANDIDATES]:
                try:
                    verified = bool(await verifier(f"{hint.id_prefix}_lei_{candidate}"))
                except OpenFilingsError:
                    verified = False
                if verified:
                    selected = candidate
                    confidence = "verified"
                    break
        hit = CrosswalkHit(
            query=query,
            market=hint.market,
            ticker=ticker,
            name=name,
            isin=isin,
            lei=selected,
            company_id=f"{hint.id_prefix}_lei_{selected}",
            method=method,
            confidence=confidence,
            candidates=candidates,
        )
        self._store(key, hit)
        return hit

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict[str, Any]] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            entries = {
                str(key): value for key, value in raw.items() if isinstance(value, dict)
            }
        self._entries = entries
        return entries

    def _store(self, key: str, hit: CrosswalkHit) -> None:
        entries = self._load()
        entries[key] = {
            "ticker": hit.ticker,
            "name": hit.name,
            "isin": hit.isin,
            "lei": hit.lei,
            "company_id": hit.company_id,
            "method": hit.method,
            "ts": datetime.now(UTC).isoformat(),
        }
        self._data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        temporary.write_text(
            json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, self._path)
