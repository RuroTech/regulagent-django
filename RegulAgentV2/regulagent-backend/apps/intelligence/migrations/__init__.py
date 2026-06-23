"""
Migration package for apps.intelligence.

Exports backfill_credential_users so both the data migration
(0009_backfill_portalcredential_users) and the test suite can call it
directly without duplicating the logic.
"""
import logging

logger = logging.getLogger(__name__)


def backfill_credential_users(tenant_uuid, credential, owner_user):
    """
    Assign *owner_user* to *credential* and mark it as the automation default,
    but ONLY if the credential does not already have a user assigned.

    This is the idempotent unit of work called by both:
      - The 0009 data migration (which resolves owner_user from the Tenant row).
      - The test suite (which supplies owner_user directly).

    The function is deliberately attribute-based — it does not call any custom
    model methods, only sets plain fields and calls .save(), so it works with
    both real model instances and the historical proxy objects Django passes
    to RunPython migrations.

    Args:
        tenant_uuid:  The string UUID stored in credential.tenant_id (unused
                      here; kept in signature for logging context by callers).
        credential:   A PortalCredential instance (real or historical).
        owner_user:   The User instance to assign as owner.
    """
    if credential.user_id is not None:
        # Already has an owner — do not overwrite.
        logger.debug(
            "backfill_credential_users: credential %s already has user_id=%s — skipping.",
            credential.pk,
            credential.user_id,
        )
        return

    credential.user = owner_user
    credential.is_default_for_automation = True
    credential.save(update_fields=['user', 'is_default_for_automation'])
    logger.info(
        "backfill_credential_users: assigned user_id=%s as owner + automation default "
        "for credential %s (tenant_uuid=%s).",
        owner_user.pk,
        credential.pk,
        tenant_uuid,
    )
