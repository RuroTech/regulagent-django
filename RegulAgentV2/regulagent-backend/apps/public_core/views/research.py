"""
Research Session API views.

Endpoints:
    POST   /api/research/sessions/               — create session + kick off indexing
    GET    /api/research/sessions/{id}/          — poll session status
    GET    /api/research/sessions/{id}/documents/ — list indexed documents
    POST   /api/research/sessions/{id}/ask/      — ask a question (SSE stream)
    GET    /api/research/sessions/{id}/chat/     — get chat history
    GET    /api/research/sessions/{id}/summary/  — aggregated well summary
"""
import logging

from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.public_core.models import ExtractedDocument, ResearchSession
from apps.public_core.serializers.research import (
    ResearchAskSerializer,
    BulkResearchSessionCreateSerializer,
    ResearchMessageSerializer,
    ResearchSessionCreateSerializer,
    ResearchSessionSerializer,
)
from apps.public_core.services.document_pipeline import detect_jurisdiction
from apps.public_core.services.research_rag import get_chat_history, stream_research_answer
from apps.public_core.tasks_research import start_research_session_task

logger = logging.getLogger(__name__)


def _get_accessible_session(session_id: str, user_tenant):
    """Fetch a ResearchSession the requesting user is allowed to see.

    - Sessions with tenant=None (public bulk ingestion) are accessible by all.
    - Sessions with tenant=T are only accessible by users belonging to T.
    - Raises ResearchSession.DoesNotExist if not found or access denied.
    """
    from django.db.models import Q
    return ResearchSession.objects.get(
        Q(tenant=user_tenant) | Q(tenant__isnull=True),
        id=session_id,
    )


