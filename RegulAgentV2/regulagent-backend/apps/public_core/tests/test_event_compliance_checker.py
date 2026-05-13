"""
Unit tests for event_compliance_checker service.

Pure-Python service with no Django model dependencies — no @pytest.mark.django_db
decoration needed.  Each test class corresponds to one rule or cross-cutting
concern.
"""
import pytest

from apps.public_core.services.event_compliance_checker import check_events


# ---------------------------------------------------------------------------
# Policy fixtures
# ---------------------------------------------------------------------------

NM_POLICY = {
    "policy_id": "nm.c103",
    "base": {
        "citations": {"source": "NMAC 19.15.25"},
        "requirements": {
            "surface_casing_shoe_plug_min_ft": {
                "value": 100,
                "text": "Min 100 ft plug length",
                "unit": "ft",
            },
            "surface_casing_shoe_plug_min_sacks": {
                "value": 25,
                "text": "Min 25 sacks",
                "unit": "sacks",
            },
            "cement_above_cibp_min_ft": {
                "value": 100,
                "text": "100 ft cement above CIBP",
                "unit": "ft",
            },
            "woc_time_hours": {
                "value": 4,
                "text": "Min 4 hours WOC",
                "unit": "h",
            },
        },
        "cement_class": {
            "cutoff_ft": 6500,
            "shallow_class": "C",
            "deep_class": "H",
        },
    },
}

