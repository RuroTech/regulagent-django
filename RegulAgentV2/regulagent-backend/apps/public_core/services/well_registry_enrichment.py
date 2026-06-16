"""
Service to enrich WellRegistry with data extracted from documents.

Implements fallback logic: W2 -> W15 -> GAU for operator, lat/lon, field, lease, well_number.
"""

import logging
from typing import Optional, Dict, Any, List
from decimal import Decimal

from apps.public_core.models import WellRegistry, ExtractedDocument

logger = logging.getLogger(__name__)


def enrich_well_registry_from_documents(
    well: WellRegistry,
    extracted_documents: Optional[List[ExtractedDocument]] = None
) -> bool:
    """
    Enrich WellRegistry fields from extracted documents using fallback order: W2 -> W15 -> GAU -> C105.

    Fields enriched:
    - operator_name
    - field_name
    - lease_name
    - well_number
    - county
    - district
    - state (derived from api14 prefix when blank)
    - lat
    - lon

    Args:
        well: WellRegistry instance to enrich
        extracted_documents: List of ExtractedDocument instances for this well.
            When None (the default), documents are fetched via the FK relationship:
            ExtractedDocument.objects.filter(well=well, status='success').

    Returns:
        True if any fields were updated, False otherwise
    """
    # Auto-fetch via FK when no list is provided (core bug fix: never rely on
    # api_number string match — ED.api_number can be in a different format).
    if extracted_documents is None:
        extracted_documents = list(
            ExtractedDocument.objects.filter(well=well, status="success")
        )

    # Organize docs by type — include c105 for NM documents
    docs_by_type = {}
    for doc in extracted_documents:
        if doc.document_type in ['w2', 'w15', 'gau', 'c105']:
            docs_by_type[doc.document_type] = doc

    # Fallback order: TX docs first (w2 > w15 > gau), then NM c105 last
    fallback_order = ['w2', 'w15', 'gau', 'c105']
    
    updated = False
    
    # Extract operator_name
    if not well.operator_name:
        operator = _extract_with_fallback(docs_by_type, fallback_order, 'operator_name')
        if operator:
            well.operator_name = operator[:128]  # Respect max_length
            updated = True
            logger.info(f"Enriched well {well.api14} operator_name: {operator}")
    
    # Extract field_name
    if not well.field_name:
        field = _extract_with_fallback(docs_by_type, fallback_order, 'field')
        if field:
            well.field_name = field[:128]
            updated = True
            logger.info(f"Enriched well {well.api14} field_name: {field}")
    
    # Extract lease_name
    if not well.lease_name:
        lease = _extract_with_fallback(docs_by_type, fallback_order, 'lease')
        if lease:
            well.lease_name = lease[:128]
            updated = True
            logger.info(f"Enriched well {well.api14} lease_name: {lease}")
    
    # Extract well_number
    if not well.well_number:
        well_no = _extract_with_fallback(docs_by_type, fallback_order, 'well_no')
        if well_no:
            well.well_number = str(well_no)[:32]
            updated = True
            logger.info(f"Enriched well {well.api14} well_number: {well_no}")

    # Extract county
    if not well.county:
        county = _extract_with_fallback(docs_by_type, fallback_order, 'county')
        if county:
            well.county = county[:64]
            updated = True
            logger.info(f"Enriched well {well.api14} county: {county}")

    # Extract district
    if not well.district:
        district = _extract_with_fallback(docs_by_type, fallback_order, 'district')
        if district:
            well.district = district[:8]
            updated = True
            logger.info(f"Enriched well {well.api14} district: {district}")

    # Derive state from api14 prefix when blank
    if not well.state:
        api14 = well.api14 or ""
        if api14.startswith("42"):
            well.state = "TX"
            updated = True
            logger.info(f"Derived state=TX for well {well.api14} from api14 prefix")
        elif api14.startswith("30"):
            well.state = "NM"
            updated = True
            logger.info(f"Derived state=NM for well {well.api14} from api14 prefix")

    # Extract lat/lon
    if not well.lat or not well.lon:
        coords = _extract_coordinates_with_fallback(docs_by_type, fallback_order)
        if coords:
            if coords['lat'] and not well.lat:
                well.lat = Decimal(str(coords['lat']))
                updated = True
                logger.info(f"Enriched well {well.api14} lat: {coords['lat']}")
            if coords['lon'] and not well.lon:
                well.lon = Decimal(str(coords['lon']))
                updated = True
                logger.info(f"Enriched well {well.api14} lon: {coords['lon']}")
    
    if updated:
        well.save()
        logger.info(f"Saved enriched WellRegistry for {well.api14}")
    
    return updated


def _extract_with_fallback(
    docs_by_type: Dict[str, ExtractedDocument],
    fallback_order: List[str],
    field_name: str
) -> Optional[str]:
    """
    Extract a field from documents using fallback order.
    
    Field mapping:
    - operator_name: operator_info.name
    - field: well_info.field
    - lease: well_info.lease
    - well_no: well_info.well_no
    """
    for doc_type in fallback_order:
        doc = docs_by_type.get(doc_type)
        if not doc:
            continue
        
        json_data = doc.json_data
        if not json_data:
            continue
        
        # Try to extract the field
        value = None
        if field_name == 'operator_name':
            operator_info = json_data.get('operator_info', {})
            value = operator_info.get('name')
        else:
            well_info = json_data.get('well_info', {})
            value = well_info.get(field_name)
        
        if value and str(value).strip() and str(value).strip().lower() not in ['n/a', 'null', 'none', '']:
            return str(value).strip()
    
    return None


