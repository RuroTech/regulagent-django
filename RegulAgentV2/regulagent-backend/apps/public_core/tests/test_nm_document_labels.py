"""
TDD RED PHASE: Tests for NM document label improvements.

These tests define expected behavior for three fixes:
1. Corrected doc-type detection in _detect_doc_type_from_context
2. Human-readable doc_type_display field on NMDocumentSerializer
3. enrich_doc_types_from_index helper that overrides scraped types with indexed LLM classifications

ALL TESTS ARE EXPECTED TO FAIL until the fixes are implemented.
"""

import pytest

from apps.public_core.services.nm_document_fetcher import (
    NMDocumentFetcher,
    NMDocument,
)
from apps.public_core.serializers.nm_well import NMDocumentSerializer
from apps.public_core.forms import get_form_display_name


# ---------------------------------------------------------------------------
# Group 1: _detect_doc_type_from_context — inverted mappings
# ---------------------------------------------------------------------------

class TestDetectDocTypeFromContextFixes:
    """
    Pin the CORRECT mappings for completion, sundry, and workover.

    Current code has:
        "completion" → c_102  (WRONG; completion report is C-105)
        "sundry"     → c_105  (WRONG; sundry is C-103)
        "workover"   → c_102  (WRONG; workover completion is C-105)

    After the fix they must return forms.py-compatible keys (no underscore):
        "completion" → c105
        "sundry"     → c103
        "workover"   → c105
    """

    def _detect(self, link_text="", row_text="", filename="bare_api_ts.pdf"):
        fetcher = NMDocumentFetcher()
        return fetcher._detect_doc_type_from_context(link_text, row_text, filename)

    # --- Tests pinning the CORRECT mappings (canonical underscore format) ---

    def test_completion_context_returns_c105(self):
        """'completion' context must map to C-105 (Completion Report)."""
        result = self._detect(link_text="Completion Report", row_text="Completion")
        assert result == "c_105", (
            f"Expected 'c_105' for 'completion' context, got {result!r}."
        )

    def test_sundry_context_returns_c103(self):
        """'sundry notice' context must map to C-103 (Sundry Notice)."""
        result = self._detect(link_text="Sundry Notice", row_text="Sundry")
        assert result == "c_103", (
            f"Expected 'c_103' for 'sundry' context, got {result!r}."
        )

    def test_workover_context_returns_c105(self):
        """'workover' context must map to C-105 (workover completion)."""
        result = self._detect(link_text="Workover", row_text="Workover Report")
        assert result == "c_105", (
            f"Expected 'c_105' for 'workover' context, got {result!r}."
        )

    # --- REGRESSION GUARDS: must still pass after fix ---

    def test_explicit_c103_string_returns_c103(self):
        """Explicit 'C-103' in text must still return c_103."""
        result = self._detect(link_text="C-103 Form", row_text="")
        assert result == "c_103", f"Regression: explicit C-103 should return 'c_103', got {result!r}"

    def test_explicit_c101_string_returns_c101(self):
        """Explicit 'C-101' in text must still return c_101."""
        result = self._detect(link_text="C-101 Permit", row_text="")
        assert result == "c_101", f"Regression: explicit C-101 should return 'c_101', got {result!r}"

    def test_explicit_c104_string_returns_c104(self):
        """Explicit 'C-104' in text must still return c_104."""
        result = self._detect(link_text="C-104 Allowable", row_text="")
        assert result == "c_104", f"Regression: explicit C-104 should return 'c_104', got {result!r}"

    def test_plugging_and_abandonment_context_returns_c103(self):
        """'plugging and abandonment' context must map to C-103."""
        result = self._detect(
            link_text="Plugging and Abandonment",
            row_text="plug and abandon",
        )
        assert result == "c_103", (
            f"Regression: 'plug & abandon' should return 'c_103', got {result!r}"
        )

    def test_pna_abbreviation_context_returns_c103(self):
        """'p&a' abbreviation context must map to C-103."""
        result = self._detect(link_text="P&A Notice", row_text="p&a")
        assert result == "c_103", (
            f"Regression: 'p&a' should return 'c_103', got {result!r}"
        )

    def test_apd_context_returns_apd(self):
        """'apd' context must return 'apd'."""
        result = self._detect(link_text="APD", row_text="Application Permit to Drill")
        assert result == "apd", (
            f"Regression: 'apd' context should return 'apd', got {result!r}"
        )

    def test_unrelated_string_returns_none(self):
        """Unrecognised context must return None."""
        result = self._detect(
            link_text="Document",
            row_text="Some unrelated text",
            filename="30015288410000_07_31_2018_02_37_53.pdf",
        )
        assert result is None, (
            f"Regression: unrelated string should return None, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Group 2: NMDocumentSerializer — doc_type_display field
# ---------------------------------------------------------------------------

class TestNMDocumentSerializerDisplayField:
    """
    NMDocumentSerializer must expose a read-only 'doc_type_display' field
    equal to get_form_display_name(doc_type) when doc_type is set, else None.
    """

    def _serialize(self, doc_type):
        doc = NMDocument(
            filename="30015288410000_07_31_2018_02_37_53.pdf",
            url="https://ocdimage.emnrd.nm.gov/path/30015288410000_07_31_2018_02_37_53.pdf",
            file_size=None,
            date=None,
            doc_type=doc_type,
        )
        serializer = NMDocumentSerializer(doc)
        return serializer.data

    def test_doc_type_c105_has_display_name(self):
        """c105 doc_type must produce the C-105 Completion Report display name."""
        data = self._serialize("c105")
        expected = get_form_display_name("c105")  # "NM C-105 - Completion Report"
        assert "doc_type_display" in data, (
            "NMDocumentSerializer is missing the 'doc_type_display' field."
        )
        assert data["doc_type_display"] == expected, (
            f"Expected {expected!r}, got {data.get('doc_type_display')!r}"
        )

    def test_doc_type_none_display_is_none(self):
        """None doc_type must produce doc_type_display=None."""
        data = self._serialize(None)
        assert "doc_type_display" in data, (
            "NMDocumentSerializer is missing the 'doc_type_display' field."
        )
        assert data["doc_type_display"] is None, (
            f"Expected None for doc_type_display when doc_type is None, got {data.get('doc_type_display')!r}"
        )

    def test_existing_fields_still_serialize(self):
        """filename, url, and doc_type fields must still appear unchanged."""
        data = self._serialize("c103")
        assert data["filename"] == "30015288410000_07_31_2018_02_37_53.pdf"
        assert "ocdimage.emnrd.nm.gov" in data["url"]
        assert data["doc_type"] == "c103"


# ---------------------------------------------------------------------------
# Group 3: enrich_doc_types_from_index — LLM index override
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestEnrichDocTypesFromIndex:
    """
    enrich_doc_types_from_index(documents, api) must:
    - Look up ExtractedDocument rows by api number (any format), source_path starts with 'http',
      excluding document_type='unknown'.
    - Key the map by source_path.rsplit('/', 1)[-1] (the filename tail).
    - Override NMDocument.doc_type where the filename matches.
    - Leave non-matching docs untouched.
    - Never let an 'unknown' row override.
    """

    def test_enrich_doc_types_from_index_is_importable(self):
        """enrich_doc_types_from_index must exist as a module-level function."""
        try:
            from apps.public_core.services.nm_document_fetcher import (
                enrich_doc_types_from_index,
            )
        except ImportError as exc:
            pytest.fail(
                f"enrich_doc_types_from_index is not yet defined in nm_document_fetcher: {exc}"
            )

    def test_matching_indexed_doc_overrides_scraped_type(self):
        """A matching ExtractedDocument (non-unknown) must override the scraped doc_type."""
        from apps.public_core.services.nm_document_fetcher import (
            enrich_doc_types_from_index,
        )
        from apps.public_core.models import ExtractedDocument

        filename = "30015288410000_12_30_2025_01_31_49.pdf"
        source_url = f"https://ocdimage.emnrd.nm.gov/Imaging/FileStore/30/{filename}"

        ExtractedDocument.objects.create(
            api_number="3001528841",  # 10-digit
            document_type="c105",
            source_path=source_url,
            source_type=ExtractedDocument.SOURCE_RRC,
            json_data={},
        )

        # Scraped doc has stale/None doc_type — index should override with c105
        doc = NMDocument(
            filename=filename,
            url=source_url,
            doc_type=None,
        )
        result = enrich_doc_types_from_index([doc], "3001528841")

        assert result[0].doc_type == "c105", (
            f"Expected indexed 'c105' to override None, got {result[0].doc_type!r}"
        )

    def test_stale_scraped_type_is_overridden(self):
        """A stale/wrong scraped doc_type must be overridden by the index."""
        from apps.public_core.services.nm_document_fetcher import (
            enrich_doc_types_from_index,
        )
        from apps.public_core.models import ExtractedDocument

        filename = "30015288410000_stale_type.pdf"
        source_url = f"https://ocdimage.emnrd.nm.gov/Imaging/FileStore/30/{filename}"

        ExtractedDocument.objects.create(
            api_number="3001528841",
            document_type="c103",
            source_path=source_url,
            source_type=ExtractedDocument.SOURCE_RRC,
            json_data={},
        )

        doc = NMDocument(
            filename=filename,
            url=source_url,
            doc_type="c102",  # stale wrong type from regex
        )
        result = enrich_doc_types_from_index([doc], "3001528841")
        assert result[0].doc_type == "c103", (
            f"Expected 'c103' from index to replace stale 'c102', got {result[0].doc_type!r}"
        )

    def test_non_matching_doc_is_unchanged(self):
        """A doc whose filename has no index entry must keep its original doc_type."""
        from apps.public_core.services.nm_document_fetcher import (
            enrich_doc_types_from_index,
        )
        from apps.public_core.models import ExtractedDocument

        ExtractedDocument.objects.create(
            api_number="3001528841",
            document_type="c105",
            source_path="https://ocdimage.emnrd.nm.gov/Imaging/FileStore/30/some_other_file.pdf",
            source_type=ExtractedDocument.SOURCE_RRC,
            json_data={},
        )

        non_matching_doc = NMDocument(
            filename="30015288410000_unindexed.pdf",
            url="https://ocdimage.emnrd.nm.gov/Imaging/FileStore/30/30015288410000_unindexed.pdf",
            doc_type="c101",
        )
        result = enrich_doc_types_from_index([non_matching_doc], "3001528841")
        assert result[0].doc_type == "c101", (
            f"Non-matching doc_type must stay 'c101', got {result[0].doc_type!r}"
        )

    def test_unknown_row_never_overrides(self):
        """An 'unknown' document_type in the index must NOT override the scraped type."""
        from apps.public_core.services.nm_document_fetcher import (
            enrich_doc_types_from_index,
        )
        from apps.public_core.models import ExtractedDocument

        filename = "30015288410000_unknown_classified.pdf"
        source_url = f"https://ocdimage.emnrd.nm.gov/Imaging/FileStore/30/{filename}"

        ExtractedDocument.objects.create(
            api_number="3001528841",
            document_type="unknown",
            source_path=source_url,
            source_type=ExtractedDocument.SOURCE_RRC,
            json_data={},
        )

        doc = NMDocument(
            filename=filename,
            url=source_url,
            doc_type="c104",  # scraped type must survive
        )
        result = enrich_doc_types_from_index([doc], "3001528841")
        assert result[0].doc_type == "c104", (
            f"'unknown' index row must NOT override scraped 'c104', got {result[0].doc_type!r}"
        )

    def test_api_formats_all_resolve(self):
        """
        Index lookup must work regardless of whether the api argument is
        10-digit, 14-digit, or hyphenated format.
        """
        from apps.public_core.services.nm_document_fetcher import (
            enrich_doc_types_from_index,
        )
        from apps.public_core.models import ExtractedDocument

        filename = "30015288410000_api_format_test.pdf"
        source_url = f"https://ocdimage.emnrd.nm.gov/Imaging/FileStore/30/{filename}"

        ExtractedDocument.objects.create(
            api_number="3001528841",  # stored as 10-digit
            document_type="c105",
            source_path=source_url,
            source_type=ExtractedDocument.SOURCE_RRC,
            json_data={},
        )

        doc = NMDocument(filename=filename, url=source_url, doc_type=None)

        # Call with 14-digit api — must still find the 10-digit record
        result = enrich_doc_types_from_index([doc], "30015288410000")
        assert result[0].doc_type == "c105", (
            f"14-digit api should resolve to the 10-digit DB record, got {result[0].doc_type!r}"
        )
