from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_welcome_email(user, temp_password: str) -> None:
    """Send a branded HTML welcome email with plain-text fallback."""
    login_url = f"{settings.FRONTEND_URL}/signin"
    first_name = getattr(user, "first_name", None) or ""
    subject = "Welcome to RegulAgent \u2014 Your account is ready"

    # ------------------------------------------------------------------
    # Plain-text fallback
    # ------------------------------------------------------------------
    text_body = (
        f"Hi {first_name or 'there'},\n\n"
        f"Your RegulAgent account has been created and is ready to use.\n\n"
        f"----\n"
        f"Email:              {user.email}\n"
        f"Temporary Password: {temp_password}\n"
        f"----\n\n"
        f"Log in at: {login_url}\n\n"
        f"Please change your password after your first login. "
        f"Do not share your temporary password.\n\n"
        f"\u00a9 2025 RegulAgent \u00b7 automate@regulagent.ai\n"
        f"This email was sent because an account was created for you on the RegulAgent platform."
    )

    # ------------------------------------------------------------------
    # HTML body — rendered from template
    # ------------------------------------------------------------------
    tenant = getattr(user, 'tenants', None)
    if tenant is not None:
        from django_tenants.utils import get_public_schema_name
        organization_name = tenant.exclude(schema_name=get_public_schema_name()).values_list('name', flat=True).first() or 'your organization'
    else:
        organization_name = 'your organization'

    html_body = render_to_string('tenants/emails/welcome.html', {
        'first_name': first_name or 'there',
        'email': user.email,
        'temp_password': temp_password,
        'login_url': login_url,
        'organization_name': organization_name,
    })

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    msg.attach_alternative(html_body, "text/html")
    try:
        msg.send()
    except Exception:
        pass  # fail_silently equivalent