class ResearchSessionListCreateView(APIView):
    """
    GET  /api/research/sessions/  — list sessions for the current tenant
    POST /api/research/sessions/  — create session + kick off indexing

    POST request body:
        api_number (str, required): Well API number.
        state (str, optional): "TX", "NM", or "UT". Auto-detected from API prefix if omitted.

    Returns 201 with the session object immediately; clients should poll
    GET /api/research/sessions/{id}/ until status == "ready".
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        GET /api/research/sessions/

        List research sessions for the current tenant.
        Supports optional filters:
            ?api_number=  — filter by API number (suffix match)
            ?status=      — filter by session status
            ?well_api14=  — filter by linked well's api14
        """
        import re as _re

        user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
        qs = ResearchSession.objects.all().order_by("-created_at")

        if user_tenant:
            qs = qs.filter(tenant=user_tenant)

        # Optional filters
        api_number = request.query_params.get("api_number")
        if api_number:
            clean = _re.sub(r"\D+", "", api_number)
            if len(clean) >= 8:
                suffix = clean[-8:]
                # Filter sessions whose normalized api_number ends with the same 8 digits
                session_ids = []
                for sess in qs.only('id', 'api_number'):
                    sess_clean = _re.sub(r"\D+", "", str(sess.api_number or ""))
                    if len(sess_clean) >= 8 and sess_clean[-8:] == suffix:
                        session_ids.append(sess.id)
                qs = qs.filter(id__in=session_ids)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        well_api14 = request.query_params.get("well_api14")
        if well_api14:
            qs = qs.filter(well__api14=well_api14)

        sessions = qs[:50]
        return Response(ResearchSessionSerializer(sessions, many=True).data)

    def post(self, request):
        serializer = ResearchSessionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        api_number = serializer.validated_data["api_number"]
        explicit_state = serializer.validated_data.get("state")
        state = detect_jurisdiction(api_number, explicit=explicit_state)
        force_fetch = serializer.validated_data.get("force_fetch", False)

        # Normalize API for consistent lookup
        import re as _re
        normalized_api = _re.sub(r"\D+", "", str(api_number))

        user_tenant = request.user.tenants.first() if request.user.is_authenticated else None

        if not force_fetch:
            # Check for existing ready session — match on normalized API suffix
            # Normalise to API10 before suffix comparison — API14 has trailing 0000
            _api_norm = normalized_api[:10] if len(normalized_api) == 14 else normalized_api
            api_suffix = _api_norm[-8:] if len(_api_norm) >= 8 else _api_norm

            existing = ResearchSession.objects.filter(
                status="ready"
            ).order_by("-created_at")

            matched_session = None
            for sess in existing[:20]:
                sess_clean = _re.sub(r"\D+", "", str(sess.api_number))
                if len(sess_clean) == 14:
                    sess_clean = sess_clean[:10]
                if len(sess_clean) >= 8 and sess_clean[-8:] == api_suffix:
                    if sess.indexed_documents == 0:
                        continue
                    matched_session = sess
                    break

            if matched_session:
                sess_tenant = matched_session.tenant
                if sess_tenant == user_tenant:
                    # Same tenant — reuse as-is (own session with own chat)
                    logger.info(f"[ResearchAPI] Reusing own session {matched_session.id} for api={api_number}")
                    return Response(
                        ResearchSessionSerializer(matched_session).data,
                        status=status.HTTP_200_OK,
                    )
                else:
                    # Different tenant — create a new ready session reusing doc counts
                    # Documents are fetched by api_number (shared), only chat is siloed
                    new_session = ResearchSession.objects.create(
                        api_number=matched_session.api_number,
                        state=matched_session.state or state,
                        status="ready",
                        tenant=user_tenant,
                        well=matched_session.well,
                        total_documents=matched_session.total_documents,
                        indexed_documents=matched_session.indexed_documents,
                        failed_documents=matched_session.failed_documents,
                        force_fetch=False,
                    )
                    logger.info(
                        f"[ResearchAPI] Created tenant-siloed session {new_session.id} "
                        f"for api={api_number} reusing docs from session {matched_session.id}"
                    )
                    return Response(
                        ResearchSessionSerializer(new_session).data,
                        status=status.HTTP_201_CREATED,
                    )

            # Check for in-progress session (same normalized matching)
            from django.db.models import Q
            in_progress = ResearchSession.objects.filter(
                Q(tenant=user_tenant) | Q(tenant__isnull=True),
                status__in=["pending", "fetching", "indexing"],
            ).order_by("-created_at")

            for sess in in_progress[:20]:
                sess_clean = _re.sub(r"\D+", "", str(sess.api_number))
                if len(sess_clean) == 14:
                    sess_clean = sess_clean[:10]
                if len(sess_clean) >= 8 and sess_clean[-8:] == api_suffix:
                    # Return in-progress session regardless of tenant — polling is safe,
                    # they'll get their own ready session next time they call POST
                    logger.info(f"[ResearchAPI] Reusing in-progress session {sess.id} for api={api_number}")
                    return Response(
                        ResearchSessionSerializer(sess).data,
                        status=status.HTTP_200_OK,
                    )
        else:
            logger.info(f"[ResearchAPI] force_fetch=True for api={api_number}, bypassing session cache")

        session = ResearchSession.objects.create(
            api_number=api_number,
            state=state,
            status="pending",
            tenant=user_tenant,
            force_fetch=force_fetch,
        )

        # Kick off the background indexing pipeline
        task = start_research_session_task.delay(str(session.id))
        session.celery_task_id = task.id
        session.save(update_fields=["celery_task_id"])

        logger.info(
            f"[ResearchAPI] Created session {session.id} for api={api_number} "
            f"state={state} task={task.id} force_fetch={force_fetch}"
        )

        return Response(
            ResearchSessionSerializer(session).data,
            status=status.HTTP_201_CREATED,
        )


class ResearchSessionDetailView(APIView):
    """
    GET /api/research/sessions/{id}/

    Return current status and metadata for a research session.
    Clients poll this until status is "ready" or "error".
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id: str):
        try:
            user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
            session = _get_accessible_session(session_id, user_tenant)
        except ResearchSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(ResearchSessionSerializer(session).data)


class ResearchSessionDocumentsView(APIView):
    """
    GET /api/research/sessions/{id}/documents/

    Return the list of ExtractedDocuments that have been indexed for this session.
    Returns the document_list metadata cached on the session plus DB-side status.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id: str):
        try:
            user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
            session = _get_accessible_session(session_id, user_tenant)
        except ResearchSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fetch ExtractedDocuments linked to this API number
        extracted_docs = ExtractedDocument.objects.filter(
            api_number=session.api_number
        ).values(
            "id",
            "document_type",
            "source_path",
            "neubus_filename",
            "source_page",
            "status",
            "model_tag",
            "created_at",
            "attribution_confidence",
            "attribution_method",
        ).order_by("-created_at")

        return Response(
            {
                "session_id": str(session.id),
                "api_number": session.api_number,
                "state": session.state,
                "total_documents": session.total_documents,
                "indexed_documents": session.indexed_documents,
                "failed_documents": session.failed_documents,
                "document_list": session.document_list,
                "extracted_documents": list(extracted_docs),
            }
        )


