"""
TDD (red phase) — GAU PDF retrieval via Django storage / MEDIA_ROOT filesystem.

These tests define EXPECTED behaviour for:
    apps.intelligence.services.customer_documents.get_gau_letter(tenant_id, api14)

Contract:
  1. Returns bytes when storage has a matching GAU file.
  2. Returns None (not raise) when no matching file exists.
  3. The path/glob pattern used matches the convention in rrc_completions_extractor.py:
       MEDIA_ROOT / "rrc" / "completions" / <api_digits> / GAU_<api_digits>_*.pdf
  4. Returns None (not raise) when the file exists but open() fails.

IMPORTANT — Storage convention note:
  rrc_completions_extractor.py does NOT use Django default_storage.  It writes
  directly to the local filesystem at:
      settings.MEDIA_ROOT / "rrc" / "completions" / <api_digits> / <filename>
  where <api_digits> is the digits-only form of the API (8, 10, or 14 digits).

  Therefore get_gau_letter MUST use glob on the local filesystem, NOT
  default_storage.open/exists.  These tests mock pathlib.Path.glob and
  builtins.open so they work without a real MEDIA_ROOT.

Paste of the exact path construction from rrc_completions_extractor.py:

    def _media_base() -> Path:
        base = getattr(settings, "MEDIA_ROOT", None)
        return Path(base or ".").resolve() / "rrc" / "completions"

    out_dir = _media_base() / api   # api = digits-only, e.g. "42317361340000"
    filename = f"{safe_type}_{api}_{existing_count + 1:03d}.pdf"
    # safe_type for GAU records == "GAU"  (no special chars)
    # → final name: GAU_42317361340000_001.pdf  (counter may vary)
"""
from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-customer-docs-tests")

# ---------------------------------------------------------------------------
# Module-under-test import helpers
# ---------------------------------------------------------------------------

def _import_get_gau_letter():
    from apps.intelligence.services.customer_documents import get_gau_letter
    return get_gau_letter


# ===========================================================================
# Test 1 — returns bytes when a GAU file exists
# ===========================================================================

class TestGetGauLetterReturnsBytesWhenFileExists:
    """get_gau_letter returns raw PDF bytes when a matching GAU file is found."""

    def test_get_gau_letter_returns_bytes_when_storage_has_file(self, settings):
        """Patch filesystem glob to find one GAU file; confirm bytes are returned."""
        settings.MEDIA_ROOT = "/fake/media"
        get_gau_letter = _import_get_gau_letter()

        fake_pdf = b"%PDF-fake-bytes"
        fake_path = MagicMock(spec=Path)
        fake_path.__str__ = MagicMock(return_value="/fake/media/rrc/completions/42317361340000/GAU_42317361340000_001.pdf")

        # Patch the glob call so it returns our fake path
        with patch(
            "apps.intelligence.services.customer_documents.Path"
        ) as mock_path_cls:
            # Build the chain: Path(MEDIA_ROOT) / "rrc" / "completions" / api  (3 divisions)
            mock_out_dir = MagicMock()
            mock_out_dir.glob.return_value = [fake_path]
            mock_path_cls.return_value.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_out_dir

            with patch("builtins.open", mock_open(read_data=fake_pdf)):
                result = get_gau_letter("00000000-0000-0000-0000-000000000002", "42-317-36134")

        assert result == fake_pdf, (
            f"Expected b'%PDF-fake-bytes', got {result!r}"
        )


# ===========================================================================
# Test 2 — returns None when no matching file exists
# ===========================================================================

class TestGetGauLetterReturnsNoneWhenNoFile:
    """get_gau_letter returns None (not raise) when glob finds no GAU files."""

    def test_get_gau_letter_returns_none_when_storage_empty(self, settings):
        """Glob returns empty list → return None, do not raise."""
        settings.MEDIA_ROOT = "/fake/media"
        get_gau_letter = _import_get_gau_letter()

        with patch(
            "apps.intelligence.services.customer_documents.Path"
        ) as mock_path_cls:
            mock_out_dir = MagicMock()
            mock_out_dir.glob.return_value = []
            mock_path_cls.return_value.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_out_dir

            result = get_gau_letter("00000000-0000-0000-0000-000000000002", "42-317-36134")

        assert result is None, (
            f"Expected None when no GAU file found, got {result!r}"
        )


# ===========================================================================
# Test 3 — storage path matches extractor convention
# ===========================================================================

