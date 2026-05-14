from __future__ import annotations

import secrets

from django.db.models import Count, Exists, OuterRef

import uuid as _uuid

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from django_tenants.utils import get_tenant_model, get_public_schema_name, schema_context
from django.db import connection
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone

from tenant_users.permissions.models import UserTenantPermissions

from apps.tenants.services.plan_service import (
    get_tenant_plan,
    get_effective_features,
    get_active_user_count,
    can_add_user,
)
from apps.tenants.tasks import send_welcome_email_task
from apps.tenants.services.usage_tracker import get_tenant_usage_summary, get_monthly_token_usage
from .models import ClientWorkspace, Notification, TenantAdminRole, UsageRecord, User, WorkspaceMembership
from .serializers import (
    ClientWorkspaceSerializer,
    ClientWorkspaceCreateSerializer,
    NotificationSerializer,
    UsageRecordSerializer,
    UserListSerializer,
    UserCreateSerializer,
    WorkspaceMembershipSerializer,
)


class TenantInfoView(APIView):
    """
    Return the tenant info for the authenticated user, including plan and effective features.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        user_tenant = user.tenants.first() if user and user.is_authenticated else None

        if not user_tenant:
            return Response({"detail": "No tenant found for user."}, status=404)

        tenant_payload = {
            "id": str(user_tenant.id),
            "name": user_tenant.name,
            "slug": user_tenant.slug,
            "created_on": user_tenant.created_on,
        }

        tenant_plan = get_tenant_plan(user_tenant)
        plan_payload = None
        if tenant_plan:
            plan = tenant_plan.plan
            plan_payload = {
                "id": getattr(plan, "id", None) if plan else None,
                "name": getattr(plan, "name", None) if plan else None,
                "slug": getattr(plan, "slug", None) if plan else None,
                "start_date": tenant_plan.start_date,
                "end_date": tenant_plan.end_date,
                "user_limit": tenant_plan.user_limit,
                "users_filled": get_active_user_count(user_tenant),
                "discount": float(tenant_plan.discount) if tenant_plan.discount is not None else None,
                "sales_rep": tenant_plan.sales_rep,
                "notes": tenant_plan.notes,
            }

        features = get_effective_features(user_tenant)

        return Response({
            "tenant": tenant_payload,
            "plan": plan_payload,
            "features": features,
        })


class UserProfileView(APIView):
    """
    GET /api/user/profile/

    Returns the authenticated user's profile information including email,
    name, title, phone, organization, and tenant details.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        user_tenant = user.tenants.first() if user and user.is_authenticated else None

        if not user_tenant:
            return Response(
                {"detail": "No tenant found for user."},
                status=status.HTTP_404_NOT_FOUND
            )

        is_admin = False
        try:
            perm = UserTenantPermissions.objects.get(profile=user)
            is_admin = perm.is_staff or getattr(perm, 'is_superuser', False)
        except UserTenantPermissions.DoesNotExist:
            pass

        return Response({
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "title": user.title or "",
            "phone": user.phone or "",
            "organization": user.organization or "",
            "tenant": {
                "id": str(user_tenant.id),
                "name": user_tenant.name,
                "slug": user_tenant.slug,
            },
            "is_tenant_admin": is_admin,
        })

    def put(self, request):
        """Update user profile information"""
        user = request.user
        
        # Update allowed fields
        allowed_fields = ["first_name", "last_name", "title", "phone", "organization"]
        for field in allowed_fields:
            if field in request.data:
                setattr(user, field, request.data[field])
        
        try:
            user.save()
            user_tenant = user.tenants.first() if user and user.is_authenticated else None

            is_admin = False
            try:
                perm = UserTenantPermissions.objects.get(profile=user)
                is_admin = perm.is_staff or getattr(perm, 'is_superuser', False)
            except UserTenantPermissions.DoesNotExist:
                pass

            return Response({
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "title": user.title or "",
                "phone": user.phone or "",
                "organization": user.organization or "",
                "tenant": {
                    "id": str(user_tenant.id),
                    "name": user_tenant.name,
                    "slug": user_tenant.slug,
                } if user_tenant else None,
                "is_tenant_admin": is_admin,
            })
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class ChangePasswordView(APIView):
    """
    POST /api/user/change-password/

    Allows the authenticated user to change their password.
    
    Request body:
    {
        "old_password": "current_password",
        "new_password": "new_password"
    }
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        old_password = request.data.get("old_password")
        new_password = request.data.get("new_password")

        if not old_password or not new_password:
            return Response(
                {"detail": "Both old_password and new_password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if old password is correct
        if not user.check_password(old_password):
            return Response(
                {"detail": "Old password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST
            )        # Set new password
        user.set_password(new_password)
        try:
            user.save()
            return Response({"detail": "Password changed successfully."})
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class ClientWorkspaceViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing client workspaces within a tenant.
    Automatically filters to current tenant's workspaces.

    Endpoints:
    - GET /api/tenant/workspaces/ - List all workspaces for current tenant
    - POST /api/tenant/workspaces/ - Create new workspace
    - GET /api/tenant/workspaces/{id}/ - Retrieve workspace details
    - PUT /api/tenant/workspaces/{id}/ - Update workspace
    - PATCH /api/tenant/workspaces/{id}/ - Partial update workspace
    - DELETE /api/tenant/workspaces/{id}/ - Delete workspace
    - POST /api/tenant/workspaces/{id}/archive/ - Archive workspace
    - POST /api/tenant/workspaces/{id}/restore/ - Restore archived workspace
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter to current tenant based on schema."""
        Tenant = get_tenant_model()
        tenant = Tenant.objects.get(schema_name=connection.schema_name)

        queryset = (
            ClientWorkspace.objects
            .filter(tenant=tenant)
            .annotate(filing_count=Count('w3_forms', distinct=True))
            .select_related('tenant')
        )

        # Optional filter by is_active
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        # Apply membership scoping for non-admin users
        user = self.request.user
        with schema_context(tenant.schema_name):
            perms = UserTenantPermissions.objects.filter(profile=user).first()
        is_admin = (perms and perms.is_staff) or TenantAdminRole.objects.filter(
            user=user, tenant=tenant, is_tenant_admin=True
        ).exists()
        if not is_admin:
            queryset = queryset.filter(memberships__user=user)

        return queryset

    def get_serializer_class(self):
        """Use create serializer for write operations."""
        if self.action in ['create', 'update', 'partial_update']:
            return ClientWorkspaceCreateSerializer
        return ClientWorkspaceSerializer

    def get_serializer_context(self):
        """Add tenant to serializer context for validation."""
        context = super().get_serializer_context()
        if hasattr(self, 'request') and hasattr(self.request, 'user'):
            user = self.request.user
            if user.is_authenticated:
                public_schema = get_public_schema_name()
                tenant = user.tenants.exclude(schema_name=public_schema).first()
                if tenant:
                    context['tenant'] = tenant
        return context

    def perform_create(self, serializer):
        """Automatically set tenant to current tenant."""
        user = self.request.user
        public_schema = get_public_schema_name()
        tenant = user.tenants.exclude(schema_name=public_schema).first()
        if not tenant:
            raise ValidationError("No tenant found for user")
        workspace = serializer.save(tenant=tenant)
        # Auto-add creator as a member so they can see the workspace immediately
        WorkspaceMembership.objects.get_or_create(workspace=workspace, user=user)

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Archive a workspace (set is_active=False)."""
        workspace = self.get_object()
        workspace.is_active = False
        workspace.save()
        serializer = self.get_serializer(workspace)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def restore(self, request, pk=None):
        """Restore an archived workspace (set is_active=True)."""
        workspace = self.get_object()
        workspace.is_active = True
        workspace.save()
        serializer = self.get_serializer(workspace)
        return Response(serializer.data)


class WorkspaceMembershipViewSet(viewsets.ViewSet):
    """
    Admin-only API for managing workspace memberships.

    Endpoints (nested under /api/tenant/workspaces/<workspace_pk>/):
    - GET  members/           — List all members of the workspace
    - POST members/           — Add a user to the workspace (body: {user_id})
    - DELETE members/<pk>/    — Remove a user from the workspace (pk = user id)
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_workspace_and_check_admin(self, request, workspace_id):
        """Get workspace scoped to current tenant and verify requesting user is admin."""
        Tenant = get_tenant_model()
        tenant = Tenant.objects.get(schema_name=connection.schema_name)
        workspace = get_object_or_404(ClientWorkspace, pk=workspace_id, tenant=tenant)
        with schema_context(tenant.schema_name):
            perms = UserTenantPermissions.objects.filter(profile=request.user).first()
        is_admin = (perms and perms.is_staff) or TenantAdminRole.objects.filter(
            user=request.user, tenant=tenant, is_tenant_admin=True
        ).exists()
        if not is_admin:
            raise PermissionDenied("Only admins can manage workspace members.")
        return workspace, tenant

    def list(self, request, workspace_pk=None):
        workspace, tenant = self._get_workspace_and_check_admin(request, workspace_pk)
        memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related('user')
        serializer = WorkspaceMembershipSerializer(memberships, many=True)
        return Response(serializer.data)

    def create(self, request, workspace_pk=None):
        workspace, tenant = self._get_workspace_and_check_admin(request, workspace_pk)
        user_id = request.data.get('user_id')
        if not user_id:
            raise ValidationError({'user_id': 'Required.'})
        UserModel = get_user_model()
        user = get_object_or_404(UserModel, pk=user_id)
        # Verify user belongs to this tenant
        if not user.tenants.filter(pk=tenant.pk).exists():
            raise ValidationError({'user_id': 'User is not a member of this tenant.'})
        membership, created = WorkspaceMembership.objects.get_or_create(workspace=workspace, user=user)
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(WorkspaceMembershipSerializer(membership).data, status=status_code)

    def destroy(self, request, workspace_pk=None, pk=None):
        workspace, tenant = self._get_workspace_and_check_admin(request, workspace_pk)
        UserModel = get_user_model()
        user = get_object_or_404(UserModel, pk=pk)
        deleted, _ = WorkspaceMembership.objects.filter(workspace=workspace, user=user).delete()
        if not deleted:
            return Response({'detail': 'Membership not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UsageSummaryView(APIView):
    """
    GET /api/tenant/usage/summary/

    Returns usage statistics for the current tenant with optional filtering.

    Query parameters:
    - start_date: ISO date string (e.g., "2024-01-01")
    - end_date: ISO date string
    - event_type: Filter by event type
    - workspace_id: Filter by workspace ID
    - group_by: Group results by 'event_type', 'workspace', 'user', 'day', or 'resource_type' (default: event_type)
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import datetime
        from django_tenants.utils import get_tenant_model

        user = request.user
        tenant = user.tenants.first() if user and user.is_authenticated else None

        if not tenant:
            return Response(
                {"detail": "No tenant found for user."},
                status=status.HTTP_404_NOT_FOUND
            )

        # Parse query parameters
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        event_type = request.query_params.get('event_type')
        workspace_id = request.query_params.get('workspace_id')
        group_by = request.query_params.get('group_by', 'event_type')
        scope = request.query_params.get('scope')

        # Parse dates
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            except ValueError:
                return Response(
                    {"detail": "Invalid start_date format. Use ISO format (YYYY-MM-DD)."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            except ValueError:
                return Response(
                    {"detail": "Invalid end_date format. Use ISO format (YYYY-MM-DD)."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Get workspace if specified
        workspace = None
        if workspace_id:
            try:
                workspace = ClientWorkspace.objects.get(id=workspace_id, tenant=tenant)
            except ClientWorkspace.DoesNotExist:
                return Response(
                    {"detail": f"Workspace {workspace_id} not found for this tenant."},
                    status=status.HTTP_404_NOT_FOUND
                )

        # Validate group_by parameter
        valid_group_by = ['event_type', 'resource_type', 'workspace', 'user', 'day']
        if group_by not in valid_group_by:
            return Response(
                {"detail": f"Invalid group_by parameter. Must be one of: {', '.join(valid_group_by)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get usage summary
        summary = get_tenant_usage_summary(
            tenant=tenant,
            start_date=start_date,
            end_date=end_date,
            event_type=event_type,
            workspace=workspace,
            group_by=group_by,
            user=request.user if scope == 'personal' else None,
        )

        # Include monthly token budget info
        monthly = get_monthly_token_usage(tenant, user=request.user if scope == 'personal' else None)

        return Response({
            'tenant': {
                'id': str(tenant.id),
                'slug': tenant.slug,
                'name': tenant.name,
            },
            'filters': {
                'start_date': start_date.isoformat() if start_date else None,
                'end_date': end_date.isoformat() if end_date else None,
                'event_type': event_type,
                'workspace_id': workspace_id,
                'group_by': group_by,
            },
            'summary': summary,
            'monthly_budget': monthly,
        })


class UsageRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only API endpoint for viewing usage records.
    Automatically filters to current tenant's usage records.

    Endpoints:
    - GET /api/tenant/usage/records/ - List all usage records for current tenant
    - GET /api/tenant/usage/records/{id}/ - Retrieve usage record details

    Query parameters:
    - event_type: Filter by event type
    - workspace_id: Filter by workspace ID
    - user_id: Filter by user ID
    - start_date: ISO date string
    - end_date: ISO date string
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = UsageRecordSerializer

    def get_queryset(self):
        """Filter to current tenant based on user."""
        from datetime import datetime

        user = self.request.user
        tenant = user.tenants.first() if user and user.is_authenticated else None

        if not tenant:
            return UsageRecord.objects.none()

        queryset = UsageRecord.objects.filter(tenant=tenant).select_related(
            'tenant', 'workspace', 'user'
        )

        # Apply filters from query parameters
        event_type = self.request.query_params.get('event_type')
        if event_type:
            queryset = queryset.filter(event_type=event_type)

        workspace_id = self.request.query_params.get('workspace_id')
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)

        user_id = self.request.query_params.get('user_id')
        if user_id:
            queryset = queryset.filter(user_id=user_id)

        start_date_str = self.request.query_params.get('start_date')
        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                queryset = queryset.filter(created_at__gte=start_date)
            except ValueError:
                pass  # Ignore invalid date format

        end_date_str = self.request.query_params.get('end_date')
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                queryset = queryset.filter(created_at__lte=end_date)
            except ValueError:
                pass  # Ignore invalid date format

        return queryset


def _resolve_tenant(user):
    """
    Return the business Tenant for the current request.

    In production, subdomain routing sets connection.schema_name to the
    tenant's schema — use that directly.  In tests (and any context where
    the connection is on the public schema), fall back to the first
    non-public tenant the user is enrolled in.
    """
    Tenant = get_tenant_model()
    public_schema = get_public_schema_name()
    schema = connection.schema_name
    if schema != public_schema:
        return Tenant.objects.get(schema_name=schema)
    return user.tenants.exclude(schema_name=public_schema).first()


class TenantUserListCreateView(APIView):
    """
    GET  /api/tenant/users/ — List all users in the current tenant with seat summary.
    POST /api/tenant/users/ — Create a new user in the current tenant.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request: object) -> Response:
        user = request.user
        tenant = _resolve_tenant(user)

        if not tenant:
            return Response({"detail": "No tenant found for user."}, status=status.HTTP_404_NOT_FOUND)

        users_qs = tenant.user_set.annotate(
            is_tenant_admin=Exists(
                TenantAdminRole.objects.filter(
                    user=OuterRef('pk'),
                    tenant=tenant,
                    is_tenant_admin=True,
                )
            )
        )
        serializer = UserListSerializer(users_qs, many=True)

        used = get_active_user_count(tenant)
        tenant_plan = get_tenant_plan(tenant)
        if tenant_plan and tenant_plan.user_limit is not None:
            limit: int | None = tenant_plan.user_limit
            available: int | None = max(0, limit - used)
        else:
            limit = None
            available = None

        return Response({
            "users": serializer.data,
            "seats": {
                "used": used,
                "limit": limit,
                "available": available,
            },
        })

    def post(self, request: object) -> Response:
        user = request.user
        tenant = _resolve_tenant(user)

        if not tenant:
            return Response({"detail": "No tenant found for user."}, status=status.HTTP_404_NOT_FOUND)

        serializer = UserCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email: str = serializer.validated_data["email"]
        first_name: str = serializer.validated_data.get("first_name", "")
        last_name: str = serializer.validated_data.get("last_name", "")
        title: str | None = serializer.validated_data.get("title", None)

        # Check for duplicate email
        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "A user with this email already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check seat limit
        if not can_add_user(tenant):
            return Response(
                {"detail": "Seat limit reached."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        temp_password: str = secrets.token_urlsafe(12)
        new_user = User.objects.create_user(
            email=email,
            password=temp_password,
            first_name=first_name,
            last_name=last_name,
            title=title,
        )
        tenant.add_user(new_user)

        send_welcome_email_task.delay(new_user.id, temp_password)

        return Response(
            {
                "id": new_user.id,
                "email": new_user.email,
                "first_name": new_user.first_name,
                "last_name": new_user.last_name,
                "title": new_user.title,
                "is_active": new_user.is_active,
                "temp_password": temp_password,
            },
            status=status.HTTP_201_CREATED,
        )


class TenantUserDeactivateView(APIView):
    """
    PATCH /api/tenant/users/<id>/deactivate/ — Deactivate a user in the current tenant.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def patch(self, request: object, id: int) -> Response:
        requesting_user = request.user
        tenant = _resolve_tenant(requesting_user)

        if not tenant:
            return Response({"detail": "No tenant found for user."}, status=status.HTTP_404_NOT_FOUND)

        target = tenant.user_set.filter(id=id).first()
        if target is None:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        if target.id == requesting_user.id:
            return Response(
                {"detail": "Cannot deactivate your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target.is_active = False
        target.save()

        return Response(UserListSerializer(target).data, status=status.HTTP_200_OK)


class TenantUserSetAdminView(APIView):
    """
    PATCH /api/tenant/users/<id>/set-admin/ — Toggle is_tenant_admin for a user.
    Only existing tenant admins (or users with UserTenantPermissions.is_staff) can call this.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def patch(self, request: object, id: int) -> Response:
        requesting_user = request.user
        tenant = _resolve_tenant(requesting_user)

        if not tenant:
            return Response({"detail": "No tenant found for user."}, status=status.HTTP_404_NOT_FOUND)

        # Check requester is an admin (TenantAdminRole OR legacy UserTenantPermissions.is_staff)
        is_admin = TenantAdminRole.objects.filter(
            user=requesting_user, tenant=tenant, is_tenant_admin=True
        ).exists()
        if not is_admin:
            perm = UserTenantPermissions.objects.filter(profile=requesting_user).first()
            is_admin = bool(perm and perm.is_staff)
        if not is_admin:
            raise PermissionDenied("Only admins can change admin status.")

        target = tenant.user_set.filter(id=id).first()
        if target is None:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        new_value = request.data.get('is_tenant_admin')
        if new_value is None:
            raise ValidationError({'is_tenant_admin': 'Required.'})

        role, _ = TenantAdminRole.objects.get_or_create(user=target, tenant=tenant)
        role.is_tenant_admin = bool(new_value)
        role.save(update_fields=['is_tenant_admin', 'updated_at'])

        return Response({"id": target.id, "is_tenant_admin": role.is_tenant_admin})


class NotificationViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    """
    API endpoints for the current user's notifications within their tenant.

    GET  /api/notifications/              — paginated list (user + tenant scoped)
    GET  /api/notifications/unread-count/ — {"count": N}
    POST /api/notifications/{id}/read/    — mark one notification read
    POST /api/notifications/read-all/     — mark all user notifications read
    """
    serializer_class = NotificationSerializer
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_tenant_id(self, request):
        """Derive tenant UUID from the authenticated user's primary tenant."""
        tenant = request.user.tenants.first()
        if tenant is None:
            return None
        return tenant.id

    def get_queryset(self):
        tenant_id = self._get_tenant_id(self.request)
        if tenant_id is None:
            return Notification.objects.none()
        return (
            Notification.objects
            .filter(user=self.request.user, tenant_id=tenant_id)
            .order_by('-created_at')
        )

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        """Return the count of unread notifications for the current user."""
        count = self.get_queryset().filter(read=False).count()
        return Response({'count': count})

    @action(detail=True, methods=['post'], url_path='read')
    def mark_read(self, request, pk=None):
        """Mark a single notification as read."""
        notif = get_object_or_404(self.get_queryset(), pk=pk)
        if not notif.read:
            notif.read = True
            notif.read_at = timezone.now()
            notif.save(update_fields=['read', 'read_at'])
        return Response({'status': 'ok'})

    @action(detail=False, methods=['post'], url_path='read-all')
    def mark_all_read(self, request):
        """Mark all unread notifications for the current user as read."""
        now = timezone.now()
        self.get_queryset().filter(read=False).update(read=True, read_at=now)
        return Response({'status': 'ok'})
