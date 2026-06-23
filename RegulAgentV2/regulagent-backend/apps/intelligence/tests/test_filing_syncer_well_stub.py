"""
TDD RED PHASE — FilingSyncer well-stub normalization bug fix.

Three bugs being fixed:
(a) RRC emits 8-digit APIs; the stub is created with 8 digits instead of the
    proper 14-digit normalized form ("42" + digits + "0000").
(b) The stub is created empty, ignoring the operator/lease/county/etc. that
    the filing already carries in filing_data["raw_data"].
(c) ResearchSession.api_number is seeded with the un-normalized 8-digit value,
    so document retrieval doesn't match manual Research lookups.

Contract that the fix must satisfy:
  1. A new module-level helper ``_stub_fields_from_filing(filing_data)`` maps
     raw_data fields → WellRegistry kwargs and enforces model max_length.
  2. ``normalize_api_14digit`` is used for the stub's api14.
  3. ``_resolve_well`` accepts a ``filing_data`` kwarg and uses (1) + (2).
  4. ResearchSession.api_number is the normalized 14-digit value.

All Python runs in Docker:
  docker compose -f compose.dev.yml exec -T web python -m pytest \
      apps/intelligence/tests/test_filing_syncer_well_stub.py -v
"""

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest

# Prefix shared by every well_api used in the integration tests below
# ("015123xx"/"015124xx" → normalized "42015123xx0000"/"42015124xx0000").
_TEST_API_PREFIX = "4201512"


@pytest.fixture(autouse=True)
def _clean_test_well_rows(django_db_blocker):
    """
    Delete any leaked test WellRegistry/ResearchSession rows for this module's
    API range before and after each test.

    ``FilingSyncer._resolve_well`` writes via ``run_in_executor``, so those
    commits escape the per-test transaction rollback. Combined with
    ``--reuse-db`` this would otherwise leak rows across tests/sessions and
    make creation tests find a pre-existing match. Scoped to the test-only
    ``42015123…`` range, so it never touches real or other-fixture data.
    """
    def _purge():
        from apps.public_core.models import WellRegistry, ResearchSession
        ResearchSession.objects.filter(api_number__startswith=_TEST_API_PREFIX).delete()
        WellRegistry.objects.filter(api14__startswith=_TEST_API_PREFIX).delete()

    with django_db_blocker.unblock():
        _purge()
    yield
    with django_db_blocker.unblock():
        _purge()


# ---------------------------------------------------------------------------
# Group A — pure helper _stub_fields_from_filing (no DB needed)
# ---------------------------------------------------------------------------


