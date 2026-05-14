"""
API endpoints for querying and managing W3 historical data.

Provides REST endpoints for:
- List/retrieve W3 events
- List/retrieve W3 plugs
- List/retrieve W3 forms
- Submit W3 forms to RRC
- Filter by API number, date range, status, etc.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.shortcuts import get_object_or_404
from django.db.models import Q, Count
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.public_core.models import W3EventORM, W3PlugORM, W3FormORM, WellRegistry
from apps.public_core.serializers.w3_orm_serializers import (
    W3EventORM_ListSerializer,
    W3EventORM_DetailSerializer,
    W3EventORM_CreateUpdateSerializer,
    W3PlugORM_ListSerializer,
    W3PlugORM_DetailSerializer,
    W3PlugORM_CreateUpdateSerializer,
    W3FormORM_ListSerializer,
    W3FormORM_DetailSerializer,
    W3FormORM_CreateUpdateSerializer,
    W3FormORM_SubmitSerializer,
)

logger = logging.getLogger(__name__)


class W3EventViewSet(viewsets.ModelViewSet):
    """
    ViewSet for W3 events.
    
    Endpoints:
    - GET /api/w3/events/ - List events
    - POST /api/w3/events/ - Create event
    - GET /api/w3/events/{id}/ - Retrieve event
    - PATCH /api/w3/events/{id}/ - Update event
    - DELETE /api/w3/events/{id}/ - Delete event
    - GET /api/w3/events/by-api/{api_number}/ - List events for API
    - GET /api/w3/events/by-date-range/ - List events in date range
    """
    
    queryset = W3EventORM.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        """Choose serializer based on action."""
        if self.action == 'list':
            return W3EventORM_ListSerializer
        elif self.action == 'retrieve':
            return W3EventORM_DetailSerializer
        else:
            return W3EventORM_CreateUpdateSerializer
    
    def get_queryset(self):
        """Filter queryset by query parameters."""
        queryset = super().get_queryset()
        
        # Filter by API number
        api_number = self.request.query_params.get('api_number')
        if api_number:
            queryset = queryset.filter(api_number=api_number)
        
        # Filter by event type
        event_type = self.request.query_params.get('event_type')
        if event_type:
            queryset = queryset.filter(event_type=event_type)
        
        # Filter by date range
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(event_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(event_date__lte=date_to)
        
        # Filter by plug number
        plug_number = self.request.query_params.get('plug_number')
        if plug_number:
            queryset = queryset.filter(plug_number=plug_number)
        
        # Filter by well
        well_id = self.request.query_params.get('well_id')
        if well_id:
            queryset = queryset.filter(well_id=well_id)
        
        return queryset.order_by('-event_date', '-event_start_time')
    
    @action(detail=False, methods=['get'])
    def by_api(self, request):
        """Get all events for a specific API number."""
        api_number = request.query_params.get('api_number')
        if not api_number:
            return Response(
                {'error': 'api_number query parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        events = self.get_queryset().filter(api_number=api_number)
        serializer = self.get_serializer(events, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def by_date_range(self, request):
        """Get events in a date range."""
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        
        if not date_from or not date_to:
            return Response(
                {'error': 'date_from and date_to query parameters required (YYYY-MM-DD)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            date_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            date_to = datetime.strptime(date_to, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        events = self.get_queryset().filter(
            event_date__gte=date_from,
            event_date__lte=date_to
        )
        serializer = self.get_serializer(events, many=True)
        return Response(serializer.data)


class W3PlugViewSet(viewsets.ModelViewSet):
    """
    ViewSet for W3 plugs.
    
    Endpoints:
    - GET /api/w3/plugs/ - List plugs
    - POST /api/w3/plugs/ - Create plug
    - GET /api/w3/plugs/{id}/ - Retrieve plug
    - PATCH /api/w3/plugs/{id}/ - Update plug
    - DELETE /api/w3/plugs/{id}/ - Delete plug
    - GET /api/w3/plugs/by-api/{api_number}/ - List plugs for API
    - GET /api/w3/plugs/{id}/events/ - Get events in plug
    """
    
    queryset = W3PlugORM.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        """Choose serializer based on action."""
        if self.action == 'list':
            return W3PlugORM_ListSerializer
        elif self.action == 'retrieve':
            return W3PlugORM_DetailSerializer
        else:
            return W3PlugORM_CreateUpdateSerializer
    
    def get_queryset(self):
        """Filter queryset by query parameters."""
        queryset = super().get_queryset()
        
        # Filter by API number
        api_number = self.request.query_params.get('api_number')
        if api_number:
            queryset = queryset.filter(api_number=api_number)
        
        # Filter by plug type
        plug_type = self.request.query_params.get('plug_type')
        if plug_type:
            queryset = queryset.filter(plug_type=plug_type)
        
        # Filter by well
        well_id = self.request.query_params.get('well_id')
        if well_id:
            queryset = queryset.filter(well_id=well_id)
        
        return queryset.order_by('plug_number')
    
    @action(detail=False, methods=['get'])
    def by_api(self, request):
        """Get all plugs for a specific API number."""
        api_number = request.query_params.get('api_number')
        if not api_number:
            return Response(
                {'error': 'api_number query parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        plugs = self.get_queryset().filter(api_number=api_number)
        serializer = self.get_serializer(plugs, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def events(self, request, pk=None):
        """Get all events associated with this plug."""
        plug = self.get_object()
        events = plug.events.all()
        serializer = W3EventORM_ListSerializer(events, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def add_event(self, request, pk=None):
        """Add an event to this plug."""
        plug = self.get_object()
        event_id = request.data.get('event_id')
        
        if not event_id:
            return Response(
                {'error': 'event_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            event = W3EventORM.objects.get(id=event_id)
            plug.events.add(event)
            return Response({'success': True, 'message': 'Event added to plug'})
        except W3EventORM.DoesNotExist:
            return Response(
                {'error': 'Event not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['post'])
    def remove_event(self, request, pk=None):
        """Remove an event from this plug."""
        plug = self.get_object()
        event_id = request.data.get('event_id')
        
        if not event_id:
            return Response(
                {'error': 'event_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            event = W3EventORM.objects.get(id=event_id)
            plug.events.remove(event)
            return Response({'success': True, 'message': 'Event removed from plug'})
        except W3EventORM.DoesNotExist:
            return Response(
                {'error': 'Event not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class W3FormViewSet(viewsets.ModelViewSet):
    """
    ViewSet for W3 forms.
    
    Endpoints:
    - GET /api/w3/forms/ - List forms
    - POST /api/w3/forms/ - Create form
    - GET /api/w3/forms/{id}/ - Retrieve form
    - PATCH /api/w3/forms/{id}/ - Update form
    - DELETE /api/w3/forms/{id}/ - Delete form (cascades to plugs and events)
    - GET /api/w3/forms/by-api/{api_number}/ - List forms for API
    - GET /api/w3/forms/{id}/submit/ - Submit form to RRC
    - GET /api/w3/forms/pending-submission/ - List draft forms
    - GET /api/w3/forms/submitted/ - List submitted forms
    """
    
    queryset = W3FormORM.objects.all()
    permission_classes = [IsAuthenticated]
    
    def get_serializer_class(self):
        """Choose serializer based on action."""
        if self.action == 'list':
            return W3FormORM_ListSerializer
        elif self.action == 'retrieve':
            return W3FormORM_DetailSerializer
        elif self.action == 'submit':
            return W3FormORM_SubmitSerializer
        else:
            return W3FormORM_CreateUpdateSerializer
    
    def get_queryset(self):
        """Filter queryset by query parameters."""
        queryset = super().get_queryset()
        
        # Filter by API number
        api_number = self.request.query_params.get('api_number')
        if api_number:
            queryset = queryset.filter(api_number=api_number)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by well
        well_id = self.request.query_params.get('well_id')
        if well_id:
            queryset = queryset.filter(well_id=well_id)
        
        # Filter by auto-generated
        auto_generated = self.request.query_params.get('auto_generated')
        if auto_generated is not None:
            queryset = queryset.filter(auto_generated=auto_generated.lower() == 'true')
        
        return queryset.order_by('-created_at')

    def perform_destroy(self, instance):
        """Only allow deletion of draft forms."""
        if instance.status != 'draft':
            raise PermissionDenied("Only draft filings can be deleted.")
        instance.delete()

    @action(detail=False, methods=['get'])
    def by_api(self, request):
        """Get all forms for a specific API number."""
        api_number = request.query_params.get('api_number')
        if not api_number:
            return Response(
                {'error': 'api_number query parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        forms = self.get_queryset().filter(api_number=api_number)
        serializer = self.get_serializer(forms, many=True)
        return Response({
            'api_number': api_number,
            'count': forms.count(),
            'forms': serializer.data
        })
    
    @action(detail=False, methods=['get'])
    def pending_submission(self, request):
        """Get all W3 forms pending submission (draft status)."""
        forms = self.get_queryset().filter(status='draft')
        serializer = self.get_serializer(forms, many=True)
        return Response({
            'count': forms.count(),
            'forms': serializer.data
        })
    
    @action(detail=False, methods=['get'])
    def submitted(self, request):
        """Get all submitted W3 forms."""
        forms = self.get_queryset().filter(status__in=['submitted', 'approved'])
        serializer = self.get_serializer(forms, many=True)
        return Response({
            'count': forms.count(),
            'forms': serializer.data
        })
    
    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Submit a W3 form to RRC."""
        form = self.get_object()
        
        if form.status != 'draft':
            return Response(
                {'error': f'Cannot submit form with status: {form.get_status_display()}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = self.get_serializer(form, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'W-3 form submitted to RRC',
                'submitted_at': form.submitted_at,
                'rrc_confirmation_number': form.rrc_confirmation_number
            })
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def plugs(self, request, pk=None):
        """Get all plugs in this W3 form."""
        form = self.get_object()
        plugs = form.plugs.all()
        serializer = W3PlugORM_ListSerializer(plugs, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def add_plug(self, request, pk=None):
        """Add a plug to this W3 form."""
        form = self.get_object()
        plug_id = request.data.get('plug_id')
        
        if not plug_id:
            return Response(
                {'error': 'plug_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            plug = W3PlugORM.objects.get(id=plug_id)
            form.plugs.add(plug)
            return Response({'success': True, 'message': 'Plug added to form'})
        except W3PlugORM.DoesNotExist:
            return Response(
                {'error': 'Plug not found'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['get'], url_path='export-pdf')
    def export_pdf(self, request, pk=None):
        """Export this W-3 form as a filled PDF."""
        import os
        from django.http import FileResponse
        from apps.public_core.services.w3_pdf_generator import (
            generate_w3_pdf,
            W3PDFGeneratorError,
        )

        form_orm = self.get_object()

        if not form_orm.form_data:
            return Response(
                {'error': 'No form data available for PDF export'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            result = generate_w3_pdf(form_orm.form_data)
            temp_path = result["temp_path"]
            api_number = result.get("api_number", "unknown")
            filename = f"W3_{api_number}.pdf"

            return FileResponse(
                open(temp_path, "rb"),
                as_attachment=True,
                filename=filename,
                content_type="application/pdf",
            )
        except W3PDFGeneratorError as e:
            logger.error(f"W-3 PDF generation failed: {e}")
            return Response(
                {'error': f'PDF generation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

