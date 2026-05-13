import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'dev-key-change-me')
DEBUG = bool(int(os.getenv('DEBUG', '1')))
ALLOWED_HOSTS = [h for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,dev.localhost').split(',') if h]

# django-tenants app split
SHARED_APPS = [
    'django_tenants',
    'apps.tenants',  # must be before django.contrib.contenttypes
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'django_filters',
    'corsheaders',
    'simple_history',  # for audit trails
    'django_celery_beat',  # for periodic task scheduling
    'tenant_users.permissions',
    'tenant_users.tenants',
    'apps.public_core',
    'apps.tenant_overlay',
    'apps.assistant',  # AI chat and plan modification
    'apps.policy',
    'apps.policy_ingest',
    'apps.kernel',
    'apps.kernel.handlers.tx',
    'apps.kernel.handlers.nm',
    'apps.intelligence',
    'ordered_model',
    'plans',
]

TENANT_APPS = [
    'apps.tenant_overlay',
]

INSTALLED_APPS = SHARED_APPS + [app for app in TENANT_APPS if app not in SHARED_APPS]

MIDDLEWARE = [
    'django_tenants.middleware.main.TenantMainMiddleware',
    'apps.tenants.middleware.TenantContextMiddleware',  # Propagate tenant to contextvars
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'simple_history.middleware.HistoryRequestMiddleware',  # for audit trail user tracking
]

ROOT_URLCONF = 'ra_config.urls'

AUTHENTICATION_BACKENDS = (
    # Use tenant-users authentication backend
    'tenant_users.permissions.backend.UserBackend',
    # Fallback to default model backend
    'django.contrib.auth.backends.ModelBackend',
)

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'ra_config.wsgi.application'

if os.getenv('DB_HOST'):
    DATABASES = {
        'default': {
            'ENGINE': 'django_tenants.postgresql_backend',
            'NAME': os.getenv('DB_NAME', 'regulagent'),
            'USER': os.getenv('DB_USER', 'postgres'),
            'PASSWORD': os.getenv('DB_PASSWORD', 'postgres'),
            'HOST': os.getenv('DB_HOST', 'localhost'),
            'PORT': int(os.getenv('DB_PORT', '5432')),
            'CONN_MAX_AGE': int(os.getenv('DB_CONN_MAX_AGE', '60')),
            'OPTIONS': {'connect_timeout': int(os.getenv('DB_CONNECT_TIMEOUT', '5'))},
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
MEDIA_URL = 'media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'mediafiles')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom user model
AUTH_USER_MODEL = 'tenants.User'

# django-tenants configuration
TENANT_MODEL = 'tenants.Tenant'
TENANT_DOMAIN_MODEL = 'tenants.Domain'
PUBLIC_SCHEMA_NAME = 'public'
DATABASE_ROUTERS = (
    'django_tenants.routers.TenantSyncRouter',
)
PUBLIC_SCHEMA_URLCONF = 'ra_config.urls'
TENANT_URLCONF = 'ra_config.urls'

# DRF configuration with JWT authentication
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

# CORS (relaxed in dev; tightened in prod)
# Default dev frontends (Next.js/Vite)
CORS_ALLOWED_ORIGINS = [
    'http://localhost:3000',
    'http://127.0.0.1:3000',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
]

# Allow credentials for local development (needed if frontend uses cookies or fetch credentials: 'include')
CORS_ALLOW_CREDENTIALS = True

# JWT Configuration
from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=4),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# django-plans configuration
PLANS_CURRENCY = 'USD'
PLANS_PLAN_MODEL = 'plans.Plan'


# ==============================================================================
# FILE UPLOAD & STORAGE SETTINGS
# ==============================================================================

# Toggle between S3 and local filesystem storage
USE_S3 = os.getenv('USE_S3', 'false').lower() == 'true'

if USE_S3:
    # ========== S3 STORAGE CONFIGURATION ==========
    # AWS credentials (set via environment variables)
    AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = os.getenv('AWS_STORAGE_BUCKET_NAME', 'regulagent-uploads')
    AWS_S3_REGION_NAME = os.getenv('AWS_S3_REGION_NAME', 'us-east-1')

    # NOTE: Do NOT set AWS_S3_CUSTOM_DOMAIN — django-storages auto-reads it
    # and returns plain (non-presigned) URLs which fail on private buckets.
    _s3_domain = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'

    # Explicitly enable presigned URLs for private bucket
    AWS_QUERYSTRING_AUTH = True

    # Security and permissions
    AWS_DEFAULT_ACL = None  # Inherit bucket ACL (recommended)
    AWS_S3_OBJECT_PARAMETERS = {
        'CacheControl': 'max-age=86400',  # 24 hours
    }

    # Use S3 for file uploads
    DEFAULT_FILE_STORAGE = 'apps.public_core.storage.TenantS3Storage'

    # Media URL fallback (presigned URLs from default_storage.url() are preferred)
    MEDIA_URL = f'https://{_s3_domain}/'
    
else:
    # ========== LOCAL FILESYSTEM STORAGE ==========
    # Store uploads in Docker container (or local dev)
    MEDIA_ROOT = os.path.join(BASE_DIR, 'mediafiles', 'uploads')
    MEDIA_URL = '/media/uploads/'
    
    # Use local filesystem for file uploads
    DEFAULT_FILE_STORAGE = 'apps.public_core.storage.TenantLocalStorage'

# File upload limits
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800  # 50MB in bytes
DATA_UPLOAD_MAX_MEMORY_SIZE = 52428800  # 50MB in bytes
FILE_UPLOAD_PERMISSIONS = 0o644

# Allowed upload file types (validation in view layer)
ALLOWED_UPLOAD_EXTENSIONS = ['.pdf']


# ==============================================================================
# CELERY SETTINGS
# ==============================================================================

# Celery broker (Redis)
CELERY_BROKER_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# Celery configuration
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

# Task result expiration
CELERY_RESULT_EXPIRES = 3600  # 1 hour

# Task routing (optional - for when you want to split tasks across queues)
# CELERY_TASK_ROUTES = {
#     'apps.assistant.tasks.*': {'queue': 'assistant'},
# }
# Note: Commented out - using default 'celery' queue for all tasks

# Task time limits (prevent runaway tasks)
CELERY_TASK_TIME_LIMIT = 300  # 5 minutes hard limit
CELERY_TASK_SOFT_TIME_LIMIT = 240  # 4 minutes soft limit

# Worker configuration
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 1  # Restart worker after each task (prevents OOM on Vision pipeline)

# Beat scheduler (for periodic tasks)
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# Static periodic task schedules (merged with DB-managed schedules by DatabaseScheduler)
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    'aggregate-rejection-patterns': {
        'task': 'apps.intelligence.tasks.aggregate_rejection_patterns',
        'schedule': crontab(minute=0, hour='*/6'),
    },
    'generate-recommendations': {
        'task': 'apps.intelligence.tasks.generate_recommendations',
        'schedule': crontab(minute=0, hour=2),  # daily at 2am
    },
    'update-recommendation-metrics': {
        'task': 'apps.intelligence.tasks.update_recommendation_metrics',
        'schedule': crontab(minute=0, hour='*/4'),
    },
    'sync-all-tenant-filings': {
        'task': 'apps.intelligence.tasks_polling.sync_all_tenant_filings',
        'schedule': crontab(hour=3, minute=0),  # daily at 3am UTC
    },
    'ingest-tx-active-wells': {
        'task': 'apps.public_core.tasks_well_ingest.ingest_tx_active_wells_task',
        'schedule': crontab(minute=0, hour=2, day_of_month=1),
    },
    'ingest-tx-iwar-wells': {
        'task': 'apps.public_core.tasks_well_ingest.ingest_tx_iwar_wells_task',
        'schedule': crontab(minute=0, hour=3, day_of_month=1),
    },
    'ingest-nm-active-wells': {
        'task': 'apps.public_core.tasks_well_ingest.ingest_nm_active_wells_task',
        'schedule': crontab(minute=0, hour=4),
    },
}


# ==============================================================================
# EMAIL SETTINGS (Microsoft Office 365 SMTP)
# ==============================================================================

EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.office365.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'true').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:5173')

