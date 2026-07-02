"""
RAG Query Service for Research Sessions.

Provides semantic search over indexed well documents and streams
AI-generated answers with citations.
"""
import json
import logging
from typing import Any, Dict, Generator, List, Optional

from django.db.models import Q
from pgvector.django import CosineDistance

from apps.public_core.models import DocumentVector, ResearchSession, ResearchMessage

logger = logging.getLogger(__name__)

# Import the embedding function from extraction service
from apps.public_core.services.openai_config import get_openai_client
from apps.public_core.services.openai_extraction import _embed_texts
from apps.public_core.services.text_processing import json_to_prose as _json_to_prose

MODEL_CHAT = "gpt-4o"


def _build_system_prompt(api_number: str, state: str) -> str:
    """Build system prompt for research Q&A."""
    return f"""You are a regulatory research assistant analyzing well documents for API {api_number} ({state}).

You have access to extracted sections from regulatory filings including W-2 (completion reports), W-15 (well tests), L-1 (location filings), GAU advisories, and plugging records.

Answer questions by synthesizing information from the provided document sections. Use your domain knowledge of oil & gas regulatory filings to interpret the data:
- In Texas RRC filings, the "well name" is the lease name (e.g. "EAST MERCHANT 25") combined with the well number (e.g. "#2506CU"). Look for these in header, well_info, and raw text sections.
- W-2 sections contain: completion data, casing records, formation tops, operator info, and lease location.
- W-15 sections contain: well test data, production rates, and formation information.
- L-1 sections contain: location plat, surface coordinates, and lease boundaries.

When a user asks about the well name, operator, field, county, or similar identifying info — synthesize it from whatever sections are available rather than saying it's not found if related data exists.

When referencing specific information, cite the source: [Source: {{doc_type}} - {{section_name}}]

Each section below is labeled PUBLIC (RRC) or PRIVATE (your upload). PUBLIC sections come from
official RRC regulatory filings and can be treated as authoritative. PRIVATE sections are
documents the tenant uploaded themselves — do not represent them as official filings or as
RRC-verified; if asked, identify which sections are public vs. private and note when an answer
relies on tenant-uploaded (unverified) material rather than official filings.

Only say information is unavailable if it is genuinely absent from all provided sections."""