class ResearchSessionAskView(APIView):
    """
    POST /api/research/sessions/{id}/ask/

    Ask a question about this well's indexed documents.
    Returns a Server-Sent Events stream.

    SSE event format:
        data: {"type": "token", "content": "..."}\n\n
        data: {"type": "citations", "citations": [...]}\n\n
        data: {"type": "done"}\n\n
        data: {"type": "error", "message": "..."}\n\n

    Request body:
        question (str, required): The question to ask.
        top_k (int, optional): Number of document sections to retrieve (1-20, default 8).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id: str):
        try:
            user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
            session = _get_accessible_session(session_id, user_tenant)
        except ResearchSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if session.status != "ready":
            return Response(
                {
                    "detail": f"Session is not ready for queries (status={session.status}). "
                              "Wait until indexing completes."
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = ResearchAskSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        question = serializer.validated_data["question"]
        top_k = serializer.validated_data["top_k"]

        logger.info(
            f"[ResearchAPI] Ask question for session {session_id}: "
            f"{question[:80]!r} (top_k={top_k})"
        )

        response = StreamingHttpResponse(
            stream_research_answer(question, session, top_k=top_k),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class ResearchSessionChatView(APIView):
    """
    GET /api/research/sessions/{id}/chat/

    Return the full chat history for this research session.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id: str):
        try:
            user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
            session = _get_accessible_session(session_id, user_tenant)
        except ResearchSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        messages = get_chat_history(session)
        return Response(
            {
                "session_id": str(session.id),
                "api_number": session.api_number,
                "messages": messages,
            }
        )


