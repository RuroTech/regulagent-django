"""
Serializers for NM well data API responses.
"""
from rest_framework import serializers
from typing import Optional

from apps.public_core.forms import get_form_display_name


class NMWellDataSerializer(serializers.Serializer):
    """Serializer for NM well data from scraper."""

    # API identifiers
    api10 = serializers.CharField(help_text="API-10 format: xx-xxx-xxxxx")
    api14 = serializers.CharField(help_text="API-14 format: 14 digits no dashes")

    # Basic info
    well_name = serializers.CharField(allow_blank=True)
    operator_name = serializers.CharField(allow_blank=True)
    operator_number = serializers.CharField(allow_blank=True)
    status = serializers.CharField(allow_blank=True)
    well_type = serializers.CharField(allow_blank=True)
    direction = serializers.CharField(allow_blank=True)

    # Location
    surface_location = serializers.CharField(allow_blank=True)
    latitude = serializers.FloatField(allow_null=True, required=False)
    longitude = serializers.FloatField(allow_null=True, required=False)
    elevation_ft = serializers.FloatField(allow_null=True, required=False)

    # Depths
    proposed_depth_ft = serializers.IntegerField(allow_null=True, required=False)
    tvd_ft = serializers.IntegerField(allow_null=True, required=False)
    formation = serializers.CharField(allow_blank=True)

    # Dates
    spud_date = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    completion_date = serializers.CharField(allow_blank=True, allow_null=True, required=False)

    # Raw data (optional, for debugging)
    raw_html = serializers.CharField(allow_blank=True, allow_null=True, required=False)


class NMDocumentSerializer(serializers.Serializer):
    """Serializer for NM document metadata."""

    filename = serializers.CharField(help_text="Document filename")
    url = serializers.URLField(help_text="Full URL to download the document")
    file_size = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    date = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    doc_type = serializers.CharField(
        allow_blank=True,
        allow_null=True,
        required=False,
        help_text="Detected document type (e.g., c_101, c_103, c_105)"
    )
    doc_type_display = serializers.SerializerMethodField(
        help_text="Human-readable form type label derived from doc_type"
    )

    def get_doc_type_display(self, obj) -> Optional[str]:
        """Return human-readable display name for the document type, or None."""
        if not obj.doc_type:
            return None
        return get_form_display_name(obj.doc_type)


class NMCombinedPDFResponseSerializer(serializers.Serializer):
    """Serializer for combined PDF download response."""

    url = serializers.URLField(help_text="URL for combined PDF download")
    api14 = serializers.CharField(help_text="API-14 used for the request")
    note = serializers.CharField(
        required=False,
        help_text="Additional information about the download"
    )
