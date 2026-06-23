from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import time
import mimetypes

import requests
from django.conf import settings
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)


_FORM_PATTERNS = [
    (r'\bw-?12\b',           "W-12",   "w12"),
    (r'\bw-?15\b|cement',    "W-15",   "w15"),
    (r'\bw-?2\b',            "W-2",    "w2"),
    (r'\bswr[\s-]*10\b',     "SWR-10", "swr10"),
    (r'\bswr[\s-]*13\b',     "SWR-13", "swr13"),
    (r'\bl-?1\b',            "L-1",    "l1"),
    (r'\bp-?14\b',           "P-14",   "p14"),
    (r'\bgau\b|groundwater', "GAU",    "gau"),
]


def _classify_form_text(text: str) -> tuple:
    """Classify a form label into (doc_type, kind). Returns (text, "other") if no match."""
    low = text.lower()
    for pattern, doc_type, kind in _FORM_PATTERNS:
        if re.search(pattern, low):
            return doc_type, kind
    return text, "other"


RRC_COMPLETIONS_SEARCH = (
    "https://webapps.rrc.texas.gov/CMPL/publicSearchAction.do?"
    "formData.methodHndlr.inputValue=init&formData.headerTabSelected=home&formData.pageForwardHndlr.inputValue=home"
)


@dataclass
class DownloadRecord:
    name: str
    url: str
    path: str
    size_bytes: int
    content_type: str


def _media_base() -> Path:
    base = getattr(settings, "MEDIA_ROOT", None)
    return Path(base or ".").resolve() / "rrc" / "completions"


def _ensure_dir(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)


def _extract_detail_page_metadata(page) -> Dict[str, str]:
    """Extract structured metadata from RRC completions detail page header table."""
    metadata = {}
    try:
        # The detail page has a header table with key-value pairs
        for row in page.query_selector_all("table.DataGrid tr, table.FormTable tr"):
            cells = row.query_selector_all("td, th")
            for i in range(0, len(cells) - 1, 2):
                label = cells[i].inner_text().strip().lower().rstrip(":")
                value = cells[i + 1].inner_text().strip() if i + 1 < len(cells) else ""
                if not value:
                    continue
                if "district" in label:
                    metadata["district"] = value
                elif "county" in label:
                    metadata["county"] = value
                elif "operator" in label:
                    metadata["operator"] = value
                elif "filing" in label and "date" in label:
                    metadata["filing_date"] = value
                elif "field" in label:
                    metadata["field"] = value
                elif "lease" in label:
                    metadata["lease"] = value
    except Exception as e:
        logger.debug(f"Failed to extract detail page metadata: {e}")
    return metadata


