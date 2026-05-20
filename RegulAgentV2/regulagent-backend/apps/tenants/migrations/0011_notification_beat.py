"""
Data migration: register the check-token-usage-thresholds periodic task
with django-celery-beat (DatabaseScheduler).

This migration is safe to run multiple times — it uses update_or_create.
"""

from django.db import migrations


def add_beat_schedule(apps, schema_editor):
    """
    Register the token-usage threshold task with django-celery-beat.
    Runs every hour using CrontabSchedule(minute=0, hour=*).
    """
    try:
        CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
        PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    except LookupError:
        # django-celery-beat may not be installed in all environments (e.g. CI)
        return

    import json

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )

    PeriodicTask.objects.update_or_create(
        name="check-token-usage-thresholds",
        defaults={
            "task": "apps.tenants.tasks.check_token_usage_thresholds",
            "crontab": schedule,
            "kwargs": json.dumps({}),
            "enabled": True,
            "description": "Hourly: notify + email at 50% and 75% of monthly token budget.",
        },
    )


def remove_beat_schedule(apps, schema_editor):
    """Reverse: delete the periodic task (leave the crontab schedule intact)."""
    try:
        PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    except LookupError:
        return
    PeriodicTask.objects.filter(name="check-token-usage-thresholds").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0010_notification"),
    ]

    operations = [
        migrations.RunPython(add_beat_schedule, reverse_code=remove_beat_schedule),
    ]
