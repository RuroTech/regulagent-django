"""Fixed-width parser for the TX RRC dbf900 Full Wellbore file.

File format:
  - EBCDIC (cp037) encoded, gzip compressed (.ebc.gz)
  - Newline-delimited variable-length records (EBCDIC newline = 0x25)
  - First 2 bytes of each record = record type code ("01"–"28")
  - Multiple record types per wellbore, grouped under a record-01 anchor

Spec source: mlbelobraydi/TXRRC_data_harvest layouts_wells_dbf900.py
             RRC Wellbore Query Data definition manual, Rev. 11/01/2013

Only the record types needed for WellRegistry are parsed here:
  01 WBROOT  — API key, district, county, original completion date
  02 WBCOMPL — well type (oil/gas)
  13 WBNEWLOC — WGS84 lat/lon coordinates
  12 WBOLDLOC — lease name (fallback location text)

All other record types are skipped but their raw text is retained in
raw_payload if they appear under the current record-01.

Output: Iterator[WellRow] — one WellRow per record-01 (wellbore root),
        with data from associated child/sibling records merged in.
"""
from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EBCDIC overpunch decoding for PIC S9(n)V9(m) DISPLAY fields
# ---------------------------------------------------------------------------

_POS_OVERPUNCH: dict[str, int] = {
    "{": 0, "A": 1, "B": 2, "C": 3, "D": 4,
    "E": 5, "F": 6, "G": 7, "H": 8, "I": 9,
}
_NEG_OVERPUNCH: dict[str, int] = {
    "}": 0, "J": 1, "K": 2, "L": 3, "M": 4,
    "N": 5, "O": 6, "P": 7, "Q": 8, "R": 9,
}


def _parse_date8(s: str) -> date | None:
    """Parse an 8-char YYYYMMDD string.  Returns None for blank/zero."""
    s = s.strip()
    if not s or s == "00000000" or not s.isdigit():
        return None
    try:
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        if y < 1800 or m < 1 or m > 12 or d < 1 or d > 31:
            return None
        return date(y, m, d)
    except ValueError:
        return None


def _parse_int(s: str) -> int | None:
    """Strip and parse a numeric field; return None if blank/non-numeric."""
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_signed_decimal(s: str, decimal_places: int) -> float | None:
    """Parse a COBOL PIC S9(n)V9(m) DISPLAY field.

    Handles EBCDIC overpunch sign encoding on the last character.
    The implied decimal (V) is applied by dividing by 10**decimal_places.
    Returns None for blank/zero fields.
    """
    s = s.strip()
    if not s or all(c in "0 " for c in s):
        return None
    last = s[-1]
    sign = 1
    if last in _POS_OVERPUNCH:
        s = s[:-1] + str(_POS_OVERPUNCH[last])
    elif last in _NEG_OVERPUNCH:
        s = s[:-1] + str(_NEG_OVERPUNCH[last])
        sign = -1
    # Remove any remaining non-digit chars that might appear in test data
    cleaned = "".join(c for c in s if c.isdigit())
    if not cleaned:
        return None
    try:
        return sign * int(cleaned) / (10**decimal_places)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass
class WellRow:
    """Parsed fields from one record-01 (wellbore root) and its children."""

    # From record 01
    api_root: str           # WB-API-CNTY(3) + WB-API-UNIQUE(5) = 8 chars
    district: str           # WB-FIELD-DISTRICT
    county_code: str        # WB-RES-CNTY-CODE
    completion_date: date | None = None   # WB-ORIG-COMPL-DATE
    total_depth: int | None = None        # WB-TOTAL-DEPTH
    water_land_code: str = ""             # L=land, O=offshore, B=bay, I=inland

    # From record 02
    well_type: str = ""     # "OIL" | "GAS" | ""

    # From record 12 (old location)
    lease_name: str = ""

    # From record 13 (new location + coordinates)
    latitude: float | None = None
    longitude: float | None = None

    # From record 23 (WBH15 — most recent MIT test; first encountered = most recent)
    operator_id: str = ""

    # Derived flags
    is_active: bool = False   # True if water_land_code is not empty (active wells)
    is_iwar: bool = False     # True if well appears on IWAR (refined later)

    # Raw captured fields for raw_payload
    extra: dict = field(default_factory=dict)

    @property
    def api14(self) -> str:
        """TX API14 = state(42) + api_root(8) + sidetrack(00) + hole(00)."""
        return f"42{self.api_root}0000"

    @property
    def county_fips(self) -> str:
        """3-digit RRC county code (not FIPS — RRC uses its own numbering)."""
        return self.api_root[:3]


# ---------------------------------------------------------------------------
# Record parsers (field offsets from spec)
# ---------------------------------------------------------------------------


def _parse_rec01(line: str) -> dict:
    return {
        "api_root": line[2:10],           # WB-API-CNTY(3) + WB-API-UNIQUE(5)
        "district": line[14:16].strip(),  # WB-FIELD-DISTRICT
        "county_code": line[16:19].strip(),  # WB-RES-CNTY-CODE
        "completion_date": _parse_date8(line[20:28]),  # WB-ORIG-COMPL-DATE
        "total_depth": _parse_int(line[28:33]),        # WB-TOTAL-DEPTH
        "water_land_code": line[131:132].strip() if len(line) > 131 else "",
    }


