"""HTTPS downloader for the TX RRC GoAnywhere MFT bulk data server.

The Texas Railroad Commission migrated bulk datasets to GoAnywhere MFT
(mft.rrc.texas.gov) in 2025. Each dataset has a stable public share UUID
at https://mft.rrc.texas.gov/link/<uuid>. Despite the PrimeFaces/JSF UI,
downloads use a plain form POST (PrimeFaces.submit, not PrimeFaces.ab AJAX),
so requests + bs4 can drive the full flow without JavaScript.

Three-step download flow:
  1. GET  /link/<uuid>                          → Set-Cookie JSESSIONID,
                                                  parse ViewState + file rows
  2. POST /webclient/godrive/PublicGoDrive.xhtml → 302 to /link/godrivedownload
  3. GET  /link/godrivedownload (same session)  → raw file bytes

ViewState values are session-bound (5-min timeout observed). Always GET
the share page immediately before POSTing — never cache a ViewState across
calls.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MFT_BASE = "https://mft.rrc.texas.gov"
_SHARE_URL = MFT_BASE + "/link/{uuid}"
_POST_URL = MFT_BASE + "/webclient/godrive/PublicGoDrive.xhtml"
_DOWNLOAD_URL = MFT_BASE + "/link/godrivedownload"

_DEFAULT_HEADERS = {
    "User-Agent": "regulagent/1.0 (public data pipeline)",
    "Accept": "text/html,application/xhtml+xml",
}

_TRANSIENT_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


@dataclass(frozen=True)
class ShareFile:
    """A single file entry parsed from a GoAnywhere GoDrive share page."""

    filename: str
    size_bytes: int
    row_idx: int  # the fileTable:<row_idx>:j_id_2f index — needed for the POST body


def _parse_share_page(html: str) -> tuple[str, list[ShareFile]]:
    """Parse a GoDrive share HTML page into (view_state, [ShareFile, ...]).

    Raises ValueError if javax.faces.ViewState is absent (i.e. the page is
    not a share listing — e.g. redirected to a login page).
    """
    soup = BeautifulSoup(html, "html.parser")

    vs_input = soup.find("input", {"name": "javax.faces.ViewState"})
    if vs_input is None:
        raise ValueError(
            "javax.faces.ViewState not found — response may not be a share "
            "listing page (possible redirect to Login.xhtml)"
        )
    view_state: str = vs_input["value"]  # type: ignore[assignment]

    files: list[ShareFile] = []
    row_idx = 0
    while True:
        btn = soup.find(id=f"fileTable:{row_idx}:j_id_2f")
        if btn is None:
            break
        row = btn.find_parent("tr")
        filename = ""
        size_bytes = 0
        if row:
            for td in row.find_all("td"):
                text = td.get_text(strip=True)
                if not text or td.find("button") or td.find("input"):
                    continue
                if not filename:
                    filename = text
                elif _looks_like_size(text) and not size_bytes:
                    size_bytes = _parse_size_text(text)
        if filename:
            files.append(ShareFile(filename=filename, size_bytes=size_bytes, row_idx=row_idx))
        row_idx += 1

    return view_state, files


def _looks_like_size(text: str) -> bool:
    upper = text.strip().upper()
    return any(upper.endswith(s) for s in ("KB", "MB", "GB", "TB", " B")) or upper.isdigit()


def _parse_size_text(text: str) -> int:
    """Convert a human-readable size ('469.56 MB') to bytes, best-effort."""
    text = text.strip().upper()
    multipliers = {"TB": 1024**4, "GB": 1024**3, "MB": 1024**2, "KB": 1024}
    for suffix, mult in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)].strip()) * mult)
            except ValueError:
                return 0
    try:
        return int(text.rstrip("B").strip())
    except ValueError:
        return 0


def list_share(uuid: str, *, timeout: float = 30.0) -> list[ShareFile]:
    """Return the list of files in a GoAnywhere MFT public share.

    Makes a single GET to /link/<uuid> and parses the DataTable HTML.
    Does not download any file bytes.
    """
    url = _SHARE_URL.format(uuid=uuid)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            session = requests.Session()
            session.headers.update(_DEFAULT_HEADERS)
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            _view_state, files = _parse_share_page(resp.text)
            logger.debug("list_share(%s): %d files found", uuid, len(files))
            return files
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            logger.warning("list_share attempt %d/%d failed: %s", attempt + 1, 3, exc)
            time.sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


def download_to_tempfile(
    uuid: str,
    row_idx: int,
    filename: str,
    *,
    timeout: float = 600.0,
) -> tuple[Path, str]:
    """Stream a single file from a GoAnywhere share to a temp file on disk.

    Executes the full three-step GET→POST→GET flow within a single requests
    session so the JSESSIONID cookie is preserved throughout.

    Returns (path_to_tempfile, source_url). The source_url is the logical
    provenance identifier: share UUID + filename fragment. It is stable
    across sessions (unlike /link/godrivedownload which is ephemeral).

    Caller is responsible for calling cleanup_tempfile() when done.
    """
    source_url = f"{MFT_BASE}/link/{uuid}#{filename}"
    last_exc: Exception | None = None

    for attempt in range(3):
        tmp = NamedTemporaryFile(delete=False, prefix="tx_rrc_", suffix=f"_{filename}")
        try:
            session = requests.Session()
            session.headers.update(_DEFAULT_HEADERS)

            # Step 1: GET share page — capture JSESSIONID cookie + ViewState
            share_resp = session.get(_SHARE_URL.format(uuid=uuid), timeout=timeout)
            share_resp.raise_for_status()
            view_state, _files = _parse_share_page(share_resp.text)

            # Step 2: POST form to queue the selected file in session state
            post_resp = session.post(
                _POST_URL,
                data={
                    "fileList": "fileList",
                    f"fileTable:{row_idx}:j_id_2f": f"fileTable:{row_idx}:j_id_2f",
                    "javax.faces.ViewState": view_state,
                    "fileList_SUBMIT": "1",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=False,
                timeout=timeout,
            )
            if post_resp.status_code not in (301, 302):
                raise RuntimeError(
                    f"POST step returned HTTP {post_resp.status_code}; expected 302. "
                    f"Response preview: {post_resp.text[:200]!r}"
                )

            # Step 3: GET the queued download (same session via cookies)
            with session.get(_DOWNLOAD_URL, stream=True, timeout=timeout) as dl_resp:
                dl_resp.raise_for_status()
                for chunk in dl_resp.iter_content(chunk_size=1 << 20):  # 1 MB
                    tmp.write(chunk)

            tmp.flush()
            tmp.close()
            size = Path(tmp.name).stat().st_size
            logger.info(
                "Downloaded %s from share %s (%s bytes) → %s",
                filename,
                uuid,
                f"{size:,}",
                tmp.name,
            )
            return Path(tmp.name), source_url

        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            tmp.close()
            cleanup_tempfile(Path(tmp.name))
            logger.warning("download_to_tempfile attempt %d/%d failed: %s", attempt + 1, 3, exc)
            time.sleep(2 ** attempt)
        except Exception:
            tmp.close()
            cleanup_tempfile(Path(tmp.name))
            raise

    raise last_exc  # type: ignore[misc]


def cleanup_tempfile(path: Path) -> None:
    """Best-effort temp file removal."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clean up tempfile %s: %s", path, exc)
