"""
Signals for automatic UserTenantPermissions management.

These signals ensure that when a user is added or removed from a tenant,
the corresponding UserTenantPermissions are automatically created or deleted.

Based on TestDriven.io guide:
https://testdriven.io/blog/django-multi-tenant/#django-tenant-users
"""
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django_tenants.utils import schema_context
from tenant_users.permissions.models import UserTenantPermissions
from django.core.exceptions import ValidationError

from apps.tenants.models import User
from apps.tenants.services.plan_service import can_add_user


@receiver(m2m_changed, sender=User.tenants.through)
def on_tenant_user_tenants_changed(
    sender, instance, action, reverse, model, pk_set, **kwargs
):
    """
    Automatically manage UserTenantPermissions when users are added/removed from tenants.
    
    Args:
        sender: The intermediate model for the many-to-many relationship
        instance: The User instance
        action: The type of update (pre_add, post_add, pre_remove, post_remove, etc.)
        reverse: Whether this is a reverse relation
        model: The Tenant model
        pk_set: Set of primary keys of the tenants being added/removed
    """
    # Enforce seat limits before adding a user to a tenant
    if action == 'pre_add':
        # If the user being added is inactive, do not count against seats
        if getattr(instance, 'is_active', True):
            for tenant_id in pk_set:
                tenant = model.objects.get(pk=tenant_id)
                if not can_add_user(tenant):
                    raise ValidationError("User limit reached for this tenant.")
    
    # Automatically create 'UserTenantPermissions' when user is added to a tenant
    if action == 'post_add':
        for tenant_id in pk_set:
            tenant = model.objects.get(pk=tenant_id)
            with schema_context(tenant.schema_name):
                UserTenantPermissions.objects.get_or_create(profile=instance)
            # Auto-enroll user in all active workspaces for this tenant
            from apps.tenants.models import WorkspaceMembership, ClientWorkspace
            for ws in ClientWorkspace.objects.filter(tenant=tenant, is_active=True):
                WorkspaceMembership.objects.get_or_create(workspace=ws, user=instance)
    
    # Automatically delete 'UserTenantPermissions' when user is removed from a tenant
    if action == 'post_remove':
        for tenant_id in pk_set:
            tenant = model.objects.get(pk=tenant_id)
            with schema_context(tenant.schema_name):
                UserTenantPermissions.objects.filter(profile=instance).delete()