def extract_completions_all_documents(api14: str, allowed_kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    api = re.sub(r"\D+", "", api14)
    if len(api) not in (8, 10, 14):
        raise ValueError("api must be 8, 10, or 14 digits")

    out_dir = _media_base() / api
    _ensure_dir(out_dir)

    # Cache policy: if files exist and the newest is within 14 days, return cached
    now = time.time()
    horizon = 14 * 24 * 60 * 60
    existing_files: List[DownloadRecord] = []
    if out_dir.exists():
        def _infer_kind_from_name(name: str) -> str:
            low = name.lower()
            for pattern, _, kind in _FORM_PATTERNS:
                if re.search(pattern, low):
                    return kind
            return "other"

        for p in out_dir.glob('*.pdf'):
            try:
                # Skip directional surveys
                if 'directional' in p.name.lower() and 'survey' in p.name.lower():
                    continue
                if allowed_kinds:
                    kind = _infer_kind_from_name(p.stem)
                    if kind not in set(k.lower() for k in allowed_kinds):
                        continue
                mtime = p.stat().st_mtime
                ctype = mimetypes.guess_type(str(p))[0] or 'application/pdf'
                existing_files.append(DownloadRecord(name=p.stem.split('_')[0], url='', path=str(p), size_bytes=p.stat().st_size, content_type=ctype))
            except Exception:
                continue
        if existing_files:
            newest = max(Path(f.path).stat().st_mtime for f in existing_files)
            if (now - newest) <= horizon:
                return {
                    "status": "success",
                    "api": api,
                    "api_search": api[-8:],
                    "output_dir": str(out_dir),
                    "files": [r.__dict__ for r in existing_files],
                    "source": "cache",
                }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(RRC_COMPLETIONS_SEARCH)
            page.wait_for_load_state("networkidle")
            # RRC search expects the 8-digit API root (county+unique); use last 8 digits
            search_api = api[-8:]
            page.fill('input[name="searchArgs.apiNoHndlr.inputValue"]', search_api)
            page.click('input[type="button"][value="Search"][onclick="doSearch();"]')
            page.wait_for_load_state("networkidle")

            # Seed a requests session with Playwright cookies for authenticated PDF downloads
            import requests as _requests
            session_req = _requests.Session()
            for cookie in context.cookies():
                session_req.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))

            # Get all rows from the DataGrid table (not just latest)
            # We need complete well history for proper analysis
            table = page.query_selector("table.DataGrid")
            if not table:
                return {"status": "no_records", "api": api, "api_search": search_api, "files": [],
                        "message": f"RRC Completions Query returned no results table for API {search_api}. The well may not have completion filings."}
            rows = table.query_selector_all("tr")  # header rows filtered below by link presence

            def parse_date(cell_text: str) -> tuple:
                # Expect mm/dd/yyyy in one of the columns; scan cells and return sort key
                import datetime as _dt
                for token in re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", cell_text):
                    try:
                        dt = _dt.datetime.strptime(token, "%m/%d/%Y")
                        return (dt.year, dt.month, dt.day)
                    except Exception:
                        continue
                return (0, 0, 0)

            if not rows:
                return {"status": "no_records", "api": api, "api_search": search_api, "files": [],
                        "message": f"RRC Completions Query found a table but no data rows for API {search_api}."}

            # Extract row data BEFORE sorting/navigating (to avoid stale element references)
            row_data: List[tuple] = []
            for row in rows:
                try:
                    link = row.query_selector("td:first-child a")
                    if not link:
                        continue
                    href = link.get_attribute("href")
                    if not href:
                        continue
                    row_text = row.inner_text() or ""
                    sort_key = parse_date(row_text)
                    row_data.append((sort_key, href, row_text))
                except Exception as e:
                    logger.debug(f"   Failed to extract row data: {e}")
                    continue
            
            if not row_data:
                return {"status": "no_records", "api": api, "api_search": search_api, "files": [],
                        "message": f"RRC Completions Query found rows but no navigable links for API {search_api}."}
            
            sorted_row_data = sorted(row_data, key=lambda x: x[0])
            
            files: List[DownloadRecord] = []
            seen_hrefs: set[str] = set()
            structured_data: List[Dict[str, Any]] = []
            
            logger.info(f"🔍 Processing {len(sorted_row_data)} rows from RRC search results (in chronological order)")
            for idx, (_sort_key, href, row_text) in enumerate(sorted_row_data, 1):
                logger.info(f"   • row[{idx}] href={href} text_snippet={row_text[:60]}")
            
            # Base URL for the RRC completions action (needed for proper navigation)
            RRC_CMPL_BASE = "https://webapps.rrc.texas.gov/CMPL/publicSearchAction.do"
            
            for row_idx, (sort_key, href, row_text) in enumerate(sorted_row_data, 1):
                logger.info(f"\n📋 Processing row {row_idx}/{len(sorted_row_data)}")
                logger.debug(f"   Row content: {row_text[:100]}...")

                try:
                    # The href from search results is a query string like "?packetSummaryId=..."
                    # We need to append it to the correct base path, not the root
                    if href.startswith("?"):
                        full_url = f"{RRC_CMPL_BASE}{href}"
                    elif href.startswith("/"):
                        full_url = f"https://webapps.rrc.texas.gov{href}"
                    elif href.startswith("http"):
                        full_url = href
                    else:
                        full_url = f"{RRC_CMPL_BASE}?{href}"
                    
                    logger.debug(f"   Navigating to: {full_url}")
                    page.goto(full_url, wait_until="networkidle")
                    logger.info(f"   ✅ Opened detail page for row {row_idx}")
                except Exception as e:
                    logger.warning(f"   ⚠️  Failed to navigate to detail page for row {row_idx}: {e}")
                    continue

                # Extract structured metadata from detail page
                page_metadata = _extract_detail_page_metadata(page)
                if page_metadata:
                    structured_data.append({"row": row_idx, **page_metadata})

                # Find the Form/Attachment table (using original working logic)
                documents_table = None
                for tbl in page.query_selector_all("table"):
                    cells = tbl.query_selector_all("th, td")
                    header = " ".join([c.inner_text().strip() for c in cells[:6]])
                    if "Form/Attachment" in header and "View Form/Attachment" in header:
                        documents_table = tbl
                        break

                fallback_links = []
                if not documents_table:
                    logger.warning(f"   ⚠️  No Form/Attachment table found in row {row_idx}, page={page.url}")
                    logger.debug(page.content()[:400])
                    fallback_links = page.query_selector_all(
                        "a[href*='viewPdfReportFormAction.do'], a[href*='dpimages/r/']"
                    )
                    if fallback_links:
                        logger.warning(f"   ⚠️  Falling back to anchor scan ({len(fallback_links)} links)")
                    else:
                        continue
                else:
                    logger.info(f"   📄 Found Form/Attachment table, extracting documents...")

                entries = documents_table.query_selector_all("tr") if documents_table else fallback_links

                for entry in entries:
                    if documents_table:
                        cols = entry.query_selector_all("td, th")
                        if len(cols) < 3:
                            continue
                        form_text = cols[0].inner_text().strip()
                        href_candidate = None
                        for a in entry.query_selector_all("a"):
                            h = a.get_attribute("href")
                            if h and ("viewPdfReportFormAction.do" in h or "dpimages/r/" in h):
                                href_candidate = h
                                break
                        if not href_candidate:
                            continue
                    else:
                        href_candidate = entry.get_attribute("href") or ""
                        form_text = entry.inner_text().strip() or "document"
                    
                    doc_type = form_text.split("\n")[0][:64]
                    is_directional = "directional survey" in doc_type.lower()

                    href_link = href_candidate
                    if href_link in seen_hrefs:
                        logger.debug(f"      Skipping duplicate href: {doc_type}")
                        continue
                    seen_hrefs.add(href_link)

                    url = (
                        f"https://webapps.rrc.texas.gov{href_link}"
                        if href_link.startswith("/")
                        else href_link
                        if href_link.startswith("http")
                        else f"https://webapps.rrc.texas.gov/{href_link}"
                    )

                    lower_href = (href_link or url).lower()
                    # URL-based detection (most reliable)
                    if "cmplw2formpdf" in lower_href:
                        doc_type, kind = "W-2", "w2"
                    elif "cmplw15formpdf" in lower_href:
                        doc_type, kind = "W-15", "w15"
                    elif is_directional:
                        doc_type, kind = doc_type, "directional"
                    else:
                        # Text-based detection from the form/attachment table label
                        doc_type, kind = _classify_form_text(doc_type)

                    # Do NOT filter by allowed_kinds in the research/fetch-all path.
                    # allowed_kinds is only honoured for the cache-scan path above.

                    safe_type = re.sub(r"[^A-Za-z0-9_.-]", "_", doc_type.replace(" ", "_"))[:32]
                    existing_count = len(list(out_dir.glob(f"{safe_type}_{api}_*.pdf")))
                    filename = f"{safe_type}_{api}_{existing_count + 1:03d}.pdf"
                    file_path = out_dir / filename

                    try:
                        logger.debug(f"      Downloading: {doc_type}")
                        resp = session_req.get(url, timeout=30)
                        if resp.status_code == 200:
                            with open(file_path, "wb") as f:
                                f.write(resp.content)
                            size = file_path.stat().st_size if file_path.exists() else 0
                            # Validate PDF: must start with %PDF magic bytes and be at least 100 bytes
                            with open(file_path, "rb") as f:
                                header = f.read(4)
                            if size < 100 or header != b"%PDF":
                                file_path.unlink(missing_ok=True)
                                logger.warning(f"      ⚠️  Skipping invalid PDF (corrupt download): {doc_type}")
                                # Corrupt/invalid PDFs: no RetrievedDocument row
                                continue
                            ctype = resp.headers.get("content-type", "")

                            # --- RetrievedDocument manifest wiring ---
                            try:
                                from apps.public_core.models import RetrievedDocument, WellRegistry
                                from apps.public_core.services.api_normalization import normalize_api_14digit
                                _norm_api = normalize_api_14digit(api14) or api
                                _well = WellRegistry.objects.filter(api14=_norm_api).first()
                                _rd_status = "skipped_directional" if is_directional else "pending"
                                RetrievedDocument.objects.update_or_create(
                                    api_number=_norm_api,
                                    href=href_link,
                                    defaults={
                                        "well": _well,
                                        "filename": filename,
                                        "local_path": str(file_path),
                                        "kind": kind,
                                        "index_status": _rd_status,
                                        "source_type": "rrc",
                                    },
                                )
                            except Exception as _rd_err:
                                logger.warning(f"      ⚠️  Failed to create RetrievedDocument manifest row: {_rd_err}")

                            if is_directional:
                                logger.debug(f"      Skipping directional survey from index list: {doc_type}")
                                # Do NOT add to files list — directional surveys are not indexed
                                continue

                            files.append(DownloadRecord(name=doc_type, url=url, path=str(file_path), size_bytes=size, content_type=ctype))
                            logger.info(f"      ✅ Downloaded: {doc_type} ({size:,} bytes)")
                        else:
                            logger.warning(f"      ⚠️  Failed to download (status {resp.status_code}): {doc_type}")
                    except Exception as e:
                        logger.warning(f"      ⚠️  Failed to download {doc_type}: {e}")
                        continue

            logger.info(f"\n✅ Completed processing all {len(sorted_row_data)} rows")
            logger.info(f"📊 Total files downloaded: {len(files)}")
            
            return {
                "status": "success" if files else "no_documents",
                "api": api,
                "api_search": search_api,
                "output_dir": str(out_dir),
                "files": [r.__dict__ for r in files],
                "source": "rrc_completions",
                "structured_data": structured_data,
                "message": None if files else f"Found {len(sorted_row_data)} completion records but could not download any PDF documents for API {search_api}.",
            }
        finally:
            context.close()
            browser.close()


