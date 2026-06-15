"""
Typed exceptions for portal scraper authentication failures.

These exceptions allow callers to distinguish between:
  - Transient failures (network errors, Playwright timeouts) — keep original exception types
  - Auth failures that should NOT be retried (InvalidCredentialsError and subclasses)

InvalidCredentialsError  — login was rejected due to bad username/password.
CredentialLockedError    — the portal has locked the account (too many failures,
                           reset-password required). Subclass of InvalidCredentialsError
                           so callers can catch either.
"""


class InvalidCredentialsError(Exception):
    """
    Raised when the portal rejects a login attempt due to invalid credentials.

    This is a terminal failure — do NOT retry; instead update the credential's
    auth_state and notify the tenant.
    """


class CredentialLockedError(InvalidCredentialsError):
    """
    Raised when the portal indicates the account has been locked or requires a
    password reset due to too many failed attempts.

    Subclass of InvalidCredentialsError so callers that catch the parent class
    automatically handle this case too.
    """
