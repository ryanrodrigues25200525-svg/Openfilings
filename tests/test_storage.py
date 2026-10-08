from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import date
from decimal import Decimal

import pytest

from openfilings.adapters.base import SourceDocument
from openfilings.exceptions import ConfigurationError
from openfilings.models import (
    ExtractionQuality,
    Filing,
    FilingContent,
    FilingFinancials,
    FinancialLineItem,
    FinancialStatement,
    FinancialValue,
    ReportingPeriod,
)
from openfilings.storage.sqlite import SQLiteCache


def test_markdown_round_trips_as_compressed_content(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    markdown = "# Filing\n\n" + ("revenue | 100\n" * 10_000)
    content = FilingContent(
        filing_id="uk_1_tx",
        markdown=markdown,
        source_url="https://example.test/document",
        sha256=hashlib.sha256(b"pdf").hexdigest(),
        quality=ExtractionQuality(
            score=72,
            status="good",
            character_count=len(markdown),
            page_count=10,
            warnings=("minor_encoding_noise",),
        ),
    )

    cache.put_content(content)
    restored = cache.get_content(content.filing_id)

    assert restored is not None
    assert restored.markdown == markdown
    assert restored.from_cache is True
    assert restored.quality.score == 72
    assert restored.quality.page_count == 10
    assert restored.quality.warnings == ("minor_encoding_noise",)
    assert (tmp_path / "cache.sqlite3").stat().st_size < len(markdown.encode())

    stats = cache.stats()
    assert stats.documents == 1
    assert stats.compressed_content_bytes > 0
    assert stats.database_bytes > 0

    removed = cache.prune_content(0, vacuum=True)
    assert removed == 1
    assert cache.stats().documents == 0
    cache.close()


def test_source_document_round_trips_and_prunes(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    source = SourceDocument(
        data=b"%PDF-1.7\n" + b"financial statements\n" * 10_000,
        media_type="application/pdf",
        source_url="file:///tmp/annual-report.pdf",
        profile="sedar-import",
    )

    cache.put_source_document("ca_sedar_filing_123", source)
    restored = cache.get_source_document("ca_sedar_filing_123")

    assert restored == source
    assert cache.stats().source_documents == 1
    assert cache.stats().compressed_source_bytes < len(source.data)
    assert cache.prune_source_documents(0) == 1
    assert cache.get_source_document("ca_sedar_filing_123") is None
    cache.close()


def test_historical_facts_preserve_restated_and_as_of_views(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "history.sqlite3")
    period = ReportingPeriod(id="fy2024", end_date=date(2024, 12, 31), kind="duration")

    def store(filing_id: str, filing_date: date, value: str) -> None:
        filing = Filing(
            id=filing_id,
            company_id="uk_lei_example",
            source="fca_nsm",
            source_id=filing_id,
            title="Annual report",
            category="accounts",
            filing_type="annual",
            filing_date=filing_date,
            source_url=f"https://example.test/{filing_id}",
        )
        financials = FilingFinancials(
            filing_id=filing_id,
            company_id=filing.company_id,
            source_url=filing.source_url,
            statements=(
                FinancialStatement(
                    statement_type="income_statement",
                    title="Income statement",
                    line_items=(
                        FinancialLineItem(
                            code="revenue",
                            name="Revenue",
                            concept="ifrs-full:Revenue",
                            values=(
                                FinancialValue(period=period, value=Decimal(value)),
                            ),
                        ),
                    ),
                ),
            ),
            fact_count=1,
            sha256="0" * 64,
        )
        cache.put_historical_facts(filing, financials)

    store("original", date(2025, 2, 1), "100")
    store("restated", date(2026, 2, 1), "110")

    as_reported = cache.historical_facts("uk_lei_example", view="as_reported")
    latest = cache.historical_facts("uk_lei_example", view="latest_restated")
    as_of = cache.historical_facts(
        "uk_lei_example", view="as_of", as_of=date(2025, 12, 31)
    )
    cache.close()

    assert [fact.value for fact in as_reported] == [Decimal("110"), Decimal("100")]
    assert [(fact.filing_id, fact.value) for fact in latest] == [
        ("restated", Decimal("110"))
    ]
    assert [(fact.filing_id, fact.value) for fact in as_of] == [
        ("original", Decimal("100"))
    ]


def test_stale_extractor_version_is_a_financials_cache_miss(tmp_path) -> None:
    """Issue #15: cached financials survive an extractor fix. Rows stamped
    with an older extractor version (or none, for pre-versioning caches)
    must read as a miss so the filing is re-extracted."""

    from openfilings.models import FINANCIALS_EXTRACTOR_VERSION

    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    financials = FilingFinancials(
        filing_id="versioned",
        company_id="c1",
        source_url="https://example.test/versioned",
        statements=(),
        fact_count=0,
        sha256="1" * 64,
    )

    cache.put_financials(financials)
    assert cache.get_financials("versioned") is not None

    connection = sqlite3.connect(tmp_path / "cache.sqlite3")
    with connection:
        connection.execute(
            "UPDATE filing_financials SET extractor_version = ? WHERE filing_id = ?",
            (FINANCIALS_EXTRACTOR_VERSION - 1, "versioned"),
        )
    connection.close()
    assert cache.get_financials("versioned") is None

    with sqlite3.connect(tmp_path / "cache.sqlite3") as migrated:
        migrated.execute(
            "UPDATE filing_financials SET extractor_version = NULL WHERE filing_id = ?",
            ("versioned",),
        )
    assert cache.get_financials("versioned") is None

    cache.put_financials(financials)
    restored = cache.get_financials("versioned")
    cache.close()

    assert restored is not None
    assert restored.from_cache is True


def test_existing_cache_is_migrated_with_quality_metadata(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE filing_content (
            filing_id TEXT PRIMARY KEY,
            markdown_zlib BLOB NOT NULL,
            source_url TEXT NOT NULL,
            media_type TEXT NOT NULL,
            extraction_method TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            extracted_at TEXT NOT NULL
        )
        """
    )
    connection.close()

    cache = SQLiteCache(path)
    content = FilingContent(
        filing_id="legacy-test",
        markdown="# Migrated",
        source_url="https://example.test/migrated",
        sha256=hashlib.sha256(b"migrated").hexdigest(),
    )
    cache.put_content(content)
    restored = cache.get_content(content.filing_id)
    cache.close()

    assert restored is not None
    assert restored.quality.warnings == ("quality_not_recorded",)


def test_existing_cache_removes_unsupported_source_records(tmp_path) -> None:
    path = tmp_path / "unsupported-source.sqlite3"
    SQLiteCache(path).close()
    company_id = "private_123"
    filing_id = "private_filing_123"
    company = {
        "id": company_id,
        "source_id": "123",
        "name": "Private Example Ltd",
        "sources": ["private_registry"],
        "source_url": "https://example.test/company/123",
    }
    filing = {
        "id": filing_id,
        "company_id": company_id,
        "source": "private_registry",
        "source_id": "filing-123",
        "title": "Private filing",
        "category": "accounts",
        "filing_type": "annual",
        "filing_date": "2025-01-01",
        "source_url": "https://example.test/filing/123",
    }
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "INSERT INTO companies VALUES (?, ?, ?)",
            (company_id, json.dumps(company), "2025-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO filings VALUES (?, ?, ?, ?)",
            (
                filing_id,
                company_id,
                json.dumps(filing),
                "2025-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO filing_content VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filing_id,
                zlib.compress(b"# Private filing"),
                filing["source_url"],
                "text/html",
                "markdownify",
                None,
                hashlib.sha256(b"private").hexdigest(),
                "2025-01-01T00:00:00+00:00",
            ),
        )
    connection.close()

    cache = SQLiteCache(path)
    stats = cache.stats()
    cache.close()

    assert stats.companies == 0
    assert stats.filings == 0
    assert stats.documents == 0


def test_fresh_cache_is_stamped_with_the_current_schema_version(tmp_path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    cache.close()

    connection = sqlite3.connect(path)
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    connection.close()
    assert version == 1


def test_a_pre_versioning_database_is_accepted_and_stamped_forward(tmp_path) -> None:
    """SQLite's own user_version defaults to 0, which is what every database
    created before schema versioning existed already has. Those are
    schema-compatible with version 1 by construction, so opening one must
    succeed and silently stamp it forward - not reject a real user's
    existing cache."""

    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    cache.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.commit()
    connection.close()

    cache = SQLiteCache(path)
    cache.close()

    connection = sqlite3.connect(path)
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    connection.close()
    assert version == 1


def test_a_cache_from_a_newer_schema_version_fails_loudly(tmp_path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    cache.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(ConfigurationError, match="newer version"):
        SQLiteCache(path)
