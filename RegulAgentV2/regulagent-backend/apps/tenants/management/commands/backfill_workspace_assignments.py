"""
Management command to backfill ClientWorkspace records for tenants that have none.

Usage:
    python manage.py backfill_workspace_assignments
    python manage.py backfill_workspace_assignments --dry-run
    python manage.py backfill_workspace_assignments --tenant <slug>
    python manage.py backfill_workspace_assignments --tenant <slug> --dry-run
"""
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name

from apps.tenants.models import Tenant, ClientWorkspace


class Command(BaseCommand):
    help = 'Create a default ClientWorkspace for any tenant that currently has none.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Log what would be done without making any database changes.',
        )
        parser.add_argument(
            '--tenant',
            type=str,
            default=None,
            metavar='SLUG',
            help='Limit backfill to the single tenant with this slug.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        tenant_slug = options['tenant']

        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN] No database changes will be made.\n'))

        public_schema = get_public_schema_name()

        # Build the queryset of tenants to inspect
        qs = Tenant.objects.exclude(schema_name=public_schema)
        if tenant_slug:
            qs = qs.filter(slug=tenant_slug)
            if not qs.exists():
                raise CommandError(f"No tenant found with slug '{tenant_slug}'.")

        created_count = 0
        skipped_count = 0

        for tenant in qs:
            has_workspace = ClientWorkspace.objects.filter(tenant=tenant).exists()

            if has_workspace:
                self.stdout.write(
                    f'  SKIP  {tenant.slug} ({tenant.name}) — workspace already exists'
                )
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(
                    f'  WOULD CREATE  {tenant.slug} ({tenant.name})'
                )
                created_count += 1
            else:
                ClientWorkspace.objects.create(
                    tenant=tenant,
                    name=tenant.name,
                    is_active=True,
                )
                self.stdout.write(
                    self.style.SUCCESS(f'  CREATED  {tenant.slug} ({tenant.name})')
                )
                created_count += 1

        self.stdout.write('')
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'[DRY RUN] Would create {created_count} workspace(s); '
                    f'{skipped_count} tenant(s) already had workspaces.'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Done. Created {created_count} workspace(s); '
                    f'{skipped_count} tenant(s) already had workspaces.'
                )
            )