class ResearchSessionSummaryView(APIView):
    """
    GET /api/research/sessions/{id}/summary/

    Return an aggregated well summary built from all successfully extracted
    documents for the session's well.  Includes document counts by type,
    well info, casing records, plug records, and a filing timeline.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id: str):
        try:
            user_tenant = request.user.tenants.first() if request.user.is_authenticated else None
            session = _get_accessible_session(session_id, user_tenant)
        except ResearchSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        import re as _re
        clean_api = _re.sub(r"\D+", "", str(session.api_number or ""))

        # Primary selection: the documents linked to this well via the FK.
        # This is reliable across api_number format variants and includes
        # RRC-sourced docs (which have an empty neubus_filename). The legacy
        # api-suffix match is kept only as a fallback for unlinked/legacy docs.
        eds = ExtractedDocument.objects.none()
        if session.well_id:
            eds = ExtractedDocument.objects.filter(well=session.well, status="success")

        if not eds.exists():
            # Fallback: match by progressively shorter api_number suffixes.
            # Do NOT fall back to all EDs — that comingles data across wells in the lease.
            all_eds = ExtractedDocument.objects.filter(
                status="success",
            ).exclude(neubus_filename="")
            for suffix_len in (8, 5):
                if len(clean_api) >= suffix_len:
                    suffix = clean_api[-suffix_len:]
                    eds = all_eds.filter(api_number__icontains=suffix)
                    if eds.exists():
                        break

        # Prefer high/medium confidence documents when available
        confident_eds = eds.filter(attribution_confidence__in=["high", "medium"])
        attribution_warning = ""
        if confident_eds.exists():
            eds = confident_eds
        else:
            attribution_warning = (
                "Documents shown may include filings from sibling wells on the same lease. "
                "Attribution confidence is low for all documents."
            )

        def _safe_dict(val):
            """Return val if it's a dict, else empty dict (handles str/None)."""
            return val if isinstance(val, dict) else {}

        # ------------------------------------------------------------------
        # Document counts by type
        # ------------------------------------------------------------------
        from django.db.models import Count

        type_counts = dict(
            eds.values_list("document_type")
            .annotate(c=Count("id"))
            .values_list("document_type", "c")
        )

        # ------------------------------------------------------------------
        # Aggregate well_info from first W-1 / W-2 / W-3 that has it
        # ------------------------------------------------------------------
        well_info: dict = {}
        for ed in eds.filter(document_type__in=["w1", "w2", "w3"]).order_by("-created_at")[:10]:
            data = ed.json_data or {}
            wi = data.get("well_info", {})
            if wi and wi.get("lease") and not well_info.get("lease"):
                well_info = {
                    "api": wi.get("api", ""),
                    "field": wi.get("field", ""),
                    "lease": wi.get("lease", ""),
                    "county": wi.get("county", ""),
                    "district": _safe_dict(data.get("header")).get("rrc_district"),
                    "total_depth_ft": wi.get("total_depth_ft"),
                    "well_no": wi.get("well_no", ""),
                }
            oi = data.get("operator_info", {})
            if oi and oi.get("name") and not well_info.get("operator"):
                well_info["operator"] = oi.get("name", "")
                well_info["operator_number"] = oi.get("operator_number", "")

        # Backfill header fields from the enriched WellRegistry row so the summary
        # stays consistent with the Well Registry table. Covers RRC-sourced wells
        # whose W-2/W-3 'lease' field was blank (which would otherwise skip the
        # block above and leave the modal showing "—").
        well = session.well
        if well:
            if not well_info.get("operator"):
                well_info["operator"] = well.operator_name or ""
            if not well_info.get("county"):
                well_info["county"] = well.county or ""
            if not well_info.get("field"):
                well_info["field"] = well.field_name or ""
            if not well_info.get("lease"):
                well_info["lease"] = well.lease_name or ""
            if not well_info.get("well_no"):
                well_info["well_no"] = well.well_number or ""
            if not well_info.get("district"):
                well_info["district"] = well.district or ""
            if not well_info.get("api"):
                well_info["api"] = well.api14 or ""

        # ------------------------------------------------------------------
        # Casing records from W-3 and W-2 docs (deduplicated)
        # Prefer most recent filing. Dedup by (type, size) since the same
        # physical string may report slightly different depths across filings.
        # ------------------------------------------------------------------
        casing_records: list[dict] = []
        seen_sizes: dict = {}  # size -> index in casing_records
        for ed in eds.filter(document_type__in=["w3", "w2"]).order_by("-created_at"):
            data = ed.json_data or {}
            date_filed = _safe_dict(data.get("header")).get("date_filed", "")
            for c in (data.get("casing_record") or []):
                # Normalize field name: W-2 uses "string", W-3 uses "string_type"
                casing_type = c.get("string_type") or c.get("string") or None
                size = c.get("size_in")
                if not size:
                    continue
                # Normalize the output
                record = {**c}
                if "string" in record and "string_type" not in record:
                    record["string_type"] = record.pop("string")
                if "weight_per_ft" in record and "weight_ppf" not in record:
                    record["weight_ppf"] = record.pop("weight_per_ft")
                record["source_doc_type"] = ed.document_type
                record["source_date"] = date_filed
                if size not in seen_sizes:
                    # First time seeing this size — add it
                    seen_sizes[size] = len(casing_records)
                    casing_records.append(record)
                elif casing_type and not casing_records[seen_sizes[size]].get("string_type"):
                    # We have an untyped record; replace with this typed one
                    casing_records[seen_sizes[size]] = record

        # ------------------------------------------------------------------
        # Plug records from W-3 docs (deduplicated)
        # ------------------------------------------------------------------
        plug_records: list[dict] = []
        seen_plugs: set = set()
        for ed in eds.filter(document_type="w3").order_by("-created_at"):
            data = ed.json_data or {}
            date_filed = _safe_dict(data.get("header")).get("date_filed", "")
            for p in (data.get("plug_record") or []):
                # Normalize depths: top should be shallower (smaller number)
                top = p.get("depth_top_ft")
                bottom = p.get("depth_bottom_ft")
                # Convert "Surface" string to 0
                if isinstance(top, str) and top.lower() == "surface":
                    top = 0
                if isinstance(bottom, str) and bottom.lower() == "surface":
                    bottom = 0
                # Swap if inverted (top deeper than bottom)
                if top is not None and bottom is not None:
                    try:
                        top_num = float(top)
                        bottom_num = float(bottom)
                        if top_num > bottom_num:
                            top, bottom = bottom, top
                    except (ValueError, TypeError):
                        pass

                key = (top, bottom, p.get("sacks"))
                if key not in seen_plugs and top is not None:
                    seen_plugs.add(key)
                    record = {**p}
                    record["depth_top_ft"] = top
                    record["depth_bottom_ft"] = bottom
                    record["source_doc_type"] = "w3"
                    record["source_date"] = date_filed
                    plug_records.append(record)

        # ------------------------------------------------------------------
        # Filing timeline — one entry per ED, sorted by date (nulls last)
        # ------------------------------------------------------------------
        filing_timeline: list[dict] = []
        for ed in eds.order_by("created_at"):
            data = ed.json_data or {}
            date_filed = _safe_dict(data.get("header")).get("date_filed")
            tracking = _safe_dict(data.get("header")).get("tracking_number") or ed.tracking_no
            operator = (_safe_dict(data.get("operator_info")).get("name", ""))
            filing_timeline.append({
                "document_type": ed.document_type,
                "date": date_filed,
                "tracking_no": tracking or "",
                "operator": operator,
                "attribution_confidence": ed.attribution_confidence,
                "attribution_method": ed.attribution_method,
            })

        filing_timeline.sort(key=lambda x: x["date"] or "9999")

        result = {
            "session_id": str(session.id),
            "api_number": session.api_number,
            "state": session.state,
            "document_counts": {
                **type_counts,
                "total": eds.count(),
            },
            "well_info": well_info,
            "casing_records": casing_records[:50],
            "plug_records": plug_records[:50],
            "filing_timeline": filing_timeline,
        }
        if attribution_warning:
            result["attribution_warning"] = attribution_warning
        return Response(result)


