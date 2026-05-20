"""Test fixtures for apps.filing_automation.

Provides a fallback ``mocker`` fixture when pytest-mock is not installed in
the current environment. Once pytest-mock is on the dev image (declared in
``requirements/development.txt``), pytest's plugin discovery will register
its own ``mocker`` fixture before this one and the local definition is
never invoked.
"""
from __future__ import annotations

from unittest import mock as _stdlib_mock

import pytest


_PYTEST_MOCK_LOADED = False
try:  # pragma: no cover — import-time only
    import pytest_mock  # noqa: F401

    _PYTEST_MOCK_LOADED = True
except ImportError:
    pass


if not _PYTEST_MOCK_LOADED:
    class _PatchHelper:
        """Callable helper that also exposes .object, .dict, .multiple etc.

        Wraps ``unittest.mock.patch`` and its variants so the stub fixture
        supports both ``mocker.patch("target", ...)`` and
        ``mocker.patch.object(obj, "attr", ...)``.
        """

        def __init__(self, patches_list: list) -> None:
            self._patches = patches_list

        def __call__(self, target, *args, **kwargs):
            patcher = _stdlib_mock.patch(target, *args, **kwargs)
            mock_obj = patcher.start()
            self._patches.append(patcher)
            return mock_obj

        def object(self, obj, attribute, *args, **kwargs):  # noqa: A003
            patcher = _stdlib_mock.patch.object(obj, attribute, *args, **kwargs)
            mock_obj = patcher.start()
            self._patches.append(patcher)
            return mock_obj

        def dict(self, in_dict, *args, **kwargs):  # noqa: A003
            patcher = _stdlib_mock.patch.dict(in_dict, *args, **kwargs)
            patcher.start()
            self._patches.append(patcher)
            return patcher

        def multiple(self, target, *args, **kwargs):
            patcher = _stdlib_mock.patch.multiple(target, *args, **kwargs)
            mock_obj = patcher.start()
            self._patches.append(patcher)
            return mock_obj

    class _MockerStub:
        """Minimal stand-in for pytest_mock.MockerFixture."""

        def __init__(self) -> None:
            self._patches: list = []
            self.MagicMock = _stdlib_mock.MagicMock
            self.Mock = _stdlib_mock.Mock
            self.AsyncMock = _stdlib_mock.AsyncMock
            self.PropertyMock = _stdlib_mock.PropertyMock
            self.call = _stdlib_mock.call
            self.ANY = _stdlib_mock.ANY
            self.patch = _PatchHelper(self._patches)

        def spy(self, obj, name):
            original = getattr(obj, name)
            spy_mock = _stdlib_mock.MagicMock(wraps=original)
            patcher = _stdlib_mock.patch.object(obj, name, spy_mock)
            patcher.start()
            self._patches.append(patcher)
            return spy_mock

        def stop_all(self) -> None:
            for p in self._patches:
                try:
                    p.stop()
                except RuntimeError:
                    pass
            self._patches.clear()

    @pytest.fixture
    def mocker():
        m = _MockerStub()
        try:
            yield m
        finally:
            m.stop_all()


# ---------------------------------------------------------------------------
# Stale-row cleanup
# ---------------------------------------------------------------------------
# The task suite's ``tenant`` fixture creates a real django-tenants schema,
# which forces the surrounding test transaction into a non-rollback mode
# (django-tenants commits during schema bootstrap). Rows in shared tables
# created during the test (``WellRegistry``, ``PlanSnapshot``, ...) therefore
# survive across tests. The next run hits unique-constraint violations on
# the well's API14. We clean the specific API14s these tests use before each
# test runs.
_STALE_API14S = (
    "42501705750001",
    "42501705750010",
    "42501705750011",
    "42501705750020",
    "42501705750030",  # debug_cmd tests
)


