"""
TDD RED PHASE — provenance-aware RAG.

Today ``vectorize_extracted_document`` (openai_extraction.py:1379) writes
``DocumentVector.metadata`` with ``tenant_id`` but no ``source_type`` or
``visibility``, and ``_retrieve_relevant_sections`` /
``_build_context_prompt`` (research_rag.py) return/render sections with only
``doc_type``/``section_name`` — the RAG answer has no way to tell the user
"this came from a tenant-uploaded (unverified) document" vs. "this is public
RRC-sourced data."

Fix under test (not yet implemented)
-------------------------------------
1. ``vectorize_extracted_document`` adds two keys to each
   ``DocumentVector.metadata`` it writes:
     - ``source_type``: copied from ``ed_obj.source_type`` (``"rrc"`` /
       ``"tenant_upload"`` / etc.)
     - ``visibility``: ``"private"`` when ``ed_obj.uploaded_by_tenant`` is
       set, else ``"public"``.
2. ``_retrieve_relevant_sections`` includes a ``visibility`` key (sourced
   from ``vec.metadata.get("visibility")``) in each returned section dict.
3. ``_build_context_prompt`` labels each section's provenance in the
   rendered prompt — a PUBLIC vs PRIVATE marker per section.
4. New management command ``backfill_document_vector_provenance`` sets
   ``metadata.visibility`` on existing ``DocumentVector`` rows:
   ``private`` when ``metadata.tenant_id`` is present/non-null, ``public``
   otherwise. Supports ``--dry-run`` (no writes).

Everything in this file must FAIL against current code and turn green,
unmodified, once BE implements the fix.

Patch targets (verified):
    apps.public_core.services.openai_extraction._embed_texts
        — used directly by vectorize_extracted_document (same module).
    apps.public_core.services.research_rag._embed_texts
        — re-imported name used by _retrieve_relevant_sections (same
          pattern as test_research_rag_privacy.py).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

DUMMY_EMBEDDING = [0.1] * 3072  # matches DocumentVector.embedding dimensions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tenant(suffix=None):
    from apps.tenants.models import Tenant

    uid = suffix or str(uuid.uuid4())[:8]
    return Tenant.objects.create(
        schema_name=f"test_prov_{uid}",
        name=f"Provenance Test Tenant {uid}",
        slug=f"prov-{uid}",
    )


def _make_well(api14=None):
    from apps.public_core.models import WellRegistry

    api14 = api14 or f"42502{uuid.uuid4().int % 10 ** 9:09d}"
    return WellRegistry.objects.create(
        api14=api14,
        state="TX",
        county="Andrews",
        operator_name="Provenance Test Op",
    )


def _make_session(well, tenant=None):
    from apps.public_core.models import ResearchSession

    return ResearchSession.objects.create(
        api_number=well.api14,
        state="TX",
        status="ready",
        well=well,
        tenant=tenant,
    )


def _make_vector(well, *, tenant_id_value=None, source_type="rrc",
                  visibility=None, section_text="Some section content"):
    """Create a DocumentVector row with explicit metadata (bypasses the
    indexing pipeline — used to pin retrieval/rendering behavior directly)."""
    from apps.public_core.models import DocumentVector

    meta = {"api_number": well.api14, "source_type": source_type}
    if tenant_id_value is not None:
        meta["tenant_id"] = str(tenant_id_value)
    else:
        meta["tenant_id"] = None
    if visibility is not None:
        meta["visibility"] = visibility

    return DocumentVector.objects.create(
        well=well,
        file_name="test.pdf",
        document_type="w2",
        section_name="header",
        section_text=section_text,
        embedding=DUMMY_EMBEDDING,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# TEST 7 — vectorize_extracted_document writes source_type + visibility.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_vector_metadata_has_source_type_and_visibility():
    from apps.public_core.models import ExtractedDocument, DocumentVector
    from apps.public_core.services.openai_extraction import vectorize_extracted_document

    well = _make_well()

    tenant_upload_doc = ExtractedDocument.objects.create(
        well=well,
        api_number=well.api14,
        document_type="w2",
        source_path="tenant-upload.pdf",
        model_tag="gpt-4o",
        status="success",
        errors=[],
        json_data={"well_info": {"api": well.api14, "operator": "Op"}},
        uploaded_by_tenant=uuid.uuid4(),
        source_type=ExtractedDocument.SOURCE_TENANT_UPLOAD,
        is_validated=True,
        validation_errors=[],
    )

    rrc_doc = ExtractedDocument.objects.create(
        well=well,
        api_number=well.api14,
        document_type="w2",
        source_path="rrc-sourced.pdf",
        model_tag="gpt-4o",
        status="success",
        errors=[],
        json_data={"well_info": {"api": well.api14, "operator": "Op"}},
        source_type=ExtractedDocument.SOURCE_RRC,
        is_validated=True,
        validation_errors=[],
    )

    with patch(
        "apps.public_core.services.openai_extraction._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    ):
        created_tenant = vectorize_extracted_document(tenant_upload_doc)
        created_rrc = vectorize_extracted_document(rrc_doc)

    assert created_tenant > 0, "Expected at least one vector for the tenant-upload doc"
    assert created_rrc > 0, "Expected at least one vector for the rrc doc"

    tenant_vec = DocumentVector.objects.filter(
        well=well, file_name="tenant-upload.pdf"
    ).first()
    assert tenant_vec is not None
    assert tenant_vec.metadata.get("source_type") == "tenant_upload", (
        f"Expected metadata.source_type='tenant_upload', got "
        f"{tenant_vec.metadata.get('source_type')!r}. metadata={tenant_vec.metadata}"
    )
    assert tenant_vec.metadata.get("visibility") == "private", (
        f"Expected metadata.visibility='private' for a tenant-uploaded doc, got "
        f"{tenant_vec.metadata.get('visibility')!r}. metadata={tenant_vec.metadata}"
    )

    rrc_vec = DocumentVector.objects.filter(
        well=well, file_name="rrc-sourced.pdf"
    ).first()
    assert rrc_vec is not None
    assert rrc_vec.metadata.get("source_type") == "rrc", (
        f"Expected metadata.source_type='rrc', got {rrc_vec.metadata.get('source_type')!r}. "
        f"metadata={rrc_vec.metadata}"
    )
    assert rrc_vec.metadata.get("visibility") == "public", (
        f"Expected metadata.visibility='public' for an RRC-sourced doc, got "
        f"{rrc_vec.metadata.get('visibility')!r}. metadata={rrc_vec.metadata}"
    )


# ---------------------------------------------------------------------------
# TEST 8 — retrieval + context-prompt rendering label public vs private.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch(
    "apps.public_core.services.research_rag._embed_texts",
    return_value=[DUMMY_EMBEDDING],
)
def test_rag_context_labels_public_vs_private(mock_embed):
    from apps.public_core.services.research_rag import (
        _retrieve_relevant_sections,
        _build_context_prompt,
    )

    tenant = _make_tenant("ctx1")
    well = _make_well()

    _make_vector(
        well, tenant_id_value=tenant.id, source_type="tenant_upload",
        visibility="private", section_text="Private tenant-uploaded content",
    )
    _make_vector(
        well, tenant_id_value=None, source_type="rrc",
        visibility="public", section_text="Public RRC-sourced content",
    )

    session = _make_session(well, tenant=tenant)

    sections = _retrieve_relevant_sections("test question", session)

    by_text = {s["section_text"]: s for s in sections}
    assert "Private tenant-uploaded content" in by_text, (
        "Expected the owning tenant's private vector to be retrieved"
    )
    assert "Public RRC-sourced content" in by_text, (
        "Expected the public vector to be retrieved"
    )

    private_section = by_text["Private tenant-uploaded content"]
    public_section = by_text["Public RRC-sourced content"]

    assert "visibility" in private_section, (
        f"Expected retrieved section dicts to carry a 'visibility' key, got keys: "
        f"{list(private_section.keys())}"
    )
    assert private_section["visibility"] == "private", (
        f"Expected visibility='private' for the tenant-uploaded section, got "
        f"{private_section.get('visibility')!r}"
    )
    assert public_section["visibility"] == "public", (
        f"Expected visibility='public' for the RRC-sourced section, got "
        f"{public_section.get('visibility')!r}"
    )

    prompt = _build_context_prompt(sections)
    assert "PRIVATE" in prompt.upper(), (
        "Expected the rendered context prompt to carry a PRIVATE label for the "
        f"tenant-uploaded section. Got prompt:\n{prompt}"
    )
    assert "PUBLIC" in prompt.upper(), (
        "Expected the rendered context prompt to carry a PUBLIC label for the "
        f"RRC-sourced section. Got prompt:\n{prompt}"
    )


# ---------------------------------------------------------------------------
# TEST 9 — regression guard: tenant isolation still holds after Part B.
# Mirrors test_research_rag_privacy.py::test_private_vector_not_returned_for_other_tenant.
# Must stay GREEN both before and after the fix.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch(
    "apps.public_core.services.research_rag._embed_texts",
    return_value=[DUMMY_EMBEDDING],
)
def test_tenant_isolation_still_holds(mock_embed):
    from apps.public_core.services.research_rag import _retrieve_relevant_sections

    tenant_a = _make_tenant("iso_a")
    tenant_b = _make_tenant("iso_b")
    well = _make_well()

    _make_vector(
        well, tenant_id_value=tenant_b.id, source_type="tenant_upload",
        visibility="private", section_text="Tenant B private content",
    )

    session_a = _make_session(well, tenant=tenant_a)

    sections = _retrieve_relevant_sections("test question", session_a)
    texts = [s["section_text"] for s in sections]
    assert "Tenant B private content" not in texts, (
        "Tenant B's private vector must not be returned for Tenant A's session "
        "(Part B must not weaken existing tenant isolation)"
    )


# ---------------------------------------------------------------------------
# TEST 10 — backfill_document_vector_provenance management command.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_backfill_command_sets_visibility():
    from django.core.management import call_command
    from apps.public_core.models import DocumentVector

    well = _make_well()

    private_vec = _make_vector(
        well, tenant_id_value=uuid.uuid4(), source_type="tenant_upload",
        section_text="Needs backfill - private",
    )
    public_vec = _make_vector(
        well, tenant_id_value=None, source_type="rrc",
        section_text="Needs backfill - public",
    )
    # Sanity: visibility absent before backfill
    assert "visibility" not in private_vec.metadata
    assert "visibility" not in public_vec.metadata

    # --dry-run must make no changes
    call_command("backfill_document_vector_provenance", "--dry-run")
    private_vec.refresh_from_db()
    public_vec.refresh_from_db()
    assert "visibility" not in private_vec.metadata, (
        "--dry-run must not write any changes"
    )
    assert "visibility" not in public_vec.metadata, (
        "--dry-run must not write any changes"
    )

    # Real run
    call_command("backfill_document_vector_provenance")
    private_vec.refresh_from_db()
    public_vec.refresh_from_db()

    assert private_vec.metadata.get("visibility") == "private", (
        f"Expected visibility='private' for a vector with a tenant_id, got "
        f"{private_vec.metadata.get('visibility')!r}"
    )
    assert public_vec.metadata.get("visibility") == "public", (
        f"Expected visibility='public' for a vector with no tenant_id, got "
        f"{public_vec.metadata.get('visibility')!r}"
    )
