from .base import *  # noqa

DEBUG = True

# Dev CORS
CORS_ALLOW_ALL_ORIGINS = True

# Allow ngrok/tunneling services for v0 preview testing
# Add your ngrok URL here when testing with v0
ALLOWED_HOSTS = ALLOWED_HOSTS + [
    '*.ngrok.io',
    '*.ngrok-free.app',
]

# Serve the public schema when no tenant domain matches the request host.
# Required so Django test client / DRF APIClient (which default to 'testserver')
# can hit public-schema endpoints without registering a domain fixture.
SHOW_PUBLIC_IF_NO_TENANT_FOUND = True