class TestGetGauLetterStoragePathMatchesExtractorConvention:
    """The directory the service looks in must match what the extractor wrote to.

    Extractor path construction (verbatim from rrc_completions_extractor.py):

        def _media_base() -> Path:
            base = getattr(settings, "MEDIA_ROOT", None)
            return Path(base or ".").resolve() / "rrc" / "completions"

        out_dir = _media_base() / api   # api = re.sub(r"\\D+", "", api14)

    So for api14="42-317-36134-0000", api (digits only) = "42317361340000",
    the directory is: MEDIA_ROOT/rrc/completions/42317361340000/
    and the glob pattern must be: GAU_42317361340000_*.pdf
    """

    def test_get_gau_letter_storage_path_matches_extractor_convention(self, settings):
        """Capture the directory and glob pattern the service uses and assert they
        match the extractor's convention."""
        settings.MEDIA_ROOT = "/test/media"
        get_gau_letter = _import_get_gau_letter()

        captured_glob_args = []

        import re

        # We need to intercept the actual Path operations to capture what directory
        # and glob pattern are used.  We let Path() construction go through normally
        # but intercept .glob() on the out_dir Path object.
        real_path = Path

        class CapturingDir:
            """Stands in for the out_dir Path object; captures glob calls."""
            def __init__(self, path_obj):
                self._p = path_obj

            def glob(self, pattern):
                captured_glob_args.append((str(self._p), pattern))
                return []  # No files found → returns None, which is fine

            def __truediv__(self, other):
                return CapturingDir(self._p / other)

            def resolve(self):
                return CapturingDir(self._p.resolve())

            def __str__(self):
                return str(self._p)

        original_path_init = real_path.__init__

        def patched_path(*args, **kwargs):
            obj = real_path(*args, **kwargs)
            return CapturingDir(obj)

        with patch("apps.intelligence.services.customer_documents.Path", side_effect=patched_path):
            try:
                get_gau_letter("00000000-0000-0000-0000-000000000002", "42-317-36134-0000")
            except Exception:
                pass  # path construction errors are fine here; we only need the glob capture

        assert captured_glob_args, (
            "get_gau_letter must call Path.glob() to find GAU files — no glob call captured"
        )
        glob_dir, glob_pattern = captured_glob_args[0]

        # The directory must contain rrc/completions/<api_digits>
        api_digits = re.sub(r"\D+", "", "42-317-36134-0000")  # "42317361340000"
        assert "rrc" in glob_dir and "completions" in glob_dir, (
            f"Service must look inside MEDIA_ROOT/rrc/completions/. Got dir: {glob_dir!r}"
        )
        assert api_digits in glob_dir, (
            f"Service must include api digits '{api_digits}' in the lookup directory. "
            f"Got dir: {glob_dir!r}"
        )
        # Glob pattern must target GAU files
        assert "GAU" in glob_pattern.upper(), (
            f"Glob pattern must target GAU files. Got pattern: {glob_pattern!r}"
        )
        assert glob_pattern.endswith(".pdf") or glob_pattern.endswith("*.pdf"), (
            f"Glob pattern must end with .pdf. Got: {glob_pattern!r}"
        )


# ===========================================================================
# Test 4 — handles open() exception gracefully
# ===========================================================================

class TestGetGauLetterHandlesExceptionGracefully:
    """get_gau_letter returns None and does not propagate when open() raises."""

    def test_get_gau_letter_handles_storage_exception_gracefully(self, settings, caplog):
        """File appears to exist (glob returns a path) but open() raises OSError.
        Service must return None and log a warning, not propagate."""
        import logging
        settings.MEDIA_ROOT = "/fake/media"
        get_gau_letter = _import_get_gau_letter()

        fake_path = MagicMock(spec=Path)
        fake_path.__str__ = MagicMock(return_value="/fake/media/rrc/completions/42317361340000/GAU_42317361340000_001.pdf")

        with patch(
            "apps.intelligence.services.customer_documents.Path"
        ) as mock_path_cls:
            mock_out_dir = MagicMock()
            mock_out_dir.glob.return_value = [fake_path]
            mock_path_cls.return_value.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_out_dir

            with patch("builtins.open", side_effect=OSError("read failed")):
                with caplog.at_level(logging.WARNING):
                    result = get_gau_letter("00000000-0000-0000-0000-000000000002", "42-317-36134")

        assert result is None, (
            f"Expected None when open() raises, got {result!r}"
        )
        assert any("warning" in r.levelname.lower() or r.levelno >= logging.WARNING
                   for r in caplog.records), (
            "Service must log a warning when it catches the OSError"
        )


# ===========================================================================
# Test 5 — 10-digit dashed input maps to 10-digit storage dir
# ===========================================================================

class TestGetGauLetterStripsDashesAndUsesDigitsOnly:
    """Confirm the api_digits computed from a dashed 10-digit input is
    exactly ``4231736134`` and the glob is invoked inside that directory.

    This regression-guards the wiring fix: api14 is the unstripped form
    (10 or 14 digits), and get_gau_letter strips non-digits to match the
    extractor's filesystem layout. Passing the 8-digit RRC form value would
    miss the directory entirely.
    """

    def test_get_gau_letter_dashed_10_digit_input_resolves_to_10_digit_dir(self, settings):
        settings.MEDIA_ROOT = "/test/media"
        get_gau_letter = _import_get_gau_letter()

        captured_glob_args = []

        real_path = Path

        class CapturingDir:
            def __init__(self, path_obj):
                self._p = path_obj

            def glob(self, pattern):
                captured_glob_args.append((str(self._p), pattern))
                return []

            def __truediv__(self, other):
                return CapturingDir(self._p / other)

            def resolve(self):
                return CapturingDir(self._p.resolve())

            def __str__(self):
                return str(self._p)

        def patched_path(*args, **kwargs):
            obj = real_path(*args, **kwargs)
            return CapturingDir(obj)

        with patch("apps.intelligence.services.customer_documents.Path", side_effect=patched_path):
            try:
                get_gau_letter(
                    "00000000-0000-0000-0000-000000000002",
                    "42-317-36134",  # dashed 10-digit form
                )
            except Exception:
                pass

        assert captured_glob_args, "Expected at least one glob call"
        glob_dir, _ = captured_glob_args[0]
        # 10-digit api_digits = re.sub(r"\D+", "", "42-317-36134") = "4231736134"
        assert "4231736134" in glob_dir, (
            f"glob dir must contain digits-only api '4231736134', got: {glob_dir!r}"
        )
        # And must NOT collapse to 8-digit form.
        assert glob_dir.endswith("4231736134") or "/4231736134/" in glob_dir or glob_dir.rstrip("/").endswith("4231736134"), (
            f"glob dir must end at the 10-digit api_digits directory. Got: {glob_dir!r}"
        )
