"""
Data migration: assign existing PortalCredential rows (user=NULL) to their
tenant's owner/admin user and mark them is_default_for_automation=True.

Tenant-id decode: PortalCredential.tenant_id stores str(uuid.UUID(int=tenant.id))
— the integer Tenant PK packed as a UUID.  Reverse: uuid.UUID(str(tenant_id)).int
See apps/intelligence/services/credential_notifications.py for the canonical
documentation of this encoding.

intelligence is in SHARED_APPS (public schema), so both Tenant and User tables
are accessible from this migration without any schema switching.

Resolution order for owner_user:
  1. tenant.owner  (nullable FK — the designated owner set at tenant creation)
  2. tenant.user_set.filter(is_active=True).first()  (M2M; any active member)
  3. If neither resolves, log a warning and leave user=NULL (do not crash).
"""
import logging
import uuid as _uuid

from django.db import migrations

logger = logging.getLogger(__name__)


def _resolve_tenant_pk(tenant_id_value):
    """
    Convert a PortalCredential.tenant_id value to the integer Tenant PK.

    Mirrors the canonical decode used in credential_notifications._resolve_tenant().
    Returns an integer or None.
    """
    try:
        return _uuid.UUID(str(tenant_id_value)).int
    except (ValueError, AttributeError, TypeError):
        pass
    try:
        return int(tenant_id_value)
    except (ValueError, TypeError):
        pass
    return None


def _find_owner_user(Tenant, tenant_pk):
    """
    Return the best candidate owner User for the tenant, or None.

    Uses historical model instances (passed in from apps.get_model inside the
    RunPython caller) — no custom model methods, only ORM attribute access.
    """
    try:
        tenant = Tenant.objects.get(pk=tenant_pk)
    except Tenant.DoesNotExist:
        logger.warning(
            "backfill_portalcredential_users: Tenant pk=%s not found — skipping.",
            tenant_pk,
        )
        return None

    # 1. Prefer the designated owner FK.
    if tenant.owner_id is not None:
        try:
            return tenant.owner
        except Exception as exc:
            logger.warning(
                "backfill_portalcredential_users: failed to fetch tenant.owner for "
                "tenant pk=%s: %s — falling back to user_set.",
                tenant_pk,
                exc,
            )

    # 2. Fall back to the first active M2M member.
    try:
        user = tenant.user_set.filter(is_active=True).first()
        if user is not None:
            return user
    except Exception as exc:
        logger.warning(
            "backfill_portalcredential_users: failed to query user_set for "
            "tenant pk=%s: %s",
            tenant_pk,
            exc,
        )

    logger.warning(
        "backfill_portalcredential_users: no resolvable active user for "
        "tenant pk=%s — credential will remain user=NULL.",
        tenant_pk,
    )
    return None


def backfill_forward(apps, schema_editor):
    """
    Assign every null-user PortalCredential to its tenant owner and set the
    automation-default flag.

    We import the helper from the package __init__ so the logic stays in one
    place (reused by tests).  We pass REAL model instances here (not historical
    proxies) because the helper only does attribute access + .save().
    """
    from apps.intelligence.migrations import backfill_credential_users

    PortalCredential = apps.get_model('intelligence', 'PortalCredential')
    Tenant = apps.get_model('tenants', 'Tenant')

    null_creds = list(PortalCredential.objects.filter(user__isnull=True))
    if not null_creds:
        logger.info("backfill_portalcredential_users: no null-user credentials — nothing to do.")
        return

    logger.info(
        "backfill_portalcredential_users: processing %d credential(s) with user=NULL.",
        len(null_creds),
    )

    for cred in null_creds:
        tenant_uuid = str(cred.tenant_id)
        tenant_pk = _resolve_tenant_pk(cred.tenant_id)

        if tenant_pk is None:
            logger.warning(
                "backfill_portalcredential_users: cannot decode tenant_id=%s for "
                "credential %s — skipping.",
                cred.tenant_id,
                cred.pk,
            )
            continue

        owner = _find_owner_user(Tenant, tenant_pk)
        if owner is None:
            # Already logged inside _find_owner_user; leave user=NULL.
            continue

        backfill_credential_users(tenant_uuid, cred, owner)


def backfill_reverse(apps, schema_editor):
    """
    Reverse: clear user and is_default_for_automation on all credentials that
    the forward pass would have set (i.e. those whose user matches the tenant
    owner).  In practice reverting to migration 0008 is enough — this is a
    no-op guard so Django doesn't complain about missing reverse.
    """
    PortalCredential = apps.get_model('intelligence', 'PortalCredential')
    PortalCredential.objects.filter(
        is_default_for_automation=True,
        user__isnull=False,
    ).update(user=None, is_default_for_automation=False)


class Migration(migrations.Migration):

    dependencies = [
        ("intelligence", "0008_portalcredential_user_and_default_for_automation"),
    ]

    operations = [
        migrations.RunPython(backfill_forward, reverse_code=backfill_reverse),
    ]
