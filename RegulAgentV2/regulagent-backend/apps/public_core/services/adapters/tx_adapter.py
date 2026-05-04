from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from apps.public_core.services.adapters.base import DocumentSpec, StateAdapter

logger = logging.getLogger(__name__)


def _rrc_name_to_doc_type(name: str) -> Optional[str]:
    """Map RRC form label to SUPPORTED_TYPES key."""
    import re
    n = name.lower()
    if re.search(r'\bw-?12\b', n): return "w12"
    if re.search(r'\bw-?15\b', n): return "w15"
    if re.search(r'\bw-?2\b', n): return "w2"
    if re.search(r'\bswr[\s-]*10\b', n): return "swr10"
    if re.search(r'\bswr[\s-]*13\b', n): return "swr13"
    if re.search(r'\bl-?1\b', n): return "l1"
    if re.search(r'\bp-?14\b', n): return "p14"
    if "gau" in n or "groundwater" in n: return "gau"
    return None


class TXAdapter(StateAdapter):
    """
    Texas adapter using the Neubus document archive.

    Replaces the old CMPL scraper approach. Now fetches documents from Neubus,
    classifies pages, extracts form data, and builds semantic index.
    """

    def __init__(self):
        self._last_fetch_error = None

    def state_code(self) -> str:
        return "TX"

    def fetch_document_list(self, api_number: str) -> List[DocumentSpec]:
        """
        Triage-first Neubus pipeline.

        1. INGEST — download all PDFs for the lease (or skip if fresh)
        2. TRIAGE — read first 2 pages of each PDF to extract API numbers
        3. Return ONLY docs matching the requested API for downstream indexing

        The caller (start_research_session_task) handles classify+extract
        via index_document_task chord. Other APIs' docs stay in cold storage
        and are processed when those APIs are requested later.
        """
        from apps.public_core.services.neubus_ingest import ingest_lease_if_stale, ingest_lease
        from apps.public_core.services.neubus_classifier import triage_lease_documents

        import re
        clean_api = re.sub(r"\D+", "", str(api_number))

        # ── Step 1: Try RRC Completions (fast, clean PDFs) ──
        try:
            from apps.public_core.services.rrc_completions_extractor import (
                extract_completions_all_documents,
            )
            rrc_result = extract_completions_all_documents(clean_api)
            rrc_files = rrc_result.get("files") or []

            if rrc_result.get("status") == "success" and rrc_files:
                logger.info(
                    f"TXAdapter: RRC completions found {len(rrc_files)} docs "
                    f"for api={api_number} (source={rrc_result.get('source')})"
                )
                docs_out = []
                for f in rrc_files:
                    path = f.get("path", "")
                    name = f.get("name", "")
                    docs_out.append(DocumentSpec(
                        filename=Path(path).name if path else name,
                        local_path=path,
                        doc_type=_rrc_name_to_doc_type(name),
                        metadata={"rrc_source": True, "rrc_form_name": name},
                    ))
                self._last_fetch_error = None
                return docs_out

            logger.info(
                f"TXAdapter: No RRC completions for api={api_number} "
                f"(status={rrc_result.get('status')}), falling back to Neubus"
            )
        except Exception as e:
            logger.warning(
                f"TXAdapter: RRC extractor failed for {api_number}: {e}, "
                f"falling back to Neubus"
            )

        # ── Step 2: Neubus fallback (existing code) ──
        try:
            # ── Step 1: INGEST ──────────────────────────────────────
            lease = ingest_lease_if_stale(clean_api)
            if lease is None:
                lease = ingest_lease(clean_api)

            # ── Step 2: TRIAGE ──────────────────────────────────────
            api_to_docs = triage_lease_documents(lease)

            # Find docs matching requested API (match on last 8 digits)
            api_suffix = clean_api[-8:] if len(clean_api) >= 8 else clean_api
            my_docs = []
            my_api = None

            for api_key, docs in api_to_docs.items():
                if api_key != "unknown" and api_suffix in api_key:
                    my_docs.extend(docs)
                    my_api = api_key

            if not my_docs:
                # Triage couldn't match the target API — fall back to "unknown" docs
                # plus ALL docs from the lease. The extraction pipeline's multi-field
                # attribution will sort out which forms belong to which well.
                unknown_docs = api_to_docs.get("unknown", [])
                available_apis = [k for k in api_to_docs.keys() if k != "unknown"]

                if unknown_docs:
                    self._last_fetch_error = {
                        "scraper_status": "no_match",
                        "message": (
                            f"Documents found in lease '{lease.lease_name}' ({lease.lease_id}) "
                            f"but none could be attributed to well {api_number}. "
                            f"This lease contains records for {len(available_apis)} other well(s): "
                            f"{', '.join(available_apis[:5])}."
                        ),
                        "api_search": clean_api,
                        "available_apis": available_apis[:20],
                    }
                    logger.warning(
                        f"TXAdapter: API {clean_api} not matched by triage in lease "
                        f"{lease.lease_id} ('{lease.lease_name}'). "
                        f"Available APIs: {available_apis[:10]}. Failing fast."
                    )
                    return []
                else:
                    logger.warning(
                        f"TXAdapter: API {clean_api} not matched by triage in lease "
                        f"{lease.lease_id}. Available APIs: {available_apis[:10]}."
                    )
                    self._last_fetch_error = {
                        "scraper_status": "no_match",
                        "message": (
                            f"Documents for this lease were downloaded, but no document matched "
                            f"API {api_number}. Triage found {len(available_apis)} other API(s) "
                            f"in this lease: {', '.join(available_apis[:5])}."
                        ),
                        "api_search": clean_api,
                        "available_apis": available_apis[:20],
                    }
                    return []

            # ── Step 3: Return DocumentSpec list ────────────────────
            # Only return docs for the requested API.
            # index_document_task (called by start_research_session_task)
            # will handle classify + extract + vectorize for each doc.
            docs_out = []
            for doc in my_docs:
                docs_out.append(DocumentSpec(
                    filename=doc.neubus_filename,
                    local_path=doc.local_path,
                    doc_type=None,
                    metadata={
                        "neubus_lease_id": lease.lease_id,
                        "form_types": doc.form_types_by_page,
                        "classification_status": doc.classification_status,
                        "extraction_status": doc.extraction_status,
                    },
                ))

            self._last_fetch_error = None
            logger.info(
                f"TXAdapter: returning {len(docs_out)} docs for api={api_number} "
                f"(lease {lease.lease_id}, {len(api_to_docs)} unique APIs in lease)"
            )
            return docs_out

        except Exception as e:
            logger.exception(f"TXAdapter: Neubus pipeline failed for {api_number}")
            self._last_fetch_error = {
                "scraper_status": "error",
                "message": f"Neubus pipeline error: {e}",
            }
            return []

    def download_document(self, doc: DocumentSpec) -> Path:
        """Documents are already downloaded during fetch_document_list."""
        local_path = Path(doc.local_path)
        logger.info(f"TXAdapter: using Neubus file at {local_path}")
        return local_path
