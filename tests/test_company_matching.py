"""Search matching across the diacritics real registered names actually use."""

from __future__ import annotations

from openfilings.adapters._common import match_text, normalize_text, ranked_matches


def _records() -> list[tuple[tuple[str, ...], str]]:
    return [
        (("ØRSTED A/S",), "orsted"),
        (("JERÓNIMO MARTINS SGPS SA",), "jeronimo"),
        (("POWSZECHNA KASA OSZCZĘDNOŚCI BANK POLSKI",), "pko"),
        (("TÜRKİYE İŞ BANKASI A.Ş.",), "isbank"),
        (("FORD OTOMOTİV SANAYİ A.Ş.",), "ford-otosan"),
        (("UNRELATED HOLDINGS PLC",), "unrelated"),
    ]


def test_ascii_query_matches_names_with_combining_accents() -> None:
    """NFKD folds these, so they worked before; guard against regression."""

    assert ranked_matches("Jeronimo", _records(), limit=3) == ["jeronimo"]
    assert ranked_matches("Oszczednosci", _records(), limit=3) == ["pko"]


def test_ascii_query_matches_letters_nfkd_cannot_decompose() -> None:
    """Slashed O and dotless i are independent letters, not base + mark.

    NFKD leaves them intact, so a plain ASCII query could never substring-
    match the registered name. Confirmed live on filings.xbrl.org: "Orsted"
    returned nothing while ØRSTED A/S was present in the Danish index.
    """

    assert ranked_matches("Orsted", _records(), limit=3) == ["orsted"]
    assert ranked_matches("Turkiye Is Bankasi", _records(), limit=3) == ["isbank"]


def test_match_text_folds_beyond_normalize_text() -> None:
    assert normalize_text("ØRSTED") == "ørsted"
    assert match_text("ØRSTED") == "orsted"
    assert match_text("Œuvre æther Straße") == "oeuvre aether strasse"


def test_unrelated_names_still_do_not_match() -> None:
    assert ranked_matches("Orsted", [(("UNRELATED HOLDINGS PLC",), "x")], limit=3) == []


def test_extra_query_token_does_not_lose_a_strong_partial_match() -> None:
    """Issue #10 part 1: "Ford Otosan" must score at least as well as "Ford".

    The full normalized query is not a substring of the registered name
    ("ford otomotiv sanayi a s"), so the strict substring check drops it.
    Token-subset scoring recovers it via the shared "ford" token.
    """

    assert ranked_matches("Ford", _records(), limit=3) == ["ford-otosan"]
    assert ranked_matches("Ford Otosan", _records(), limit=3) == ["ford-otosan"]


def test_extra_token_tolerance_still_rejects_near_misses() -> None:
    """Loosening the matcher must not match a 2,000-issuer registry loosely.

    Fewer than half the significant tokens hit here ("unrelated" is the
    only one), so the record stays excluded; a pure alias with no token
    overlap ("PKO") is still unresolvable without alias data (deferred).
    """

    assert ranked_matches("Ford Unrelated Holdings", _records(), limit=3) == [
        "unrelated"
    ]
    assert ranked_matches("PKO", _records(), limit=3) == []
    # Only short tokens and no substring hit: must match nothing rather
    # than falling through to a match-everything token path.
    assert ranked_matches("Xq Zq", _records(), limit=3) == []
