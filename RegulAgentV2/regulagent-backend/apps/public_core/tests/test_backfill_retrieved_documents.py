"""
Unit tests for backfill_retrieved_documents management command.

Covers:
  1. ED → RetrievedDocument field mapping (source_path, neubus_filename, bare ED).
  2. Directional-survey detection from disk filenames.
  3. Disk-orphan detection (file exists on disk, no matching ED source_path).
  4. Idempotent upsert: re-running the command creates 0 new rows.
  5. --dry-run mode: no DB writes, but counts are reported correctly.

Filesystem is mocked via tmp_path / monkeypatching _media_completions_root.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.public_core.models import ExtractedDocument, RetrievedDocument, WellRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_API14 = "42901555550000"   # distinct — not used by other test modules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def well(db):
    return WellRegistry.objects.create(
        api14=TEST_API14,
        state="TX",
        county="Reeves",
        district="8",
        operator_name="Backfill Test Co",
        field_name="Test Field",
        lease_name="Test Lease",
        well_number="1",
    )


@pytest.fixture
def ed_success(db, well):
    """ExtractedDocument with a real source_path (RRC-sourced, success)."""
    return ExtractedDocument.objects.create(
        api_number=TEST_API14,
        well=well,
        document_type="w2",
        source_path="/media/rrc/completions/42901555550000/W-2_42901555550000_001.pdf",
        status="success",
        source_type="rrc",
        json_data={"test": True},
    )


@pytest.fixture
def ed_neubus(db, well):
    """ExtractedDocument with a neubus_filename (Neubus-sourced, partial)."""
    return ExtractedDocument.objects.create(
        api_number=TEST_API14,
        well=well,
        document_type="c_105",
        neubus_filename="original_c105.pdf",
        source_path="",
        status="partial",
        source_type="neubus",
        json_data={"test": True},
    )


@pytest.fixture
def ed_bare(db, well):
    """ExtractedDocument with neither source_path nor neubus_filename."""
    return ExtractedDocument.objects.create(
        api_number=TEST_API14,
        well=well,
        document_type="gau",
        source_path="",
        neubus_filename="",
        status="error",
        source_type="rrc",
        json_data={"test": True},
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_command(*args, dry_run=False, well=None):
    """Run the management command and return stdout as a string."""
    stdout = StringIO()
    cmd_args = []
    if dry_run:
        cmd_args.append("--dry-run")
    if well:
        cmd_args.extend(["--well", well])
    call_command("backfill_retrieved_documents", *cmd_args, stdout=stdout)
    return stdout.getvalue()


# ---------------------------------------------------------------------------
# Group 1: ED → RetrievedDocument field mapping
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestEDMapping:
    def test_source_path_ed(self, ed_success):
        """RRC ED with source_path: filename and local_path must be set."""
        _run_command(well=TEST_API14)

        rd = RetrievedDocument.objects.get(api_number=TEST_API14, extracted_document=ed_success)
        assert rd.filename == "W-2_42901555550000_001.pdf"
        assert rd.local_path == ed_success.source_path
        assert rd.kind == "w2"
        assert rd.index_status == "success"
        assert rd.extracted_document == ed_success
        assert rd.source_type == "rrc"

    def test_neubus_ed(self, ed_neubus):
        """Neubus ED with neubus_filename: filename set, local_path empty, kind = c_105."""
        _run_command(well=TEST_API14)

        rd = RetrievedDocument.objects.get(api_number=TEST_API14, extracted_document=ed_neubus)
        assert rd.filename == "original_c105.pdf"
        assert rd.local_path == ""
        assert rd.kind == "c_105"
        assert rd.index_status == "partial"
        assert rd.source_type == "neubus"

    def test_bare_ed(self, ed_bare):
        """ED with no path or filename: synthetic filename generated, status=error."""
        _run_command(well=TEST_API14)

        rd = RetrievedDocument.objects.get(api_number=TEST_API14, extracted_document=ed_bare)
        assert rd.filename.startswith("gau_")
        assert rd.kind == "gau"
        assert rd.index_status == "error"

    def test_multiple_eds_all_create_rows(self, ed_success, ed_neubus, ed_bare):
        """Three distinct EDs for the same well → three distinct RD rows."""
        _run_command(well=TEST_API14)
        count = RetrievedDocument.objects.filter(api_number=TEST_API14).count()
        assert count == 3


# ---------------------------------------------------------------------------
# Group 2: Directional-survey detection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDirectionalDetection:
    def test_directional_filename_flagged(self, tmp_path, well):
        """A file named *directional*survey*.pdf must get index_status='skipped_directional'."""
        api_dir = tmp_path / TEST_API14
        api_dir.mkdir()
        # Create a minimal valid-looking PDF so _sha256_of_file works
        pdf_file = api_dir / "Directional_Survey_42901555550000_001.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        with patch(
            "apps.public_core.management.commands.backfill_retrieved_documents._media_completions_root",
            return_value=tmp_path,
        ):
            _run_command(well=TEST_API14)

        rd = RetrievedDocument.objects.get(api_number=TEST_API14, filename=pdf_file.name)
        assert rd.index_status == "skipped_directional"
        assert rd.kind == "directional"

    def test_non_directional_filename_pending(self, tmp_path, well):
        """A regular PDF must get index_status='pending'."""
        api_dir = tmp_path / TEST_API14
        api_dir.mkdir()
        pdf_file = api_dir / "W-2_42901555550000_001.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        with patch(
            "apps.public_core.management.commands.backfill_retrieved_documents._media_completions_root",
            return_value=tmp_path,
        ):
            _run_command(well=TEST_API14)

        rd = RetrievedDocument.objects.get(api_number=TEST_API14, filename=pdf_file.name)
        assert rd.index_status == "pending"


# ---------------------------------------------------------------------------
# Group 3: Disk-orphan detection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDiskOrphanDetection:
    def test_orphan_disk_file_creates_rd(self, tmp_path, well):
        """A PDF on disk with no matching ED source_path must produce a pending RD row."""
        api_dir = tmp_path / TEST_API14
        api_dir.mkdir()
        orphan = api_dir / "GAU_42901555550000_001.pdf"
        orphan.write_bytes(b"%PDF-1.4 orphan content")

        with patch(
            "apps.public_core.management.commands.backfill_retrieved_documents._media_completions_root",
            return_value=tmp_path,
        ):
            _run_command(well=TEST_API14)

        rds = RetrievedDocument.objects.filter(api_number=TEST_API14)
        assert rds.count() == 1
        rd = rds.first()
        assert rd.filename == "GAU_42901555550000_001.pdf"
        assert rd.index_status == "pending"
        assert rd.extracted_document is None
        assert rd.local_path == str(orphan)

    def test_ed_covered_file_not_duplicated(self, tmp_path, well, ed_success):
        """If the disk file path matches an ED's source_path, we get ONE RD (from the ED path),
        not two (once from ED, once as orphan)."""
        # Build the mock completions dir entirely under tmp_path
        completions_root = tmp_path / "completions"
        api_dir = completions_root / TEST_API14
        api_dir.mkdir(parents=True)
        real_file = api_dir / "W-2_42901555550000_001.pdf"
        real_file.write_bytes(b"%PDF-1.4 real content")

        # Patch the ED's source_path to match the tmp_path file
        ed_success.source_path = str(real_file)
        ed_success.save(update_fields=["source_path"])

        with patch(
            "apps.public_core.management.commands.backfill_retrieved_documents._media_completions_root",
            return_value=completions_root,
        ):
            _run_command(well=TEST_API14)

        # Only one RD row — the ED row (not a separate orphan row)
        rds = RetrievedDocument.objects.filter(api_number=TEST_API14)
        assert rds.count() == 1
        rd = rds.first()
        assert rd.extracted_document == ed_success


# ---------------------------------------------------------------------------
# Group 4: Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestIdempotency:
    def test_rerun_creates_zero_rows(self, ed_success, ed_neubus):
        """Running the command twice for the same well yields no extra rows."""
        _run_command(well=TEST_API14)
        count_after_first = RetrievedDocument.objects.filter(api_number=TEST_API14).count()

        _run_command(well=TEST_API14)
        count_after_second = RetrievedDocument.objects.filter(api_number=TEST_API14).count()

        assert count_after_first == count_after_second == 2


# ---------------------------------------------------------------------------
# Group 5: Dry-run
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDryRun:
    def test_dry_run_no_writes(self, ed_success):
        """--dry-run must not write any rows to the database."""
        output = _run_command(well=TEST_API14, dry_run=True)

        assert "DRY RUN" in output
        assert not RetrievedDocument.objects.filter(api_number=TEST_API14).exists()

    def test_dry_run_reports_would_create(self, ed_success):
        """--dry-run output must mention the well and non-zero creation count."""
        output = _run_command(well=TEST_API14, dry_run=True)
        # Should show "+1" for the one ED
        assert "+1" in output or "created=1" in output