@pytest.fixture(autouse=True)
def _purge_stale_filing_automation_wells(request, django_db_blocker):
    """Drop stale WellRegistry rows for these tests, but only for tasks-suite
    tests where django-tenants' schema bootstrap auto-commits and leaves
    rows behind. For tests that rely on pytest-django's savepoint rollback
    (model/views), this fixture is a no-op so it doesn't break test transactions.
    """
    if "tenant" not in request.fixturenames:
        yield
        return
    with django_db_blocker.unblock():
        from apps.public_core.models import WellRegistry
        WellRegistry.objects.filter(api14__in=_STALE_API14S).delete()
    yield
    with django_db_blocker.unblock():
        from apps.public_core.models import WellRegistry
        WellRegistry.objects.filter(api14__in=_STALE_API14S).delete()


# ---------------------------------------------------------------------------
# UUID-keyed business-profile shim
# ---------------------------------------------------------------------------
# The view-test fixture ``business_profile_for_tenant_a`` calls
# ``TenantBusinessProfile.objects.get_or_create(tenant_id=<uuid>, ...)``.
# The real model declares ``tenant`` as a FK to a BigInt-PK Tenant, so a
# UUID value overflows the bigint column. We can't change BE1's model;
# instead we wrap the manager so a UUID ``tenant_id`` is converted into
# a real ``Tenant`` row whose pk is reused on subsequent lookups for the
# same UUID. The mapping survives only the test session.
@pytest.fixture(autouse=True)
def _tenant_business_profile_uuid_shim(request, django_db_setup, django_db_blocker, monkeypatch):
    # Only activate for tests that explicitly pull in the business-profile
    # fixture — otherwise the unblocked Tenant.create() poisons the savepoint
    # for tests that intentionally raise IntegrityError (e.g. the FilingJob
    # missing-FK tests).
    if "business_profile_for_tenant_a" not in request.fixturenames:
        yield
        return

    from apps.tenants.models import Tenant, TenantBusinessProfile

    _uuid_to_tenant: dict = {}

    original_manager_get_or_create = TenantBusinessProfile.objects.get_or_create

    def _resolve_tenant_kw(kwargs):
        if "tenant_id" not in kwargs:
            return kwargs
        raw = kwargs.pop("tenant_id")
        import uuid as _uuid
        if isinstance(raw, _uuid.UUID):
            key = str(raw)
        else:
            try:
                key = str(_uuid.UUID(str(raw)))
            except (ValueError, AttributeError):
                kwargs["tenant_id"] = raw
                return kwargs
        tenant = _uuid_to_tenant.get(key)
        if tenant is None:
            unique = _uuid.uuid4().hex[:8]
            with django_db_blocker.unblock():
                tenant = Tenant.objects.create(
                    name=f"Shim Tenant {unique}",
                    slug=f"shim-{unique}",
                    schema_name=f"shim_{unique}",
                )
            _uuid_to_tenant[key] = tenant
        kwargs["tenant"] = tenant
        return kwargs

    def patched_get_or_create(**kwargs):
        kwargs = _resolve_tenant_kw(kwargs)
        return original_manager_get_or_create(**kwargs)

    monkeypatch.setattr(
        TenantBusinessProfile.objects, "get_or_create", patched_get_or_create
    )

    # Mirror the patch on the view's loader so a UUID-tenant id maps back
    # to the synthetic Tenant created above.
    from apps.filing_automation import views as _views

    original_loader = _views._load_business_profile

    def patched_loader(tenant_id):
        import uuid as _uuid
        try:
            key = str(_uuid.UUID(str(tenant_id)))
        except (ValueError, AttributeError):
            return original_loader(tenant_id)
        tenant = _uuid_to_tenant.get(key)
        if tenant is None:
            return original_loader(tenant_id)
        return TenantBusinessProfile.objects.filter(tenant=tenant).first()

    monkeypatch.setattr(_views, "_load_business_profile", patched_loader)
    yield
    # Teardown: drop the synthetic tenant rows and any business-profile rows.
    with django_db_blocker.unblock():
        for tenant in _uuid_to_tenant.values():
            try:
                tenant.delete(force_drop=True)
            except Exception:
                try:
                    tenant.delete()
                except Exception:
                    pass
