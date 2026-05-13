"""
TDD tests for well_ingestor service — written BEFORE implementation.

These tests define the expected behaviour of:
    apps.public_core.services.well_ingestor.ingest_tx_wells
    apps.public_core.services.well_ingestor.ingest_nm_wells

All tests must FAIL until the module is implemented.

Test matrix
-----------
TX ingestion
  1. Creates a new WellRegistry row and fires start_research_session_task.delay
  2. Skips an existing well whose operator_name is already populated
  3. Updates lat/lon on an existing well when those fields are blank
  4. Dry-run mode: counts what WOULD be created but writes nothing to the DB
  5. limit= parameter caps the number of wells processed

NM ingestion
  6. Creates a new WellRegistry row from NM Water Data API response
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# WellRow dataclass — mirrors the shape the parser will produce.
# Defined here so tests don't depend on the (not yet existing) parser module.
# ---------------------------------------------------------------------------


@dataclass
class WellRow:
    api_root: str        # 8 chars: county(3) + unique(5)
    district: str
    county_code: str
    well_type: str       # "OIL" | "GAS" | ""
    lease_name: str
    latitude: float | None
    longitude: float | None
    is_active: bool      # True if well is actively producing
    is_iwar: bool        # True if well appears on IWAR

    @property
    def api14(self) -> str:
        return f"42{self.api_root}0000"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

TX_API_ROOT = "50170575"          # county=501, unique=70575
TX_API14 = f"42{TX_API_ROOT}0000"   # "42501705750000"

NM_API14 = "30015288410000"


def _make_tx_wellrow(**overrides) -> WellRow:
    defaults = dict(
        api_root=TX_API_ROOT,
        district="01",
        county_code="501",
        well_type="OIL",
        lease_name="TEST LEASE",
        latitude=29.123456,
        longitude=-95.654321,
        is_active=True,
        is_iwar=False,
    )
    defaults.update(overrides)
    return WellRow(**defaults)


def _nm_well_record(**overrides) -> dict:
    defaults = dict(
        api14=NM_API14,
        operator="TEST OP NM",
        county="Eddy",
        latitude=32.5,
        longitude=-104.2,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Patch targets (modules that well_ingestor will import)
# ---------------------------------------------------------------------------

DOWNLOADER_DOWNLOAD = "apps.public_core.services.rrc_bulk_downloader.download_to_tempfile"
DOWNLOADER_CLEANUP = "apps.public_core.services.rrc_bulk_downloader.cleanup_tempfile"
DOWNLOADER_LIST = "apps.public_core.services.rrc_bulk_downloader.list_share"
PARSER = "apps.public_core.services.rrc_wellbore_parser.parse_wellbore_file"
NM_CLIENT_CLS = "apps.public_core.services.nm_wda_client.NMWaterDataClient"
RESEARCH_TASK = "apps.public_core.tasks_research.start_research_session_task"


def _patch_tx_dependencies(parser_rows: list[WellRow]):
    """Return a list of patch objects covering all TX downloader + parser deps."""
    return [
        patch(DOWNLOADER_LIST, return_value=[{"name": "fake_wellbore.bin", "url": "https://fake/wellbore.bin"}]),
        patch(DOWNLOADER_DOWNLOAD, return_value=("/tmp/fake_wellbore.bin", "https://fake/wellbore.bin")),
        patch(DOWNLOADER_CLEANUP, return_value=None),
        patch(PARSER, return_value=iter(parser_rows)),
    ]


# ---------------------------------------------------------------------------
# 1. TX happy path: new well is created + research task is fired
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_tx_creates_well_registry_row():
    """
    When the parser yields one active WellRow that does not exist in the DB,
    ingest_tx_wells should:
      - create a WellRegistry row with the correct api14
      - return result["created"] == 1, result["updated"] == 0
      - call start_research_session_task.delay exactly once
    """
    from apps.public_core.models import WellRegistry
    from apps.public_core.services.well_ingestor import ingest_tx_wells

    rows = [_make_tx_wellrow()]
    task_mock = MagicMock()

    patches = _patch_tx_dependencies(rows)
    patches.append(patch(RESEARCH_TASK, task_mock))

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = ingest_tx_wells(source="active", dry_run=False)

    assert WellRegistry.objects.filter(api14=TX_API14).exists(), (
        f"Expected WellRegistry row with api14={TX_API14!r} but none was found."
    )
    assert result["created"] == 1, f"Expected created=1, got {result['created']}"
    assert result["updated"] == 0, f"Expected updated=0, got {result['updated']}"
    task_mock.delay.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Existing well with operator already set → skipped, no research task
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_tx_skips_existing_well():
    """
    When a WellRegistry row already exists AND operator_name is populated,
    ingest_tx_wells should:
      - not create a duplicate row
      - return result["created"] == 0, result["updated"] == 0
      - NOT call start_research_session_task.delay
    """
    from apps.public_core.models import WellRegistry
    from apps.public_core.services.well_ingestor import ingest_tx_wells

    WellRegistry.objects.create(
        api14=TX_API14,
        state="TX",
        operator_name="EXISTING OP",
    )

    rows = [_make_tx_wellrow()]
    task_mock = MagicMock()

    patches = _patch_tx_dependencies(rows)
    patches.append(patch(RESEARCH_TASK, task_mock))

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = ingest_tx_wells(source="active", dry_run=False)

    assert WellRegistry.objects.filter(api14=TX_API14).count() == 1, (
        "Duplicate WellRegistry row was created — expected exactly one row."
    )
    assert result["created"] == 0, f"Expected created=0, got {result['created']}"
    assert result["updated"] == 0, f"Expected updated=0, got {result['updated']}"
    task_mock.delay.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Existing well with blank lat/lon → fields updated, no research task
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_tx_updates_blank_fields_on_existing():
    """
    When a WellRegistry row exists but lat/lon (and operator_name) are blank,
    ingest_tx_wells should:
      - update those fields with the values from the parsed WellRow
      - return result["updated"] == 1
      - NOT fire start_research_session_task (well already exists)
    """
    from apps.public_core.models import WellRegistry
    from apps.public_core.services.well_ingestor import ingest_tx_wells

    WellRegistry.objects.create(
        api14=TX_API14,
        state="TX",
        # operator_name, lat, lon intentionally left blank
    )

    rows = [_make_tx_wellrow(latitude=29.123456, longitude=-95.654321)]
    task_mock = MagicMock()

    patches = _patch_tx_dependencies(rows)
    patches.append(patch(RESEARCH_TASK, task_mock))

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = ingest_tx_wells(dry_run=False)

    well = WellRegistry.objects.get(api14=TX_API14)
    assert well.lat is not None, "lat should have been updated from WellRow but is still None"
    assert result["updated"] == 1, f"Expected updated=1, got {result['updated']}"
    task_mock.delay.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Dry-run: counts are populated but no DB writes occur
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_tx_dry_run_no_db_writes():
    """
    When dry_run=True, ingest_tx_wells should:
      - NOT write any WellRegistry rows
      - NOT call start_research_session_task.delay
      - still return result["created"] == 1 (reflects what WOULD have happened)
    """
    from apps.public_core.models import WellRegistry
    from apps.public_core.services.well_ingestor import ingest_tx_wells

    rows = [_make_tx_wellrow()]
    task_mock = MagicMock()

    patches = _patch_tx_dependencies(rows)
    patches.append(patch(RESEARCH_TASK, task_mock))

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = ingest_tx_wells(dry_run=True)

    assert WellRegistry.objects.filter(api14=TX_API14).count() == 0, (
        "dry_run=True must not write to the DB, but a WellRegistry row was found."
    )
    task_mock.delay.assert_not_called()
    assert result["created"] == 1, (
        f"dry_run should still count what WOULD be created. Expected 1, got {result['created']}"
    )


# ---------------------------------------------------------------------------
# 5. limit= parameter caps rows processed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_tx_limit_respected():
    """
    When limit=3, ingest_tx_wells should process at most 3 WellRows even if
    the parser yields more. The sum of created + skipped + updated + errors
    must be <= 3.
    """
    from apps.public_core.services.well_ingestor import ingest_tx_wells

    # Generate 10 distinct wells
    rows = [
        _make_tx_wellrow(
            api_root=f"5017{str(i).zfill(4)}",
            county_code="501",
        )
        for i in range(10)
    ]
    task_mock = MagicMock()

    # parser needs to be re-patchable each call so use a fresh iter each time
    with (
        patch(DOWNLOADER_LIST, return_value=[{"name": "f.bin", "url": "https://x/f.bin"}]),
        patch(DOWNLOADER_DOWNLOAD, return_value=("/tmp/f.bin", "https://x/f.bin")),
        patch(DOWNLOADER_CLEANUP, return_value=None),
        patch(PARSER, return_value=iter(rows)),
        patch(RESEARCH_TASK, task_mock),
    ):
        result = ingest_tx_wells(limit=3, dry_run=True)

    total_processed = result["created"] + result["updated"] + result["skipped"] + result["errors"]
    assert total_processed <= 3, (
        f"limit=3 was set but {total_processed} rows were processed."
    )


# ---------------------------------------------------------------------------
# 6. NM happy path: new well created from Water Data API record
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_nm_creates_well_registry_row():
    """
    When the NM Water Data client yields one well record that does not yet
    exist in the DB, ingest_nm_wells should:
      - create a WellRegistry row with state="NM" and the correct api14
      - return result["created"] == 1
      - call start_research_session_task.delay exactly once
    """
    from apps.public_core.models import WellRegistry
    from apps.public_core.services.well_ingestor import ingest_nm_wells

    nm_record = _nm_well_record()
    task_mock = MagicMock()

    nm_client_instance = MagicMock()
    nm_client_instance.list_wells.return_value = iter([nm_record])

    with (
        patch(NM_CLIENT_CLS, return_value=nm_client_instance),
        patch(RESEARCH_TASK, task_mock),
    ):
        result = ingest_nm_wells(dry_run=False)

    assert WellRegistry.objects.filter(api14=NM_API14, state="NM").exists(), (
        f"Expected WellRegistry row api14={NM_API14!r} state='NM' but none was found."
    )
    assert result["created"] == 1, f"Expected created=1, got {result['created']}"
    task_mock.delay.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Result dict always contains the required keys
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ingest_tx_result_shape():
    """
    ingest_tx_wells must always return a dict containing exactly these keys:
    created, updated, skipped, errors, elapsed_s.
    """
    from apps.public_core.services.well_ingestor import ingest_tx_wells

    task_mock = MagicMock()
    rows: list[WellRow] = []  # empty parser output — nothing to ingest

    with (
        patch(DOWNLOADER_LIST, return_value=[]),
        patch(DOWNLOADER_DOWNLOAD, return_value=("/tmp/f.bin", "https://x/f.bin")),
        patch(DOWNLOADER_CLEANUP, return_value=None),
        patch(PARSER, return_value=iter(rows)),
        patch(RESEARCH_TASK, task_mock),
    ):
        result = ingest_tx_wells(dry_run=True)

    required_keys = {"created", "updated", "skipped", "errors", "elapsed_s"}
    missing = required_keys - set(result.keys())
    assert not missing, f"Result dict is missing keys: {missing}"


@pytest.mark.django_db
def test_ingest_nm_result_shape():
    """
    ingest_nm_wells must always return a dict containing exactly these keys:
    created, updated, skipped, errors, elapsed_s.
    """
    from apps.public_core.services.well_ingestor import ingest_nm_wells

    task_mock = MagicMock()
    nm_client_instance = MagicMock()
    nm_client_instance.list_wells.return_value = iter([])

    with (
        patch(NM_CLIENT_CLS, return_value=nm_client_instance),
        patch(RESEARCH_TASK, task_mock),
    ):
        result = ingest_nm_wells(dry_run=True)

    required_keys = {"created", "updated", "skipped", "errors", "elapsed_s"}
    missing = required_keys - set(result.keys())
    assert not missing, f"Result dict is missing keys: {missing}"
