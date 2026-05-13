"""
TDD: Failing tests for tenant isolation in Research Session endpoints.

These tests are written BEFORE the fixes are implemented.  They are expected
to FAIL right now because the views use unguarded ``ResearchSession.objects.get(id=...)``
and ``ChatThread.objects.get(id=...)`` calls that do not filter by tenant.

Expected failure mode (current behaviour):
  - cross-tenant GET /api/research/sessions/{id}/  → 200  (should be 404)
  - cross-tenant GET  …/documents/                 → 200  (should be 404)
  - cross-tenant POST …/ask/                       → 409 or 200 stream  (should be 404)
  - cross-tenant GET  …/chat/                      → 200  (should be 404)
  - cross-tenant GET  …/summary/                   → 200  (should be 404)
  - POST /api/research/sessions/ leaks in-progress session across tenants → wrong tenant in response
  - GET  /api/chat/threads/{id}/debug-permissions/ → 200 with wrong tenant's data (should be 404)

Endpoints under test
--------------------
    GET    /api/research/sessions/{id}/
    GET    /api/research/sessions/{id}/documents/
    POST   /api/research/sessions/{id}/ask/
    GET    /api/research/sessions/{id}/chat/
    GET    /api/research/sessions/{id}/summary/
    POST   /api/research/sessions/
    GET    /api/chat/threads/{id}/debug-permissions/
"""
import uuid
import pytest
from unittest.mock import patch, MagicMock
from rest_framework.test import APIClient

from apps.public_core.models import ResearchSession


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_public_tenant(db_fixture):
    """
    Ensure the public tenant + 'testserver' domain exist.

    The TenantMainMiddleware resolves every test request to a tenant via the
    Host header.  APIClient sends Host: testserver by default, so the public
    tenant must be mapped to that domain.

    We insert via raw SQL to break the circular-dependency between Tenant
    (needs owner FK) and User (needs Tenant to exist first).  Uses
    ON CONFLICT DO NOTHING so it is safe to call multiple times.
    """
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_conn, transaction

    User = get_user_model()

    # Create an owner user first (User table has no Tenant FK at the DB level)
    owner, _ = User.objects.get_or_create(
        email="public_tenant_owner@test.internal",
        defaults={"is_active": True, "first_name": "", "last_name": ""},
    )

    with transaction.atomic():
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants_tenant
                    (schema_name, name, slug, owner_id, created, modified, created_on, vault_passphrase_hash)
                VALUES ('public', 'Public', 'public', %s, NOW(), NOW(), NOW(), '')
                ON CONFLICT (schema_name) DO NOTHING
                """,
                [owner.id],
            )

    tenant = Tenant.objects.get(schema_name="public")
    Domain.objects.get_or_create(
        domain="testserver",
        defaults={"tenant": tenant, "is_primary": True},
    )
    return tenant


def _make_org_tenant(slug: str, name: str):
    """
    Create a minimal org-level Tenant row (no schema creation — we only need
    the FK target for ResearchSession.tenant).  Uses raw SQL so we can skip
    schema migrations which are slow and fragile in unit tests.

    NOTE: auto_create_schema = True on Tenant will fire a post-save signal
    that tries to create a Postgres schema.  We work around this by inserting
    directly via SQL and fetching the row back.
    """
    from apps.tenants.models import Tenant
    from django.db import connection as db_conn

    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants_tenant
                (schema_name, name, slug, owner_id, created, modified, created_on, vault_passphrase_hash)
            VALUES (%s, %s, %s, NULL, NOW(), NOW(), NOW(), '')
            ON CONFLICT (schema_name) DO NOTHING
            """,
            [slug, name, slug],
        )
    return Tenant.objects.get(schema_name=slug)