TX_POLICY = {
    "policy_id": "tx.w3a",
    "base": {
        "citations": {"source": "16 TAC §3.14"},
        "requirements": {
            "surface_casing_shoe_plug_min_ft": {
                "value": 50,
                "text": "Min 50 ft plug length",
                "unit": "ft",
            },
            "cement_above_cibp_min_ft": {
                "value": 20,
                "text": "20 ft cement above CIBP",
                "unit": "ft",
            },
            "woc_time_hours": {
                "value": 8,
                "text": "Min 8 hours WOC",
                "unit": "h",
            },
        },
        "cement_class": {
            "cutoff_ft": 6500,
            "shallow_class": "C",
            "deep_class": "H",
        },
    },
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_parse_result(events, day_number=1):
    return {
        "days": [
            {
                "day_number": day_number,
                "work_date": "2026-01-15",
                "events": events,
            }
        ]
    }


def _flags_with_rule(result, rule_id):
    """Return all flags matching a specific rule_id."""
    return [f for f in result["flags"] if f["rule_id"] == rule_id]


# ---------------------------------------------------------------------------
# Rule: evt_cement_class_depth
# ---------------------------------------------------------------------------

class TestCementClassDepth:
    def test_cement_class_violation_deep_well(self):
        """Class C cement at 8000 ft (below 6500 ft cutoff) → violation."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 7900,
                "depth_bottom_ft": 8000,
                "cement_class": "C",
                "sacks": 30,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_cement_class_depth")
        assert len(flags) == 1
        assert flags[0]["severity"] == "violation"

    def test_cement_class_ok_shallow_well(self):
        """Class C cement at 3000 ft (above cutoff) → no class-depth flag."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 2900,
                "depth_bottom_ft": 3000,
                "cement_class": "C",
                "sacks": 30,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_cement_class_depth")
        assert flags == []

    def test_cement_class_ok_deep_with_class_h(self):
        """Class H cement at 8000 ft → no class-depth flag."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 7900,
                "depth_bottom_ft": 8000,
                "cement_class": "H",
                "sacks": 30,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_cement_class_depth")
        assert flags == []


# ---------------------------------------------------------------------------
# Rule: evt_min_plug_length
# ---------------------------------------------------------------------------

class TestMinPlugLength:
    def test_min_plug_length_nm_violation(self):
        """NM: 50 ft plug is below the 100 ft minimum → warning."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5050,
                "cement_class": "C",
                "sacks": 30,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_min_plug_length")
        assert len(flags) == 1
        assert flags[0]["severity"] == "warning"

    def test_min_plug_length_tx_ok(self):
        """TX: 50 ft plug meets the 50 ft minimum → no flag for this rule."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5050,
                "cement_class": "C",
                "sacks": 30,
            }
        ]
        result = check_events(_make_parse_result(events), TX_POLICY, "TX")
        flags = _flags_with_rule(result, "evt_min_plug_length")
        assert flags == []


# ---------------------------------------------------------------------------
# Rule: evt_min_sacks (NM only)
# ---------------------------------------------------------------------------

class TestMinSacks:
    def test_min_sacks_nm_violation(self):
        """NM: 20 sacks is below the 25-sack minimum → warning."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5100,
                "cement_class": "C",
                "sacks": 20,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_min_sacks")
        assert len(flags) == 1
        assert flags[0]["severity"] == "warning"

    def test_min_sacks_tx_not_applicable(self):
        """TX: sack minimum doesn't exist — 20 sacks raises no evt_min_sacks flag."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5100,
                "cement_class": "C",
                "sacks": 20,
            }
        ]
        result = check_events(_make_parse_result(events), TX_POLICY, "TX")
        flags = _flags_with_rule(result, "evt_min_sacks")
        assert flags == []


# ---------------------------------------------------------------------------
# Rule: evt_woc_duration
# ---------------------------------------------------------------------------

class TestWocDuration:
    def test_woc_duration_violation_nm(self):
        """NM: 2-hour WOC is below the 4-hour minimum → violation."""
        events = [{"event_type": "woc", "woc_hours": 2}]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_woc_duration")
        assert len(flags) == 1
        assert flags[0]["severity"] == "violation"

    def test_woc_duration_violation_tx(self):
        """TX: 6-hour WOC is below the 8-hour minimum → violation."""
        events = [{"event_type": "woc", "woc_hours": 6}]
        result = check_events(_make_parse_result(events), TX_POLICY, "TX")
        flags = _flags_with_rule(result, "evt_woc_duration")
        assert len(flags) == 1
        assert flags[0]["severity"] == "violation"

    def test_woc_duration_ok_nm(self):
        """NM: 5-hour WOC meets the 4-hour minimum → no evt_woc_duration flag."""
        events = [{"event_type": "woc", "woc_hours": 5}]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_woc_duration")
        assert flags == []


# ---------------------------------------------------------------------------
# Rule: evt_cibp_cap
# ---------------------------------------------------------------------------

class TestCibpCap:
    def test_cibp_cap_warning_no_cement(self):
        """Bridge plug set with no subsequent cement event → warning."""
        events = [
            {
                "event_type": "set_bridge_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5000,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_cibp_cap")
        assert len(flags) == 1
        assert flags[0]["severity"] == "warning"

    def test_cibp_cap_ok_with_cement(self):
        """Bridge plug followed by cement plug whose bottom is within 50 ft of the CIBP top → no warning."""
        events = [
            {
                "event_type": "set_bridge_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5000,
            },
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 4900,
                "depth_bottom_ft": 5010,  # within 50 ft of CIBP top (5000)
                "cement_class": "C",
                "sacks": 30,
            },
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_cibp_cap")
        assert flags == []


# ---------------------------------------------------------------------------
# Rule: evt_missing_depths
# ---------------------------------------------------------------------------

class TestMissingDepths:
    def test_missing_depths_warning(self):
        """Operational event with both depths null → warning."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": None,
                "depth_bottom_ft": None,
                "cement_class": "C",
                "sacks": 30,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_missing_depths")
        assert len(flags) == 1
        assert flags[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Rule: evt_missing_sacks
# ---------------------------------------------------------------------------

class TestMissingSacks:
    def test_missing_sacks_info(self):
        """Cement plug with null sacks → info flag."""
        events = [
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 5000,
                "depth_bottom_ft": 5100,
                "cement_class": "C",
                "sacks": None,
            }
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_missing_sacks")
        assert len(flags) == 1
        assert flags[0]["severity"] == "info"


# ---------------------------------------------------------------------------
# Rule: evt_missing_woc
# ---------------------------------------------------------------------------

class TestMissingWoc:
    def test_missing_woc_warning(self):
        """WOC event with null woc_hours → warning."""
        events = [{"event_type": "woc", "woc_hours": None}]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_missing_woc")
        assert len(flags) == 1
        assert flags[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Rule: evt_missing_pressure
# ---------------------------------------------------------------------------

class TestMissingPressure:
    def test_missing_pressure_info(self):
        """Pressure test with null pressure_psi → info flag."""
        events = [{"event_type": "pressure_test", "pressure_psi": None}]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        flags = _flags_with_rule(result, "evt_missing_pressure")
        assert len(flags) == 1
        assert flags[0]["severity"] == "info"


# ---------------------------------------------------------------------------
# Summary counts
# ---------------------------------------------------------------------------

class TestSummaryCounts:
    def test_summary_counts(self):
        """Mixed events → summary.violations, warnings, info totals are correct.

        Violation source: deep well with Class C cement (evt_cement_class_depth).
        Warning source:   bridge plug with no subsequent cement cap (evt_cibp_cap).
        Info source:      pressure test with no pressure value (evt_missing_pressure).
        """
        events = [
            # violation: deep well with Class C → evt_cement_class_depth
            {
                "event_type": "set_cement_plug",
                "depth_top_ft": 7900,
                "depth_bottom_ft": 8000,
                "cement_class": "C",
                "sacks": 30,
            },
            # warning: bridge plug with no cement cap → evt_cibp_cap
            {
                "event_type": "set_bridge_plug",
                "depth_top_ft": 6000,
                "depth_bottom_ft": 6000,
            },
            # info: missing pressure on pressure_test → evt_missing_pressure
            {
                "event_type": "pressure_test",
                "pressure_psi": None,
            },
        ]
        result = check_events(_make_parse_result(events), NM_POLICY, "NM")
        summary = result["summary"]
        assert summary["violations"] >= 1
        assert summary["warnings"] >= 1
        assert summary["info"] >= 1

    def test_empty_parse_result(self):
        """Empty parse_result → no flags, no errors, counts all zero."""
        result = check_events({"days": []}, NM_POLICY, "NM")
        assert result["flags"] == []
        assert result["summary"]["violations"] == 0
        assert result["summary"]["warnings"] == 0
        assert result["summary"]["info"] == 0
        assert result["summary"]["total_events_checked"] == 0


# ---------------------------------------------------------------------------
# Return-value structure
# ---------------------------------------------------------------------------

class TestReturnValueStructure:
    def test_check_events_returns_required_keys(self):
        """Top-level result must contain all required keys."""
        result = check_events({"days": []}, NM_POLICY, "NM")
        for key in ("jurisdiction", "policy_id", "checked_at", "summary", "flags"):
            assert key in result, f"Missing key: {key}"

    def test_jurisdiction_and_policy_id_propagated(self):
        """jurisdiction and policy_id are reflected in the result."""
        result = check_events({"days": []}, NM_POLICY, "NM")
        assert result["jurisdiction"] == "NM"
        assert result["policy_id"] == "nm.c103"

    def test_tx_jurisdiction_and_policy_id(self):
        """TX jurisdiction and tx.w3a policy_id are reflected correctly."""
        result = check_events({"days": []}, TX_POLICY, "TX")
        assert result["jurisdiction"] == "TX"
        assert result["policy_id"] == "tx.w3a"
