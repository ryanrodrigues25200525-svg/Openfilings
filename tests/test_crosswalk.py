"""Offline tests for the ticker/ISIN-to-LEI crosswalk."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from openfilings import crosswalk as crosswalk_module
from openfilings.adapters.esef import ENABLED_ESEF_MARKETS
from openfilings.crosswalk import (
    MARKET_HINTS,
    Crosswalk,
    GleifClient,
    OpenFigiClient,
)
from openfilings.exceptions import ConfigurationError

ASML_LEI = "724500Y6DUVHQD6OXN27"
TESCO_LEI = "2138002P5RNKC5W2JZ46"
ISIN = "NL0010273215"


def _lei(index: int) -> str:
    """Synthetic but well-formed 20-character LEI."""

    return f"LEI{index:017d}"


def _figi_row(
    *,
    ticker: str,
    name: str,
    exchange: str | None = "NA",
    sector: str = "Equity",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "figi": "BBG000000001",
        "name": name,
        "ticker": ticker,
        "securityType": "Common Stock",
        "marketSector": sector,
    }
    if exchange is not None:
        row["exchCode"] = exchange
    return row


def _gleif_payload(records: Sequence[tuple[str, str, str, str]]) -> dict[str, Any]:
    return {
        "data": [
            {
                "type": "lei-records",
                "id": lei,
                "attributes": {
                    "lei": lei,
                    "entity": {
                        "legalName": {"name": name},
                        "jurisdiction": jurisdiction,
                        "status": status,
                    },
                },
            }
            for lei, name, jurisdiction, status in records
        ]
    }


def _attach_clients(
    crosswalk: Crosswalk,
    responder: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Wire MockTransport-backed OpenFIGI and GLEIF clients into a Crosswalk."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    transport = httpx.MockTransport(handler)
    crosswalk._openfigi = OpenFigiClient(client=httpx.AsyncClient(transport=transport))
    crosswalk._gleif = GleifClient(client=httpx.AsyncClient(transport=transport))
    return requests


def _no_requests(request: httpx.Request) -> httpx.Response:
    raise AssertionError(
        f"No HTTP request expected, got {request.method} {request.url}"
    )


def _mapping_client(
    responder: Callable[[httpx.Request], httpx.Response],
) -> OpenFigiClient:
    transport = httpx.MockTransport(responder)
    return OpenFigiClient(client=httpx.AsyncClient(transport=transport))


def test_market_hints_cover_uk_and_every_esef_market() -> None:
    assert set(MARKET_HINTS) == {"uk"} | {
        market.market_code.casefold() for market in ENABLED_ESEF_MARKETS
    }
    for code, hint in MARKET_HINTS.items():
        assert hint.market == code
        assert hint.exchange_codes, f"{code} needs at least one OpenFIGI exchange code"
        assert len(hint.jurisdiction) == 2
        assert hint.id_prefix == code
        assert hint.source in {"esef", "fca_nsm"}
        assert hint.country_name
    assert MARKET_HINTS["uk"].jurisdiction == "GB"
    assert MARKET_HINTS["uk"].id_prefix == "uk"
    assert MARKET_HINTS["uk"].source == "fca_nsm"
    assert MARKET_HINTS["nl"].source == "esef"
    # Codes that are easy to confuse with ISO country codes.
    assert MARKET_HINTS["pl"].exchange_codes == ("PW",)
    assert MARKET_HINTS["pt"].exchange_codes == ("PL",)
    assert MARKET_HINTS["lu"].exchange_codes == ("LX",)


def test_default_data_dir_follows_settings_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENFILINGS_DATA_DIR", str(tmp_path))
    assert Crosswalk()._path == tmp_path.resolve() / "crosswalk.json"
    explicit = tmp_path / "explicit"
    assert Crosswalk(data_dir=explicit)._path == explicit / "crosswalk.json"


@pytest.mark.asyncio
async def test_map_ids_chunks_batches_and_keeps_positional_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crosswalk_module, "_MIN_POST_INTERVAL_SECONDS", 0.0)
    bodies: list[list[dict[str, Any]]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/mapping"
        payload = json.loads(request.content)
        bodies.append(payload)
        entries = []
        for job in payload:
            entries.append(
                {
                    "data": [
                        _figi_row(
                            ticker=job["idValue"],
                            name=f"NAME {job['idValue']}",
                            exchange=job.get("exchCode"),
                        )
                    ]
                }
            )
        return httpx.Response(200, json=entries)

    client = _mapping_client(responder)
    jobs = [(f"tk{index:02d}", "NA" if index % 2 else None) for index in range(23)]
    results = await client.map_ids(jobs)

    assert [len(batch) for batch in bodies] == [10, 10, 3]
    # Tickers are case-sensitive at OpenFIGI, so they go out uppercased, and
    # a job without an exchange must not send "exchCode": null (an error).
    assert bodies[0][0]["idValue"] == "TK00"
    assert "exchCode" not in bodies[0][0]
    assert bodies[0][1]["exchCode"] == "NA"
    assert all(job["idType"] == "TICKER" for batch in bodies for job in batch)
    assert len(results) == 23
    for index, result in enumerate(results):
        assert result is not None
        assert result["ticker"] == f"TK{index:02d}"


@pytest.mark.asyncio
async def test_map_ids_marks_failed_jobs_and_non_equity_rows_as_none() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        assert len(json.loads(request.content)) == 4
        return httpx.Response(
            200,
            json=[
                {"data": [_figi_row(ticker="GOOD", name="GOOD PLC", exchange="LN")]},
                {"warning": "No identifier found."},
                {"error": "Invalid idValue in value field."},
                {
                    "data": [
                        _figi_row(
                            ticker="CASH",
                            name="MONEY MARKET FUND",
                            sector="Money Market",
                        )
                    ]
                },
            ],
        )

    client = _mapping_client(responder)
    results = await client.map_ids(
        [("GOOD", "LN"), ("NOPE", "LN"), ("BAD", "LN"), ("CASH", "LN")]
    )
    assert results[0] is not None
    assert results[0]["ticker"] == "GOOD"
    assert results[1] is None
    assert results[2] is None
    assert results[3] is None


@pytest.mark.asyncio
async def test_map_ids_paces_consecutive_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crosswalk_module, "_MIN_POST_INTERVAL_SECONDS", 0.05)

    def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {"data": [_figi_row(ticker=job["idValue"], name="PACED")]}
                for job in payload
            ],
        )

    client = _mapping_client(responder)
    started = time.monotonic()
    await client.map_ids([(f"T{index}", None) for index in range(11)])
    elapsed = time.monotonic() - started
    # Two POSTs, so at least one inter-request interval must have elapsed.
    assert elapsed >= 0.04


@pytest.mark.asyncio
async def test_rate_limit_reset_parks_the_next_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crosswalk_module, "_MIN_POST_INTERVAL_SECONDS", 0.0)
    calls = 0

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=[{"data": [_figi_row(ticker="X", name="X")]}],
            headers={
                "ratelimit-limit": "25;w=60",
                "ratelimit-remaining": "0",
                "ratelimit-reset": "0.2",
            },
        )

    client = _mapping_client(responder)
    await client.map_ids([("A", None)])
    assert crosswalk_module._pacing_state().blocked_until > time.monotonic()

    started = time.monotonic()
    await client.map_ids([("B", None)])
    elapsed = time.monotonic() - started
    assert calls == 2
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_resolve_ticker_maps_name_to_lei_company_id(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    gleif_params: list[httpx.QueryParams] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json.loads(request.content) == [
                {"idType": "TICKER", "idValue": "ASML", "exchCode": "NA"}
            ]
            return httpx.Response(
                200,
                json=[{"data": [_figi_row(ticker="ASML", name="ASML HOLDING NV")]}],
            )
        assert request.url.path == "/api/v1/lei-records"
        gleif_params.append(request.url.params)
        return httpx.Response(
            200, json=_gleif_payload([(ASML_LEI, "ASML HOLDING NV", "NL", "ACTIVE")])
        )

    requests = _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve("asml", market="NL")

    assert hit.query == "asml"
    assert hit.market == "nl"
    assert hit.ticker == "ASML"
    assert hit.name == "ASML HOLDING NV"
    assert hit.isin is None
    assert hit.lei == ASML_LEI
    assert hit.company_id == f"nl_lei_{ASML_LEI}"
    assert hit.method == "ticker"
    assert hit.confidence == "candidate"
    assert hit.candidates == (ASML_LEI,)
    assert gleif_params[0]["filter[entity.legalName]"] == "ASML HOLDING"
    assert gleif_params[0]["filter[entity.jurisdiction]"] == "NL"
    assert gleif_params[0]["page[size]"] == "25"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_jurisdiction_scoped_search_falls_back_to_unscoped(
    tmp_path: Path,
) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    gleif_params: list[httpx.QueryParams] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[{"data": [_figi_row(ticker="ASML", name="ASML HOLDING NV")]}],
            )
        gleif_params.append(request.url.params)
        if "filter[entity.jurisdiction]" in request.url.params:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200, json=_gleif_payload([(ASML_LEI, "ASML HOLDING NV", "NL", "ACTIVE")])
        )

    _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve("ASML", market="nl")

    assert hit.company_id == f"nl_lei_{ASML_LEI}"
    # The scoped jurisdiction is tried with the name head and then the name
    # as written before the unscoped retry searches wide.
    assert [
        (
            params.get("filter[entity.jurisdiction]"),
            params.get("filter[entity.legalName]"),
        )
        for params in gleif_params
    ] == [
        ("NL", "ASML HOLDING"),
        ("NL", "ASML HOLDING NV"),
        (None, "ASML HOLDING"),
    ]


@pytest.mark.asyncio
async def test_resolve_isin_uses_id_isin_without_exchange(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json.loads(request.content) == [
                {"idType": "ID_ISIN", "idValue": ISIN}
            ]
            return httpx.Response(
                200,
                json=[{"data": [_figi_row(ticker="ASML", name="ASML HOLDING NV")]}],
            )
        return httpx.Response(
            200, json=_gleif_payload([(ASML_LEI, "ASML HOLDING NV", "NL", "ACTIVE")])
        )

    requests = _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve(ISIN.lower(), market="nl")

    assert hit.method == "isin"
    assert hit.isin == ISIN
    assert hit.ticker == "ASML"
    assert hit.name == "ASML HOLDING NV"
    assert hit.lei == ASML_LEI
    assert hit.company_id == f"nl_lei_{ASML_LEI}"
    assert hit.candidates == (ASML_LEI,)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_resolve_isin_without_market_infers_market(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    gleif_params: list[httpx.QueryParams] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[{"data": [_figi_row(ticker="ASML", name="ASML HOLDING NV")]}],
            )
        gleif_params.append(request.url.params)
        return httpx.Response(
            200, json=_gleif_payload([(ASML_LEI, "ASML HOLDING NV", "NL", "ACTIVE")])
        )

    _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve(ISIN)

    # The mapping row's own exchange code supplies the missing market, so a
    # bare ISIN resolves all the way to the market-scoped company ID.
    assert hit.market == "nl"
    assert hit.method == "isin"
    assert hit.lei == ASML_LEI
    assert hit.company_id == f"nl_lei_{ASML_LEI}"
    assert hit.candidates == (ASML_LEI,)
    assert "filter[entity.jurisdiction]" in gleif_params[0]
    assert f"nl:{ISIN}" in json.loads((tmp_path / "crosswalk.json").read_text())


@pytest.mark.asyncio
async def test_isin_without_market_or_supported_exchange_reports_lei_only(
    tmp_path: Path,
) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[
                    {
                        "data": [
                            _figi_row(
                                ticker="AAPL",
                                name="APPLE INC",
                                exchange="UW",
                            )
                        ]
                    }
                ],
            )
        return httpx.Response(
            200, json=_gleif_payload([(_lei(9), "APPLE INC", "US", "ACTIVE")])
        )

    _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve(ISIN)

    assert hit.market is None
    assert hit.lei == _lei(9)
    assert hit.company_id is None
    assert not (tmp_path / "crosswalk.json").exists()


@pytest.mark.asyncio
async def test_resolve_lei_passes_through_without_network(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    _attach_clients(crosswalk, _no_requests)

    hit = await crosswalk.resolve(ASML_LEI, market="nl")
    assert hit.method == "lei"
    assert hit.lei == ASML_LEI
    assert hit.company_id == f"nl_lei_{ASML_LEI}"
    assert hit.candidates == (ASML_LEI,)

    united = await crosswalk.resolve(ASML_LEI, market="uk")
    assert united.company_id == f"uk_lei_{ASML_LEI}"

    bare = await crosswalk.resolve(ASML_LEI.lower())
    assert bare.method == "lei"
    assert bare.lei == ASML_LEI
    assert bare.company_id is None
    assert bare.market is None
    assert bare.candidates == ()


@pytest.mark.asyncio
async def test_verifier_selects_second_candidate(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    first_lei = _lei(1)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[
                    {
                        "data": [
                            _figi_row(ticker="TSCO", name="TESCO PLC", exchange="LN")
                        ]
                    }
                ],
            )
        return httpx.Response(
            200,
            json=_gleif_payload(
                [
                    (first_lei, "TESCO PLC", "GB", "ACTIVE"),
                    (TESCO_LEI, "TESCO PLC", "GB", "ACTIVE"),
                ]
            ),
        )

    _attach_clients(crosswalk, responder)
    seen: list[str] = []

    async def verifier(company_id: str) -> bool:
        seen.append(company_id)
        return len(seen) == 2

    hit = await crosswalk.resolve("TSCO", market="uk", verifier=verifier)

    assert seen == [f"uk_lei_{first_lei}", f"uk_lei_{TESCO_LEI}"]
    assert hit.confidence == "verified"
    assert hit.lei == TESCO_LEI
    assert hit.company_id == f"uk_lei_{TESCO_LEI}"
    assert hit.method == "ticker"
    assert hit.candidates == (first_lei, TESCO_LEI)


@pytest.mark.asyncio
async def test_verifier_is_capped_at_three_candidates(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    leis = [_lei(index) for index in range(1, 5)]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[
                    {
                        "data": [
                            _figi_row(ticker="TSCO", name="TESCO PLC", exchange="LN")
                        ]
                    }
                ],
            )
        statuses = ["ACTIVE", "ACTIVE", "ACTIVE", "ISSUED"]
        return httpx.Response(
            200,
            json=_gleif_payload(
                [
                    (lei, "TESCO PLC", "GB", status)
                    for lei, status in zip(leis, statuses, strict=True)
                ]
            ),
        )

    _attach_clients(crosswalk, responder)
    seen: list[str] = []

    async def verifier(company_id: str) -> bool:
        seen.append(company_id)
        return False

    hit = await crosswalk.resolve("TSCO", market="uk", verifier=verifier)

    assert len(seen) == 3
    assert hit.confidence == "candidate"
    assert hit.lei == leis[0]
    assert hit.company_id == f"uk_lei_{leis[0]}"
    # ACTIVE records are preferred, and the exhausted verifier keeps the
    # full candidate list best-first.
    assert hit.candidates == tuple(leis)


@pytest.mark.asyncio
async def test_gleif_search_prefers_active_records_and_sends_jurisdiction() -> None:
    params: list[httpx.QueryParams] = []

    def responder(request: httpx.Request) -> httpx.Response:
        params.append(request.url.params)
        return httpx.Response(
            200,
            json=_gleif_payload(
                [
                    (_lei(1), "TESCO PLC", "GB", "ISSUED"),
                    (TESCO_LEI, "TESCO PLC", "GB", "ACTIVE"),
                ]
            ),
        )

    transport = httpx.MockTransport(responder)
    client = GleifClient(client=httpx.AsyncClient(transport=transport))
    records = await client.search_legal_name("TESCO PLC", jurisdiction="gb", limit=2)

    assert [record.lei for record in records] == [TESCO_LEI, _lei(1)]
    assert records[0].status == "ACTIVE"
    assert records[0].legal_name == "TESCO PLC"
    assert records[0].jurisdiction == "GB"
    assert params[0]["filter[entity.legalName]"] == "TESCO"
    assert params[0]["filter[entity.jurisdiction]"] == "GB"
    assert params[0]["page[size]"] == "2"

    assert await client.search_legal_name("   ") == []
    assert len(params) == 1


@pytest.mark.asyncio
async def test_gleif_search_queries_the_name_head_and_ranks_exact_matches() -> None:
    params: list[httpx.QueryParams] = []

    def responder(request: httpx.Request) -> httpx.Response:
        params.append(request.url.params)
        return httpx.Response(
            200,
            json=_gleif_payload(
                [
                    (_lei(7), "ASML Holding NV ADRhedged(TM)", "US", "ACTIVE"),
                    (ASML_LEI, "ASML Holding N.V.", "NL", "ACTIVE"),
                ]
            ),
        )

    transport = httpx.MockTransport(responder)
    client = GleifClient(client=httpx.AsyncClient(transport=transport))
    records = await client.search_legal_name("ASML HOLDING NV")

    # GLEIF's filter is a contains-match, so the full OpenFIGI spelling
    # ("ASML HOLDING NV") reaches only the fund that starts with it. The
    # name head reaches the register's own "ASML Holding N.V." as well, and
    # the punctuation-insensitive exact check ranks it first.
    assert params[0]["filter[entity.legalName]"] == "ASML HOLDING"
    assert [record.lei for record in records] == [ASML_LEI, _lei(7)]
    assert len(params) == 1


@pytest.mark.asyncio
async def test_gleif_search_retries_the_name_as_written_without_exact_match() -> None:
    params: list[httpx.QueryParams] = []

    def responder(request: httpx.Request) -> httpx.Response:
        term = request.url.params["filter[entity.legalName]"]
        params.append(request.url.params)
        if term == "INTEL":
            return httpx.Response(
                200,
                json=_gleif_payload([(_lei(4), "INTEL CORPORATION", "US", "ACTIVE")]),
            )
        return httpx.Response(
            200, json=_gleif_payload([(_lei(5), "INTEL CORP", "US", "ACTIVE")])
        )

    transport = httpx.MockTransport(responder)
    client = GleifClient(client=httpx.AsyncClient(transport=transport))
    records = await client.search_legal_name("INTEL CORP")

    # The head query found no exact match, so the name as written was tried
    # too and both pools are kept - exact match first, first mention of an
    # LEI winning.
    assert [record.lei for record in records] == [_lei(5), _lei(4)]
    assert [params_["filter[entity.legalName]"] for params_ in params] == [
        "INTEL",
        "INTEL CORP",
    ]


def test_search_term_shortens_names_for_gleif() -> None:
    term = crosswalk_module._search_term
    normalized = crosswalk_module._normalized_name

    assert term("ASML HOLDING NV") == "ASML HOLDING"
    assert term("TESCO PLC") == "TESCO"
    assert term("CHINA CONSTRUCTION BANK CORP") == "CHINA CONSTRUCTION BANK"
    assert term("ASML") == "ASML"
    assert term("NORTHERN & SHELL PLC") == "NORTHERN SHELL"
    assert normalized("ASML Holding N.V.") == normalized("ASML HOLDING NV")
    assert normalized("ASML Holding NV") != normalized("ASML Holding NV ADRhedged(TM)")


@pytest.mark.asyncio
async def test_source_errors_degrade_to_candidate_less_hits(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    requests = _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve("ASML", market="nl")

    assert hit.market == "nl"
    assert hit.ticker == "ASML"
    assert hit.lei is None
    assert hit.company_id is None
    assert hit.candidates == ()
    assert hit.method == "ticker"
    assert len(requests) == 2
    assert not (tmp_path / "crosswalk.json").exists()


@pytest.mark.asyncio
async def test_bare_ticker_infers_market_from_exchange(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    bodies: list[Any] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=[
                    {
                        "data": [
                            _figi_row(ticker="TSCO", name="TESCO PLC", exchange="LN")
                        ]
                    }
                ],
            )
        return httpx.Response(
            200, json=_gleif_payload([(TESCO_LEI, "TESCO PLC", "GB", "ACTIVE")])
        )

    requests = _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve("tsco")

    assert bodies == [[{"idType": "TICKER", "idValue": "TSCO"}]]
    assert hit.market == "uk"
    assert hit.method == "ticker"
    assert hit.ticker == "TSCO"
    assert hit.lei == TESCO_LEI
    assert hit.company_id == f"uk_lei_{TESCO_LEI}"
    assert len(requests) == 2
    assert "uk:TSCO" in json.loads((tmp_path / "crosswalk.json").read_text())


@pytest.mark.asyncio
async def test_bare_ticker_on_unsupported_exchange_stays_unresolved(
    tmp_path: Path,
) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[
                    {
                        "data": [
                            _figi_row(ticker="AAPL", name="APPLE INC", exchange="UW")
                        ]
                    }
                ],
            )
        raise AssertionError("GLEIF is not consulted without a supported market")

    requests = _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve("AAPL")

    assert hit.market is None
    assert hit.ticker == "AAPL"
    assert hit.name == "APPLE INC"
    assert hit.lei is None
    assert hit.company_id is None
    assert hit.candidates == ()
    assert hit.method == "ticker"
    assert hit.confidence == "candidate"
    assert len(requests) == 1
    assert not (tmp_path / "crosswalk.json").exists()


@pytest.mark.asyncio
async def test_bare_ticker_prefers_the_verified_exchange(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    first_lei = _lei(1)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[
                    {
                        "data": [
                            _figi_row(ticker="TSCO", name="TESCO", exchange="SM"),
                            _figi_row(ticker="TSCO", name="TESCO PLC", exchange="LN"),
                        ]
                    }
                ],
            )
        jurisdiction = request.url.params.get("filter[entity.jurisdiction]")
        if jurisdiction == "ES":
            return httpx.Response(
                200, json=_gleif_payload([(first_lei, "TESCO", "ES", "ACTIVE")])
            )
        return httpx.Response(
            200, json=_gleif_payload([(TESCO_LEI, "TESCO PLC", "GB", "ACTIVE")])
        )

    async def verifier(company_id: str) -> bool:
        return company_id == f"uk_lei_{TESCO_LEI}"

    requests = _attach_clients(crosswalk, responder)
    hit = await crosswalk.resolve("TSCO", verifier=verifier)

    assert hit.confidence == "verified"
    assert hit.market == "uk"
    assert hit.company_id == f"uk_lei_{TESCO_LEI}"
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_failed_lookup_is_not_cached(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    posts = 0

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json=[{"warning": "No identifier found."}])
        return httpx.Response(200, json={"data": []})

    requests = _attach_clients(crosswalk, responder)

    first = await crosswalk.resolve("ZZZZ", market="nl")
    assert first.market == "nl"
    assert first.method == "ticker"
    assert first.lei is None
    assert first.company_id is None
    assert first.candidates == ()

    second = await crosswalk.resolve("ZZZZ", market="nl")
    assert second.lei is None
    # Both attempts really hit the network: 2 POSTs plus the jurisdiction
    # scoped/unscoped GLEIF probes, and nothing was written to disk.
    assert posts == 2
    assert len(requests) == 6
    assert not (tmp_path / "crosswalk.json").exists()


@pytest.mark.asyncio
async def test_positive_mapping_is_cached_and_file_round_trips(tmp_path: Path) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json=[{"data": [_figi_row(ticker="ASML", name="ASML HOLDING NV")]}],
            )
        return httpx.Response(
            200, json=_gleif_payload([(ASML_LEI, "ASML HOLDING NV", "NL", "ACTIVE")])
        )

    requests = _attach_clients(crosswalk, responder)
    first = await crosswalk.resolve("ASML", market="nl")
    assert len(requests) == 2

    second = await crosswalk.resolve("ASML", market="nl")
    assert len(requests) == 2
    assert second == first
    assert second.confidence == "candidate"

    cached = json.loads((tmp_path / "crosswalk.json").read_text(encoding="utf-8"))
    assert set(cached) == {"nl:ASML"}
    entry = cached["nl:ASML"]
    assert entry["ticker"] == "ASML"
    assert entry["name"] == "ASML HOLDING NV"
    assert entry["isin"] is None
    assert entry["lei"] == ASML_LEI
    assert entry["company_id"] == f"nl_lei_{ASML_LEI}"
    assert entry["method"] == "ticker"
    assert entry["ts"]
    assert not (tmp_path / "crosswalk.json.tmp").exists()

    reloaded = Crosswalk(data_dir=tmp_path)
    _attach_clients(reloaded, _no_requests)
    third = await reloaded.resolve("ASML", market="nl")
    assert third.company_id == f"nl_lei_{ASML_LEI}"
    assert third.lei == ASML_LEI
    assert third.candidates == (ASML_LEI,)


@pytest.mark.asyncio
async def test_unknown_market_and_empty_query_raise_configuration_error(
    tmp_path: Path,
) -> None:
    crosswalk = Crosswalk(data_dir=tmp_path)
    _attach_clients(crosswalk, _no_requests)

    with pytest.raises(ConfigurationError, match="Unknown market"):
        await crosswalk.resolve("ASML", market="de")
    with pytest.raises(ConfigurationError, match="non-empty"):
        await crosswalk.resolve("   ", market="nl")