class BulkResearchSessionCreateView(APIView):
    """
    POST /api/research/sessions/bulk/

    Create up to 50 research sessions in one request.
    Each well goes through the same start_research_session_task pipeline as single-well add.

    Request:  { "api_numbers": ["42-xxx", "30-xxx", ...], "state": "TX"|"NM"|null }
    Response (201): { "submitted": N, "sessions": [...] }

    Sessions that can't be created (unknown state, duplicate) return error rows with
    session_id=null. HTTP status is always 201 regardless of partial failures.
    """
    permission_classes = [IsAuthenticated]

    _PREFIX_MAP = {"42": "TX", "30": "NM"}

    def post(self, request):
        import re

        serializer = BulkResearchSessionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        api_numbers = serializer.validated_data["api_numbers"]
        global_state = serializer.validated_data.get("state")

        # Get tenant from the authenticated user — same pattern as ResearchSessionListCreateView
        user_tenant = request.user.tenants.first() if request.user.is_authenticated else None

        results = []
        seen_normalized = {}  # normalized_api -> original api_number (for dedup)

        for raw_api in api_numbers:
            # Normalize: strip non-digits, pad to 14 chars
            normalized = re.sub(r"\D+", "", raw_api)
            prefix = normalized[:2] if len(normalized) >= 2 else ""

            # State resolution
            if prefix in self._PREFIX_MAP:
                resolved_state = self._PREFIX_MAP[prefix]
            elif global_state:
                resolved_state = global_state
            else:
                results.append({
                    "api_number": raw_api,
                    "session_id": None,
                    "state": None,
                    "status": "error",
                    "error": (
                        f"Cannot detect state from prefix '{prefix}'. "
                        "Provide a state override or use a 30-xxx (NM) / 42-xxx (TX) API number."
                    ),
                })
                continue

            # Intra-batch dedup
            dedup_key = normalized[:14] if len(normalized) >= 14 else normalized
            if dedup_key in seen_normalized:
                results.append({
                    "api_number": raw_api,
                    "session_id": None,
                    "state": resolved_state,
                    "status": "error",
                    "error": f"Duplicate of '{seen_normalized[dedup_key]}' in this request.",
                })
                continue
            seen_normalized[dedup_key] = raw_api

            # Create session and dispatch task
            # Normalize api_number to digits-only (max 14) before saving to fit the varchar(16) field
            stored_api = normalized[:14] if len(normalized) >= 14 else normalized

            try:
                session = ResearchSession.objects.create(
                    api_number=stored_api,
                    state=resolved_state,
                    status="pending",
                    tenant=user_tenant,
                    force_fetch=False,
                )
                task = start_research_session_task.delay(str(session.id))
                session.celery_task_id = task.id
                session.save(update_fields=["celery_task_id"])

                results.append({
                    "api_number": stored_api,
                    "session_id": str(session.id),
                    "state": resolved_state,
                    "status": "pending",
                    "error": None,
                })
                logger.info(
                    "[BulkResearch] Created session %s api=%s state=%s task=%s",
                    session.id, stored_api, resolved_state, task.id,
                )
            except Exception as exc:
                logger.exception("[BulkResearch] Failed to create session for api=%s", raw_api)
                results.append({
                    "api_number": raw_api,
                    "session_id": None,
                    "state": resolved_state,
                    "status": "error",
                    "error": str(exc),
                })

        return Response(
            {"submitted": len(results), "sessions": results},
            status=status.HTTP_201_CREATED,
        )
