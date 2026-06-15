"""
Notification helper for PortalCredential auth-failure circuit-breaker events.

Called by BE2's task code (sync_portal_filings / fetch_filing_remarks) after
record_login_failure() is called on a credential.  Never called directly from
within this module — the task layer calls it so it can decide timing/dedup.

Import pattern: import inside the function body to avoid circular imports with
apps.intelligence.models and apps.tenants.models at module load time.

--- Tenant-id encoding (READ THIS BEFORE EDITING) ---------------------------
PortalCredential.tenant_id is a UUIDField whose value is produced by
apps.intelligence.views._get_tenant_id():

    str(uuid.UUID(int=tenant.id))          # views.py:88-89

i.e. the *integer* Tenant primary key (BigAutoField) packed into a UUID. This
encoding is reversible:  tenant.id == uuid.UUID(str(credential.tenant_id)).int

To resolve the tenant's users we therefore DECODE credential.tenant_id back to
the integer PK, fetch the Tenant, and use the canonical user lookup
``tenant.user_set.filter(is_active=True)`` — the same relation used by
apps.tenants.tasks.check_token_usage_thresholds (tasks.py:52).

The Notification row is written with ``tenant_id=tenant.id`` (the RAW integer
PK).  Notification.tenant_id is a FlexTenantIdField (BigInteger), and the read
path (apps.tenants.views NotificationViewSet.get_queryset, views.py:825/833)
filters on the raw integer ``tenant.id`` — so writing the raw int is what makes
the notification actually visible to the frontend.
"""
import logging
import uuid

logger = logging.getLogger(__name__)


def _resolve_tenant(tenant_id):
    """
    Resolve the real Tenant from a PortalCredential.tenant_id value.

    Handles the canonical UUID(int=pk) encoding produced by
    views._get_tenant_id(), and also tolerates a raw-int-like value.
    Returns the Tenant instance or None (never raises).
    """
    from apps.tenants.models import Tenant

    candidate_pks = []

    # 1. Canonical path: tenant_id is a UUID encoding the integer PK.
    try:
        candidate_pks.append(uuid.UUID(str(tenant_id)).int)
    except (ValueError, AttributeError, TypeError):
        pass

    # 2. Fallback: tenant_id might already be a raw integer (or int-like string).
    try:
        candidate_pks.append(int(tenant_id))
    except (ValueError, TypeError):
        pass

    for pk in candidate_pks:
        try:
            return Tenant.objects.get(pk=pk)
        except (Tenant.DoesNotExist, ValueError, OverflowError):
            continue

    return None


def notify_credential_needs_attention(credential) -> int:
    """
    Create a Notification row for each active user in the credential's tenant.

    Notification content depends on auth_state:
      - 'locked'      -> notif_type='error',   tells user RRC locked the account
      - anything else -> notif_type='warning',  tells user to update their password

    action_url points to the filing tracker / credentials page at '/filing-tracker'.

    Returns the number of Notification rows created.
    If the tenant or its users cannot be resolved, returns 0 without raising.
    """
    from apps.tenants.models import Notification

    auth_state = getattr(credential, 'auth_state', 'needs_reauth')
    agency = getattr(credential, 'agency', 'RRC')

    if auth_state == 'locked':
        notif_type = 'error'
        verb = f'credential_locked_{agency}'
        message = (
            f"Your {agency} portal account has been locked due to too many failed "
            "login attempts. Please visit the RRC portal to reset your password, "
            "then update your credentials in the Filing Tracker."
        )
    else:
        notif_type = 'warning'
        verb = f'credential_needs_reauth_{agency}'
        message = (
            f"Your {agency} portal login credentials appear to be invalid or expired. "
            "Please update your username and password in the Filing Tracker settings."
        )

    action_url = '/filing-tracker'

    tenant = _resolve_tenant(credential.tenant_id)
    if tenant is None:
        logger.warning(
            "notify_credential_needs_attention: could not resolve tenant for "
            "credential.tenant_id=%s — skipping notification creation.",
            credential.tenant_id,
        )
        return 0

    # Canonical user lookup: the M2M reverse relation on Tenant
    # (same as apps.tenants.tasks.check_token_usage_thresholds, tasks.py:52).
    try:
        users = list(tenant.user_set.filter(is_active=True))
    except Exception as exc:
        logger.warning(
            "notify_credential_needs_attention: failed to load users for tenant %s: %s",
            tenant.id,
            exc,
        )
        users = []

    if not users:
        logger.warning(
            "notify_credential_needs_attention: no active users for tenant %s — "
            "skipping notification creation.",
            tenant.id,
        )
        return 0

    created = 0
    for user in users:
        try:
            Notification.objects.create(
                user=user,
                # Raw integer PK — matches the Notification read path
                # (NotificationViewSet filters tenant_id=tenant.id).
                tenant_id=tenant.id,
                verb=verb,
                message=message,
                notif_type=notif_type,
                action_url=action_url,
            )
            created += 1
        except Exception as exc:
            logger.warning(
                "notify_credential_needs_attention: failed to create notification "
                "for user %s tenant %s: %s",
                user.pk,
                tenant.id,
                exc,
            )

    return created
