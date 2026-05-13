"""
Utility functions for tenant and user provisioning.

Based on TestDriven.io guide:
https://testdriven.io/blog/django-multi-tenant/#django-tenant-users
"""
from typing import Tuple

from django.db import transaction
from tenant_users.tenants.utils import create_public_tenant as tenant_users_create_public_tenant

from apps.tenants.models import Tenant, Domain, User


def create_public_tenant(
    domain_url: str = "localhost",
    owner_email: str = "admin@localhost",
    **kwargs
) -> Tuple[Tenant, Domain]:
    """
    Create the public (shared) tenant with a root user.
    
    This should be run once during initial setup.
    
    Args:
        domain_url: Domain for the public tenant (default: "localhost")
        owner_email: Email for the root superuser
        **kwargs: Additional user fields (username, password, etc.)
    
    Returns:
        Tuple of (Tenant, Domain)
    """
    return tenant_users_create_public_tenant(
        domain_url=domain_url,
        owner_email=owner_email,
        **kwargs
    )


def provision_tenant(
    tenant_name: str,
    tenant_slug: str,
    schema_name: str,
    owner: User,
    is_superuser: bool = False,
    is_staff: bool = False,
) -> Tuple[Tenant, Domain]:
    """
    Provision a new tenant with its own schema and domain.
    
    Args:
        tenant_name: Human-readable name for the tenant
        tenant_slug: URL-safe slug for the tenant
        schema_name: PostgreSQL schema name (should match slug)
        owner: User instance who will own this tenant
        is_superuser: Whether owner has superuser permissions in this tenant
        is_staff: Whether owner has staff permissions in this tenant
    
    Returns:
        Tuple of (Tenant, Domain)
    
    Example:
        >>> user = User.objects.get(email='user@example.com')
        >>> tenant, domain = provision_tenant(
        ...     tenant_name="Acme Corp",
        ...     tenant_slug="acme",
        ...     schema_name="acme",
        ...     owner=user,
        ...     is_staff=True
        ... )
    """
    with transaction.atomic():
        # Create the tenant
        tenant = Tenant.objects.create(
            name=tenant_name,
            slug=tenant_slug,
            schema_name=schema_name,
            owner=owner,
        )
        
        # Create the domain (subdomain routing)
        domain = Domain.objects.create(
            domain=f"{tenant_slug}.localhost",  # Adjust for production
            tenant=tenant,
            is_primary=True,
        )
        
        # Add the owner to the tenant with appropriate permissions (if not already)
        if owner not in tenant.user_set.all():
            try:
                tenant.add_user(
                    owner,
                    is_superuser=is_superuser,
                    is_staff=is_staff,
                )
            except Exception:
                # User already has permissions in this tenant, skip
                pass

        # Auto-create a default ClientWorkspace for this tenant
        from apps.tenants.models import ClientWorkspace
        ClientWorkspace.objects.get_or_create(
            tenant=tenant,
            name=tenant.name,
            defaults={'is_active': True},
        )

        return tenant, domain