def _extract_coordinates_with_fallback(
    docs_by_type: Dict[str, ExtractedDocument],
    fallback_order: List[str]
) -> Optional[Dict[str, Optional[float]]]:
    """
    Extract lat/lon coordinates from documents using fallback order.
    
    Returns dict with 'lat' and 'lon' keys, or None if not found.
    """
    for doc_type in fallback_order:
        doc = docs_by_type.get(doc_type)
        if not doc:
            continue
        
        json_data = doc.json_data
        if not json_data:
            continue
        
        well_info = json_data.get('well_info', {})
        location = well_info.get('location', {})
        
        lat = location.get('lat')
        lon = location.get('lon')
        
        # Check if we have valid coordinates
        if lat is not None and lon is not None:
            try:
                lat_float = float(lat)
                lon_float = float(lon)
                
                # Sanity check: valid range for Texas coordinates
                # Lat: 25.8° to 36.5° N, Lon: -93.5° to -106.5° W
                if 25.0 <= lat_float <= 37.0 and -107.0 <= lon_float <= -93.0:
                    return {'lat': lat_float, 'lon': lon_float}
            except (ValueError, TypeError):
                continue

    return None


def enrich_from_structured_scrapers(well: WellRegistry) -> bool:
    """
    Enrich WellRegistry from structured RRC web scrapers.
    Calls wellbore query and lease detail scrapers, merges results.
    Does NOT overwrite fields that already have values.

    Returns:
        True if any fields were updated, False otherwise.
    """
    merged: Dict[str, Any] = {}

    # Wellbore Query scraper
    try:
        from apps.public_core.services.rrc_wellbore_scraper import scrape_wellbore_data
        wb_data = scrape_wellbore_data(well.api14)
        if wb_data:
            merged.update(wb_data)
            logger.info(f"[StructuredEnrich] Wellbore data for {well.api14}: {list(wb_data.keys())}")
    except Exception as e:
        logger.warning(f"[StructuredEnrich] Wellbore scraper failed for {well.api14}: {e}")

    # Lease Detail scraper
    try:
        from apps.public_core.services.rrc_lease_scraper import scrape_lease_data
        lease_data = scrape_lease_data(well.api14)
        if lease_data:
            # Don't overwrite wellbore data with lease data
            for k, v in lease_data.items():
                if k not in merged or not merged[k]:
                    merged[k] = v
            logger.info(f"[StructuredEnrich] Lease data for {well.api14}: {list(lease_data.keys())}")
    except Exception as e:
        logger.warning(f"[StructuredEnrich] Lease scraper failed for {well.api14}: {e}")

    if not merged:
        return False

    updated = False

    # Apply to WellRegistry — only fill empty fields
    if not well.operator_name and merged.get("operator_name"):
        well.operator_name = merged["operator_name"][:128]
        updated = True

    if not well.field_name and merged.get("field_name"):
        well.field_name = merged["field_name"][:128]
        updated = True

    if not well.lease_name and merged.get("lease_name"):
        well.lease_name = merged["lease_name"][:128]
        updated = True

    if not well.county and merged.get("county"):
        well.county = merged["county"][:64]
        updated = True

    if not well.district and merged.get("district"):
        well.district = merged["district"][:32]
        updated = True

    if updated:
        well.save()
        logger.info(f"[StructuredEnrich] Saved enriched WellRegistry for {well.api14}")

    return updated


def build_lease_well_map(lease_id: str, state: str = "TX") -> dict:
    """
    Build a {well_number: api14} mapping for all wells on a lease.

    Sources (in order):
    1. WellRegistry records with this lease_id (already populated by triage/enrichment)
    2. RRC Lease Detail scraper (fills gaps for wells not yet in registry)

    Args:
        lease_id: The lease identifier (e.g., RRC lease number)
        state: State code (TX, NM)

    Returns:
        Dict mapping normalized well_number (stripped leading zeros) to api14.
        Example: {"1": "42003356630000", "2": "42003356640000"}
    """
    from apps.public_core.models import WellRegistry

    well_map = {}

    # 1. Pull from existing WellRegistry records
    registry_wells = WellRegistry.objects.filter(lease_id=lease_id).exclude(
        well_number__isnull=True
    ).exclude(well_number="")

    for w in registry_wells:
        normalized = w.well_number.strip().lstrip("0")
        if normalized and w.api14:
            well_map[normalized] = w.api14

    # 2. Cross-reference with NeubusDocument metadata
    # Neubus stores well_number per document from its own metadata — often more
    # complete than WellRegistry. Group by (well_number, api_number on linked EDs)
    # to discover mappings.
    if lease_id:
        try:
            from apps.public_core.models.neubus_lease import NeubusLease, NeubusDocument
            neubus_lease = NeubusLease.objects.filter(lease_id=lease_id).first()
            if neubus_lease:
                neubus_docs = neubus_lease.documents.exclude(
                    well_number=""
                ).values_list("well_number", flat=True).distinct()
                for wn in neubus_docs:
                    normalized = str(wn).strip().lstrip("0")
                    if normalized and normalized not in well_map:
                        # Try to find a WellRegistry match for this well_number on the lease
                        match = WellRegistry.objects.filter(
                            lease_id=lease_id,
                            well_number__iexact=normalized,
                        ).first()
                        if match and match.api14:
                            well_map[normalized] = match.api14
        except Exception as e:
            logger.warning(f"Neubus cross-reference failed for lease {lease_id}: {e}")

    logger.info(f"Built lease-well map for lease {lease_id}: {len(well_map)} wells")
    return well_map
