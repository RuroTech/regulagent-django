"""
URL configuration for ra_config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from apps.public_core.views.well_registry import WellRegistryViewSet
from apps.public_core.views.public_facts import PublicFactsViewSet
from apps.public_core.views.public_casing_string import PublicCasingStringViewSet
from apps.public_core.views.public_perforation import PublicPerforationViewSet
from apps.public_core.views.public_well_depths import PublicWellDepthsViewSet
from apps.kernel.views.plan_preview import PlanPreviewView
from apps.tenant_overlay.views.resolved_facts import ResolvedFactsView
from apps.policy_ingest import urls as policy_urls
from apps.kernel.views.advisory import AdvisorySanityCheckView
from apps.public_core.views.rrc_extractions import RRCCompletionsExtractView
from apps.public_core.views.w3a_from_api import W3AFromApiView
from apps.public_core.views.w3a_segmented import (
    W3AInitialView,
    W3ACombinedPDFView,
    W3AConfirmDocsView,
    W3AConfirmExtractionsView,
    W3AGeometryView,
    W3AConfirmGeometryView,
    W3AApplyEditsView,
    W3ABrowseEditsView,
)
from apps.public_core.views.plan_history import PlanHistoryView
from apps.public_core.views.plan_artifacts import PlanArtifactsView
from apps.public_core.views.artifact_download import ArtifactDownloadView
from apps.public_core.views.filing_export import FilingExportView
from apps.public_core.views.similar_wells import SimilarWellsView
from apps.public_core.views.plan_modify_ai import PlanModifyAIView
from apps.public_core.views.plan_modify import PlanModifyView
from apps.public_core.views.document_list import DocumentListView
from apps.public_core.views.document_upload import DocumentUploadView
from apps.public_core.views.operator_packet_upload import OperatorPacketUploadView
from apps.public_core.views.plan_detail import get_plan_detail
from apps.public_core.views.plan_status import (
    modify_plan,
    approve_plan,
    file_plan,
    get_plan_status,
)
from apps.public_core.views.w3_from_pna import BuildW3FromPNAView, W3HealthCheckView
from apps.public_core.views.w3_orm_endpoints import W3FormViewSet, W3PlugViewSet, W3EventViewSet
from apps.public_core.views.c103_endpoints import C103FormViewSet, C103PlugViewSet, C103EventViewSet
from apps.public_core.views.well_filings import WellFilingsView
from apps.public_core.views.all_filings import AllFilingsView
from apps.public_core.views.filing_metrics import FilingMetricsView
from apps.public_core.views.filing_breakdown import FilingBreakdownView
from apps.public_core.views.filing_breakdown_timeline import FilingBreakdownTimelineView
from apps.tenants.views import (
    TenantInfoView, UserProfileView, ChangePasswordView,
    ClientWorkspaceViewSet, UsageSummaryView, UsageRecordViewSet,
    TenantUserListCreateView, TenantUserDeactivateView, TenantUserSetAdminView,
    WorkspaceMembershipViewSet, NotificationViewSet,
    TenantBusinessProfileView, TenantBusinessProfileSchemaView,
)
from apps.filing_automation.views import W3ASubmitView, FilingJobDetailView
from apps.tenant_overlay.views.tenant_wells import (
    get_well_by_api,
    bulk_get_wells,
    get_tenant_well_history,
    import_wells_view,
)
from apps.public_core.views.well_components import (
    well_components_view,
    delete_well_component_view,
    well_wbd_sync_view,
)
from apps.public_core.views.manual_wbd import manual_wbd_list_create, manual_wbd_detail
from apps.tenant_overlay.views.guardrail_policy import (
    TenantGuardrailPolicyView,
    get_risk_profiles,
    validate_policy_change,
)
from apps.assistant.urls import plan_version_urls
from apps.public_core.views.bulk_operations import (
    bulk_generate_plans_view,
    bulk_update_plan_status_view,
    get_bulk_job_status,
    list_bulk_jobs,
)
from apps.public_core.views.nm_wells import (
    NMWellDetailView,
    NMWellDocumentsView,
    NMWellCombinedPDFView,
)
from apps.public_core.views.nm_well_import import (
    NMWellImportView,
    NMWellBatchImportView,
)
from apps.public_core.views.research import (
    ResearchSessionListCreateView,
    ResearchSessionDetailView,
    ResearchSessionDocumentsView,
    ResearchSessionAskView,
    ResearchSessionChatView,
    ResearchSessionSummaryView,
    BulkResearchSessionCreateView,
)
from apps.public_core.views.timeline_views import WellTimelineView, WellTimelineRefreshView
from apps.public_core.views.document_pdf import DocumentPDFView
from apps.public_core.views.document_delete import DocumentDeleteView

router = DefaultRouter()
router.register(r'public/wells', WellRegistryViewSet, basename='public-wells')
router.register(r'public/facts', PublicFactsViewSet, basename='public-facts')
router.register(r'public/casing', PublicCasingStringViewSet, basename='public-casing')
router.register(r'public/perforations', PublicPerforationViewSet, basename='public-perforations')
router.register(r'public/depths', PublicWellDepthsViewSet, basename='public-depths')
router.register(r'tenant/workspaces', ClientWorkspaceViewSet, basename='client-workspaces')
router.register(r'tenant/usage/records', UsageRecordViewSet, basename='usage-records')
router.register(r'notifications', NotificationViewSet, basename='notifications')
router.register(r'w3/forms', W3FormViewSet, basename='w3-forms')
router.register(r'w3/plugs', W3PlugViewSet, basename='w3-plugs')
router.register(r'w3/events', W3EventViewSet, basename='w3-events')
router.register(r'c103/forms', C103FormViewSet, basename='c103-forms')
router.register(r'c103/events', C103EventViewSet, basename='c103-events')

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # JWT Authentication endpoints
    path('api/auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/auth/token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    
    # API routes
    path('api/', include(router.urls)),

    # C-103 nested plug routes (scoped to a specific form)
    path('api/c103/forms/<int:form_pk>/plugs/', C103PlugViewSet.as_view({'get': 'list', 'post': 'create'}), name='c103-form-plugs-list'),
    path('api/c103/forms/<int:form_pk>/plugs/<int:pk>/', C103PlugViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='c103-form-plugs-detail'),
    path('api/overlay/engagements/<int:engagement_id>/resolved-facts', ResolvedFactsView.as_view()),
    path('api/plans/preview', PlanPreviewView.as_view()),
    path('api/advisory/sanity-check', AdvisorySanityCheckView.as_view()),
    path('api/rrc/extractions/completions', RRCCompletionsExtractView.as_view()),
    path('api/plans/w3a/from-api', W3AFromApiView.as_view()),
    
    # Segmented W3A flow (multi-stage with user verification)
    path('api/w3a/initial', W3AInitialView.as_view(), name='w3a-initial'),
    path('api/w3a/<str:temp_plan_id>/combined.pdf', W3ACombinedPDFView.as_view(), name='w3a-combined-pdf'),
    path('api/w3a/<str:temp_plan_id>/confirm-docs', W3AConfirmDocsView.as_view(), name='w3a-confirm-docs'),
    path('api/w3a/<str:temp_plan_id>/extractions', W3AConfirmExtractionsView.as_view(), name='w3a-confirm-extractions'),
    path('api/w3a/<str:temp_plan_id>/geometry', W3AGeometryView.as_view(), name='w3a-geometry'),
    path('api/w3a/<str:temp_plan_id>/confirm-geometry', W3AConfirmGeometryView.as_view(), name='w3a-confirm-geometry'),
    path('api/w3a/<str:plan_id>/apply-edits', W3AApplyEditsView.as_view(), name='w3a-apply-edits'),
    path('api/w3a/edits', W3ABrowseEditsView.as_view(), name='w3a-browse-edits'),
    
    # W-3 Form Generation from pnaexchange
    path('api/w3/health/', W3HealthCheckView.as_view(), name='w3-health'),
    path('api/w3/build-from-pna/', BuildW3FromPNAView.as_view(), name='w3-build-from-pna'),

    # W-3 Wizard (upload, parse, reconcile, generate)
    path('api/w3-wizard/', include('apps.public_core.urls_w3_wizard')),
    
    # Well Filings Unified Endpoints
    path('api/filings/', AllFilingsView.as_view(), name='all-filings'),
    path('api/filings/metrics/', FilingMetricsView.as_view(), name='filing-metrics'),
    path('api/filings/breakdown/', FilingBreakdownView.as_view(), name='filing-breakdown'),
    path('api/filings/breakdown-timeline/', FilingBreakdownTimelineView.as_view(), name='filing-breakdown-timeline'),
    path('api/wells/<str:api14>/filings/', WellFilingsView.as_view(), name='well-filings'),

    # Well Timeline
    path("api/wells/<str:api14>/timeline/", WellTimelineView.as_view(), name="well-timeline"),
    path("api/wells/<str:api14>/timeline/refresh/", WellTimelineRefreshView.as_view(), name="well-timeline-refresh"),
    
    path('api/plans/<str:api>/history', PlanHistoryView.as_view()),
    path('api/plans/<str:api>/artifacts', PlanArtifactsView.as_view()),
    path('api/artifacts/<uuid:artifact_id>/download', ArtifactDownloadView.as_view()),
    path('api/plans/<str:api>/filing/export', FilingExportView.as_view()),
    path('api/similar-wells', SimilarWellsView.as_view()),
    path('api/plans/<str:api>/modify/ai', PlanModifyAIView.as_view()),
    path('api/plans/<str:api>/modify', PlanModifyView.as_view()),
    path('api/documents/', DocumentListView.as_view(), name='document_list'),
    path('api/documents/upload/', DocumentUploadView.as_view(), name='document_upload'),
    path('api/documents/operator-packet/', OperatorPacketUploadView.as_view(), name='operator_packet_upload'),
    path('api/documents/<int:doc_id>/', DocumentDeleteView.as_view(), name='document_delete'),
    path('api/documents/<int:doc_id>/pdf/', DocumentPDFView.as_view(), name='document_pdf'),
    
    # Plan detail endpoint (full payload for viewing and chat interaction)
    path('api/plans/<str:plan_id>/', get_plan_detail, name='plan_detail'),
    
    # Plan status workflow endpoints
    path('api/plans/<str:plan_id>/status/', get_plan_status, name='plan_status'),
    path('api/plans/<str:plan_id>/status/modify/', modify_plan, name='plan_status_modify'),
    path('api/plans/<str:plan_id>/status/approve/', approve_plan, name='plan_status_approve'),
    path('api/plans/<str:plan_id>/status/file/', file_plan, name='plan_status_file'),
    
    # Tenant info endpoint
    path('api/tenant/', TenantInfoView.as_view(), name='tenant_info'),

    # Tenant business profile (JSON blob for filing automation)
    path('api/tenant/business-profile/', TenantBusinessProfileView.as_view(), name='tenant-business-profile'),
    path('api/tenant/business-profile/schema/', TenantBusinessProfileSchemaView.as_view(), name='tenant-business-profile-schema'),

    # W-3A filing automation (snapshot pk is a BigAutoField; job pk is UUID)
    path('api/w3a/<int:snapshot_id>/submit/', W3ASubmitView.as_view(), name='w3a-submit'),
    path('api/w3a/jobs/<uuid:job_id>/', FilingJobDetailView.as_view(), name='filing-job-detail'),

    # Tenant user management endpoints
    path('api/tenant/users/', TenantUserListCreateView.as_view(), name='tenant-users-list'),
    path('api/tenant/users/<int:id>/deactivate/', TenantUserDeactivateView.as_view(), name='tenant-user-deactivate'),
    path('api/tenant/users/<int:id>/set-admin/', TenantUserSetAdminView.as_view(), name='tenant-user-set-admin'),

    # Workspace membership endpoints (admin-only, nested under workspaces)
    path('api/tenant/workspaces/<workspace_pk>/members/', WorkspaceMembershipViewSet.as_view({'get': 'list', 'post': 'create'}), name='workspace-members-list'),
    path('api/tenant/workspaces/<workspace_pk>/members/<pk>/', WorkspaceMembershipViewSet.as_view({'delete': 'destroy'}), name='workspace-members-detail'),
    
    # User profile endpoints
    path('api/user/profile/', UserProfileView.as_view(), name='user_profile'),
    path('api/user/change-password/', ChangePasswordView.as_view(), name='change_password'),

    # Usage tracking endpoints
    path('api/tenant/usage/summary/', UsageSummaryView.as_view(), name='usage_summary'),
    
    # Manual WBD endpoints
    path('api/tenant/manual-wbd/', manual_wbd_list_create, name='manual-wbd-list'),
    path('api/tenant/manual-wbd/<uuid:wbd_id>/', manual_wbd_detail, name='manual-wbd-detail'),

    # Tenant wells endpoints (specific routes first, then generic)
    path('api/tenant/wells/import/', import_wells_view, name='tenant_wells_import'),
    path('api/tenant/wells/history/', get_tenant_well_history, name='tenant_well_history'),
    path('api/tenant/wells/bulk/', bulk_get_wells, name='tenant_wells_bulk'),
    path('api/tenant/wells/<str:api14>/components/', well_components_view, name='well-components-list'),
    path('api/tenant/wells/<str:api14>/components/<uuid:component_id>/', delete_well_component_view, name='well-components-delete'),
    path('api/tenant/wells/<str:api14>/wbd-sync/', well_wbd_sync_view, name='well-wbd-sync'),
    path('api/tenant/wells/<str:api14>/', get_well_by_api, name='tenant_well_by_api'),
    
    # Tenant guardrail policy endpoints
    path('api/tenant/settings/guardrails/', TenantGuardrailPolicyView.as_view(), name='tenant_guardrails'),
    path('api/tenant/settings/guardrails/risk-profiles/', get_risk_profiles, name='guardrail_profiles'),
    path('api/tenant/settings/guardrails/validate/', validate_policy_change, name='guardrail_validate'),

    # Bulk operations endpoints
    path('api/wells/bulk/generate-plans/', bulk_generate_plans_view, name='bulk_generate_plans'),
    path('api/plans/bulk/update-status/', bulk_update_plan_status_view, name='bulk_update_status'),
    path('api/jobs/<uuid:job_id>/', get_bulk_job_status, name='bulk_job_status'),
    path('api/jobs/', list_bulk_jobs, name='bulk_jobs_list'),

    # NM well lookup endpoints
    path('api/nm/wells/<str:api>/', NMWellDetailView.as_view(), name='nm_well_detail'),
    path('api/nm/wells/<str:api>/documents/', NMWellDocumentsView.as_view(), name='nm_well_documents'),
    path('api/nm/wells/<str:api>/documents/download/', NMWellCombinedPDFView.as_view(), name='nm_well_combined_pdf'),

    # NM well import endpoints
    path('api/nm/import/', NMWellImportView.as_view(), name='nm_well_import'),
    path('api/nm/batch-import/', NMWellBatchImportView.as_view(), name='nm_well_batch_import'),

    # Research session endpoints
    path('api/research/sessions/', ResearchSessionListCreateView.as_view(), name='research_session_list_create'),
    path('api/research/sessions/bulk/', BulkResearchSessionCreateView.as_view(), name='research_session_bulk_create'),
    path('api/research/sessions/<uuid:session_id>/', ResearchSessionDetailView.as_view(), name='research_session_detail'),
    path('api/research/sessions/<uuid:session_id>/documents/', ResearchSessionDocumentsView.as_view(), name='research_session_documents'),
    path('api/research/sessions/<uuid:session_id>/ask/', ResearchSessionAskView.as_view(), name='research_session_ask'),
    path('api/research/sessions/<uuid:session_id>/chat/', ResearchSessionChatView.as_view(), name='research_session_chat'),
    path('api/research/sessions/<uuid:session_id>/summary/', ResearchSessionSummaryView.as_view(), name='research_session_summary'),

    # Chat and assistant endpoints
    path('api/chat/', include('apps.assistant.urls')),
    
    # Plan version history endpoints (from assistant app)
    path('api/plans/', include(plan_version_urls)),
    
    path('api/policy/', include((policy_urls, 'policy_ingest'), namespace='policy')),

    # Intelligence app (filing status, rejections, recommendations, trends)
    path('api/intelligence/', include('apps.intelligence.urls')),
]

# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
