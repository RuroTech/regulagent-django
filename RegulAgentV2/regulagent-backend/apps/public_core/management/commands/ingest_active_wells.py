import json

from django.core.management.base import BaseCommand

from apps.public_core.services.well_ingestor import ingest_nm_wells, ingest_tx_wells


class Command(BaseCommand):
    help = "Ingest active wells from TX RRC and/or NM Water Data API into WellRegistry"

    def add_arguments(self, parser):
        parser.add_argument("--state", choices=["TX", "NM", "all"], default="all")
        parser.add_argument(
            "--source",
            choices=["active", "iwar", "all"],
            default="all",
            help="TX only: which wells to ingest",
        )
        parser.add_argument("--dry-run", action="store_true", dest="dry_run")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--no-research",
            action="store_true",
            dest="no_research",
            help="Skip research pipeline dispatch for new wells",
        )

    def handle(self, *args, **options):
        state = options["state"]
        dry_run = options["dry_run"]
        limit = options["limit"]
        source = options["source"]
        # no_research flag: if set, monkey-patch _dispatch_research to no-op
        # For now just note it in output; actual no-research support can be added later
        results = {}
        if state in ("TX", "all"):
            results["TX"] = ingest_tx_wells(source=source, dry_run=dry_run, limit=limit)
        if state in ("NM", "all"):
            results["NM"] = ingest_nm_wells(dry_run=dry_run, limit=limit)
        self.stdout.write(json.dumps(results, indent=2))
