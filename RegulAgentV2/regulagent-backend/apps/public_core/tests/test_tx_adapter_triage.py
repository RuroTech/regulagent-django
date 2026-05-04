"""
Tests for TXAdapter triage fast-fail behaviour.

Bug: When Neubus triage finds lease documents but NONE match the target API,
the fallback path returned ALL lease docs (including unknown-bucket docs), causing
16+ ghost `index_document_task` jobs that never completed.

Fix (to be implemented in tx_adapter.py): the `unknown_docs` branch must set
`self._last_fetch_error` with a human-readable message and return [] — exactly
the same behaviour as the already-correct no-unknown branch.

Reference: apps/public_core/services/adapters/tx_adapter.py  lines ~115-148
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apps.public_core.services.adapters.tx_adapter import TXAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_neubus_doc(filename: str = "doc.pdf", local_path: str = "/tmp/doc.pdf") -> MagicMock:
    """Return a minimal NeubusDocument-like mock."""
    doc = MagicMock()
    doc.neubus_filename = filename
    doc.local_path = Path(local_path)
    doc.form_types_by_page = {}
    doc.classification_status = "pending"
    doc.extraction_status = "pending"
    return doc


def _make_lease(lease_id: str = "03361", lease_name: str = "ZULETTE") -> MagicMock:
    lease = MagicMock()
    lease.lease_id = lease_id
    lease.lease_name = lease_name
    return lease


# ---------------------------------------------------------------------------
# Shared patch targets
#
# fetch_document_list uses local (in-function) imports, so we must patch at
# the source module, not at tx_adapter's namespace.
# ---------------------------------------------------------------------------

_INGEST_STALE = "apps.public_core.services.neubus_ingest.ingest_lease_if_stale"
_INGEST = "apps.public_core.services.neubus_ingest.ingest_lease"
_TRIAGE = "apps.public_core.services.neubus_classifier.triage_lease_documents"
_RRC = "apps.public_core.services.rrc_completions_extractor.extract_completions_all_documents"

# RRC response that forces the Neubus path
_RRC_NO_RECORDS = {"status": "no_records", "files": []}


# ---------------------------------------------------------------------------
# The test class
# ---------------------------------------------------------------------------

class TestTXAdapterTriageFastFail:
    """
    Contract tests for the triage no-match fast-fail path in TXAdapter.

    Tests 1 and 2 define the DESIRED behaviour (Test 1 will FAIL until the fix
    is applied).  Tests 3 and 4 are regression/contract tests for paths that
    must continue working correctly.
    """

    # ------------------------------------------------------------------
    # Test 1  (EXPECTED TO FAIL before fix) — unknown-docs branch must
    # return [] and set _last_fetch_error, not flood the queue.
    # ------------------------------------------------------------------

    def test_unknown_docs_fallback_returns_empty_and_sets_error(self):
        """
        FAILING TEST: triage returns unknown docs + docs for a different API
        but NONE for the requested API.

        Current buggy behaviour: returns all docs (unknown + other-API docs).
        Expected fixed behaviour: returns [] and sets _last_fetch_error with
        scraper_status="no_match", the lease name, and the available API list.
        """
        target_api = "4238331431"          # 10-digit; not present in triage result
        other_api  = "42003356630000"      # 14-digit different well on same lease

        mock_doc1 = _make_neubus_doc("unknown_doc1.pdf", "/tmp/unknown_doc1.pdf")
        mock_doc2 = _make_neubus_doc("unknown_doc2.pdf", "/tmp/unknown_doc2.pdf")
        mock_doc3 = _make_neubus_doc("other_well_doc.pdf", "/tmp/other_well_doc.pdf")

        triage_result = {
            "unknown": [mock_doc1, mock_doc2],
            other_api: [mock_doc3],
        }

        lease = _make_lease("03361", "ZULETTE")

        adapter = TXAdapter()

        with (
            patch(
                _RRC,
                return_value=_RRC_NO_RECORDS,
            ),
            patch(_INGEST_STALE, return_value=lease),
            patch(_INGEST, return_value=lease),
            patch(_TRIAGE, return_value=triage_result),
        ):
            result = adapter.fetch_document_list(target_api)

        # ── Assertions ──────────────────────────────────────────────────
        assert result == [], (
            "Expected [] but got non-empty list — ghost index_document_task "
            "jobs will be queued for documents that do not belong to this well."
        )

        assert adapter._last_fetch_error is not None, (
            "_last_fetch_error must be set so the session flips to status=error"
        )

        err = adapter._last_fetch_error
        assert err.get("scraper_status") == "no_match", (
            f"scraper_status should be 'no_match', got {err.get('scraper_status')!r}"
        )

        msg = err.get("message", "")
        assert "ZULETTE" in msg or "03361" in msg, (
            f"Error message should include lease name/id to aid debugging; got: {msg!r}"
        )

        available = err.get("available_apis", [])
        assert other_api in available or any(other_api in str(a) for a in available), (
            f"available_apis should include {other_api!r}; got {available!r}"
        )

    # ------------------------------------------------------------------
    # Test 2  (regression) — no-unknown branch already works; must still
    # work after the fix lands.
    # ------------------------------------------------------------------

    def test_no_unknown_docs_fallback_returns_empty_and_sets_error(self):
        """
        Regression: triage has docs for a different API only (no unknown bucket).
        This path already sets _last_fetch_error and returns [] — verify it
        continues to do so after the fix is applied.
        """
        target_api = "4238331431"
        other_api  = "42003356630000"

        mock_doc1 = _make_neubus_doc("other_well_doc.pdf", "/tmp/other_well_doc.pdf")

        triage_result = {other_api: [mock_doc1]}

        lease = _make_lease("03361", "ZULETTE")
        adapter = TXAdapter()

        with (
            patch(
                _RRC,
                return_value=_RRC_NO_RECORDS,
            ),
            patch(_INGEST_STALE, return_value=lease),
            patch(_INGEST, return_value=lease),
            patch(_TRIAGE, return_value=triage_result),
        ):
            result = adapter.fetch_document_list(target_api)

        assert result == [], "No-unknown branch: must return [] when no match found"

        assert adapter._last_fetch_error is not None
        assert adapter._last_fetch_error.get("scraper_status") == "no_match"

    # ------------------------------------------------------------------
    # Test 3  (regression) — exact API match still returns docs.
    # ------------------------------------------------------------------

    def test_matched_api_still_returns_docs(self):
        """
        Regression: triage returns documents attributed to the exact requested
        API — they must still be returned as DocumentSpec objects.
        """
        target_api = "4238331431"

        mock_doc1 = _make_neubus_doc("well_doc1.pdf", "/tmp/well_doc1.pdf")
        mock_doc2 = _make_neubus_doc("well_doc2.pdf", "/tmp/well_doc2.pdf")

        # Triage key is the same 10-digit API the caller passed in
        triage_result = {target_api: [mock_doc1, mock_doc2]}

        lease = _make_lease("03361", "ZULETTE")
        adapter = TXAdapter()

        with (
            patch(
                _RRC,
                return_value=_RRC_NO_RECORDS,
            ),
            patch(_INGEST_STALE, return_value=lease),
            patch(_INGEST, return_value=lease),
            patch(_TRIAGE, return_value=triage_result),
        ):
            result = adapter.fetch_document_list(target_api)

        assert len(result) == 2, (
            f"Expected 2 DocumentSpec objects for the matched API, got {len(result)}"
        )
        assert adapter._last_fetch_error is None, (
            "_last_fetch_error must be None on successful match"
        )

        # Spot-check DocumentSpec structure
        for spec in result:
            assert hasattr(spec, "filename")
            assert hasattr(spec, "local_path")

    # ------------------------------------------------------------------
    # Test 4  (regression) — suffix match (10-digit requested, 14-digit
    # in triage) still returns docs.
    # ------------------------------------------------------------------

    def test_suffix_match_still_returns_docs(self):
        """
        Regression: caller passes a 10-digit API; triage stores the full 14-digit
        key. The last-8-digit suffix match must still find the documents.

        e.g. requested "4238331431" → suffix "38331431" → matches "42383314310000".
        """
        target_api     = "4238331431"      # 10-digit
        triage_api_key = "42383314310000"  # 14-digit version in triage result

        mock_doc1 = _make_neubus_doc("lease_doc.pdf", "/tmp/lease_doc.pdf")

        triage_result = {triage_api_key: [mock_doc1]}

        lease = _make_lease("03361", "ZULETTE")
        adapter = TXAdapter()

        with (
            patch(
                _RRC,
                return_value=_RRC_NO_RECORDS,
            ),
            patch(_INGEST_STALE, return_value=lease),
            patch(_INGEST, return_value=lease),
            patch(_TRIAGE, return_value=triage_result),
        ):
            result = adapter.fetch_document_list(target_api)

        assert len(result) == 1, (
            f"Suffix match should return 1 doc; got {len(result)}. "
            f"suffix '38331431' should have matched triage key '{triage_api_key}'."
        )
        assert adapter._last_fetch_error is None, (
            "_last_fetch_error must be None on successful suffix match"
        )
