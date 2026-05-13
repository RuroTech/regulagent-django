"""
TDD: Tenant privacy tests for _retrieve_relevant_sections.

These tests FAIL against the current code (no tenant_id filter in base_qs)
and PASS after the fix adds:

    tenant_id = str(session.tenant_id) if session.tenant_id else None
    if tenant_id:
        base_qs = base_qs.filter(
            Q(metadata__tenant_id__isnull=True) |
            Q(metadata__tenant_id=tenant_id)
        )
    else:
        base_qs = base_qs.filter(metadata__tenant_id__isnull=True)

The test DB is PostgreSQL + pgvector (see root conftest.py), so real
CosineDistance ordering works. We mock _embed_texts to avoid OpenAI calls.
"""
import uuid
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_EMBEDDING = [0.1] * 3072  # matches DocumentVector.embedding dimensions


def _make_tenant(suffix=None):
    """Create a lightweight Tenant row without spinning up a full PG schema."""
    from apps.tenants.models import Tenant

    uid = suffix or str(uuid.uuid4())[:8]
    return Tenant.objects.create(
        schema_name=f"test_privacy_{uid}",
        name=f"Privacy Test Tenant {uid}",
        slug=f"privacy-{uid}",
    )


def _make_well(api14=None):
    """Create a minimal WellRegistry row."""
    from apps.public_core.models import WellRegistry

    api14 = api14 or f"42501{uuid.uuid4().int % 10 ** 9:09d}"
    return WellRegistry.objects.create(
        api14=api14,
        state="TX",
        county="Andrews",
        operator_name="Privacy Test Op",
    )


def _make_session(well, tenant=None):
    """Create a ResearchSession linked to a well (and optionally a tenant)."""
    from apps.public_core.models import ResearchSession

    return ResearchSession.objects.create(
        api_number=well.api14,
        state="TX",
        status="ready",
        well=well,
        tenant=tenant,
    )


