# My Data — Proprietary Tenant Data Strategy
## Full Scope & Phased Roadmap

---

## Problem Statement

Public regulatory filings (W-2, W-15, L-1) are historical snapshots. A completion report from 1987 tells you how a well was built, not what condition it's in today. Operators hold data the regulator doesn't: casing inspection surveys, internal logs, workover records, environmental assessments, and firsthand field observations.

**The gap between what regulators see and what operators know is where liability hides.**

My Data is the platform layer that closes this gap — letting tenants upload their proprietary records so the platform can reason over public + private data together to generate better plans, more accurate liability scores, and more defensible regulatory submissions.

---

## Strategic Value Chain

```
Public Data (W-2, W-15, L-1, GAU)
        +
My Data (private uploads, operator records, structured overrides)
        │
        ▼
┌────────────────────────────────────────────────────────┐
│              Unified Well Knowledge Layer               │
│   Shared public vectors + tenant-scoped private vectors │
└────────────────────────────────────────────────────────┘
        │
        ├──► Plugging Plan Generation (Phase 3)
        │     More accurate plug specs, informed barrier placement,
        │     current-condition-aware cement calculations
        │
        ├──► Environmental Liability Score (Phase 4)
        │     Three views: Public-only floor | Public+MyData truth | Delta
        │
        ├──► Due Diligence Reports (Phase 5)
        │     "Regulatory record vs. what you actually know"
        │     Critical for acquisitions and divestitures
        │
        ├──► Fleet Risk Ranking (Phase 4)
        │     Rank all wells by plugging liability across both signals
        │
        └──► Research Chat (Phase 2)
              Answers cite source: [Public: W-2] or [My Data: casing_survey.pdf]
```

---

## Data Taxonomy

| Category | Examples | Gap Filled |
|---|---|---|
| **Downhole condition logs** | CIS (casing inspection), MFC (multi-finger caliper), temperature/cement bond logs | Public W-2 says what was installed; private says what's still intact |
| **Internal well logs** | Gamma ray, resistivity, sonic logs never submitted to regulator | Better formation tops, more precise barrier placement |
| **Workover / recompletion records** | Internal workover reports, stimulation records, perforation changes | Public record often incomplete or filed late |
| **Environmental surveys** | Phase I/II ESAs, groundwater monitoring, soil samples | Baseline liability picture, contamination disclosure |
| **Site condition data** | Field photos, wellhead condition notes, surface access | Risk scoring, mobilization cost estimates |
| **Financial / liability data** | Internal cost estimates, bond documents, insurance records | Accurate liability calculations |
| **Structured operator data** | Tubing sizes, current pressures, internal production history | Fill fields public filings don't capture |
| **Schematics / as-builts** | Internal diagrams, plats, deviation surveys | WBD accuracy, plug placement geometry |

**File types to support (phased):**
- Phase 1–2: PDF (already works via DocumentUploader)
- Phase 3+: LAS (well logs — industry standard binary format)
- Phase 4+: CSV (structured data imports), JPEG/PNG (site photos)

---

## Architecture

### Data Isolation Model

```
public schema (shared)
  ├── DocumentVector  (public_source = True)  → visible to all tenants
  └── ExtractedDocument  (public filings)

tenant_X schema
  ├── DocumentVector  (public_source = False, tenant-scoped)  → only tenant_X
  └── ExtractedDocument  (tenant uploads)
```

Private uploads follow the same pipeline as public docs:
`PDF → OpenAI extraction → structured JSON → DocumentVector (pgvector)`

The only difference: private vectors are stored in the tenant schema, not the public schema.
The RAG query merges both pools — public vectors from the shared schema + tenant-private vectors from the tenant schema.

### Source Attribution

Every retrieved section gets a `source_type` label:
- `public` — from regulatory filings
- `tenant` — from tenant-uploaded documents

This flows through to:
- Research chat citations: `[My Data: casing_survey.pdf - Inspection Results]`
- Plan generation: fields show their source (public / my data / derived)
- Liability score: computed separately for each data set, then merged

---

## UI: My Data Tab (replaces Documents tab)

The **Documents** tab is removed from Well Detail. **My Data** absorbs its function and elevates it.

### Layout

```
MY DATA
─────────────────────────────────────────────────────────
 Private Documents                          [+ Upload]

  ┌───────────────────┬───────────┬──────────┬───────────┐
  │ File              │ Type      │ Sections │ Indexed   │
  ├───────────────────┼───────────┼──────────┼───────────┤
  │ casing_survey.pdf │ Schematic │ 12       │ ✓         │
  │ workover_2023.pdf │ W-2       │ 8        │ ✓         │
  └───────────────────┴───────────┴──────────┴───────────┘

─────────────────────────────────────────────────────────
 Well Component Overrides                   [+ Add]

  ┌────────────────┬──────────┬──────────┬────────────────┐
  │ Component      │ Type     │ From     │ To             │
  ├────────────────┼──────────┼──────────┼────────────────┤
  │ Surface Csg    │ Casing   │ 0 ft     │ 500 ft         │
  └────────────────┴──────────┴──────────┴────────────────┘
```

Long-term: every field in a plugging plan shows a source badge — **Public Filing** / **My Data** / **Derived**. When My Data overrides a public field, the plan shows why.

---

## Phased Roadmap

---

### Phase 1 — Foundation (Merge & Activate)
**Goal:** Remove Documents tab, absorb it into My Data, enable Add Component button.

**Scope:**
- `WellDetail.tsx`: Remove "Documents" tab. In "My Data" tab, add two sections:
  - "Private Documents" — DocumentUploader component (moved from Documents tab)
  - "Well Component Overrides" — existing component table with Add Component button enabled
