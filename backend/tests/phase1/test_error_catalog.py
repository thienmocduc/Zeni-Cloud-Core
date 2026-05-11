"""Unit tests cho error_catalog — Phase 1 P1.4."""
import pytest

from app.core.error_catalog import (
    ERROR_CATALOG,
    lookup,
    format_error_response,
    list_codes_by_category,
)


def test_lookup_known_code():
    detail = lookup("DOMAIN_INVALID_FORMAT")
    assert detail["code"] == "DOMAIN_INVALID_FORMAT"
    assert detail["http_status"] == 422
    assert detail["category"] == "L1_DOMAIN"
    assert "user_msg" in detail
    assert "hint" in detail


def test_lookup_unknown_code_returns_internal_error():
    detail = lookup("SOME_FAKE_CODE_NOT_IN_CATALOG")
    assert detail["code"] == "INTERNAL_ERROR"


def test_lookup_with_context_interpolation():
    """Context vars should be substituted into messages."""
    # Use a code with {domain} placeholder (none yet — but test format() safe)
    detail = lookup("DOMAIN_DNS_NOT_PROPAGATED", domain="app.example.com")
    # Hint mentions {domain} → should be substituted
    assert "app.example.com" in detail["hint"]


def test_lookup_safe_when_missing_context_keys():
    """If hint has {var} but context doesn't provide it, return raw not crash."""
    detail = lookup("DOMAIN_DNS_NOT_PROPAGATED")  # no context
    # Should not raise — falls back to raw string
    assert "hint" in detail


def test_format_error_response_returns_dict():
    detail = format_error_response("AUTH_INVALID_TOKEN")
    assert isinstance(detail, dict)
    assert detail["http_status"] == 401


def test_list_codes_by_category():
    l1_codes = list_codes_by_category("L1_BUILD")
    assert len(l1_codes) >= 3
    assert "BUILD_NO_DOCKERFILE" in l1_codes


def test_all_catalog_entries_have_required_fields():
    """Every error catalog entry must have core required fields."""
    required = {"user_msg", "hint", "docs", "http_status", "category", "severity"}
    for code, entry in ERROR_CATALOG.items():
        missing = required - set(entry.keys())
        assert not missing, f"Code {code} missing fields: {missing}"


def test_all_severity_levels_valid():
    valid = {"info", "warning", "user_error", "blocker", "critical"}
    for code, entry in ERROR_CATALOG.items():
        assert entry["severity"] in valid, \
            f"Code {code} has invalid severity {entry['severity']}"


def test_all_http_status_valid():
    """HTTP status must be valid (4xx, 5xx, or special 425)."""
    for code, entry in ERROR_CATALOG.items():
        status = entry["http_status"]
        assert 400 <= status <= 599, \
            f"Code {code} has invalid http_status {status}"


def test_all_docs_urls_valid_pattern():
    """Docs URL must point to zenicloud.io domain."""
    for code, entry in ERROR_CATALOG.items():
        docs = entry["docs"]
        assert docs.startswith("https://zenicloud.io/"), \
            f"Code {code} has docs URL not on zenicloud.io: {docs}"


def test_billing_codes_have_402_status():
    """All BILLING_* codes should return 402 Payment Required."""
    billing_codes = list_codes_by_category("BILLING")
    for code in billing_codes:
        entry = ERROR_CATALOG[code]
        assert entry["http_status"] == 402, \
            f"BILLING code {code} should be 402, got {entry['http_status']}"


def test_quota_exceeded_codes_have_402():
    """All QUOTA_EXCEEDED codes should be 402."""
    quota_codes = [c for c in ERROR_CATALOG if "QUOTA_EXCEEDED" in c]
    for code in quota_codes:
        assert ERROR_CATALOG[code]["http_status"] == 402


def test_no_duplicate_codes():
    """ERROR_CATALOG dict implicitly has unique keys — sanity check."""
    codes = list(ERROR_CATALOG.keys())
    assert len(codes) == len(set(codes))


def test_min_catalog_size():
    """Should have at least 25 errors to be useful."""
    assert len(ERROR_CATALOG) >= 25
