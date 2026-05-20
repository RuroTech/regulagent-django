"""
Celery configuration for RegulAgent.

This file initializes the Celery app and auto-discovers tasks from all Django apps.
"""

import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ra_config.settings.development')

app = Celery('regulagent')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related config keys
#   should have a `CELERY_` prefix.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()
app.autodiscover_tasks(related_name='tasks_polling')
app.autodiscover_tasks(related_name='tasks_w3_wizard')

# Route the W-3A submit task to a dedicated browser queue. The same worker
# pool can pick it up in dev (no Playwright container split yet).
app.conf.task_routes = {
    **(getattr(app.conf, 'task_routes', {}) or {}),
    'apps.filing_automation.tasks.submit_w3a_to_rrc': {'queue': 'browser'},
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task to verify Celery is working."""
    print(f'Request: {self.request!r}')

