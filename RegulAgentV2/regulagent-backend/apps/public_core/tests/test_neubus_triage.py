"""
Regression tests for triage_lease_documents() — hotfix for re-triage bug.

Bug: triage_lease_documents() was re-triaging documents stored with
     api="" and triage_confidence="unidentified" (previously determined
     to have no API match). The LLM non-deterministically attributed them
     to the target well on second runs, causing wrong documents to be indexed.

Fix: Changed filter(api="") to filter(api="").exclude(triage_confidence="unidentified")
     in apps/public_core/services/neubus_classifier.py, line 460.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from apps.public_core.models.neubus_lease import NeubusLease, NeubusDocument


@pytest.mark.django_db
class TestTriageDoesNotReTriageUnidentified:
    """
    Regression suite: documents with triage_confidence="unidentified" must
    not be re-submitted to the LLM triage process on subsequent runs.
    """

    def _make_lease(self) -> NeubusLease:
        return NeubusLease.objects.create(
            lease_id="TEST-LEASE-001",
            field_name="Test Field",
            lease_name="Test Lease",
            operator="Test Operator",
            county="Howard",
            district="8",
        )

    def _make_doc(self, lease, neubus_filename, api="", triage_confidence="") -> NeubusDocument:
        return NeubusDocument.objects.create(
            lease=lease,
            neubus_filename=neubus_filename,
            api=api,
            triage_confidence=triage_confidence,
            local_path="/tmp/fake-path-for-test.pdf",
        )

    # ------------------------------------------------------------------
    # Test 1: previously-unidentified docs must NOT be re-triaged
    # ------------------------------------------------------------------

    def test_already_unidentified_docs_not_re_triaged(self):
        """
        Documents stored with api="" + triage_confidence="unidentified" have
        already been through triage and found to have no API match.
        They must not be submitted to triage_document() again.

        Also asserts the result dict correctly groups:
          - "unknown" key → the 3 unidentified docs
          - "42383309610000" key → the 1 doc already triaged with a real API
        """
        lease = self._make_lease()

        # 3 docs previously triaged → no match found
        unidentified_docs = [
            self._make_doc(lease, f"unidentified_{i}.pdf",
                           api="", triage_confidence="unidentified")
            for i in range(3)
        ]

        # 1 doc already triaged to a real API
        triaged_doc = self._make_doc(lease, "already_triaged.pdf",
                                     api="42383309610000",
                                     triage_confidence="high")

        with patch("apps.public_core.services.neubus_classifier.triage_document") as mock_triage:
            from apps.public_core.services.neubus_classifier import triage_lease_documents
            result = triage_lease_documents(lease)

        # LLM must never be called — no docs qualify for re-triage
        mock_triage.assert_not_called()

        # Unidentified docs appear under "unknown"
        assert "unknown" in result, "Expected 'unknown' key in result"
        unknown_ids = {d.id for d in result["unknown"]}
        expected_unknown_ids = {d.id for d in unidentified_docs}
        assert unknown_ids == expected_unknown_ids, (
            f"unknown ids mismatch: got {unknown_ids}, expected {expected_unknown_ids}"
        )

        # Already-triaged doc appears under its API key
        assert "42383309610000" in result, "Expected API key '42383309610000' in result"
        triaged_ids = {d.id for d in result["42383309610000"]}
        assert triaged_doc.id in triaged_ids, (
            "Expected the already-triaged doc to appear under its API key"
        )

    # ------------------------------------------------------------------
    # Test 2: truly un-triaged docs (triage_confidence="") DO get triaged
    # ------------------------------------------------------------------

    def test_truly_untriaged_docs_are_still_triaged(self):
        """
        Documents with api="" and triage_confidence="" have never been through
        triage at all.  They must be processed by triage_document().

        The fix must not over-exclude: only "unidentified" is skipped,
        not blank-confidence docs.
        """
        lease = self._make_lease()

        # 2 docs that have never been triaged
        self._make_doc(lease, "untriaged_a.pdf", api="", triage_confidence="")
        self._make_doc(lease, "untriaged_b.pdf", api="", triage_confidence="")

        triage_return = {
            "api_number": "42383309610000",
            "well_number": "1",
            "confidence": "high",
            "pages_scanned": 2,
        }

        # local_path does not exist on disk — we rely on the mock catching it
        # before _triage_one() reaches pdf_path.exists()
        with patch("apps.public_core.services.neubus_classifier.triage_document",
                   return_value=triage_return) as mock_triage:
            # Also mock Path.exists so the code doesn't short-circuit on missing file
            with patch("apps.public_core.services.neubus_classifier.Path") as mock_path_cls:
                mock_path_instance = MagicMock()
                mock_path_instance.exists.return_value = True
                mock_path_instance.name = "fake.pdf"
                mock_path_cls.return_value = mock_path_instance

                from apps.public_core.services.neubus_classifier import triage_lease_documents
                triage_lease_documents(lease)

        # Both un-triaged docs must have been submitted to triage_document()
        assert mock_triage.call_count == 2, (
            f"Expected triage_document to be called 2 times, "
            f"got {mock_triage.call_count}"
        )
