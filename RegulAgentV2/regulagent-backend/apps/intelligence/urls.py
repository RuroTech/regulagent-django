from django.urls import path

from . import views

app_name = "intelligence"

urlpatterns = [
    # Recommendations
    path("recommendations/", views.RecommendationListView.as_view(), name="recommendation-list"),
    path("recommendations/check-field/", views.FieldCheckView.as_view(), name="check-field"),
    path(
        "recommendations/<uuid:pk>/interact/",
        views.RecommendationInteractView.as_view(),
        name="recommendation-interact",
    ),
    # Rejections
    path("rejections/", views.RejectionListView.as_view(), name="rejection-list"),
    path("rejections/<uuid:pk>/", views.RejectionDetailView.as_view(), name="rejection-detail"),
    path("rejections/<uuid:pk>/verify/", views.RejectionVerifyView.as_view(), name="rejection-verify"),
    path(
        "rejections/<uuid:pk>/apply-corrections/",
        views.RejectionApplyCorrectionsView.as_view(),
        name="rejection-apply-corrections",
    ),
    # Filing Status
    path("filing-status/", views.FilingStatusListCreateView.as_view(), name="filing-status-list"),
    path(
        "filing-status/<uuid:pk>/",
        views.FilingStatusDetailView.as_view(),
        name="filing-status-detail",
    ),
    # Trends & Analytics
    path("trends/", views.TrendsView.as_view(), name="trends"),
    path("trends/heatmap/", views.TrendsHeatmapView.as_view(), name="trends-heatmap"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    # Filing Sync
    path("filing-status/sync/", views.FilingSyncView.as_view(), name="filing-sync"),
    path(
        "filing-status/sync/<str:task_id>/",
        views.FilingSyncStatusView.as_view(),
        name="filing-sync-status",
    ),
    # Portal Credentials
    path("credentials/", views.PortalCredentialListCreateView.as_view(), name="credential-list"),
    path("credentials/<uuid:pk>/", views.PortalCredentialDeleteView.as_view(), name="credential-delete"),
]