def _parse_rec02(line: str) -> dict:
    """Record 02 WBCOMPL — derive well type from the fluid indicator at byte 2."""
    fluid = line[2:3].strip().upper() if len(line) > 2 else ""
    well_type = "OIL" if fluid == "O" else ("GAS" if fluid == "G" else "")
    return {"well_type": well_type}


def _parse_rec12(line: str) -> dict:
    """Record 12 WBOLDLOC — lease name at offset 2, len 32."""
    lease = line[2:34].strip() if len(line) > 34 else ""
    return {"lease_name": lease}


def _parse_rec13(line: str) -> dict:
    """Record 13 WBNEWLOC — WGS84 lat/lon at offsets 132 and 142 (len 10 each).

    PIC S9(3)V9(7) — 3 integer digits + 7 decimal = 10 chars, with EBCDIC
    overpunch sign on the last character.
    Texas latitudes:  ~25.8°N – 36.5°N  → positive
    Texas longitudes: ~93.5°W – 106.6°W → negative
    """
    lat: float | None = None
    lon: float | None = None
    if len(line) > 152:
        lat = _parse_signed_decimal(line[132:142], decimal_places=7)
        lon = _parse_signed_decimal(line[142:152], decimal_places=7)
        # Sanity-check: TX bounds
        if lat is not None and not (25.0 <= lat <= 37.0):
            lat = None
        if lon is not None:
            # Longitude may be stored as positive; TX is western hemisphere
            if lon > 0:
                lon = -lon
            if not (-107.0 <= lon <= -93.0):
                lon = None
    return {"latitude": lat, "longitude": lon}


# ---------------------------------------------------------------------------
# Streaming file parser
# ---------------------------------------------------------------------------


def parse_wellbore_file(path: Path) -> Iterator[WellRow]:
    """Stream-parse a dbf900.ebc.gz (or dbf900.txt.gz) file.

    Opens the gzip file in text mode with cp037 encoding (handles EBCDIC).
    If the file is already ASCII (e.g. txt.gz), cp037 decodes ASCII bytes
    correctly for the printable range, so the same code works for both.

    Yields one WellRow per record-01.  Merges data from records 02, 12, 13
    that appear between the current record-01 and the next record-01.
    """
    current: WellRow | None = None
    records_seen = 0
    wells_emitted = 0

    RECORD_LEN = 247
    READ_CHUNK = RECORD_LEN * 4096  # read 4096 records at a time (~1 MB decompressed)

    try:
        with gzip.open(path, "rb") as fh:
            leftover = b""
            while True:
                raw = fh.read(READ_CHUNK)
                if not raw:
                    break
                data = leftover + raw
                # Process all complete 247-byte records
                n_complete = len(data) // RECORD_LEN
                for i in range(n_complete):
                    chunk = data[i * RECORD_LEN : (i + 1) * RECORD_LEN]
                    line = chunk.decode("cp037", errors="replace")
                    if len(line) < 2:
                        continue
                    rec_type = line[:2]
                    records_seen += 1

                    if rec_type == "01":
                        if current is not None:
                            yield current
                            wells_emitted += 1
                        f = _parse_rec01(line)
                        water_land_code = f["water_land_code"]
                        current = WellRow(
                            api_root=f["api_root"],
                            district=f["district"],
                            county_code=f["county_code"],
                            completion_date=f["completion_date"],
                            total_depth=f["total_depth"],
                            water_land_code=water_land_code,
                            # Set is_active based on water_land_code presence
                            is_active=bool(water_land_code),
                            is_iwar=False,
                        )
                    elif current is not None:
                        if rec_type == "02":
                            f = _parse_rec02(line)
                            if f["well_type"] and not current.well_type:
                                current.well_type = f["well_type"]
                        elif rec_type == "12":
                            f = _parse_rec12(line)
                            if f["lease_name"] and not current.lease_name:
                                current.lease_name = f["lease_name"]
                        elif rec_type == "13":
                            f = _parse_rec13(line)
                            if f["latitude"] is not None:
                                current.latitude = f["latitude"]
                            if f["longitude"] is not None:
                                current.longitude = f["longitude"]
                        elif rec_type == "23":
                            # WBH15 — first encountered = most recent test
                            if not current.operator_id and len(line) > 17:
                                current.operator_id = line[11:17].strip()
                leftover = data[n_complete * RECORD_LEN :]

        # Emit the final wellbore
        if current is not None:
            yield current
            wells_emitted += 1

    except Exception as exc:
        logger.error(
            "parse_wellbore_file failed after %d records / %d wells: %s",
            records_seen,
            wells_emitted,
            exc,
        )
        raise

    logger.info(
        "parse_wellbore_file complete: %d records, %d wells emitted",
        records_seen,
        wells_emitted,
    )
