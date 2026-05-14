import logging
from celery import shared_task
from django.utils import timezone
from django_tenants.utils import get_tenant_model

from apps.tenants.services.email_service import send_welcome_email, send_usage_alert_email
from apps.tenants.services.usage_tracker import get_monthly_token_usage

logger = logging.getLogger(__name__)


@shared_task
def send_welcome_email_task(user_id: int, temp_password: str) -> None:
    from apps.tenants.models import User
    try:
        user = User.objects.get(id=user_id)
        send_welcome_email(user, temp_password)
    except User.DoesNotExist:
        logger.warning("send_welcome_email_task: user %s not found", user_id)


@shared_task
def check_token_usage_thresholds() -> None:
    """
    Hourly task: check each active tenant's token usage and create a
    Notification (+ send alert email) when the 50% or 75% threshold is
    crossed for the first time in the current billing month.

    Deduplication is handled via the Notification.verb field, which
    encodes both the threshold level and the billing month, e.g.
    "usage_50pct_2026-05".
    """
    from apps.tenants.models import Notification

    Tenant = get_tenant_model()
    tenants = Tenant.objects.exclude(schema_name='public')

    thresholds = [
        (50, 'usage_50pct', 'warning'),
        (75, 'usage_75pct', 'error'),
    ]
    billing_month = timezone.now().strftime('%Y-%m')

    for tenant in tenants:
        tenant_uuid = tenant.id
        usage = get_monthly_token_usage(tenant)
        percentage = usage.get('percentage', 0)
        tokens_used = usage.get('tokens_used', 0)
        monthly_budget = usage.get('monthly_budget', 0)

        # Get all users enrolled in this tenant
        users = list(tenant.user_set.filter(is_active=True))

        for threshold_pct, base_verb, notif_type in thresholds:
            if percentage < threshold_pct:
                continue

            verb = f"{base_verb}_{billing_month}"

            # Dedup: skip if a notification with this verb already exists for this tenant
            already_sent = Notification.objects.filter(
                tenant_id=tenant_uuid, verb=verb
            ).exists()
            if already_sent:
                continue

            # Create notifications and send emails for all active users
            for user in users:
                Notification.objects.create(
                    user=user,
                    tenant_id=tenant_uuid,
                    verb=verb,
                    message=(
                        f"Your team has used {percentage:.0f}% "
                        f"({tokens_used:,} tokens) of your "
                        f"{monthly_budget:,} monthly token budget."
                    ),
                    notif_type=notif_type,
                    action_url='/settings/usage',
                )
                send_usage_alert_email(user, percentage, tokens_used, monthly_budget)