def delete_tenant(
    tenant: Tenant,
    force: bool = False,
    backup_dir: str = None,
    skip_backup: bool = False,
    soft_delete: bool = False,
    retention_days: int = None,
    deleted_by_email: str = None,
    deletion_reason: str = None,
    use_pg_dump: bool = True,
) -> dict:
    """
    Safely delete a tenant with backup and cleanup.

    This function implements a comprehensive tenant deletion workflow:
    1. Verify tenant exists and is valid for deletion
    2. Create backup of all tenant data (using pg_dump by default)
    3. Verify backup integrity
    4. Create DeletedTenantBackup record
    5. Optionally soft-delete (mark for deletion but keep schema)
    6. Nullify public schema references
    7. Delete tenant (drops PostgreSQL schema) or schedule for later
    8. Clean up orphaned files

    Args:
        tenant: Tenant instance to delete
        force: If True, skip interactive confirmations
        backup_dir: Custom directory for backup (default: settings.TENANT_BACKUP_ROOT)
        skip_backup: If True, skip backup creation (DANGEROUS - requires force=True)
        soft_delete: If True, mark tenant as deleted but don't drop schema immediately
        retention_days: Days to retain schema before hard delete (default: settings.TENANT_RETENTION_DAYS or 30)
        deleted_by_email: Email of user initiating deletion (for audit trail)
        deletion_reason: Reason for deletion (for audit trail)
        use_pg_dump: If True, use pg_dump for backup; if False, use JSON serialization

    Returns:
        dict: Deletion summary with backup path, counts, etc.

    Raises:
        ValueError: If tenant is invalid or backup fails

    Warning:
        Unless soft_delete=True, this will permanently delete all data in the tenant's schema!
        Always ensure backups are created and verified unless explicitly skipped.
    """
    import logging
    from datetime import timedelta
    from django.utils import timezone
    from django.conf import settings
    from apps.tenants.models import DeletedTenantBackup
    from apps.tenants.services.backup_service import TenantBackupService
    from apps.tenants.services.pg_dump_service import PgDumpBackupService
    from apps.public_core.models import PlanSnapshot

    logger = logging.getLogger(__name__)

    # Safety checks
    if not tenant:
        raise ValueError("Tenant instance is required")

    if not tenant.id:
        raise ValueError("Cannot delete unsaved tenant")

    if skip_backup and not force:
        raise ValueError("skip_backup=True requires force=True for safety")

    # Set default retention period
    if retention_days is None:
        retention_days = getattr(settings, 'TENANT_RETENTION_DAYS', 30)

    logger.info(
        f"Starting deletion process for tenant: {tenant.slug} (ID: {tenant.id})"
        f" - soft_delete={soft_delete}, retention_days={retention_days}"
    )

    deletion_summary = {
        'tenant_id': str(tenant.id),
        'tenant_slug': tenant.slug,
        'tenant_name': tenant.name,
        'schema_name': tenant.schema_name,
        'backup_created': False,
        'backup_path': None,
        'backup_size_bytes': None,
        'backup_checksum': None,
        'backup_verified': False,
        'backup_record_id': None,
        'soft_delete': soft_delete,
        'scheduled_deletion_at': None,
        'public_refs_nullified': 0,
        'files_listed': 0,
        'tenant_deleted': False,
        'errors': [],
    }

    try:
        # Step 1: Create backup (unless skipped)
        backup_path = None
        backup_size = None
        backup_checksum = None

        if not skip_backup:
            logger.info(f"Creating backup for tenant {tenant.slug}...")

            if use_pg_dump:
                # Use pg_dump for database-level backup
                backup_service = PgDumpBackupService()
                backup_path, backup_size, backup_checksum = backup_service.backup_tenant_schema(
                    tenant,
                    backup_dir=backup_dir
                )
                deletion_summary['backup_created'] = True
                deletion_summary['backup_path'] = backup_path
                deletion_summary['backup_size_bytes'] = backup_size
                deletion_summary['backup_checksum'] = backup_checksum
                logger.info(
                    f"pg_dump backup created: {backup_path} "
                    f"({backup_size} bytes, checksum: {backup_checksum})"
                )

                # Step 2: Verify backup
                logger.info("Verifying pg_dump backup integrity...")
                is_valid, message = backup_service.verify_backup(backup_path)
                if not is_valid:
                    raise ValueError(f"Backup verification failed: {message}")

                deletion_summary['backup_verified'] = True
                logger.info(f"pg_dump backup verified: {message}")

            else:
                # Use JSON serialization backup (legacy)
                backup_service = TenantBackupService()
                backup_path = backup_service.backup_tenant(tenant, backup_dir=backup_dir)
                deletion_summary['backup_created'] = True
                deletion_summary['backup_path'] = backup_path
                logger.info(f"JSON backup created: {backup_path}")

                # Step 2: Verify backup
                logger.info("Verifying JSON backup integrity...")
                is_valid, message = backup_service.verify_backup(backup_path)
                if not is_valid:
                    raise ValueError(f"Backup verification failed: {message}")

                deletion_summary['backup_verified'] = True
                logger.info("JSON backup verified successfully")

                # List files for summary
                files = backup_service.list_tenant_files(tenant)
                deletion_summary['files_listed'] = len(files)
        else:
            logger.warning(f"SKIPPING BACKUP for tenant {tenant.slug} (skip_backup=True)")

        # Step 3: Create DeletedTenantBackup record
        scheduled_deletion_at = None
        if soft_delete:
            scheduled_deletion_at = timezone.now() + timedelta(days=retention_days)
            deletion_summary['scheduled_deletion_at'] = scheduled_deletion_at.isoformat()
            logger.info(f"Scheduled hard deletion at: {scheduled_deletion_at}")

        backup_record = DeletedTenantBackup.objects.create(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            tenant_name=tenant.name,
            schema_name=tenant.schema_name,
            backup_path=backup_path or '',
            backup_size_bytes=backup_size,
            backup_checksum=backup_checksum or '',
            backup_verified=deletion_summary['backup_verified'],
            verification_message=message if deletion_summary['backup_verified'] else '',
            scheduled_deletion_at=scheduled_deletion_at,
            deleted_by_email=deleted_by_email or '',
            deletion_reason=deletion_reason or '',
            metadata={
                'use_pg_dump': use_pg_dump,
                'soft_delete': soft_delete,
                'retention_days': retention_days,
            }
        )
        deletion_summary['backup_record_id'] = backup_record.id
        logger.info(f"Created DeletedTenantBackup record: {backup_record.id}")

        # Step 4: If soft delete, stop here (don't drop schema yet)
        if soft_delete:
            logger.info(
                f"Soft delete complete. Schema {tenant.schema_name} will be "
                f"retained until {scheduled_deletion_at}"
            )
            deletion_summary['tenant_deleted'] = False
            return deletion_summary

        # Step 5: Nullify public schema references
        logger.info("Nullifying public schema references...")

        # Update PlanSnapshot records that reference this tenant
        plan_snapshots_updated = PlanSnapshot.objects.filter(
            tenant_id=tenant.id
        ).update(tenant_id=None)

        deletion_summary['public_refs_nullified'] = plan_snapshots_updated
        logger.info(f"Nullified {plan_snapshots_updated} PlanSnapshot references")

        # Step 6: Delete tenant (drops schema)
        logger.info(f"Deleting tenant schema: {tenant.schema_name}")
        tenant.delete(force_drop=True)
        deletion_summary['tenant_deleted'] = True
        logger.info(f"Tenant {tenant.slug} deleted successfully")

        # Update backup record to mark hard deletion
        backup_record.hard_deleted_at = timezone.now()
        backup_record.save(update_fields=['hard_deleted_at'])

        # Note: We don't delete physical files from storage here
        # as they may be needed for recovery or audit purposes.
        # Files should be cleaned up separately if needed.

        return deletion_summary

    except Exception as e:
        error_msg = f"Tenant deletion failed: {str(e)}"
        logger.error(error_msg)
        deletion_summary['errors'].append(error_msg)
        raise


def add_user_to_tenant(
    user: User,
    tenant: Tenant,
    is_superuser: bool = False,
    is_staff: bool = False,
):
    """
    Add an existing user to a tenant with specific permissions.
    
    Args:
        user: User instance to add
        tenant: Tenant to add the user to
        is_superuser: Whether user has superuser permissions in this tenant
        is_staff: Whether user has staff permissions in this tenant
    """
    tenant.add_user(user, is_superuser=is_superuser, is_staff=is_staff)


def remove_user_from_tenant(user: User, tenant: Tenant):
    """
    Remove a user from a tenant.
    
    Args:
        user: User instance to remove
        tenant: Tenant to remove the user from
    
    Raises:
        ValidationError: If trying to remove the tenant owner
    """
    if tenant.owner == user:
        from django.core.exceptions import ValidationError
        raise ValidationError("Cannot remove the tenant owner from the tenant")
    
    tenant.remove_user(user)

