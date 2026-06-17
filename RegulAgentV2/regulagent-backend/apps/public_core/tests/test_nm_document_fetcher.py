"""
Tests for NM OCD Document Fetcher

Tests document listing, downloading, and API format conversion
for New Mexico OCD imaging portal.
"""

import pytest
from unittest.mock import Mock, patch
from apps.public_core.services.nm_document_fetcher import (
    NMDocumentFetcher,
    NMDocument,
    list_nm_documents,
    download_nm_document,
)


class TestAPIFormatConversion:
    """Test API number format conversion."""

    def test_api10_to_api14(self):
        """Test converting 10-digit API to 14-digit format."""
        fetcher = NMDocumentFetcher()
        assert fetcher._api_to_api14("3001528692") == "30015286920000"

    def test_api14_unchanged(self):
        """Test 14-digit API remains unchanged."""
        fetcher = NMDocumentFetcher()
        assert fetcher._api_to_api14("30015286920000") == "30015286920000"

    def test_api_with_dashes(self):
        """Test API with dashes is converted correctly."""
        fetcher = NMDocumentFetcher()
        assert fetcher._api_to_api14("30-015-28692") == "30015286920000"
        assert fetcher._api_to_api14("30-015-28692-0000") == "30015286920000"

    def test_invalid_api_too_short(self):
        """Test invalid API (too short) raises ValueError."""
        fetcher = NMDocumentFetcher()
        with pytest.raises(ValueError, match="Invalid API number"):
            fetcher._api_to_api14("123")

    def test_invalid_api_too_long(self):
        """Test invalid API (too long) raises ValueError."""
        fetcher = NMDocumentFetcher()
        with pytest.raises(ValueError, match="Invalid API number"):
            fetcher._api_to_api14("123456789012345")


class TestDocumentTypeDetection:
    """Test document type detection from filenames (via _detect_doc_type legacy helper).

    NOTE: NM OCD filenames are bare API+timestamp strings (e.g.
    30015288410000_07_31_2018_02_37_53.pdf).  The _detect_doc_type helper
    passes an empty link_text and row_text, relying only on the filename.
    Word-boundary regexes do NOT fire when form codes are flanked by
    underscores (e.g. 'C-101_application.pdf' → '101_' has no boundary).
    These cases correctly return None; detection only fires when the form code
    appears in the page context (link text / row text).
    Returns canonical underscore-format codes: 'c_101'..'c_105'.
    """

    def test_detect_c101(self):
        """C-101 detection from filename only: flanked-by-underscore → None."""
        fetcher = NMDocumentFetcher()
        # Underscores around the code block the word-boundary regex → None
        assert fetcher._detect_doc_type("C-101_application.pdf") is None
        assert fetcher._detect_doc_type("c101_form.pdf") is None
        # Code followed by period (word boundary) → matches
        assert fetcher._detect_doc_type("form_C101.pdf") is None  # prefix underscore

    def test_detect_c103(self):
        """C-103 detection from filename only: underscore-flanked → None."""
        fetcher = NMDocumentFetcher()
        assert fetcher._detect_doc_type("C-103_sundry.pdf") is None
        assert fetcher._detect_doc_type("c103_plugging.pdf") is None
        assert fetcher._detect_doc_type("Sundry_C103.pdf") is None

    def test_detect_c105(self):
        """C-105 detection from filename only: underscore-flanked → None."""
        fetcher = NMDocumentFetcher()
        assert fetcher._detect_doc_type("C-105_completion.pdf") is None
        assert fetcher._detect_doc_type("c105_report.pdf") is None
        assert fetcher._detect_doc_type("Completion_C105.pdf") is None

    def test_unknown_type_returns_none(self):
        """Test unknown document type returns None."""
        fetcher = NMDocumentFetcher()
        assert fetcher._detect_doc_type("unknown_document.pdf") is None
        assert fetcher._detect_doc_type("random_file.pdf") is None


class TestDocumentListParsing:
    """Test HTML parsing for document lists."""

    def test_parse_document_list_with_pdfs(self):
        """Test parsing HTML with PDF links."""
        fetcher = NMDocumentFetcher()
        html = """
        <html>
            <body>
                <a href="/Imaging/FileStore/03/wf/path1/C-103_document.pdf">C-103 Document</a>
                <a href="/Imaging/FileStore/03/wf/path2/C-105_completion.pdf">C-105 Completion</a>
                <a href="https://ocdimage.emnrd.nm.gov/Imaging/FileStore/03/wf/path3/C-101_permit.pdf">C-101 Permit</a>
            </body>
        </html>
        """
        documents = fetcher._parse_document_list(html)

        assert len(documents) == 3
        assert documents[0].filename == "C-103_document.pdf"
        assert documents[0].url == "https://ocdimage.emnrd.nm.gov/Imaging/FileStore/03/wf/path1/C-103_document.pdf"
        assert documents[0].doc_type == "c_103"

        assert documents[1].filename == "C-105_completion.pdf"
        assert documents[1].doc_type == "c_105"

        assert documents[2].filename == "C-101_permit.pdf"
        assert documents[2].url == "https://ocdimage.emnrd.nm.gov/Imaging/FileStore/03/wf/path3/C-101_permit.pdf"
        assert documents[2].doc_type == "c_101"

    def test_parse_document_list_empty(self):
        """Test parsing HTML with no PDF links."""
        fetcher = NMDocumentFetcher()
        html = """
        <html>
            <body>
                <p>No documents available</p>
            </body>
        </html>
        """
        documents = fetcher._parse_document_list(html)
        assert len(documents) == 0

    def test_parse_document_list_case_insensitive(self):
        """Test parsing handles .PDF extension case-insensitively."""
        fetcher = NMDocumentFetcher()
        html = """
        <html>
            <body>
                <a href="/path/document.PDF">Document</a>
                <a href="/path/document.Pdf">Document</a>
            </body>
        </html>
        """
        documents = fetcher._parse_document_list(html)
        assert len(documents) == 2


