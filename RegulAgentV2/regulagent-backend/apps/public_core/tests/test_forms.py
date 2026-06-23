"""
Tests for form type mapping utilities.

Tests cross-jurisdiction form mapping between TX, NM, and future jurisdictions.
"""

import pytest
from apps.public_core.forms import (
    get_equivalent_form,
    normalize_form_type,
    get_form_display_name,
    is_plugging_form,
    is_completion_form,
    get_jurisdiction_from_form,
    TX_W3A, TX_W2, NM_C103, NM_C105,
    PUBLIC_DOC_TYPES,
)


class TestNormalizeFormType:
    """Test form type normalization."""

    def test_normalize_tx_forms(self):
        """Test normalization of Texas forms."""
        assert normalize_form_type("W-3A") == "w3a"
        assert normalize_form_type("W-2") == "w2"
        assert normalize_form_type("w3a") == "w3a"
        assert normalize_form_type("W 3 A") == "w3a"

    def test_normalize_nm_forms(self):
        """Test normalization of New Mexico forms."""
        assert normalize_form_type("C-103") == "c103"
        assert normalize_form_type("C-105") == "c105"
        assert normalize_form_type("c103") == "c103"
        assert normalize_form_type("C 103") == "c103"

    def test_empty_input(self):
        """Test handling of empty input."""
        assert normalize_form_type("") == ""
        assert normalize_form_type(None) == ""


class TestFormEquivalence:
    """Test form equivalence mapping between jurisdictions."""

    def test_tx_to_nm_plugging(self):
        """Test TX W-3A maps to NM C-103."""
        assert get_equivalent_form("w3a", "TX", "NM") == "c103"
        assert get_equivalent_form("W-3A", "TX", "NM") == "c103"

    def test_nm_to_tx_plugging(self):
        """Test NM C-103 maps to TX W-3A."""
        assert get_equivalent_form("c103", "NM", "TX") == "w3a"
        assert get_equivalent_form("C-103", "NM", "TX") == "w3a"

    def test_tx_to_nm_completion(self):
        """Test TX W-2 maps to NM C-105."""
        assert get_equivalent_form("w2", "TX", "NM") == "c105"
        assert get_equivalent_form("W-2", "TX", "NM") == "c105"

    def test_nm_to_tx_completion(self):
        """Test NM C-105 maps to TX W-2."""
        assert get_equivalent_form("c105", "NM", "TX") == "w2"
        assert get_equivalent_form("C-105", "NM", "TX") == "w2"

    def test_same_jurisdiction_returns_original(self):
        """Test same jurisdiction returns original form."""
        assert get_equivalent_form("w3a", "TX", "TX") == "w3a"
        assert get_equivalent_form("c103", "NM", "NM") == "c103"

    def test_unknown_mapping_returns_original(self):
        """Test unknown mapping returns original form type."""
        assert get_equivalent_form("unknown", "TX", "CO") == "unknown"


class TestFormDisplayNames:
    """Test human-readable form names."""

    def test_tx_form_names(self):
        """Test Texas form display names."""
        assert "W-3A" in get_form_display_name("w3a")
        assert "Plugging Plan" in get_form_display_name("w3a")
        assert "W-2" in get_form_display_name("w2")
        assert "Completion" in get_form_display_name("w2")

    def test_nm_form_names(self):
        """Test New Mexico form display names."""
        assert "C-103" in get_form_display_name("c103")
        assert "Sundry" in get_form_display_name("c103")
        assert "C-105" in get_form_display_name("c105")
        assert "Completion" in get_form_display_name("c105")

    def test_unknown_form_name(self):
        """Test unknown form returns normalized name."""
        assert get_form_display_name("unknown") == "UNKNOWN"

    def test_underscore_format_resolves_same_as_no_underscore(self):
        """Canonical underscore runtime format must resolve to same display name."""
        # c_105 (canonical runtime) must resolve same as c105 (FORM_NAMES key)
        assert get_form_display_name("c_105") == get_form_display_name("c105")
        assert "C-105" in get_form_display_name("c_105")
        assert "Completion" in get_form_display_name("c_105")

        # c_103 (canonical runtime) must resolve same as c103
        assert get_form_display_name("c_103") == get_form_display_name("c103")
        assert "C-103" in get_form_display_name("c_103")

        # formation_tops hits on first lookup (no underscore strip needed)
        assert get_form_display_name("formation_tops") == "Formation Tops"

        # pa_procedure must still work
        assert "P&A" in get_form_display_name("pa_procedure")


class TestFormClassification:
    """Test form type classification helpers."""

    def test_plugging_forms(self):
        """Test plugging form detection."""
        assert is_plugging_form("w3a") is True
        assert is_plugging_form("W-3A") is True
        assert is_plugging_form("c103") is True
        assert is_plugging_form("C-103") is True
        assert is_plugging_form("w2") is False

    def test_completion_forms(self):
        """Test completion form detection."""
        assert is_completion_form("w2") is True
        assert is_completion_form("W-2") is True
        assert is_completion_form("c105") is True
        assert is_completion_form("C-105") is True
        assert is_completion_form("w3a") is False


class TestJurisdictionInference:
    """Test jurisdiction inference from form types."""

    def test_tx_forms(self):
        """Test Texas form jurisdiction detection."""
        assert get_jurisdiction_from_form("w3a") == "TX"
        assert get_jurisdiction_from_form("w2") == "TX"
        assert get_jurisdiction_from_form("W-3A") == "TX"

    def test_nm_forms(self):
        """Test New Mexico form jurisdiction detection."""
        assert get_jurisdiction_from_form("c103") == "NM"
        assert get_jurisdiction_from_form("c105") == "NM"
        assert get_jurisdiction_from_form("C-103") == "NM"

    def test_unknown_form(self):
        """Test unknown form returns None."""
        assert get_jurisdiction_from_form("unknown") is None


class TestPublicDocTypes:
    """Test public document type constants."""

    def test_tx_public_docs_included(self):
        """Test TX public docs are in PUBLIC_DOC_TYPES."""
        assert "w2" in PUBLIC_DOC_TYPES
        assert "w15" in PUBLIC_DOC_TYPES
        assert "gau" in PUBLIC_DOC_TYPES
        assert "w3" in PUBLIC_DOC_TYPES
        assert "w3a" in PUBLIC_DOC_TYPES

    def test_nm_public_docs_included(self):
        """Test NM public docs are in PUBLIC_DOC_TYPES."""
        assert "c103" in PUBLIC_DOC_TYPES
        assert "c105" in PUBLIC_DOC_TYPES

    def test_private_forms_not_included(self):
        """Test drilling permits are not public docs."""
        # W-1 (drilling permits) should not be public
        assert "w1" not in PUBLIC_DOC_TYPES
        assert "c101" not in PUBLIC_DOC_TYPES
