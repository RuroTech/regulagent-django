from django.urls import path
from .views import (
    PolicySectionsListView,
    PolicyRulesListView,
    DistrictOverlayListView,
    DistrictOverlayCountiesView,
)


urlpatterns = [
    path('sections/', PolicySectionsListView.as_view(), name='policy-sections'),
    path('rules/', PolicyRulesListView.as_view(), name='policy-rules'),
    path('district-overlays/', DistrictOverlayListView.as_view(), name='district-overlays-list'),
    path(
        'district-overlays/<str:district_code>/counties/',
        DistrictOverlayCountiesView.as_view(),
        name='district-overlays-counties',
    ),
]


