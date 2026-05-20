"""Customer document retrieval helpers.

Provides utilities for fetching documents that are stored on the local
filesystem by pipeline services (e.g. rrc_completions_extractor.py).

NOTE: GAU PDFs are NOT in Django ``default_storage``.  The extractor
writes directly to the local filesystem at::

    MEDIA_ROOT/rrc/completions/<api_digits>/<filename>

where ``<api_digits>`` is the digits-only form of the API number (e.g.
``re.sub(r"\\D+", "", api14)`` — may be 8, 10, or 14 digits depending on
what was passed to the extractor).  The filename pattern is::

    GAU_<api_digits>_NNN.pdf

This module therefore uses ``pathlib.Path`` + ``builtins.open``, NOT
``default_storage``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


def get_well_type(api14: str) -> str | None:
    """Infer well type from RRC completion document extractions.

    W-15 (injection wells) takes precedence — a well converted from oil to
    injection is currently classified as Injection.

    Returns "Injection", "Oil", or None.

    API matching handles all three storage formats: digits-only 10/14 and
    hyphenated. Accepts docs from any source_type (rrc / tenant_upload /
    neubus) — all are legitimate signals.
    """
    from apps.public_core.models.extracted_document import ExtractedDocument  # local import avoids circular deps
    from django.db.models import Q

    api_digits_all = re.sub(r"\D+", "", api14)
    api10 = api_digits_all[:10] if len(api_digits_all) >= 10 else api_digits_all

    # Match either: the hyphenated form, OR any value starting with the 10-digit prefix
    # (which covers both "4222736390" and "4222736390XXXX").
    hyphenated = f"{api10[:2]}-{api10[2:5]}-{api10[5:10]}" if len(api10) == 10 else None

    api_filter = Q(api_number__startswith=api10)
    if hyphenated:
        api_filter |= Q(api_number=hyphenated)

    # Fetch all successful document types for this well in one query.
    # No source_type filter — rrc, tenant_upload, and neubus are all valid signals.
    doc_types = list(
        ExtractedDocument.objects.filter(
            api_filter,
            status="success",
        ).values_list("document_type", flat=True)
    )

    if "w15" in doc_types:
        return "Injection"
    if "w2" in doc_types:
        return "Oil"
    return None


def get_gau_letter(tenant_id: str, api14: str) -> Optional[bytes]:
    """Return GAU PDF bytes from the local filesystem, or None if not found.

    GAU PDFs are public regulator records (not tenant-scoped), but
    ``tenant_id`` is accepted for forward compatibility and audit logging.

    Path construction matches ``rrc_completions_extractor._media_base()``::

        Path(settings.MEDIA_ROOT) / "rrc" / "completions" / <api_digits>

    where ``api_digits = re.sub(r"\\D+", "", api14)``.

    The glob pattern ``GAU_*.pdf`` is used to find the first matching file.

    Returns:
        bytes if a GAU PDF is found and readable, else None.

    Never raises — OSError on open is caught, logged as WARNING, and None
    is returned.
    """
    api_digits = re.sub(r"\D+", "", api14)

    # Mirror the path convention from rrc_completions_extractor._media_base():
    #   MEDIA_ROOT / "rrc" / "completions" / api_digits
    api_dir = Path(settings.MEDIA_ROOT) / "rrc" / "completions" / api_digits

    matches = list(api_dir.glob("GAU_*.pdf"))
    if not matches:
        return None

    gau_path = matches[0]
    try:
        with open(str(gau_path), "rb") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning(
            "get_gau_letter: failed to read GAU PDF for api14=%r tenant_id=%r path=%r: %s",
            api14,
            tenant_id,
            str(gau_path),
            exc,
        )
        return None