- `WellDetail.tsx`: Wire "Add Component" button → modal form (component_name, component_type, depth_from, depth_to). Calls `POST /api/tenant/wells/<api14>/components/` (endpoint already exists).
- `DocumentUploader.tsx`: Add "Sections Found" column showing count from ExtractedDocument after upload.

**Backend:** No new endpoints needed.

**Key files:**
- `v0-regul-agent/.../src/pages/Regulagent/WellDetail.tsx`
- `v0-regul-agent/.../src/components/documents/DocumentUploader.tsx`
- `v0-regul-agent/.../src/lib/api/wells.ts` (add `createWellComponent` function)

**Verification:**
1. Documents tab gone from WellDetail
2. My Data shows uploaded files + component overrides in two sections
3. Upload a PDF → appears in table with section count
4. Add a tenant component (e.g., surface casing 0–500 ft) → appears in table

---

### Phase 2 — Source Attribution in Research Chat
**Goal:** Tenant-uploaded documents flow into the RAG pipeline. Citations distinguish public vs private sources.

**Scope:**
- Confirm `DocumentVector` placement: tenant-uploaded vectors must live in the tenant schema, not the public schema. Audit `models.py` and `tasks_research.py` before implementing.
- `research_rag.py` (`_retrieve_relevant_sections`): merge public schema vectors + tenant schema vectors in a single ranked result.
- `research_rag.py` (`_extract_citations`): include `source_type` in citation objects.
- `Research.tsx`: citation badges show `[My Data]` vs `[Public]` with distinct styling.
- System prompt: acknowledge My Data sections as higher-trust (operator firsthand) vs public filings.

**Key files:**
- `regulagent-backend/apps/public_core/services/research_rag.py`
- `regulagent-backend/apps/public_core/models.py`
- `regulagent-backend/apps/public_core/tasks_research.py`
- `v0-regul-agent/.../src/pages/Regulagent/Research.tsx`

**Critical open question:** Are tenant-uploaded documents currently stored in the public schema? If so, Phase 2 requires a schema migration for `DocumentVector` + `ExtractedDocument` to support tenant-scoped records.

---

### Phase 3 — Plan Generation with My Data
**Goal:** Tenant component overrides and uploaded data feed into plugging plan generation as higher-priority facts.

**Scope:**
- Plan fact builders: query tenant-layer `WellComponent` records first (layer=tenant), fall back to public/derived.
- Each plan fact carries a `source` label: `public_filing` / `my_data` / `derived`.
- Plan detail UI: source badge next to key fields (formation tops, barrier depths, casing OD/weight).
- If tenant uploaded a casing inspection showing corroded casing, cement calculations reflect current condition.

**Key files:**
- `regulagent-backend/apps/public_core/services/research_supplement.py`
- `regulagent-backend/apps/kernel/services/` (plan fact builders — TX and NM)
- `v0-regul-agent/.../src/pages/Regulagent/PlanDetails.tsx`

---

### Phase 4 — Environmental Liability Score
**Goal:** Per-well liability score with three views: public-only floor, public+my data truth, and the delta (hidden exposure).

**Score inputs:**
- Well depth, formation type, proximity to groundwater (public)
- Casing integrity, corrosion data (from My Data uploads)
- Known contamination markers (from Phase I/II ESA uploads)
- Plugging complexity estimate (from plan generation)

**Three views:**
1. **Floor** — public data only. What any member of the public or regulator could calculate.
2. **True** — public + My Data. What the operator knows.
3. **Delta** — the undisclosed exposure gap. Critical for M&A/divestiture due diligence.

**UI additions:**
- Liability score card on WellDetail (Well Data tab or new Liability tab)
- `WellRegistry.tsx`: sortable by liability score for fleet-level risk ranking

**Key files:**
- `regulagent-backend/apps/public_core/services/liability_scorer.py` (new)
- New API endpoint on well detail view
- `v0-regul-agent/.../src/pages/Regulagent/WellDetail.tsx`
- `v0-regul-agent/.../src/pages/Regulagent/WellRegistry.tsx`

---

### Phase 5 — Due Diligence Report
**Goal:** Exportable report comparing regulatory record vs operator knowledge. Used for acquisitions, divestitures, and internal audits.

**Report sections:**
1. Well Summary
2. Public Regulatory Record (what RRC/NMOCD has)
3. Operator Data Summary (what's in My Data)
4. Gap Analysis (fields in public record that My Data contradicts or supplements)
5. Liability Assessment (three-view score from Phase 4)
6. Recommendations

**Gated to:** plan/enterprise tier.

---

### Phase 6 — Advanced Data Types (Future)
- **LAS file support**: Parse binary well log files, extract curves (GR, RHOB, NPHI, etc.), store as structured vectors for formation analysis.
- **CSV imports**: Bulk import of structured operator data (production history, pressures, workovers).
- **Photo attachments**: Site condition photos with condition tagging (wellhead integrity, surface equipment, road access).
- **Multi-well data packages**: Upload a ZIP containing multiple wells' data in one operation.

---

## Key Decisions to Resolve Before Phase 1

| Decision | Options | Recommendation |
|---|---|---|
| DocumentVector schema placement | Public schema (current) vs tenant schema | Tenant schema — private data must be schema-isolated |
| Upload file size limit | TBD | 50MB per file, 500MB per well |
| LAS file timeline | Phase 2 vs Phase 6 | Phase 6 — PDF covers 80% of near-term value |
| Liability scoring model | Rules-based vs ML | Rules-based first — auditable, faster to ship |
| Due diligence report format | PDF export vs web view | Web-first, PDF export in Phase 5 |

---

*Created: 2026-04-30*
