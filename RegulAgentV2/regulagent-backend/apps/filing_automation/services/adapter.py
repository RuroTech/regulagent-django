"""Adapter implementation lives at adapter.py — BE2 owns the rest."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

from apps.filing_automation._vendor.regulagent_core.automation.base.data_models import (
    FormData,
)
from apps.filing_automation._vendor.regulagent_core.domain.oil_gas.well_record import (
    Casing,
    ExistingTool,
    Perforation,
)
from apps.filing_automation.services.profile_schema import BusinessProfileIncomplete
from apps.intelligence.services.customer_documents import get_well_type


class PayloadIncomplete(Exception):
    """Raised when the PlanSnapshot.payload is missing a required key."""

    def __init__(self, field: str):
        super().__init__(f"Missing required payload field: {field}")
        self.field = field


def assert_profile_complete(profile, required_paths: list[str]) -> None:
    for path in required_paths:
        if _profile_get(profile, path) in (None, ""):
            raise BusinessProfileIncomplete(field=path)


@dataclass
class _PlugRow:
    top_ft: float
    bottom_ft: float
    cement_class: str
    sacks: int
    formation: str | None = None
    # Kernel step ``type`` and ``plug_type`` (when present). These drive the
    # RRC W-3A Plugging Proposal row-edit form's Type dropdown + the
    # Select-All-That-Apply multi-select. See _fill_plugging_proposal in
    # apps/filing_automation/_vendor/.../rrc_form_automator.py.
    type: str | None = None
    plug_type: str | None = None


@dataclass
class _WellRecordLite:
    """Lightweight stand-in for the prototype's pydantic WellRecord.

    The vendored pydantic model demands Location, well_status, well_type, etc.,
    none of which the W-3A filler actually reads. We surface only the fields
    the RRC adapter (and the failing adapter tests) consume.
    """

    api_number: str
    operator_name: str | None = None
    lease_name: str | None = None
    field_name: str | None = None
    district: str | None = None
    permit_number: str | None = None
    casing_program: list[Casing] = field(default_factory=list)
    perforations: list[Perforation] = field(default_factory=list)
    existing_tools: list[ExistingTool] = field(default_factory=list)
    formation_tops: list[dict] = field(default_factory=list)
    plug_rows: list[_PlugRow] = field(default_factory=list)
    total_depth_ft: float | None = None


_PROFILE_PREFIX = "rrc.w3a"

_REQUIRED_PROFILE_FIELDS = [
    f"{_PROFILE_PREFIX}.cementing_company_name",
    f"{_PROFILE_PREFIX}.contact_phone",
    f"{_PROFILE_PREFIX}.contact_email",
    f"{_PROFILE_PREFIX}.submitter_default_name",
    f"{_PROFILE_PREFIX}.submitter_default_title",
]


def _normalize_api(raw: Any) -> str:
    """Normalize an arbitrary API string to the 8-digit RRC W-3A form value.

    The TX RRC W-3A form's "API" field accepts the 8-digit RRC API
    (county prefix 3 digits + well sequence 5 digits). State code ``42`` is
    implicit and trailing completion / sidetrack location codes are NOT part
    of the form value.

    Strips ``42-`` state prefix and trailing zero pairs (``-00-00`` / ``0000``
    location-completion suffixes) so a 14-digit Texas API like
    ``42-317-36134-00-00`` reduces to ``31736134``.

    Inputs already at 8 digits (e.g. ``31736134``) pass through. The function
    raises ``PayloadIncomplete`` when the result is not exactly 8 digits, so
    we fail fast at the adapter rather than letting the portal show a
    confusing "lease not found" error after browser navigation.
    """
    if raw is None:
        raise PayloadIncomplete(field="inputs_summary.api14")
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        raise PayloadIncomplete(field="inputs_summary.api14")

    # Drop Texas state prefix.
    if digits.startswith("42") and len(digits) > 8:
        digits = digits[2:]

    # Texas API formats land here in one of two shapes after the 42 strip:
    #   - 12 digits: county(3) + well(5) + completion(2) + sidetrack(2)
    #   - 10 digits: county(3) + well(5) + completion(2) (older format)
    # In both cases the portal-form value is the leading 8 digits.
    if len(digits) > 8:
        digits = digits[:8]

    # Pad short county-stripped values back to 8 (rare; mostly defensive).
    if len(digits) < 8:
        raise PayloadIncomplete(field="inputs_summary.api14")

    if len(digits) != 8:
        raise PayloadIncomplete(field="inputs_summary.api14")

    return digits


def _profile_get(profile, dotted: str, default=None):
    """Read a dotted-path value from a TenantBusinessProfile-like object or dict.

    Supports three shapes:
      1. ``TenantBusinessProfile`` with a ``.get(dotted_path, default)`` helper
         AND a ``.data`` dict (real Django model).
      2. ``SimpleNamespace(data={...})`` — used by the adapter unit tests.
      3. Plain ``dict``.
    """
    if profile is None:
        return default

    # Prefer the model's dotted-path helper when it exists and behaves like
    # a 2-arg getter (not the dict.get of a plain dict).
    getter = getattr(profile, "get", None)
    if callable(getter) and not isinstance(profile, dict):
        try:
            value = getter(dotted, default)
        except TypeError:
            value = None
        if value is not default and value is not None:
            return value

    data = getattr(profile, "data", None)
    if data is None and isinstance(profile, dict):
        data = profile
    node: Any = data if isinstance(data, dict) else {}
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _coerce_role_to_casing_name(role: str | None) -> str:
    if not role:
        return "production"
    role = str(role).lower()
    if role in ("surface", "intermediate", "production", "liner"):
        return role
    return "production"


def _build_casings(casing_record: list[dict]) -> list[Casing]:
    casings: list[Casing] = []
    for row in casing_record:
        if not isinstance(row, dict):
            continue
        shoe = row.get("bottom_ft") or row.get("shoe_ft")
        if shoe is None:
            continue
        casings.append(
            Casing(
                name=_coerce_role_to_casing_name(row.get("role") or row.get("name")),
                size_in=float(row.get("size_in") or 0.0001) or 0.0001,
                weight_lb_ft=row.get("weight_ppf") or row.get("weight_lb_ft"),
                grade=row.get("grade"),
                shoe_ft=float(shoe),
            )
        )
    return casings


# Mapping from kernel casing role/name -> RRC W-3A "Type" radio value.
# Only Casing/Liner are supported. Tubing is not emitted by the kernel today
# but kept here for completeness against the form's three radio options.
_RRC_CASING_TYPE_BY_ROLE: dict[str, str] = {
    "surface": "Casing",
    "intermediate": "Casing",
    "production": "Casing",
    "liner": "Liner",
}


# --- RRC fractional-inch formatter --------------------------------------
# RRC's W-3A Casing Record validates `casing_size` and `hole_size` as a
# fractional-inch string ("9 5/8", "13 3/8", etc.), NOT a decimal. Decimal
# values like "13.375" (6 chars) trip RRC's 5-char Text length validator
# AND wouldn't parse as the form's expected shape. See
# memory `project_rrc_casing_size_format.md`.
_EIGHTHS: dict[float, str] = {
    0.0: "",
    0.125: "1/8",
    0.25: "1/4",
    0.375: "3/8",
    0.5: "1/2",
    0.625: "5/8",
    0.75: "3/4",
    0.875: "7/8",
}


def _int_or_none(v) -> int | None:
    """Coerce a numeric value to int (rounded), or None.

    RRC's Casing Record numeric fields (depth, top_of_cement,
    cement_sacks, anticipated_recovery) have a 5-character text-length
    validator. Floats like 1200.0 stringify to "1200.0" (6 chars) and
    trigger the validator; integers stringify to "1200" (4 chars). The
    kernel emits floats for depths but RRC depths are always whole feet,
    so rounding to int is safe.
    """
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _to_rrc_fraction(value: float | int | None) -> str | None:
    """Convert a decimal inch value to RRC's fractional inch string.

    Returns None for None input. Snaps to the nearest eighth within a
    0.005 tolerance.

    Examples:
        13.375 -> "13 3/8"
        9.625  -> "9 5/8"
        7.0    -> "7"
        0.5    -> "1/2"
        None   -> None

    Raises:
        ValueError: if the value is negative or cannot be snapped to an
            eighth within tolerance (better to fail loudly during adapter
            wiring than to ship a row that RRC will silently reject).
    """
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"Casing size cannot be negative: {value}")
    whole = int(value)
    frac = round(value - whole, 4)
    matched: str | None = None
    for eighth, label in _EIGHTHS.items():
        if abs(frac - eighth) < 0.005:
            matched = label
            break
    if matched is None:
        raise ValueError(
            f"Casing size {value} not snappable to an eighth (frac={frac})"
        )
    if whole == 0 and matched == "":
        return "0"
    if whole == 0:
        return matched
    if matched == "":
        return str(whole)
    return f"{whole} {matched}"


def _build_rrc_casing_rows(casing_record: list[dict]) -> list[dict]:
    """Transform raw geometry casing_record dicts into RRC W-3A Casing Record rows.

    Output shape (one dict per row) consumed by
    ``RrcFormAutomator._fill_casing_record``:

    - type: "Casing" | "Liner" | "Tubing"
    - casing_size: str (inches, e.g. "9.625")
    - hole_size: str | None (inches)
    - depth: float (feet, the bottom/shoe of the string)
    - top_of_cement: float | None
    - cement_sacks: int | None         (kernel doesn't emit yet; getattr-style)
    - anticipated_recovery: float | None (same)
    - top_of_cement_method: "Calculated" (hardcoded)
    - sub_type: str | None             (plan-review UI, future)
    - additives: str | None            (plan-review UI, future)

    Rows whose role doesn't map to a known RRC type are skipped.
    """
    rows: list[dict] = []
    for raw in casing_record:
        if not isinstance(raw, dict):
            continue
        role = (raw.get("role") or raw.get("name") or "").lower().strip()
        rrc_type = _RRC_CASING_TYPE_BY_ROLE.get(role)
        if not rrc_type:
            continue

        depth = raw.get("bottom_ft") or raw.get("shoe_ft")
        if depth is None:
            continue
        # RRC's depth field has a 5-char text-length validator; floats like
        # 1200.0 stringify to "1200.0" (6 chars) and fail. Coerce to int.
        depth_int = _int_or_none(depth)
        if depth_int is None:
            continue

        # RRC requires fractional-inch strings (e.g. "9 5/8"), not decimals.
        # _to_rrc_fraction returns None for None input, "" for an unsnappable
        # zero, and raises ValueError on unsnappable non-eighths — let that
        # bubble up so bad geometry is caught immediately rather than
        # producing a silently-rejected RRC row.
        size_in_raw = raw.get("size_in")
        try:
            size_in_f = float(size_in_raw) if size_in_raw is not None else None
        except (TypeError, ValueError):
            size_in_f = None
        casing_size = _to_rrc_fraction(size_in_f) or ""

        hole_size_raw = raw.get("hole_size_in")
        try:
            hole_size_f = float(hole_size_raw) if hole_size_raw is not None else None
        except (TypeError, ValueError):
            hole_size_f = None
        hole_size = _to_rrc_fraction(hole_size_f)

        # All numeric fields go through _int_or_none — same 5-char validator
        # applies form-wide on these inputs.
        top_of_cement = _int_or_none(raw.get("cement_top_ft"))

        # Defensive reads for keys the kernel does not emit yet (follow-up
        # tasks #5 + #6). Once they land, no second pass is needed.
        cement_sacks = _int_or_none(raw.get("cement_sacks"))
        anticipated_recovery = _int_or_none(raw.get("anticipated_recovery"))
        sub_type = raw.get("sub_type")
        additives = raw.get("additives")

        rows.append(
            {
                "type": rrc_type,
                "casing_size": casing_size,
                "hole_size": hole_size,
                "depth": depth_int,
                "top_of_cement": top_of_cement,
                "cement_sacks": cement_sacks,
                "anticipated_recovery": anticipated_recovery,
                "top_of_cement_method": "Calculated",
                "sub_type": sub_type,
                "additives": additives,
            }
        )
    return rows


def _build_existing_tools(mechanical_barriers: list[dict]) -> list[ExistingTool]:
    tools: list[ExistingTool] = []
    for row in mechanical_barriers:
        if not isinstance(row, dict):
            continue
        depth = row.get("depth_ft") or row.get("md_ft")
        tool_type = (row.get("type") or row.get("tool_type") or "").upper()
        if tool_type not in {"CIBP", "RETAINER", "DV", "PACKER", "PLUG", "BRIDGE_PLUG"}:
            # Normalise common alias.
            if tool_type in {"BRIDGEPLUG"}:
                tool_type = "BRIDGE_PLUG"
            else:
                continue
        if depth is None:
            continue
        tools.append(
            ExistingTool(
                tool_type=tool_type,
                md_ft=float(depth),
            )
        )
    return tools


def _normalize_plug_thickness(
    top_ft: float,
    bottom_ft: float,
    prev_bottom_ft: float | None = None,
    next_top_ft: float | None = None,
) -> tuple[float, float]:
    """Adjust ``top_ft`` and/or ``bottom_ft`` to satisfy the RRC W-3A
    Plugging Proposal's minimum-thickness validator. Returns the adjusted
    ``(top_ft, bottom_ft)`` pair.

    RRC rule (verbatim from the portal's row-edit help-block):
        Required minimum plug thickness is 100ft, plus 10% for each 1000ft
        of depth from ground surface to bottom of plug.

    Encoded as: ``min_thickness(b) = 100 + 10 * (b / 1000)``.

    Strategy (in order):
      1. **Raise top** — preferred. Preserves the bottom anchor (casing
         shoe / formation interface). Subject to: top cannot go below 0
         (surface) and cannot overlap the previous shallower plug
         (must be ``>= prev_bottom_ft``).
      2. **Lower bottom** — fallback when the top is pinned at the floor.
         Extends bottom downward toward ``next_top_ft`` (the next deeper
         plug's top, used as the ceiling). Solving
         ``new_bottom - floor_top >= 100 + 10 * (new_bottom / 1000)``
         gives ``new_bottom >= (100 + floor_top) / 0.99`` — rounded UP via
         ``math.ceil`` so we stay strictly compliant after float
         truncation.
      3. **Sandwiched** — neither strategy fits (rare; adjacent plugs too
         close). Extend bottom as far as possible toward the ceiling and
         log a warning. RRC may still reject; that's a kernel data-quality
         issue tracked in Trello card #96.

    This is an architectural shaping step on the way to the portal — same
    pattern as the casing-size fraction conversion. Original kernel
    payload in the DB is untouched; only the FormData passed to the
    filler is normalized.
    """
    def min_thickness(b: float) -> float:
        return 100 + 10 * (b / 1000)

    current_thickness = bottom_ft - top_ft
    needed = min_thickness(bottom_ft)
    if current_thickness >= needed:
        return top_ft, bottom_ft  # already compliant

    # --- Strategy 1: raise top (preserve bottom) ------------------------
    required_top = bottom_ft - needed
    floor_top = max(prev_bottom_ft or 0, 0)
    if required_top >= floor_top:
        logger.info(
            "📐 Plug thickness normalized (raised top): bottom=%s "
            "top %s → %s (min_thickness=%.1f)",
            bottom_ft,
            top_ft,
            required_top,
            needed,
        )
        return required_top, bottom_ft

    # --- Strategy 2: lower bottom (top pinned at floor_top) -------------
    # Need: new_bottom - floor_top >= 100 + 10*(new_bottom/1000)
    # ⇒    new_bottom * (1 - 0.01) >= 100 + floor_top
    # ⇒    new_bottom >= (100 + floor_top) / 0.99
    # Use math.ceil so float truncation doesn't drop us below the
    # threshold after the cast.
    target_bottom = float(math.ceil((100 + floor_top) / 0.99))
    ceiling_bottom = next_top_ft if next_top_ft is not None else float("inf")
    if target_bottom <= ceiling_bottom:
        logger.info(
            "📐 Plug thickness normalized (extended bottom): top=%s "
            "bottom %s → %s (min_thickness=%.1f)",
            floor_top,
            bottom_ft,
            target_bottom,
            min_thickness(target_bottom),
        )
        return floor_top, target_bottom

    # --- Strategy 3: sandwiched — clamp ---------------------------------
    logger.warning(
        "⚠️  Plug thickness normalization clamped — sandwiched: "
        "top=%s bottom_target=%s ceiling=%s. Tracked: Trello #96.",
        floor_top,
        target_bottom,
        ceiling_bottom,
    )
    return floor_top, float(ceiling_bottom)


def _build_plug_rows(steps: list[dict]) -> list[_PlugRow]:
    rows: list[_PlugRow] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        # The orchestrator emits various 'type' values (plug, cement_plug, etc).
        # Accept anything that carries a cement_class + top/bottom depths.
        if step.get("cement_class") is None and step.get("type") not in ("plug", "cement_plug"):
            continue
        top = step.get("top_ft")
        bottom = step.get("bottom_ft")
        if top is None or bottom is None:
            continue
        rows.append(
            _PlugRow(
                top_ft=float(top),
                bottom_ft=float(bottom),
                cement_class=str(step.get("cement_class") or "A"),
                sacks=int(step.get("sacks") or 0),
                formation=step.get("formation"),
                type=step.get("type"),
                plug_type=step.get("plug_type"),
            )
        )

    # Normalize each plug's thickness to satisfy RRC's minimum-thickness
    # rule. Sort shallow→deep (by bottom_ft ASC) so we can track both:
    #   - prev_bottom_ft: the SHALLOWER plug's bottom — used as the FLOOR
    #     for raising this plug's top (Strategy 1).
    #   - next_top_ft: the next DEEPER plug's CURRENT top — used as the
    #     CEILING for extending this plug's bottom (Strategy 2).
    #
    # Concrete: plugs A(0–50), B(1500–1600), C(3000–3100). When normalizing
    # B, prev_bottom=50 (A's bottom) and next_top=3000 (C's top).
    # We process shallow→deep so by the time we reach C, B may have
    # already been adjusted; but C reads B's ORIGINAL top here (next_top
    # snapshot), which is fine — we're only concerned about NOT crashing
    # into a plug we haven't yet processed. The shallower plug we already
    # processed contributes its (possibly extended) bottom via prev_bottom
    # on the NEXT iteration.
    rows.sort(key=lambda r: r.bottom_ft)

    # Pre-compute the next-deeper plug's top for each index. After the
    # sort, next_tops[i] = rows[i+1].top_ft; last row has no successor.
    next_tops: list[float | None] = [
        rows[i + 1].top_ft if i + 1 < len(rows) else None
        for i in range(len(rows))
    ]

    adjusted: list[_PlugRow] = []
    prev_bottom: float | None = None
    for i, r in enumerate(rows):
        new_top, new_bottom = _normalize_plug_thickness(
            r.top_ft, r.bottom_ft, prev_bottom, next_tops[i]
        )
        if new_top != r.top_ft or new_bottom != r.bottom_ft:
            r = dataclasses.replace(r, top_ft=new_top, bottom_ft=new_bottom)
        adjusted.append(r)
        prev_bottom = r.bottom_ft

    # Existing downstream expectation: rows sorted by top_ft (the test
    # ``test_plug_rows_three_entries_sorted_with_cement_class`` asserts
    # sorted depths). Preserve that.
    adjusted.sort(key=lambda r: r.top_ft)
    return adjusted


def _deepest_shoe(casings: list[Casing]) -> float | None:
    if not casings:
        return None
    return max(c.shoe_ft for c in casings)


def _deepest_production_shoe(casings: list[Casing]) -> float | None:
    prod = [c for c in casings if c.name == "production"]
    if not prod:
        return None
    return max(c.shoe_ft for c in prod)


def plan_snapshot_to_form_data(
    snap, attestation, profile, *, enforce_profile: bool = True
) -> tuple[FormData, _WellRecordLite]:
    """Pure transformer: PlanSnapshot + request attestation + tenant profile → (FormData, WellRecord).

    Args:
        enforce_profile: when True (default — used by the submit view and the
            adapter unit tests), missing required profile keys raise
            ``BusinessProfileIncomplete``. The Celery task path sets this
            False because the submit view has already gated on profile
            completeness; this avoids a redundant raise inside the worker
            that would mask the real filing error.

    Raises:
        BusinessProfileIncomplete: a required profile key is missing
            (only when ``enforce_profile=True``).
        PayloadIncomplete: a required payload key is missing.
        pydantic.ValidationError: relayed from WellRecord/Casing construction.
    """
    payload = getattr(snap, "payload", None) or {}
    if not isinstance(payload, dict):
        raise PayloadIncomplete(field="payload")

    inputs_summary = payload.get("inputs_summary")
    if not isinstance(inputs_summary, dict) or "api14" not in inputs_summary:
        raise PayloadIncomplete(field="inputs_summary.api14")

    geometry = payload.get("geometry") or {}
    # If geometry is present but lacks the casing record, that's a hard
    # validation failure (the W-3A form cannot be assembled without it).
    # A wholly missing geometry block is tolerated here — the upstream
    # orchestrator gates production filings on a richer payload, and the
    # task-level test harness submits minimal payloads with a mocked filler.
    if "geometry" in payload and isinstance(payload["geometry"], dict) and "casing_record" not in geometry:
        raise PayloadIncomplete(field="geometry.casing_record")

    # Required profile keys per plan section 0.
    if enforce_profile:
        assert_profile_complete(profile, _REQUIRED_PROFILE_FIELDS)

    well = getattr(snap, "well", None)

    api_canonical = _normalize_api(inputs_summary.get("api14") or getattr(well, "api14", None))

    casing_record = geometry.get("casing_record") or []
    formation_tops = list(geometry.get("formation_tops") or [])
    mechanical_barriers = geometry.get("mechanical_barriers") or []

    casings = _build_casings(casing_record)
    existing_tools = _build_existing_tools(mechanical_barriers)
    plug_rows = _build_plug_rows(payload.get("steps") or [])

    total_depth = _deepest_shoe(casings)
    casing_depth = _deepest_production_shoe(casings) or total_depth

    cementing_company = _profile_get(profile, f"{_PROFILE_PREFIX}.cementing_company_name")
    contact_phone = _profile_get(profile, f"{_PROFILE_PREFIX}.contact_phone")
    contact_email = _profile_get(profile, f"{_PROFILE_PREFIX}.contact_email")
    cementing_company_address = _profile_get(profile, f"{_PROFILE_PREFIX}.cementing_company_address")
    cementing_company_p5 = _profile_get(profile, f"{_PROFILE_PREFIX}.cementing_company_p5")
    contact_ext = _profile_get(profile, f"{_PROFILE_PREFIX}.contact_ext")
    offset_days = _profile_get(
        profile, f"{_PROFILE_PREFIX}.default_plugging_date_offset_days", default=30
    )
    try:
        offset_days = int(offset_days) if offset_days is not None else 30
    except (TypeError, ValueError):
        offset_days = 30

    anticipated_date = _dt.date.today() + _dt.timedelta(days=offset_days)

    submitter_name = (attestation or {}).get("submitter_name")
    submitter_title = (attestation or {}).get("submitter_title")

    district = payload.get("district") or getattr(well, "district", None) or ""

    calculated = {
        "operator_name": getattr(well, "operator_name", None),
        "lease_name": getattr(well, "lease_name", None),
        "field_name": getattr(well, "field_name", None),
        "district": district,
        "permit_number": getattr(well, "permit_number", None),
        "cementing_company_name": cementing_company,
        "contact_phone": contact_phone,
        "contact_email": contact_email,
        "submitter_name": submitter_name,
        "submitter_title": submitter_title,
        "anticipated_plugging_date": anticipated_date.isoformat(),
        "total_depth": total_depth,
        "casing_depth": casing_depth,
        "formation_tops": formation_tops,
        # plug_rows: int-stringification at the emit boundary so the RRC
        # portal's 5-character text-length validator on the Bottom/Top/Sacks
        # number inputs accepts the values. Same pattern as the Casing
        # depth/sacks fields (see _build_rrc_casing_rows just above). The
        # _PlugRow dataclass keeps floats internally (normalizer math
        # stays in floats); only the dict shipped to the filler is
        # int-cast.
        "plug_rows": [
            {
                "top_ft": _int_or_none(r.top_ft),
                "bottom_ft": _int_or_none(r.bottom_ft),
                "cement_class": r.cement_class,
                "sacks": _int_or_none(r.sacks),
                "formation": r.formation,
                "type": r.type,
                "plug_type": r.plug_type,
            }
            for r in plug_rows
        ],
        # RRC W-3A Casing Record rows, one dict per row. Consumed by
        # RrcFormAutomator._fill_casing_record. Empty list when geometry has
        # no casing_record (the form's grid simply renders empty).
        "casings": _build_rrc_casing_rows(casing_record),
    }

    # Optional profile fields: only emit when the profile actually supplies a value
    # (non-None).  This keeps "key in calculated_data" a reliable signal downstream
    # (e.g. the EXT-fill guard in _fill_contact_information checks for key presence).
    if cementing_company_address is not None:
        calculated["cementing_company_address"] = cementing_company_address
    if cementing_company_p5 is not None:
        calculated["cementing_company_p5"] = cementing_company_p5
    if contact_ext is not None:
        calculated["contact_ext"] = contact_ext

    # Infer well type from RRC extraction documents (tenant-agnostic public records).
    # Use the full api14 so the lookup matches however ExtractedDocument stores the number.
    _api14_full = str(inputs_summary.get("api14") or getattr(well, "api14", "") or "")
    well_type = get_well_type(_api14_full)
    if well_type:
        calculated["well_type"] = well_type

    form_data = FormData(
        api_number=api_canonical,
        form_type="W-3A",
        calculated_data=calculated,
        client_metadata={
            "plan_snapshot_id": str(getattr(snap, "id", "")),
            "tenant_id": str(getattr(snap, "tenant_id", "")),
            # Full (unstripped) api14 for downstream services that need the
            # 10/14-digit form — e.g. GAU PDF lookup under
            # MEDIA_ROOT/rrc/completions/<api_digits>/.  api_number is the
            # 8-digit RRC form value and is NOT suitable for that lookup.
            "api14_full": str(inputs_summary.get("api14") or getattr(well, "api14", "") or ""),
        },
    )

    well_record = _WellRecordLite(
        api_number=api_canonical,
        operator_name=getattr(well, "operator_name", None),
        lease_name=getattr(well, "lease_name", None),
        field_name=getattr(well, "field_name", None),
        district=str(district) if district else None,
        permit_number=getattr(well, "permit_number", None),
        casing_program=casings,
        existing_tools=existing_tools,
        formation_tops=formation_tops,
        plug_rows=plug_rows,
        total_depth_ft=total_depth,
    )

    return form_data, well_record
