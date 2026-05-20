"""
Debug endpoint to check thread permissions.
"""

import logging
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.shortcuts import get_object_or_404

from apps.assistant.models import ChatThread

logger = logging.getLogger(__name__)


@api_view(['GET'])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def debug_thread_permissions(request, thread_id):
    """
    Debug endpoint to check thread permissions for current user.
    
    GET /api/chat/threads/{thread_id}/debug-permissions/
    
    Response:
    {
      "thread_id": 1,
      "thread_owner_id": 5,
      "thread_owner_email": "demo@example.com",
      "current_user_id": 5,
      "current_user_email": "demo@example.com",
      "is_owner": true,
      "is_shared_user": false,
      "shared_with_count": 0,
      "shared_with_emails": [],
      "permissions": {
        "can_view": true,
        "can_edit": true
      },
      "tenant_id": "uuid",
      "diagnosis": "✅ User IS the owner - should have edit rights"
    }
    """
    try:
        thread = ChatThread.objects.select_related('created_by').prefetch_related('shared_with').get(id=thread_id)
    except ChatThread.DoesNotExist:
        return Response({"error": f"Thread {thread_id} not found"}, status=404)

    user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
    if user_tenant and str(thread.tenant_id) != str(user_tenant.id):
        return Response({"error": f"Thread {thread_id} not found"}, status=404)

    user = request.user
    
    # Check permissions
    can_view = thread.can_view(user)
    can_edit = thread.can_edit(user)
    
    # Check if user is in shared list
    is_shared_user = thread.shared_with.filter(id=user.id).exists()
    
    # Determine diagnosis
    if thread.created_by_id == user.id:
        diagnosis = "✅ User IS the owner - should have edit rights"
    elif is_shared_user:
        diagnosis = "👥 User has shared access - read-only"
    else:
        diagnosis = "❌ User is NOT owner or shared - no access"
    
    return Response({
        "thread_id": thread.id,
        "thread_owner_id": thread.created_by_id,
        "thread_owner_email": thread.created_by.email if thread.created_by else None,
        "current_user_id": user.id,
        "current_user_email": user.email,
        "is_owner": thread.created_by_id == user.id,
        "is_shared_user": is_shared_user,
        "shared_with_count": thread.shared_with.count(),
        "shared_with_emails": [u.email for u in thread.shared_with.all()],
        "permissions": {
            "can_view": can_view,
            "can_edit": can_edit
        },
        "tenant_id": str(thread.tenant_id),
        "diagnosis": diagnosis
    })

