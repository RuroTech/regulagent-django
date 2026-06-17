"""
API endpoints for New Mexico well data lookup.

Provides REST API access to NM OCD well data scraper and document fetcher.
"""
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from apps.public_core.services.nm_well_scraper import NMWellScraper
from apps.public_core.services.nm_document_fetcher import NMDocumentFetcher, enrich_doc_types_from_index
from apps.public_core.serializers.nm_well import (
    NMWellDataSerializer,
    NMDocumentSerializer,
    NMCombinedPDFResponseSerializer,
)

logger = logging.getLogger(__name__)


class NMWellDetailView(APIView):
    """
    GET /api/nm/wells/{api}/

    Returns scraped well data from NM OCD Permitting portal.

    Path Parameters:
        api: API number in any format (10-digit with/without dashes, or 14-digit)

    Query Parameters:
        include_raw_html: (optional) If 'true', includes raw HTML in response for debugging

    Returns:
        200: Well data with all available fields
        400: Invalid API number format
        404: Well not found or network error
        500: Internal server error
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, api: str):
        """Fetch well data for given API number."""
        try:
            # Parse query params
            include_raw_html = request.query_params.get('include_raw_html', 'false').lower() == 'true'

            # Fetch well data using scraper
            logger.info(f"Fetching NM well data for API: {api}")
            with NMWellScraper() as scraper:
                well_data = scraper.fetch_well(api, include_raw_html=include_raw_html)

            # Serialize and return
            serializer = NMWellDataSerializer(well_data.to_dict())
            return Response(serializer.data, status=status.HTTP_200_OK)

        except ValueError as e:
            # Invalid API format
            logger.warning(f"Invalid API format: {api} - {str(e)}")
            return Response(
                {"detail": str(e), "api": api},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            # Network errors, scraping errors, etc.
            logger.error(f"Error fetching NM well {api}: {str(e)}", exc_info=True)
            return Response(
                {"detail": f"Failed to fetch well data: {str(e)}", "api": api},
                status=status.HTTP_404_NOT_FOUND
            )


class NMWellDocumentsView(APIView):
    """
    GET /api/nm/wells/{api}/documents/

    Returns list of available documents for a well from NM OCD imaging portal.

    Path Parameters:
        api: API number in any format (10-digit with/without dashes, or 14-digit)

    Returns:
        200: List of available documents with metadata
        400: Invalid API number format
        404: Well not found or no documents available
        500: Internal server error
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, api: str):
        """List documents for given API number."""
        try:
            # Fetch document list
            logger.info(f"Listing NM documents for API: {api}")
            with NMDocumentFetcher() as fetcher:
                documents = fetcher.list_documents(api)

            # Enrich doc_type with indexed LLM classifications where available
            documents = enrich_doc_types_from_index(documents, api)

            # Serialize and return
            serializer = NMDocumentSerializer(documents, many=True)
            return Response(
                {
                    "api": api,
                    "count": len(documents),
                    "documents": serializer.data
                },
                status=status.HTTP_200_OK
            )

        except ValueError as e:
            # Invalid API format
            logger.warning(f"Invalid API format: {api} - {str(e)}")
            return Response(
                {"detail": str(e), "api": api},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            # Network errors, scraping errors, etc.
            logger.error(f"Error listing documents for NM well {api}: {str(e)}", exc_info=True)
            return Response(
                {"detail": f"Failed to list documents: {str(e)}", "api": api},
                status=status.HTTP_404_NOT_FOUND
            )


class NMWellCombinedPDFView(APIView):
    """
    GET /api/nm/wells/{api}/documents/download/

    Returns URL for combined PDF download of all well documents.

    Path Parameters:
        api: API number in any format (10-digit with/without dashes, or 14-digit)

    Returns:
        200: Combined PDF download URL
        400: Invalid API number format
        500: Internal server error

    Note:
        The NM OCD portal may require form submission or JavaScript to generate
        the combined PDF. This endpoint returns the base URL with ViewAll=true
        parameter. Clients may need to handle additional steps for actual download.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, api: str):
        """Get combined PDF URL for given API number."""
        try:
            # Get combined PDF URL
            logger.info(f"Getting combined PDF URL for NM well API: {api}")
            with NMDocumentFetcher() as fetcher:
                url = fetcher.get_combined_pdf_url(api)
                # Also get the api14 for reference
                api14 = fetcher._api_to_api14(api)

            # Serialize and return
            serializer = NMCombinedPDFResponseSerializer({
                "url": url,
                "api14": api14,
                "note": "This URL may require browser interaction to generate the combined PDF. "
                        "Consider downloading individual documents if this does not work."
            })
            return Response(serializer.data, status=status.HTTP_200_OK)

        except ValueError as e:
            # Invalid API format
            logger.warning(f"Invalid API format: {api} - {str(e)}")
            return Response(
                {"detail": str(e), "api": api},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            # Unexpected errors
            logger.error(f"Error getting combined PDF URL for NM well {api}: {str(e)}", exc_info=True)
            return Response(
                {"detail": f"Failed to generate combined PDF URL: {str(e)}", "api": api},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
