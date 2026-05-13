import logging
from celery import shared_task

from apps.tenants.services.email_service import send_welcome_email

logger = logging.getLogger(__name__)


@shared_task
def send_welcome_email_task(user_id: int, temp_password: str) -> None:
    from apps.tenants.models import User
    try:
        user = User.objects.get(id=user_id)
        send_welcome_email(user, temp_password)
    except User.DoesNotExist:
        logger.warning("send_welcome_email_task: user %s not found", user_id)