def _make_vector(well, tenant_id_value, section_text="Some section content"):
    """
    Create a DocumentVector row.

    tenant_id_value: str (tenant UUID) → private doc
                     None              → public doc
    """
    from apps.public_core.models import DocumentVector

    meta = {"api_number": well.api14}
    if tenant_id_value is not None:
        meta["tenant_id"] = str(tenant_id_value)
    # Public docs omit the key entirely (None → key absent), matching real pipeline.

    return DocumentVector.objects.create(
        well=well,
        file_name="test.pdf",
        document_type="c_103",
        section_name="header",
        section_text=section_text,
        embedding=DUMMY_EMBEDDING,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestResearchRagTenantPrivacy:
    """
    Tenant isolation tests for _retrieve_relevant_sections.

    All four tests are expected to FAIL against the current code because the
    current code never filters DocumentVector rows by metadata['tenant_id'].
    """

    @patch(
        "apps.public_core.services.research_rag._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    )
    def test_private_vector_not_returned_for_other_tenant(self, mock_embed):
        """
        A DocumentVector tagged with Tenant B's tenant_id must NOT appear in
        results when the ResearchSession belongs to Tenant A.

        FAILS now: base_qs has no tenant filter, so the vector leaks through.
        PASSES after fix: the Q() filter excludes Tenant B's private vectors.
        """
        from apps.public_core.services.research_rag import _retrieve_relevant_sections

        tenant_a = _make_tenant("ta1")
        tenant_b = _make_tenant("tb1")
        well = _make_well()

        # Vector owned by Tenant B
        _make_vector(well, tenant_id_value=tenant_b.id, section_text="Private B content")

        # Session belongs to Tenant A
        session_a = _make_session(well, tenant=tenant_a)

        sections = _retrieve_relevant_sections("test question", session_a)

        section_texts = [s["section_text"] for s in sections]
        assert "Private B content" not in section_texts, (
            "Tenant B's private vector must not be returned for Tenant A's session"
        )

    @patch(
        "apps.public_core.services.research_rag._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    )
    def test_private_vector_returned_for_owning_tenant(self, mock_embed):
        """
        A DocumentVector tagged with Tenant B's tenant_id MUST appear in
        results when the ResearchSession belongs to Tenant B.

        FAILS now: the fix doesn't exist yet, but this particular assertion
        passes accidentally — however, the companion test_1 above fails, so
        at least one of the pair exposes the bug.

        NOTE: This test actually passes with current code too (no filter = all
        returned). We keep it here so the full suite documents correct post-fix
        behaviour and guards against over-filtering regressions.
        """
        from apps.public_core.services.research_rag import _retrieve_relevant_sections

        tenant_b = _make_tenant("tb2")
        well = _make_well()

        # Vector owned by Tenant B
        _make_vector(well, tenant_id_value=tenant_b.id, section_text="Private B content owner")

        # Session also belongs to Tenant B
        session_b = _make_session(well, tenant=tenant_b)

        sections = _retrieve_relevant_sections("test question", session_b)

        section_texts = [s["section_text"] for s in sections]
        assert "Private B content owner" in section_texts, (
            "Tenant B's private vector must be returned for Tenant B's own session"
        )

    @patch(
        "apps.public_core.services.research_rag._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    )
    def test_public_vector_returned_for_all_tenants(self, mock_embed):
        """
        A DocumentVector with no tenant_id in metadata (public doc) must be
        returned for sessions belonging to any tenant.

        FAILS now: with the current code this passes trivially (no filter at
        all). After the fix the Q() must explicitly include public docs
        (metadata__tenant_id__isnull=True) — a regression here would break
        the entire RAG pipeline.

        We pair it with the cross-tenant leak test so the combined suite
        forces the fix to be correct on both sides.
        """
        from apps.public_core.services.research_rag import _retrieve_relevant_sections

        tenant_a = _make_tenant("ta3")
        tenant_b = _make_tenant("tb3")
        well = _make_well()

        # Public vector (no tenant_id key in metadata)
        _make_vector(well, tenant_id_value=None, section_text="Public shared content")

        session_a = _make_session(well, tenant=tenant_a)
        session_b = _make_session(well, tenant=tenant_b)

        sections_a = _retrieve_relevant_sections("test question", session_a)
        sections_b = _retrieve_relevant_sections("test question", session_b)

        texts_a = [s["section_text"] for s in sections_a]
        texts_b = [s["section_text"] for s in sections_b]

        assert "Public shared content" in texts_a, (
            "Public vector must be returned for Tenant A"
        )
        assert "Public shared content" in texts_b, (
            "Public vector must be returned for Tenant B"
        )

    @patch(
        "apps.public_core.services.research_rag._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    )
    def test_no_tenant_session_only_sees_public_vectors(self, mock_embed):
        """
        A ResearchSession with no tenant (tenant=None / anonymous) must ONLY
        see vectors where metadata['tenant_id'] is absent or null. Private
        vectors from any tenant must be excluded.

        FAILS now: no tenant_id filter, so private vectors from any tenant
        are returned in no-tenant sessions.
        PASSES after fix: the else branch filters to metadata__tenant_id__isnull=True.
        """
        from apps.public_core.services.research_rag import _retrieve_relevant_sections

        some_tenant = _make_tenant("tx4")
        well = _make_well()

        # One public vector and one private vector for same well
        _make_vector(well, tenant_id_value=None, section_text="Public content no tenant")
        _make_vector(
            well,
            tenant_id_value=some_tenant.id,
            section_text="Private content should be hidden",
        )

        # Anonymous/no-tenant session
        session_anon = _make_session(well, tenant=None)

        sections = _retrieve_relevant_sections("test question", session_anon)

        section_texts = [s["section_text"] for s in sections]

        assert "Public content no tenant" in section_texts, (
            "Public vector must still be returned for no-tenant session"
        )
        assert "Private content should be hidden" not in section_texts, (
            "Private vector from a real tenant must NOT be returned for a no-tenant session"
        )
