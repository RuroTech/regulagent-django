from rest_framework import generics
from rest_framework.filters import OrderingFilter
from rest_framework.exceptions import NotFound
from django_filters.rest_framework import DjangoFilterBackend

from .models import PolicySection, PolicyRule, DistrictOverlay, CountyOverlay
from .serializers import (
    PolicySectionSerializer,
    PolicyRuleSerializer,
    DistrictOverlaySerializer,
    CountyOverlaySerializer,
)


class PolicySectionsListView(generics.ListAPIView):
    queryset = PolicySection.objects.select_related('rule').all()
    serializer_class = PolicySectionSerializer
    pagination_class = None
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['rule__rule_id', 'version_tag', 'path']
    ordering_fields = ['order_idx']
    ordering = ['order_idx']


class PolicyRulesListView(generics.ListAPIView):
    queryset = PolicyRule.objects.all()
    serializer_class = PolicyRuleSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['rule_id', 'version_tag', 'jurisdiction']
    pagination_class = None


class DistrictOverlayListView(generics.ListAPIView):
    """List all district overlays, optionally filtered by ?jurisdiction=TX|NM."""
    serializer_class = DistrictOverlaySerializer
    pagination_class = None
    authentication_classes = []
    permission_classes = []

    def get_queryset(self):
        qs = DistrictOverlay.objects.all()
        jurisdiction = self.request.query_params.get('jurisdiction')
        if jurisdiction:
            qs = qs.filter(jurisdiction=jurisdiction)
        return qs


class DistrictOverlayCountiesView(generics.ListAPIView):
    """List all county overlays for a given district_code. Returns 404 if district not found."""
    serializer_class = CountyOverlaySerializer
    pagination_class = None
    authentication_classes = []
    permission_classes = []

    def get_queryset(self):
        district_code = self.kwargs['district_code']
        try:
            district = DistrictOverlay.objects.get(district_code=district_code)
        except DistrictOverlay.DoesNotExist:
            raise NotFound(detail=f"District '{district_code}' not found.")
        return CountyOverlay.objects.filter(district_overlay=district)


