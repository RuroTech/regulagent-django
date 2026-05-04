"""
Integration tests for Research Session REST API endpoints.

Endpoints tested:
    POST   /api/research/sessions/
    GET    /api/research/sessions/{id}/
    GET    /api/research/sessions/{id}/documents/
    POST   /api/research/sessions/{id}/ask/
    GET    /api/research/sessions/{id}/chat/
"""
import uuid
import pytest
from unittest.mock import patch, MagicMock
from rest_framework.test import APIClient

from apps.public_core.models import ResearchSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def public_tenant(db):
    """
    Create the public tenant + Domain so TenantMainMiddleware resolves 'testserver'.

    tenant_users.TenantBase.owner is NOT NULL, creating a circular dependency:
    User.create_user() requires a Tenant, but Tenant requires an owner User.
    We break the cycle by inserting via raw SQL (bypassing the constraint temporarily),
    creating a User, then backfilling owner_id.
    """
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_connection

    User = get_user_model()

    # Break the circular dependency (Tenant needs owner, User's create_user needs Tenant).
    # Use SET CONSTRAINTS DEFERRED to allow inserting Tenant with owner FK within
    # the same transaction as the User row is created.
    from django.db import transaction

    # First create User outside a transaction (User table has no Tenant FK at DB level)
    owner, _ = User.objects.get_or_create(
        email="tenant_owner@test.internal",
        defaults={"is_active": True, "first_name": "", "last_name": ""},
    )

    # Then create Tenant with valid owner_id (deferred constraints not needed now)
    with transaction.atomic():
        with db_connection.cursor() as cur:
            cur.execute("""
                INSERT INTO tenants_tenant (schema_name, name, slug, owner_id, created, modified, created_on, vault_passphrase_hash)
                VALUES ('public', 'Public', 'public', %s, NOW(), NOW(), NOW(), '')
                ON CONFLICT (schema_name) DO NOTHING
            """, [owner.id])

    tenant = Tenant.objects.get(schema_name="public")

    # Step 4: Register 'testserver' domain so middleware routes to public schema
    Domain.objects.get_or_create(
        domain="testserver",
        defaults={"tenant": tenant, "is_primary": True},
    )
    return tenant


@pytest.fixture
def api_client(db, public_tenant):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        email="testresearch@example.com",
        defaults={"is_active": True},
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def pending_session(db):
    return ResearchSession.objects.create(
        api_number="30-015-28692",
        state="NM",
        status="pending",
    )


@pytest.fixture
def ready_session(db):
    return ResearchSession.objects.create(
        api_number="30-015-28692",
        state="NM",
        status="ready",
        total_documents=3,
        indexed_documents=3,
    )


# ---------------------------------------------------------------------------
# POST /api/research/sessions/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.views.research.start_research_session_task")
def test_create_session_returns_201(mock_task, api_client):
    mock_task_result = MagicMock()
    mock_task_result.id = "fake-celery-task-id"
    mock_task.delay.return_value = mock_task_result

    resp = api_client.post(
        "/api/research/sessions/",
        {"api_number": "30-015-28692"},
        format="json",
    )

    assert resp.status_code == 201
    assert resp.data["api_number"] == "30-015-28692"
    assert resp.data["state"] == "NM"
    assert resp.data["status"] == "pending"
    mock_task.delay.assert_called_once()


@pytest.mark.django_db
@patch("apps.public_core.views.research.start_research_session_task")
def test_create_session_nm_api_number_detected(mock_task, api_client):
    mock_task_result = MagicMock()
    mock_task_result.id = "task-abc"
    mock_task.delay.return_value = mock_task_result

    resp = api_client.post(
        "/api/research/sessions/",
        {"api_number": "30-015-28692"},
        format="json",
    )

    assert resp.status_code == 201
    assert resp.data["state"] == "NM"


@pytest.mark.django_db
@patch("apps.public_core.views.research.start_research_session_task")
def test_create_session_tx_api_number_detected(mock_task, api_client):
    mock_task_result = MagicMock()
    mock_task_result.id = "task-def"
    mock_task.delay.return_value = mock_task_result

    resp = api_client.post(
        "/api/research/sessions/",
        {"api_number": "42-501-70575"},
        format="json",
    )

    assert resp.status_code == 201
    assert resp.data["state"] == "TX"