class TestStubFieldsFromFiling:
    """
    Tests for the (not-yet-existing) module-level helper
    ``_stub_fields_from_filing(filing_data) -> dict``.

    Expected import path:
        from apps.intelligence.services.filing_syncer import _stub_fields_from_filing
    """

    def _import(self):
        # Will raise ImportError / AttributeError until the helper is implemented.
        from apps.intelligence.services.filing_syncer import _stub_fields_from_filing  # noqa: F401
        return _stub_fields_from_filing

    def _full_filing(self, **overrides):
        raw = {
            "operator": "ACME OIL CO",
            "operator_number": "12345",
            "lease_name": "SMITH",
            "lease_or_gas_id": "67890",
            "well_number": "1H",
            "county": "MIDLAND",
            "district": "08",
        }
        raw.update(overrides)
        return {"well_api": "01512345", "raw_data": raw}

    # A-1 — field mapping
    def test_maps_operator_to_operator_name(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert result["operator_name"] == "ACME OIL CO"

    def test_maps_lease_name(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert result["lease_name"] == "SMITH"

    def test_maps_well_number(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert result["well_number"] == "1H"

    def test_maps_county(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert result["county"] == "MIDLAND"

    def test_maps_district(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert result["district"] == "08"

    def test_maps_lease_or_gas_id_to_lease_id(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert result["lease_id"] == "67890"

    # A-2 — omit empty/missing keys
    def test_omits_operator_name_when_empty(self):
        fn = self._import()
        result = fn(self._full_filing(operator=""))
        assert "operator_name" not in result

    def test_omits_operator_name_when_absent(self):
        fn = self._import()
        filing = {"well_api": "01512345", "raw_data": {"lease_name": "X"}}
        result = fn(filing)
        assert "operator_name" not in result

    def test_omits_lease_name_when_empty(self):
        fn = self._import()
        result = fn(self._full_filing(lease_name=""))
        assert "lease_name" not in result

    def test_omits_county_when_missing(self):
        fn = self._import()
        filing = {"well_api": "01512345", "raw_data": {"operator": "X"}}
        result = fn(filing)
        assert "county" not in result

    def test_omits_district_when_empty(self):
        fn = self._import()
        result = fn(self._full_filing(district=""))
        assert "district" not in result

    def test_omits_lease_id_when_missing(self):
        fn = self._import()
        filing = {"well_api": "01512345", "raw_data": {"operator": "X"}}
        result = fn(filing)
        assert "lease_id" not in result

    # A-3 — max_length truncation
    def test_truncates_operator_name_to_128(self):
        fn = self._import()
        long_op = "A" * 200
        result = fn(self._full_filing(operator=long_op))
        assert len(result["operator_name"]) <= 128

    def test_truncates_well_number_to_32(self):
        fn = self._import()
        result = fn(self._full_filing(well_number="W" * 40))
        assert len(result["well_number"]) <= 32

    def test_truncates_district_to_8(self):
        fn = self._import()
        result = fn(self._full_filing(district="D" * 20))
        assert len(result["district"]) <= 8

    def test_truncates_county_to_64(self):
        fn = self._import()
        result = fn(self._full_filing(county="C" * 100))
        assert len(result["county"]) <= 64

    def test_truncates_lease_name_to_128(self):
        fn = self._import()
        result = fn(self._full_filing(lease_name="L" * 200))
        assert len(result["lease_name"]) <= 128

    def test_truncates_lease_id_to_32(self):
        fn = self._import()
        result = fn(self._full_filing(lease_or_gas_id="I" * 40))
        assert len(result["lease_id"]) <= 32

    # A-4 — no raw_data key / empty raw_data → empty dict, no crash
    def test_no_raw_data_key_returns_empty_dict(self):
        fn = self._import()
        result = fn({"well_api": "01512345"})
        assert result == {}

    def test_empty_raw_data_returns_empty_dict(self):
        fn = self._import()
        result = fn({"well_api": "01512345", "raw_data": {}})
        assert result == {}

    # A-5 — does NOT include api14 or state
    def test_does_not_include_api14(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert "api14" not in result

    def test_does_not_include_state(self):
        fn = self._import()
        result = fn(self._full_filing())
        assert "state" not in result


# ---------------------------------------------------------------------------
# Group B — _resolve_well integration (async, real DB)
# ---------------------------------------------------------------------------

TASK_PATCH_TARGET = "apps.public_core.tasks_research.start_research_session_task"


def _make_filing_data(well_api: str = "01512345") -> dict:
    """Minimal filing_data dict matching the RRC scraper shape."""
    return {
        "well_api": well_api,
        "raw_data": {
            "operator": "ACME OIL CO",
            "operator_number": "99",
            "lease_name": "SMITH",
            "lease_or_gas_id": "12345",
            "well_number": "1H",
            "county": "MIDLAND",
            "district": "08",
        },
    }


class TestResolveWellIntegration:
    """
    Integration tests for FilingSyncer._resolve_well with the new
    ``filing_data`` parameter.

    All tests run against the real ORM (django_db transaction=True so
    async/run_in_executor can see committed rows within the same process).
    The Celery task dispatch is always mocked to avoid worker dependency.
    """

    # B-1 — new well: normalized api14, metadata populated, ResearchSession seeded correctly
    @pytest.mark.django_db(transaction=True)
    def test_new_well_returns_was_created_true(self):
        """_resolve_well returns (well, True) for a new 8-digit API."""
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512345")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            _well, was_created = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512345",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert was_created is True

    @pytest.mark.django_db(transaction=True)
    def test_new_well_api14_is_normalized_14digits(self):
        """
        Bug (a): 8-digit RRC API must be stored as 14-digit normalized form.
        "01512345" → "42015123450000"  (prepend "42", append "0000")
        """
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512345")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512345",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        # THIS is the core bug assertion — currently the stub stores "01512345"
        assert well.api14 == "42015123450000", (
            f"Expected normalized API-14 '42015123450000', got '{well.api14}'"
        )

    @pytest.mark.django_db(transaction=True)
    def test_new_well_stub_has_operator_name(self):
        """Bug (b): stub must be populated with operator from filing_data."""
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512346")  # unique api per test

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512346",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well.operator_name == "ACME OIL CO"

    @pytest.mark.django_db(transaction=True)
    def test_new_well_stub_has_lease_name(self):
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512347")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512347",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well.lease_name == "SMITH"

    @pytest.mark.django_db(transaction=True)
    def test_new_well_stub_has_well_number(self):
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512348")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512348",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well.well_number == "1H"

    @pytest.mark.django_db(transaction=True)
    def test_new_well_stub_has_county(self):
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512349")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512349",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well.county == "MIDLAND"

    @pytest.mark.django_db(transaction=True)
    def test_new_well_stub_has_district(self):
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512350")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512350",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well.district == "08"

    @pytest.mark.django_db(transaction=True)
    def test_new_well_stub_has_lease_id(self):
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512351")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512351",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well.lease_id == "12345"

    @pytest.mark.django_db(transaction=True)
    def test_research_session_api_number_is_normalized(self):
        """
        Bug (c): ResearchSession.api_number must be the 14-digit normalized
        value so document retrieval matches what manual Research feeds the adapter.
        """
        from apps.intelligence.services.filing_syncer import FilingSyncer
        from apps.public_core.models import ResearchSession

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512352")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512352",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )
            # Verify delay was called once
            mock_task.delay.assert_called_once()

        session = ResearchSession.objects.filter(well=well).first()
        assert session is not None, "ResearchSession should have been created"
        assert session.api_number == "42015123520000", (
            f"Expected normalized api_number '42015123520000', got '{session.api_number}'"
        )

    # B-2 — existing well: no overwrite, no new ResearchSession
    @pytest.mark.django_db(transaction=True)
    def test_existing_well_returns_was_created_false(self):
        """Pre-existing well → (well, False); matched via normalized api14."""
        from apps.intelligence.services.filing_syncer import FilingSyncer
        from apps.public_core.models import WellRegistry

        # Pre-create the well with the normalized api14
        existing = WellRegistry.objects.create(
            api14="42015123600000",
            state="TX",
            operator_name="PRE-EXISTING",
        )

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512360")  # normalizes to "42015123600000"

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, was_created = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512360",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert was_created is False
        assert well.pk == existing.pk

    @pytest.mark.django_db(transaction=True)
    def test_existing_well_operator_not_overwritten(self):
        """Metadata from filing_data must NOT overwrite an existing well's fields."""
        from apps.intelligence.services.filing_syncer import FilingSyncer
        from apps.public_core.models import WellRegistry

        existing = WellRegistry.objects.create(
            api14="42015123610000",
            state="TX",
            operator_name="PRE-EXISTING",
        )

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512361")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, _ = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512361",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )
            # delay must NOT be called for an already-existing well
            mock_task.delay.assert_not_called()

        existing.refresh_from_db()
        assert existing.operator_name == "PRE-EXISTING"

    @pytest.mark.django_db(transaction=True)
    def test_existing_well_no_research_session_dispatched(self):
        """No ResearchSession should be dispatched when well already exists."""
        from apps.intelligence.services.filing_syncer import FilingSyncer
        from apps.public_core.models import WellRegistry, ResearchSession

        existing = WellRegistry.objects.create(
            api14="42015123620000",
            state="TX",
            operator_name="PRE-EXISTING",
        )
        prior_session_count = ResearchSession.objects.filter(well=existing).count()

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("01512362")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="01512362",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )
            mock_task.delay.assert_not_called()

        after_count = ResearchSession.objects.filter(well=existing).count()
        assert after_count == prior_session_count

    # B-3 — blank / invalid API → (None, False), no row created
    @pytest.mark.django_db(transaction=True)
    def test_blank_well_api_returns_none(self):
        from apps.intelligence.services.filing_syncer import FilingSyncer

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("")

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, was_created = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well is None
        assert was_created is False

    @pytest.mark.django_db(transaction=True)
    def test_invalid_alpha_api_returns_none_no_row_created(self):
        """
        "abc" strips to "" (all non-digits removed), normalize_api_14digit → None.
        _resolve_well must return (None, False) and create no WellRegistry row.
        """
        from apps.intelligence.services.filing_syncer import FilingSyncer
        from apps.public_core.models import WellRegistry

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("abc")

        before_count = WellRegistry.objects.count()

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, was_created = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="abc",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well is None
        assert was_created is False
        assert WellRegistry.objects.count() == before_count, (
            "No WellRegistry row should be created for an invalid API"
        )

    @pytest.mark.django_db(transaction=True)
    def test_too_short_api_returns_none_no_row_created(self):
        """
        A 3-digit value is below the 8-digit minimum; normalize_api_14digit → None.
        """
        from apps.intelligence.services.filing_syncer import FilingSyncer
        from apps.public_core.models import WellRegistry

        syncer = FilingSyncer()
        tenant_id = str(uuid.uuid4())
        filing_data = _make_filing_data("123")

        before_count = WellRegistry.objects.count()

        with patch(TASK_PATCH_TARGET) as mock_task:
            mock_task.delay = MagicMock()
            well, was_created = asyncio.get_event_loop().run_until_complete(
                syncer._resolve_well(
                    well_api="123",
                    tenant_id=tenant_id,
                    agency="RRC",
                    filing_data=filing_data,
                )
            )

        assert well is None
        assert was_created is False
        assert WellRegistry.objects.count() == before_count
