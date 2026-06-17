"""
NM OCD Document Fetcher

Downloads well file documents from NM OCD imaging portal.
"""
from __future__ import annotations

import re
import logging
from typing import List, Optional
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class NMDocument:
    """Metadata for an NM well document."""
    filename: str
    url: str
    file_size: Optional[str] = None
    date: Optional[str] = None
    doc_type: Optional[str] = None  # C-101, C-103, etc. if detectable


class NMDocumentFetcher:
    """Fetcher for NM OCD well documents."""

    BASE_URL = "https://ocdimage.emnrd.nm.gov/imaging/WellFileView.aspx"

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self.session = requests.Session()

    def _api_to_api14(self, api: str) -> str:
        """
        Convert any API format to 14-digit no-dash format.

        Args:
            api: API number in any format (with or without dashes)

        Returns:
            14-digit API number with no dashes

        Raises:
            ValueError: If API cannot be converted to valid 14-digit format
        """
        digits = re.sub(r'[^0-9]', '', api)
        if len(digits) == 10:
            digits = digits + "0000"
        if len(digits) != 14:
            raise ValueError(f"Invalid API number: {api}")
        return digits

    def list_documents(self, api: str) -> List[NMDocument]:
        """
        List all available documents for a well.

        Args:
            api: API number in any format

        Returns:
            List of NMDocument with metadata
        """
        api14 = self._api_to_api14(api)
        url = f"{self.BASE_URL}?RefType=WF&RefID={api14}"

        logger.info(f"Listing NM documents: {url}")
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()

        return self._parse_document_list(response.text)

    def _parse_document_list(self, html: str) -> List[NMDocument]:
        """Parse document list from HTML."""
        soup = BeautifulSoup(html, 'html.parser')
        documents = []

        # Find all PDF links
        for link in soup.find_all('a', href=re.compile(r'\.pdf$', re.IGNORECASE)):
            href = link.get('href', '')
            if href:
                # Make absolute URL if relative
                if not href.startswith('http'):
                    href = f"https://ocdimage.emnrd.nm.gov{href}"

                filename = href.split('/')[-1]

                # Extract link text and parent row text for context-based detection
                link_text = link.get_text(strip=True)
                row = link.find_parent('tr')
                row_text = row.get_text(separator=' ', strip=True) if row else ''

                doc_type = self._detect_doc_type_from_context(link_text, row_text, filename)

                documents.append(NMDocument(
                    filename=filename,
                    url=href,
                    file_size=None,  # Could parse from page if available
                    date=None,  # Could parse from page if available
                    doc_type=doc_type
                ))

        return documents

    def _detect_doc_type_from_context(
        self, link_text: str, row_text: str, filename: str
    ) -> Optional[str]:
        """
        Detect document type from link text, row text, or filename.

        NM OCD filenames are typically bare API numbers + timestamps (e.g.
        30015288410000_07_31_2018_02_37_53.pdf) so the page context is the
        primary signal; filename check is a fallback for the rare cases where
        the form type is embedded in the name.

        Returns canonical underscore-format codes: 'c_101'..'c_105', 'apd'.
        Form semantics (per forms.py):
            C-101 = Application to Drill
            C-102 = Well Location Plat
            C-103 = Sundry Notice (also used for plug & abandon)
            C-104 = Request for Allowable
            C-105 = Completion Report (also workover completion)
        """
        # Combine link text, row text, and filename for a single pass
        combined = f"{link_text} {row_text}".lower()
        combined_and_filename = combined + " " + filename.lower()

        # C-103 / plug & abandon / sundry notice
        # (check plug+abandon before the generic completion branch)
        if (re.search(r'\bc-?103\b', combined_and_filename)
                or re.search(r'\bsundry\b', combined_and_filename)
                or re.search(r'p&a', combined_and_filename)
                or (re.search(r'\bplug', combined_and_filename)
                    and re.search(r'\babandon\b', combined_and_filename))):
            return 'c_103'

        # C-101 / application to drill
        if re.search(r'\bc-?101\b', combined_and_filename):
            return 'c_101'

        # C-102 / well location plat (explicit code only — NOT triggered by text keywords)
        if re.search(r'\bc-?102\b', combined_and_filename):
            return 'c_102'

        # C-104 / request for allowable
        if re.search(r'\bc-?104\b', combined_and_filename):
            return 'c_104'

        # C-105 / completion report or workover completion
        if (re.search(r'\bc-?105\b', combined_and_filename)
                or re.search(r'\bcompletion\b', combined_and_filename)
                or re.search(r'\bworkover\b', combined_and_filename)):
            return 'c_105'

        # APD
        if re.search(r'\bapd\b', combined_and_filename) or re.search(r'application.*permit.*drill', combined_and_filename):
            return 'apd'

        return None

    def _detect_doc_type(self, filename: str) -> Optional[str]:
        """Try to detect document type from filename only (legacy helper)."""
        return self._detect_doc_type_from_context('', '', filename)

    def download_document(self, doc: NMDocument) -> bytes:
        """
        Download a single document.

        Args:
            doc: NMDocument with URL

        Returns:
            PDF bytes
        """
        logger.info(f"Downloading: {doc.filename}")
        response = self.session.get(doc.url, timeout=self.timeout)
        response.raise_for_status()
        return response.content

    def download_all_documents(self, api: str) -> List[tuple[NMDocument, bytes]]:
        """
        Download all documents for a well.

        Args:
            api: API number

        Returns:
            List of (NMDocument, bytes) tuples
        """
        documents = self.list_documents(api)
        results = []

        for doc in documents:
            try:
                content = self.download_document(doc)
                results.append((doc, content))
            except Exception as e:
                logger.error(f"Failed to download {doc.filename}: {e}")

        return results

    def get_combined_pdf_url(self, api: str) -> str:
        """
        Get the URL for the combined PDF download.

        Note: The actual "View All" functionality may require
        form submission or JavaScript. This returns the base URL.
        """
        api14 = self._api_to_api14(api)
        return f"{self.BASE_URL}?RefType=WF&RefID={api14}&ViewAll=true"

    def close(self):
        """Close HTTP session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def enrich_doc_types_from_index(documents: List[NMDocument], api: str) -> List[NMDocument]:
    """
    Override regex-detected doc_type values with indexed LLM classifications
    from ExtractedDocument when available.

    The index is keyed by filename tail (``source_path.rsplit('/', 1)[-1]``).
    Only non-'unknown' rows are used.  A DB error is caught and logged so the
    live scrape result is always returned.

    Args:
        documents: List of scraped NMDocument objects (mutated in place).
        api: API number in any format (10-digit, 14-digit, hyphenated).

    Returns:
        The (mutated) documents list.
    """
    if not documents:
        return documents

    try:
        from apps.public_core.models import ExtractedDocument
        from django.db.models import Q

        # Build API variants for the query.  NM api_number is stored as 10-digit
        # (no trailing zeros) or hyphenated.  Strip non-digits then take first 10.
        digits = re.sub(r'[^0-9]', '', api)
        api10 = digits[:10] if len(digits) >= 10 else digits
        api14 = digits[:10] + '0000' if len(digits) == 10 else digits

        api_filter = Q(api_number=api10)
        if api14 != api10:
            api_filter |= Q(api_number=api14)
        # Also match hyphenated 10-digit: XX-XXX-XXXXX
        if len(api10) == 10:
            hyphenated = f"{api10[:2]}-{api10[2:5]}-{api10[5:]}"
            api_filter |= Q(api_number=hyphenated)

        rows = (
            ExtractedDocument.objects
            .filter(api_filter, source_path__startswith='http')
            .exclude(document_type='unknown')
            .values('source_path', 'document_type')
        )

        # Build map: filename_tail → document_type
        index_map: dict = {}
        for row in rows:
            tail = row['source_path'].rsplit('/', 1)[-1]
            index_map[tail] = row['document_type']

        if not index_map:
            return documents

        for doc in documents:
            indexed = index_map.get(doc.filename)
            if indexed:
                doc.doc_type = indexed

    except Exception as exc:
        logger.warning(
            f"enrich_doc_types_from_index: DB lookup failed for api={api!r}: {exc}"
        )

    return documents


# Convenience functions
def list_nm_documents(api: str) -> List[NMDocument]:
    """List documents for an NM well."""
    with NMDocumentFetcher() as fetcher:
        return fetcher.list_documents(api)


def download_nm_document(url: str) -> bytes:
    """Download a single document by URL."""
    response = requests.get(url, timeout=60.0)
    response.raise_for_status()
    return response.content