@pytest.mark.django_db
@patch("apps.public_core.views.research.start_research_session_task")
def test_create_session_explicit_state_override(mock_task, api_client):
    mock_task_result = MagicMock()
    mock_task_result.id = "task-xyz"
    mock_task.delay.return_value = mock_task_result

    resp = api_client.post(
        "/api/research/sessions/",
        {"api_number": "30-015-28692", "state": "TX"},
        format="json",
    )

    assert resp.status_code == 201
    assert resp.data["state"] == "TX"


@pytest.mark.django_db
def test_create_session_missing_api_number_returns_400(api_client):
    resp = api_client.post("/api/research/sessions/", {}, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_create_session_unauthenticated_returns_401():
    client = APIClient()
    resp = client.post(
        "/api/research/sessions/",
        {"api_number": "30-015-28692"},
        format="json",
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/research/sessions/{id}/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_session_returns_session_data(api_client, pending_session):
    resp = api_client.get(f"/api/research/sessions/{pending_session.id}/")
    assert resp.status_code == 200
    assert str(resp.data["id"]) == str(pending_session.id)
    assert resp.data["api_number"] == "30-015-28692"
    assert resp.data["state"] == "NM"
    assert resp.data["status"] == "pending"


@pytest.mark.django_db
def test_get_session_not_found_returns_404(api_client):
    resp = api_client.get(f"/api/research/sessions/{uuid.uuid4()}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_get_session_ready_status(api_client, ready_session):
    resp = api_client.get(f"/api/research/sessions/{ready_session.id}/")
    assert resp.status_code == 200
    assert resp.data["status"] == "ready"


# ---------------------------------------------------------------------------
# GET /api/research/sessions/{id}/documents/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_documents_returns_document_list(api_client, pending_session):
    resp = api_client.get(f"/api/research/sessions/{pending_session.id}/documents/")
    assert resp.status_code == 200
    assert resp.data["session_id"] == str(pending_session.id)
    assert resp.data["api_number"] == "30-015-28692"
    assert resp.data["state"] == "NM"
    assert "document_list" in resp.data
    assert "extracted_documents" in resp.data


@pytest.mark.django_db
def test_get_documents_not_found_returns_404(api_client):
    resp = api_client.get(f"/api/research/sessions/{uuid.uuid4()}/documents/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_get_documents_returns_counts(api_client, ready_session):
    resp = api_client.get(f"/api/research/sessions/{ready_session.id}/documents/")
    assert resp.status_code == 200
    assert resp.data["total_documents"] == 3
    assert resp.data["indexed_documents"] == 3


# ---------------------------------------------------------------------------
# POST /api/research/sessions/{id}/ask/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_ask_on_non_ready_session_returns_409(api_client, pending_session):
    resp = api_client.post(
        f"/api/research/sessions/{pending_session.id}/ask/",
        {"question": "What is the casing depth?"},
        format="json",
    )
    assert resp.status_code == 409


@pytest.mark.django_db
def test_ask_missing_question_returns_400(api_client, ready_session):
    resp = api_client.post(
        f"/api/research/sessions/{ready_session.id}/ask/",
        {},
        format="json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_ask_not_found_returns_404(api_client):
    resp = api_client.post(
        f"/api/research/sessions/{uuid.uuid4()}/ask/",
        {"question": "What is the surface casing depth?"},
        format="json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
@patch("apps.public_core.views.research.stream_research_answer")
def test_ask_on_ready_session_returns_streaming_response(mock_stream, api_client, ready_session):
    def fake_stream(*args, **kwargs):
        yield 'data: {"type": "token", "content": "Hello"}\n\n'
        yield 'data: {"type": "done"}\n\n'

    mock_stream.return_value = fake_stream()

    resp = api_client.post(
        f"/api/research/sessions/{ready_session.id}/ask/",
        {"question": "What is the surface casing depth?"},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.get("Content-Type", "").startswith("text/event-stream")


# ---------------------------------------------------------------------------
# GET /api/research/sessions/{id}/chat/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_chat_returns_empty_list_initially(api_client, pending_session):
    resp = api_client.get(f"/api/research/sessions/{pending_session.id}/chat/")
    assert resp.status_code == 200
    assert resp.data["session_id"] == str(pending_session.id)
    assert resp.data["messages"] == []


@pytest.mark.django_db
def test_get_chat_not_found_returns_404(api_client):
    resp = api_client.get(f"/api/research/sessions/{uuid.uuid4()}/chat/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_get_chat_returns_messages_after_interaction(api_client, pending_session):
    from apps.public_core.models import ResearchMessage
    ResearchMessage.objects.create(
        session=pending_session,
        role="user",
        content="What are the casing depths?",
    )
    ResearchMessage.objects.create(
        session=pending_session,
        role="assistant",
        content="The surface casing is set at 500 ft.",
        citations=[{"doc_type": "c_105", "section_name": "casing_record", "excerpt": "..."}],
    )

    resp = api_client.get(f"/api/research/sessions/{pending_session.id}/chat/")
    assert resp.status_code == 200
    assert len(resp.data["messages"]) == 2
    assert resp.data["messages"][0]["role"] == "user"
    assert resp.data["messages"][1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# POST /api/research/sessions/bulk/
# ---------------------------------------------------------------------------

BULK_URL = "/api/research/sessions/bulk/"

MOCK_PATH = "apps.public_core.views.research.start_research_session_task"


@pytest.mark.django_db
class TestBulkResearchSessionCreate:
    """
    Failing tests for the bulk research session creation endpoint.

    These tests are written BEFORE the endpoint is implemented (TDD).
    They are expected to fail with 404 (URL not registered) until
    the Backend Engineer wires up the view.
    """

    # ------------------------------------------------------------------
    # 1. Happy path — two valid APIs (TX + NM)
    # ------------------------------------------------------------------

    @patch(MOCK_PATH)
    def test_creates_sessions_for_valid_apis(self, mock_task, api_client):
        mock_task.delay.return_value = MagicMock(id="test-celery-id")

        resp = api_client.post(
            BULK_URL,
            {"api_numbers": ["42-123-45678-0000", "30-015-28692-0000"]},
            format="json",
        )

        assert resp.status_code == 201
        assert resp.data["submitted"] == 2

        sessions = resp.data["sessions"]
        assert len(sessions) == 2

        # Each entry should have a real UUID session_id and pending status
        for entry in sessions:
            assert entry["session_id"] is not None, (
                f"Expected a session_id UUID but got None for api {entry['api_number']}"
            )
            assert entry["status"] == "pending"
            assert entry["error"] is None

        # Verify state auto-detection
        tx_entry = next(e for e in sessions if "42" in e["api_number"])
        nm_entry = next(e for e in sessions if "30" in e["api_number"])
        assert tx_entry["state"] == "TX"
        assert nm_entry["state"] == "NM"

        # Task must have been dispatched exactly twice
        assert mock_task.delay.call_count == 2

        # Both sessions must exist in DB
        assert ResearchSession.objects.count() == 2

    # ------------------------------------------------------------------
    # 2. Unknown prefix without global state override → error row
    # ------------------------------------------------------------------

    @patch(MOCK_PATH)
    def test_unknown_prefix_without_override_returns_error_row(self, mock_task, api_client):
        mock_task.delay.return_value = MagicMock(id="test-celery-id")

        resp = api_client.post(
            BULK_URL,
            {"api_numbers": ["99-000-00000-0000"]},
            format="json",
        )

        assert resp.status_code == 201
        assert resp.data["submitted"] == 1

        entry = resp.data["sessions"][0]
        assert entry["session_id"] is None
        assert entry["status"] == "error"
        assert entry["error"] is not None and len(entry["error"]) > 0

        # No Celery task dispatched, no DB row created
        mock_task.delay.assert_not_called()
        assert ResearchSession.objects.count() == 0

    # ------------------------------------------------------------------
    # 3. Unknown prefix WITH global state override → session created
    # ------------------------------------------------------------------

    @patch(MOCK_PATH)
    def test_global_state_override_covers_unknown_prefix(self, mock_task, api_client):
        mock_task.delay.return_value = MagicMock(id="test-celery-id")

        resp = api_client.post(
            BULK_URL,
            {"api_numbers": ["99-000-00000-0000"], "state": "TX"},
            format="json",
        )

        assert resp.status_code == 201
        assert resp.data["submitted"] == 1

        entry = resp.data["sessions"][0]
        assert entry["session_id"] is not None
        assert entry["status"] == "pending"
        assert entry["state"] == "TX"
        assert entry["error"] is None

        # Session must be persisted with state=TX
        assert ResearchSession.objects.filter(state="TX").count() == 1
        assert mock_task.delay.call_count == 1

    # ------------------------------------------------------------------
    # 4. Intra-batch deduplication
    # ------------------------------------------------------------------

    @patch(MOCK_PATH)
    def test_deduplicates_within_request(self, mock_task, api_client):
        mock_task.delay.return_value = MagicMock(id="test-celery-id")

        resp = api_client.post(
            BULK_URL,
            {"api_numbers": ["42-123-45678-0000", "42-123-45678-0000"]},
            format="json",
        )

        assert resp.status_code == 201
        assert resp.data["submitted"] == 2

        sessions = resp.data["sessions"]
        assert len(sessions) == 2

        first, second = sessions[0], sessions[1]

        # First occurrence succeeds
        assert first["session_id"] is not None
        assert first["status"] == "pending"

        # Second occurrence is an error row
        assert second["session_id"] is None
        assert second["status"] == "error"
        assert second["error"] is not None
        assert "duplicate" in second["error"].lower(), (
            f"Expected 'duplicate' (case-insensitive) in error message, got: {second['error']!r}"
        )

        # Only one DB row created, task dispatched once
        assert ResearchSession.objects.count() == 1
        assert mock_task.delay.call_count == 1

    # ------------------------------------------------------------------
    # 5. Validation: max 50 API numbers
    # ------------------------------------------------------------------

    @patch(MOCK_PATH)
    def test_enforces_max_50(self, mock_task, api_client):
        mock_task.delay.return_value = MagicMock(id="test-celery-id")

        # Build 51 unique-looking TX API numbers
        api_numbers = [f"42-{i:03d}-{i:05d}-0000" for i in range(1, 52)]
        assert len(api_numbers) == 51

        resp = api_client.post(
            BULK_URL,
            {"api_numbers": api_numbers},
            format="json",
        )

        assert resp.status_code == 400
        mock_task.delay.assert_not_called()
        assert ResearchSession.objects.count() == 0

    # ------------------------------------------------------------------
    # 6. Partial failure still returns 201
    # ------------------------------------------------------------------

    @patch(MOCK_PATH)
    def test_partial_failure_returns_201(self, mock_task, api_client):
        mock_task.delay.return_value = MagicMock(id="test-celery-id")

        resp = api_client.post(
            BULK_URL,
            {
                "api_numbers": [
                    "42-123-45678-0000",   # valid TX
                    "30-015-28692-0000",   # valid NM
                    "99-000-00000-0000",   # unknown prefix, no override → error
                ]
            },
            format="json",
        )

        assert resp.status_code == 201
        assert resp.data["submitted"] == 3

        sessions = resp.data["sessions"]
        assert len(sessions) == 3

        successful = [s for s in sessions if s["status"] == "pending"]
        failed = [s for s in sessions if s["status"] == "error"]

        assert len(successful) == 2, f"Expected 2 successes, got {len(successful)}"
        assert len(failed) == 1, f"Expected 1 failure, got {len(failed)}"

        for s in successful:
            assert s["session_id"] is not None
            assert s["error"] is None

        assert failed[0]["session_id"] is None
        assert failed[0]["error"] is not None

        # Two sessions in DB, task dispatched twice
        assert ResearchSession.objects.count() == 2
        assert mock_task.delay.call_count == 2
