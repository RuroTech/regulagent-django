import os
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand, CommandParser

# Resolve the district_overlays pack directory relative to this file's location.
# Structure: apps/policy_ingest/management/commands/ -> 4 parents up -> apps/
_COMMANDS_DIR = Path(__file__).resolve().parent          # .../management/commands
_APP_ROOT = _COMMANDS_DIR.parents[3]                     # .../regulagent-backend (project root / BASE_DIR)
OVERLAYS_DIR = _APP_ROOT / "apps" / "policy" / "packs" / "tx" / "w3a" / "district_overlays"


class Command(BaseCommand):
    help = "Ingest district overlay YAML files into DistrictOverlay + CountyOverlay tables."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--jurisdiction",
            default="TX",
            help="Jurisdiction to ingest (default: TX). NM is not yet supported.",
        )

    def handle(self, *args, **options) -> None:
        jurisdiction: str = options["jurisdiction"].upper()

        if jurisdiction != "TX":
            self.stderr.write(
                self.style.WARNING(
                    f"NM district overlay files have a different format and are not "
                    f"yet supported. Skipping jurisdiction '{jurisdiction}'."
                )
            )
            return

        from apps.policy_ingest.models import CountyOverlay, DistrictOverlay  # noqa: PLC0415

        districts_created = 0
        districts_updated = 0
        counties_created = 0
        counties_updated = 0

        # ── Pass 1: *_county_procedures.yml ──────────────────────────────────────
        procedures_files = sorted(OVERLAYS_DIR.glob("*_county_procedures.yml"))
        for yaml_path in procedures_files:
            try:
                data = _load_yaml(yaml_path)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f"Failed to parse {yaml_path}: {exc}"))
                continue

            district_code: str = str(data.get("district", "")).strip()
            if not district_code:
                self.stderr.write(self.style.WARNING(f"No 'district' key in {yaml_path.name} – skipping."))
                continue

            requirements: dict = data.get("requirements") or {}
            preferences_raw: dict = (data.get("preferences") or {}).copy()
            plugging_chart: dict = preferences_raw.pop("plugging_chart", {}) or {}
            preferences: dict = preferences_raw

            overlay, created = DistrictOverlay.objects.update_or_create(
                jurisdiction=jurisdiction,
                district_code=district_code,
                defaults={
                    "source_file": str(yaml_path),
                    "requirements": requirements,
                    "preferences": preferences,
                    "plugging_chart": plugging_chart,
                },
            )
            if created:
                districts_created += 1
            else:
                districts_updated += 1

            for county_name, county_data in (data.get("counties") or {}).items():
                if not isinstance(county_data, dict):
                    county_data = {}
                _, c_created = CountyOverlay.objects.update_or_create(
                    district_overlay=overlay,
                    county_name=county_name,
                    defaults={
                        "requirements": county_data.get("requirements") or {},
                        "preferences": county_data.get("preferences") or {},
                        "county_procedures": county_data.get("county_procedures") or {},
                        "formation_data": county_data.get("overrides") or {},
                    },
                )
                if c_created:
                    counties_created += 1
                else:
                    counties_updated += 1

        # ── Pass 2: *__auto.yml ───────────────────────────────────────────────────
        auto_files = sorted(OVERLAYS_DIR.glob("*__auto.yml"))
        for yaml_path in auto_files:
            try:
                data = _load_yaml(yaml_path)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f"Failed to parse {yaml_path}: {exc}"))
                continue

            district_code = str(data.get("district", "")).strip()
            if not district_code:
                self.stderr.write(self.style.WARNING(f"No 'district' key in {yaml_path.name} – skipping."))
                continue

            # get_or_create: preserve existing district data from pass 1
            overlay, created = DistrictOverlay.objects.get_or_create(
                jurisdiction=jurisdiction,
                district_code=district_code,
                defaults={
                    "source_file": str(yaml_path),
                    "requirements": {},
                    "preferences": {},
                    "plugging_chart": {},
                },
            )
            if created:
                districts_created += 1

            for county_name, county_data in (data.get("counties") or {}).items():
                if not isinstance(county_data, dict):
                    county_data = {}
                formation_data: dict = county_data.get("overrides") or {}

                obj, c_created = CountyOverlay.objects.get_or_create(
                    district_overlay=overlay,
                    county_name=county_name,
                    defaults={
                        "requirements": county_data.get("requirements") or {},
                        "preferences": county_data.get("preferences") or {},
                        "county_procedures": {},
                        "formation_data": formation_data,
                        "notes": county_data.get("notes") or [],
                    },
                )
                if not c_created:
                    obj.formation_data = formation_data
                    obj.notes = county_data.get("notes") or []
                    obj.save(update_fields=["formation_data", "notes"])
                    counties_updated += 1
                else:
                    counties_created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Districts: {districts_created} created, {districts_updated} updated. "
                f"Counties: {counties_created} created, {counties_updated} updated."
            )
        )


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