def _retrieve_relevant_sections(
    question: str,
    session: ResearchSession,
    top_k: int = 15,
    exclude_section_names: Optional[List[str]] = None,
    prefer_doc_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Embed the question and find top-k most similar document sections.

    Uses pgvector cosine distance search via the HNSW index.
    """
    # Embed the question
    query_embedding = _embed_texts([question])[0]

    # Build base queryset — prefer the well FK, but fall back to api_number when
    # it yields nothing. Vectors created before the well link existed (or for a
    # well that wasn't resolved at index time) have well=None; without this
    # fallback the search returns zero sections and the model answers with a
    # confusing "no document sections found" apology.
    base_qs = DocumentVector.objects.none()
    if session.well:
        base_qs = DocumentVector.objects.filter(well=session.well)
    if not base_qs.exists():
        base_qs = DocumentVector.objects.filter(
            metadata__api_number=session.api_number
        )

    # Apply tenant isolation: public vectors + this tenant's private vectors.
    # NOTE: vectors store metadata.tenant_id as JSON null for public docs (key
    # present, value null). Django's JSONB `__isnull=True` only matches ABSENT
    # keys, so it misses present-null — which excluded every public vector and
    # made tenant-scoped sessions retrieve nothing. Match both null forms, and
    # accept the tenant id as str or int.
    public = Q(metadata__tenant_id__isnull=True) | Q(metadata__tenant_id=None)
    if session.tenant_id is not None:
        own = (
            Q(metadata__tenant_id=str(session.tenant_id)) |
            Q(metadata__tenant_id=session.tenant_id)
        )
        base_qs = base_qs.filter(public | own)
    else:
        # No-tenant session: only public vectors
        base_qs = base_qs.filter(public)

    if exclude_section_names:
        base_qs = base_qs.exclude(section_name__in=exclude_section_names)

    fetch_k = top_k * 3 if prefer_doc_types else top_k * 2

    # Cosine similarity search
    results = (
        base_qs
        .annotate(distance=CosineDistance("embedding", query_embedding))
        .order_by("distance")[:fetch_k]
    )

    sections = []
    for vec in results:
        # visibility: prefer the explicit metadata written by
        # vectorize_extracted_document; fall back to tenant_id presence for
        # vectors indexed before that field existed (pre-backfill).
        visibility = vec.metadata.get("visibility") or (
            "private" if vec.metadata.get("tenant_id") else "public"
        )
        sections.append({
            "doc_type": vec.document_type,
            "section_name": vec.section_name,
            "section_text": vec.section_text,
            "distance": float(vec.distance),
            "file_name": vec.file_name,
            "visibility": visibility,
            "source_type": vec.metadata.get("source_type") or "",
        })

    # Filter by cosine distance threshold
    # text-embedding-3-small with JSON content has higher distances than prose;
    # useful sections typically range 0.45-0.65 for domain-specific queries.
    distance_threshold = 0.75
    pre_filter_count = len(sections)
    sections = [s for s in sections if s["distance"] <= distance_threshold]
    if pre_filter_count != len(sections):
        logger.info(
            "Distance threshold filtered %d/%d sections (threshold=%.2f)",
            pre_filter_count - len(sections), pre_filter_count, distance_threshold,
        )

    # Deduplicate near-identical chunks — P&A documents often produce
    # 3-5 copies of the same procedure text via raw + description chunks.
    deduped = []
    seen_texts = []
    for s in sections:
        text_sig = s["section_text"][:200].lower()
        is_dup = False
        for prev in seen_texts:
            # Simple overlap: if first 200 chars share >60% of words, skip
            words_a = set(text_sig.split())
            words_b = set(prev.split())
            if words_a and words_b:
                overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
                if overlap > 0.6:
                    is_dup = True
                    break
        if not is_dup:
            deduped.append(s)
            seen_texts.append(text_sig)
    if len(deduped) < len(sections):
        logger.info(
            "Dedup removed %d/%d near-duplicate chunks",
            len(sections) - len(deduped), len(sections),
        )
    sections = deduped

    # Boost preferred document types
    if prefer_doc_types and sections:
        preferred = [s for s in sections if s["doc_type"] in prefer_doc_types]
        others = [s for s in sections if s["doc_type"] not in prefer_doc_types]
        sections = (preferred + others)[:top_k]
    else:
        sections = sections[:top_k]

    return sections



def _build_context_prompt(sections: List[Dict[str, Any]]) -> str:
    """Build the context prompt from retrieved sections."""
    if not sections:
        return "No relevant document sections were found for this query."

    parts = ["Here are the relevant document sections:\n"]
    for i, sec in enumerate(sections, 1):
        label = "PRIVATE (your upload)" if sec.get("visibility") == "private" else "PUBLIC (RRC)"
        parts.append(f"--- Section {i} [{label} · {sec['doc_type']} - {sec['section_name']}] ---")
        text = sec["section_text"]
        # Convert JSON to prose for better LLM comprehension
        if text and text.strip() and text.strip()[0] in ("{", "["):
            text = _json_to_prose(sec["section_name"], text)
        parts.append(text)
        parts.append("")

    return "\n".join(parts)


def _extract_citations(sections: List[Dict[str, Any]], max_excerpt_len: int = 200) -> List[Dict[str, str]]:
    """Build citation list from retrieved sections."""
    citations = []
    seen = set()
    for sec in sections:
        key = (sec["doc_type"], sec["section_name"])
        if key not in seen:
            seen.add(key)
            excerpt = sec["section_text"][:max_excerpt_len]
            if len(sec["section_text"]) > max_excerpt_len:
                excerpt += "..."
            citations.append({
                "doc_type": sec["doc_type"],
                "section_name": sec["section_name"],
                "excerpt": excerpt,
                "visibility": sec.get("visibility"),
            })
    return citations


def stream_research_answer(
    question: str,
    session: ResearchSession,
    top_k: int = 15,
) -> Generator[str, None, None]:
    """
    Stream an AI-generated answer to a research question with citations.

    Yields Server-Sent Event formatted strings:
    - data: {"type": "token", "content": "..."}\n\n
    - data: {"type": "citations", "citations": [...]}\n\n
    - data: {"type": "done"}\n\n
    - data: {"type": "error", "message": "..."}\n\n

    Also persists the question and answer as ResearchMessage rows.
    """
    try:
        # Save user message
        ResearchMessage.objects.create(
            session=session,
            role="user",
            content=question,
        )

        # Retrieve relevant sections
        sections = _retrieve_relevant_sections(question, session, top_k=top_k)
        citations = _extract_citations(sections)

        # Build prompts
        system_prompt = _build_system_prompt(session.api_number, session.state)
        context_prompt = _build_context_prompt(sections)

        # Include conversation history for continuity
        history_msgs = list(
            ResearchMessage.objects.filter(session=session)
            .order_by("-created_at")[:10]  # Last 5 exchanges
        )
        history_msgs.reverse()

        messages = [
            {"role": "system", "content": system_prompt},
        ]
        # Add prior conversation turns
        for msg in history_msgs:
            messages.append({"role": msg.role, "content": msg.content})
        # Add current question with context
        messages.append(
            {"role": "user", "content": f"{context_prompt}\n\nQuestion: {question}"},
        )

        # Stream completion
        client = get_openai_client(operation="research_rag")
        stream = client.chat.completions.create(
            model=MODEL_CHAT,
            messages=messages,
            stream=True,
            temperature=0,
            max_tokens=2048,
        )

        full_response = []
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                token = delta.content
                full_response.append(token)
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

        # Send citations
        yield f"data: {json.dumps({'type': 'citations', 'citations': citations})}\n\n"

        # Save assistant message
        assistant_content = "".join(full_response)
        ResearchMessage.objects.create(
            session=session,
            role="assistant",
            content=assistant_content,
            citations=citations,
            metadata={
                "model": MODEL_CHAT,
                "sections_retrieved": len(sections),
                "top_distance": sections[0]["distance"] if sections else None,
            },
        )

        # Done signal
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except Exception as e:
        logger.exception(f"Research RAG error: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


def get_chat_history(session: ResearchSession) -> List[Dict[str, Any]]:
    """Return serialized chat history for a session."""
    messages = session.messages.all()
    return [
        {
            "id": str(msg.id),
            "role": msg.role,
            "content": msg.content,
            "citations": msg.citations,
            "created_at": msg.created_at.isoformat(),
        }
        for msg in messages
    ]
