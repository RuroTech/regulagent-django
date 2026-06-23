"""
TDD: Tenant privacy tests for _retrieve_relevant_sections.

Tenant isolation: public vectors + the session tenant's private vectors.

IMPORTANT — real metadata shape: the indexing pipeline stores
metadata['tenant_id'] as JSON **null** for public docs (key PRESENT, value
null), not as an absent key. Django's `metadata__tenant_id__isnull=True` only
matches ABSENT keys in JSONB, so a filter relying solely on it silently
excludes every present-null public vector — which broke RAG retrieval in
prod while these tests stayed green (the old fixture omitted the key). The
fixture below now defaults to the present-null shape, and
`test_public_vector_present_null_*` pins it explicitly.

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


def _make_vector(well, tenant_id_value, section_text="Some section content",
                 public_style="null"):
    """
    Create a DocumentVector row.

    tenant_id_value: str (tenant UUID) → private doc
                     None              → public doc

    public_style controls how a public doc represents tenant_id in metadata:
      "null"   → {"tenant_id": None}  (present-null — what the real pipeline writes)
      "absent" → key omitted entirely
    Both must be treated as public; the prod bug only affected "null".
    """
    from apps.public_core.models import DocumentVector

    meta = {"api_number": well.api14}
    if tenant_id_value is not None:
        meta["tenant_id"] = str(tenant_id_value)
    elif public_style == "null":
        meta["tenant_id"] = None  # present-null, matches real indexing pipeline
    # public_style == "absent" → leave the key out

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

    @pytest.mark.parametrize("public_style", ["null", "absent"])
    @patch(
        "apps.public_core.services.research_rag._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    )
    def test_public_vector_both_representations_returned(self, mock_embed, public_style):
        """
        Regression guard for the prod bug: a public vector must be returned to
        a tenant-scoped session whether metadata.tenant_id is present-null
        ({"tenant_id": null}) or the key is absent.

        The present-null case is what the real pipeline writes; before the fix
        the tenant filter used metadata__tenant_id__isnull=True, which matched
        ONLY the absent-key form, so present-null public vectors were silently
        dropped and tenant sessions retrieved nothing. This test fails against
        that old filter for public_style="null".
        """
        from apps.public_core.services.research_rag import _retrieve_relevant_sections

        tenant_a = _make_tenant(f"pub_{public_style[:3]}")
        well = _make_well()
        _make_vector(
            well, tenant_id_value=None,
            section_text=f"Public {public_style} content",
            public_style=public_style,
        )
        session_a = _make_session(well, tenant=tenant_a)

        sections = _retrieve_relevant_sections("test question", session_a)
        texts = [s["section_text"] for s in sections]
        assert f"Public {public_style} content" in texts, (
            f"Public vector (tenant_id {public_style}) must be returned for a "
            f"tenant-scoped session"
        )

    @patch(
        "apps.public_core.services.research_rag._embed_texts",
        return_value=[DUMMY_EMBEDDING],
    )
    def test_owning_tenant_match_handles_int_and_str_ids(self, mock_embed):
        """
        The owning-tenant branch must match the tenant id whether the vector
        stored it as a string or an int (real data has both forms). Guards the
        str/int acceptance added alongside the present-null fix.
        """
        from apps.public_core.models import DocumentVector
        from apps.public_core.services.research_rag import _retrieve_relevant_sections

        tenant_b = _make_tenant("intid")
        well = _make_well()
        # Vector that stored tenant_id as an int rather than a str
        DocumentVector.objects.create(
            well=well, file_name="t.pdf", document_type="c_103",
            section_name="header", section_text="Int-tagged private content",
            embedding=DUMMY_EMBEDDING,
            metadata={"api_number": well.api14, "tenant_id": tenant_b.id},
        )
        session_b = _make_session(well, tenant=tenant_b)

        texts = [s["section_text"] for s in _retrieve_relevant_sections("q", session_b)]
        assert "Int-tagged private content" in texts, (
            "Owning tenant must match its vector even when tenant_id was stored as int"
        )