def _make_user(email: str, tenant=None):
    """
    Create a User in the public schema and (optionally) link it to *tenant*.

    We deliberately do NOT call tenant.add_user() because that fires
    django-tenant-users signals that expect a real Postgres schema to exist.
    Instead we manually add the M2M link through the through-table so
    request.user.tenants.first() resolves to the correct tenant.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user, _ = User.objects.get_or_create(
        email=email,
        defaults={"is_active": True, "first_name": "", "last_name": ""},
    )

    if tenant is not None:
        # Link user to tenant via the M2M through table, bypassing the
        # schema-creation side-effects of Tenant.add_user().
        user.tenants.add(tenant)

    return user


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def public_tenant(db):
    """Ensure the public tenant and testserver domain exist."""
    return _make_public_tenant(db)


@pytest.fixture
def tenant_a(db, public_tenant):
    """A minimal org tenant 'alpha' — no Postgres schema created."""
    return _make_org_tenant(slug="alpha", name="Alpha Corp")


@pytest.fixture
def tenant_b(db, public_tenant):
    """A minimal org tenant 'beta' — no Postgres schema created."""
    return _make_org_tenant(slug="beta", name="Beta Corp")


@pytest.fixture
def user_a(db, tenant_a):
    """User belonging to tenant_a."""
    return _make_user("user_a@alpha.test", tenant=tenant_a)


@pytest.fixture
def user_b(db, tenant_b):
    """User belonging to tenant_b."""
    return _make_user("user_b@beta.test", tenant=tenant_b)


@pytest.fixture
def client_a(user_a, public_tenant):
    """APIClient authenticated as user_a (tenant_a)."""
    client = APIClient()
    client.force_authenticate(user=user_a)
    return client


@pytest.fixture
def client_b(user_b, public_tenant):
    """APIClient authenticated as user_b (tenant_b)."""
    client = APIClient()
    client.force_authenticate(user=user_b)
    return client


@pytest.fixture
def session_a(db, tenant_a):
    """A ready ResearchSession belonging to tenant_a."""
    return ResearchSession.objects.create(
        api_number="42501705750000",
        state="TX",
        status="ready",
        tenant=tenant_a,
        total_documents=5,
        indexed_documents=5,
    )


@pytest.fixture
def pending_session_a(db, tenant_a):
    """An in-progress ResearchSession belonging to tenant_a."""
    return ResearchSession.objects.create(
        api_number="42501705750000",
        state="TX",
        status="pending",
        tenant=tenant_a,
    )


@pytest.fixture
def public_session(db):
    """A ready ResearchSession with no tenant (public / bulk-ingestion origin)."""
    return ResearchSession.objects.create(
        api_number="42501705750000",
        state="TX",
        status="ready",
        tenant=None,
        total_documents=3,
        indexed_documents=3,
    )


# ---------------------------------------------------------------------------
# 1. GET /api/research/sessions/{id}/ — detail view
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_detail_view_blocks_cross_tenant_access(client_b, session_a):
    """
    FAILS now: ResearchSessionDetailView has no tenant filter, returns 200.
    PASSES after fix: must return 404 when tenant_b user requests tenant_a session.
    """
    resp = client_b.get(f"/api/research/sessions/{session_a.id}/")
    assert resp.status_code == 404, (
        f"Expected 404 for cross-tenant access, got {resp.status_code}. "
        "Fix: filter ResearchSession.objects.get(id=...) by user tenant."
    )


@pytest.mark.django_db
def test_detail_view_allows_own_tenant_access(client_a, session_a):
    """
    Should pass before and after the fix: same tenant must be allowed.
    """
    resp = client_a.get(f"/api/research/sessions/{session_a.id}/")
    assert resp.status_code == 200, (
        f"Expected 200 for own-tenant access, got {resp.status_code}."
    )


@pytest.mark.django_db
def test_detail_view_allows_public_session_access(client_b, public_session):
    """
    Sessions with tenant=None (public) must remain accessible by any authenticated user.
    Should pass before and after the fix.
    """
    resp = client_b.get(f"/api/research/sessions/{public_session.id}/")
    assert resp.status_code == 200, (
        f"Expected 200 for public (tenant=None) session, got {resp.status_code}."
    )


# ---------------------------------------------------------------------------
# 2. GET /api/research/sessions/{id}/documents/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_documents_view_blocks_cross_tenant_access(client_b, session_a):
    """
    FAILS now: ResearchSessionDocumentsView has no tenant filter, returns 200.
    PASSES after fix: must return 404 for cross-tenant access.
    """
    resp = client_b.get(f"/api/research/sessions/{session_a.id}/documents/")
    assert resp.status_code == 404, (
        f"Expected 404 for cross-tenant documents access, got {resp.status_code}."
    )


# ---------------------------------------------------------------------------
# 3. POST /api/research/sessions/{id}/ask/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ask_view_blocks_cross_tenant_access(client_b, session_a):
    """
    FAILS now: ResearchSessionAskView does the tenant-unguarded .get() before
    the status check, so a cross-tenant request to a 'ready' session reaches
    the streaming path (200) or hits serializer validation.  Must return 404.

    The session is status='ready' so we bypass the 409 conflict early-exit —
    the only blocker should be the tenant check.
    """
    resp = client_b.post(
        f"/api/research/sessions/{session_a.id}/ask/",
        {"question": "What is the casing depth?", "top_k": 3},
        format="json",
    )
    assert resp.status_code == 404, (
        f"Expected 404 for cross-tenant ask, got {resp.status_code}. "
        "Fix: add tenant guard before the status check in ResearchSessionAskView."
    )


# ---------------------------------------------------------------------------
# 4. GET /api/research/sessions/{id}/chat/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_chat_view_blocks_cross_tenant_access(client_b, session_a):
    """
    FAILS now: ResearchSessionChatView has no tenant filter, returns 200.
    PASSES after fix: must return 404 for cross-tenant access.
    """
    resp = client_b.get(f"/api/research/sessions/{session_a.id}/chat/")
    assert resp.status_code == 404, (
        f"Expected 404 for cross-tenant chat access, got {resp.status_code}."
    )


# ---------------------------------------------------------------------------
# 5. GET /api/research/sessions/{id}/summary/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_summary_view_blocks_cross_tenant_access(client_b, session_a):
    """
    FAILS now: ResearchSessionSummaryView has no tenant filter, returns 200.
    PASSES after fix: must return 404 for cross-tenant access.
    """
    resp = client_b.get(f"/api/research/sessions/{session_a.id}/summary/")
    assert resp.status_code == 404, (
        f"Expected 404 for cross-tenant summary access, got {resp.status_code}."
    )


# ---------------------------------------------------------------------------
# 6. POST /api/research/sessions/ — in-progress session must NOT be leaked
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.public_core.views.research.start_research_session_task")
def test_list_create_post_does_not_return_other_tenant_inprogress_session(
    mock_task, client_b, pending_session_a, tenant_b
):
    """
    FAILS now: ResearchSessionListCreateView returns the in-progress session from
    tenant_a when tenant_b user POSTs for the same api_number (line ~173 in research.py).

    Expected correct behaviour after fix:
    - The response session ID must NOT equal pending_session_a.id
    - Either a new pending session is created (201) for tenant_b, or a 202/pending
      response for a newly dispatched task — but in NO case should the tenant_a session
      be handed to tenant_b.

    We check two invariants:
      1. response session_id != tenant_a's session id
      2. If a session object is returned, its tenant must be tenant_b (not tenant_a)
    """
    mock_task_result = MagicMock()
    mock_task_result.id = "fake-celery-id"
    mock_task.delay.return_value = mock_task_result

    resp = client_b.post(
        "/api/research/sessions/",
        {"api_number": "42501705750000", "state": "TX"},
        format="json",
    )

    assert resp.status_code in (200, 201), (
        f"Expected 200 or 201, got {resp.status_code}"
    )

    returned_id = str(resp.data.get("id", ""))
    assert returned_id != str(pending_session_a.id), (
        "Tenant isolation bug: POST /api/research/sessions/ returned tenant_a's "
        f"in-progress session ({pending_session_a.id}) to a tenant_b user. "
        "Fix: scope the in-progress session lookup to the requesting user's tenant."
    )

    # If the response contains a session, verify its tenant is tenant_b
    if returned_id:
        try:
            returned_session = ResearchSession.objects.get(id=returned_id)
            assert returned_session.tenant_id == tenant_b.id, (
                f"Returned session belongs to tenant {returned_session.tenant_id} "
                f"but expected tenant_b ({tenant_b.id})."
            )
        except ResearchSession.DoesNotExist:
            pass  # UUID in response didn't correspond to a DB row — unexpected but skip FK check


# ---------------------------------------------------------------------------
# 7. GET /api/chat/threads/{id}/debug-permissions/ — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_debug_thread_permissions_blocks_cross_tenant_access(
    db, public_tenant, user_a, user_b, tenant_a
):
    """
    FAILS now: debug_thread_permissions does a bare ChatThread.objects.get(id=...)
    without any tenant filter, returning full debug info for any thread to any user.

    PASSES after fix: must return 404 when a user from tenant_b requests a thread
    that belongs to tenant_a.

    ChatThread.tenant_id is a UUIDField (not a FK), so we can create the thread
    without a Postgres schema for tenant_a.

    Note: ChatThread also requires a 'well' FK (WellRegistry) and a
    'baseline_plan' FK (PlanSnapshot), so we create minimal supporting rows.
    """
    from apps.public_core.models import WellRegistry, PlanSnapshot
    from apps.assistant.models import ChatThread

    # Create a minimal well and plan snapshot (needed by ChatThread FKs)
    well = WellRegistry.objects.create(
        api14="42501705750099",
        state="TX",
        county="Andrews",
        district="08A",
        operator_name="Test Operator",
        field_name="Test Field",
        lease_name="Test Lease",
        well_number="99",
    )
    plan = PlanSnapshot.objects.create(
        well=well,
        plan_id="42501705750099:baseline",
        kind="baseline",
        status="draft",
        payload={"steps": [], "kernel_version": "1.0"},
    )

    # Create a ChatThread owned by user_a in tenant_a
    thread = ChatThread.objects.create(
        tenant_id=tenant_a.id,
        created_by=user_a,
        well=well,
        baseline_plan=plan,
        current_plan=plan,
        title="Test thread",
    )

    # Authenticate as user_b (belongs to tenant_b — different tenant)
    client_b_local = APIClient()
    client_b_local.force_authenticate(user=user_b)

    resp = client_b_local.get(f"/api/chat/threads/{thread.id}/debug-permissions/")

    assert resp.status_code == 404, (
        f"Expected 404 for cross-tenant debug-permissions access, got {resp.status_code}. "
        "Fix: add tenant_id filter in debug_thread_permissions view before returning "
        "thread data. A user from tenant_b must not see tenant_a's thread details."
    )