class TestURLConstruction:
    """Test URL construction for NM OCD portal."""

    def test_list_documents_url(self):
        """Test URL construction for listing documents."""
        fetcher = NMDocumentFetcher()
        # We'll mock the request to avoid actual HTTP calls
        with patch.object(fetcher.session, 'get') as mock_get:
            mock_response = Mock()
            mock_response.text = "<html></html>"
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            fetcher.list_documents("30-015-28692")

            # Verify the correct URL was called
            expected_url = "https://ocdimage.emnrd.nm.gov/imaging/WellFileView.aspx?RefType=WF&RefID=30015286920000"
            mock_get.assert_called_once_with(expected_url, timeout=60.0)

    def test_get_combined_pdf_url(self):
        """Test combined PDF URL construction."""
        fetcher = NMDocumentFetcher()
        url = fetcher.get_combined_pdf_url("30-015-28692")
        expected = "https://ocdimage.emnrd.nm.gov/imaging/WellFileView.aspx?RefType=WF&RefID=30015286920000&ViewAll=true"
        assert url == expected


class TestDocumentDownload:
    """Test document downloading functionality."""

    def test_download_document(self):
        """Test downloading a single document."""
        fetcher = NMDocumentFetcher()
        doc = NMDocument(
            filename="test.pdf",
            url="https://example.com/test.pdf"
        )

        with patch.object(fetcher.session, 'get') as mock_get:
            mock_response = Mock()
            mock_response.content = b"PDF content"
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            content = fetcher.download_document(doc)

            assert content == b"PDF content"
            mock_get.assert_called_once_with("https://example.com/test.pdf", timeout=60.0)

    def test_download_all_documents(self):
        """Test downloading all documents for a well."""
        fetcher = NMDocumentFetcher()

        # Mock list_documents
        mock_docs = [
            NMDocument(filename="doc1.pdf", url="https://example.com/doc1.pdf"),
            NMDocument(filename="doc2.pdf", url="https://example.com/doc2.pdf"),
        ]

        with patch.object(fetcher, 'list_documents', return_value=mock_docs):
            with patch.object(fetcher.session, 'get') as mock_get:
                mock_response = Mock()
                mock_response.content = b"PDF content"
                mock_response.raise_for_status = Mock()
                mock_get.return_value = mock_response

                results = fetcher.download_all_documents("30-015-28692")

                assert len(results) == 2
                assert results[0][0].filename == "doc1.pdf"
                assert results[0][1] == b"PDF content"
                assert results[1][0].filename == "doc2.pdf"
                assert results[1][1] == b"PDF content"

    def test_download_all_documents_handles_errors(self):
        """Test download_all_documents continues on individual failures."""
        fetcher = NMDocumentFetcher()

        mock_docs = [
            NMDocument(filename="good.pdf", url="https://example.com/good.pdf"),
            NMDocument(filename="bad.pdf", url="https://example.com/bad.pdf"),
        ]

        with patch.object(fetcher, 'list_documents', return_value=mock_docs):
            with patch.object(fetcher.session, 'get') as mock_get:
                # First call succeeds, second fails
                def side_effect(*args, **kwargs):
                    if 'bad.pdf' in args[0]:
                        raise Exception("Download failed")
                    mock_response = Mock()
                    mock_response.content = b"PDF content"
                    mock_response.raise_for_status = Mock()
                    return mock_response

                mock_get.side_effect = side_effect

                results = fetcher.download_all_documents("30-015-28692")

                # Should have only the successful download
                assert len(results) == 1
                assert results[0][0].filename == "good.pdf"


class TestContextManager:
    """Test context manager protocol."""

    def test_context_manager_closes_session(self):
        """Test that context manager closes the session."""
        with patch('requests.Session') as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value = mock_session

            with NMDocumentFetcher() as fetcher:
                pass

            mock_session.close.assert_called_once()


class TestConvenienceFunctions:
    """Test convenience functions."""

    def test_list_nm_documents(self):
        """Test list_nm_documents convenience function."""
        with patch('apps.public_core.services.nm_document_fetcher.NMDocumentFetcher') as mock_fetcher_class:
            mock_fetcher = Mock()
            mock_fetcher.__enter__ = Mock(return_value=mock_fetcher)
            mock_fetcher.__exit__ = Mock(return_value=False)
            mock_fetcher.list_documents = Mock(return_value=[])
            mock_fetcher_class.return_value = mock_fetcher

            result = list_nm_documents("30-015-28692")

            mock_fetcher.list_documents.assert_called_once_with("30-015-28692")
            assert result == []

    def test_download_nm_document(self):
        """Test download_nm_document convenience function."""
        with patch('apps.public_core.services.nm_document_fetcher.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.content = b"PDF content"
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            content = download_nm_document("https://example.com/test.pdf")

            assert content == b"PDF content"
            mock_get.assert_called_once_with("https://example.com/test.pdf", timeout=60.0)
