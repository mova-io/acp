# ACP — Architecture deck

Slide source. Every `##` is one slide; `---` separates them. Renders as-is in Marp, reveal.js,
Deckset or Slides.com, and reads fine as a plain document if nobody projects it.

Companion to `acp-overview-deck.md`, which covers *what ACP does* for a non-technical audience.
This one covers *how it is built* — service names, libraries, and the decisions behind them —
for an engineering or architecture-review audience. Customer-facing product name is **Movate
AccessOps**; **ACP** is the internal codebase name used throughout this deck.

**Everything here was read from the running system / `origin/main` on 2026-08-19**, not from
memory: container names and sizes from the deploy scripts (`deploy/public/*.sh`), libraries from
`api/requirements.txt` and the `.csproj` files, ADR numbers from `docs/adr/`. Re-check before
presenting if it has been a while — the topology moves. (This deck was itself materially stale
between 2026-07-29 and this refresh; the biggest corrections are the AI lane, the new sources,
and the deploy story.)

---

## How the deck is arranged, and why

Slides grouped in five movements. The order is deliberate: it answers the questions an
architecture reviewer asks *in the order they ask them*.

| # | Movement | The question it answers |
|---|---|---|
| 1 | **Shape** | What is deployed, where do documents come from, and what talks to what? |
| 2 | **Engines** | What actually reads a document — and why three of them? |
| 3 | **AI & observability** | Where do models sit, and how is every call made auditable? |
| 4 | **Flow** | What happens to one file, end to end? |
| 5 | **Honesty & ops** | How do we know it's right, how does it ship, and what is weak? |

Two things about the arrangement worth keeping if you re-cut it:

**Lead with the picture, not the stack.** The topology slide comes first. A reviewer cannot hold
a library list in their head until they know how many processes there are and which one is
public-facing.

**Put the weaknesses on the deck, not in the Q&A.** The last slide names the soft spots first.
Every architecture review finds them anyway; a deck that names them first is trusted on
everything else. (The old headline weakness — "the deploy is manual" — is now fixed; see the CD
slide.)

If you need a **5-minute cut**: the topology, the three engines, the AI lane, one-file-end-to-end,
and the honesty bar. That is the whole argument in five slides.

---

## ACP in one picture

Two first-party Container Apps run the identical image and differ only by entrypoint, plus a
small set of managed dependencies. Everything else is optional and opt-in.

```
                 ┌──────────────┐
   browser ──────►   acp-app     │  external ingress, 1 CPU / 2 GiB, single replica
                 │  FastAPI      │  uvicorn app:app  ·  serves the SPA + REST
                 └──────┬───────┘
                        │  Postgres job queue (ADR 0004)  ·  Redis scan-token durability
                 ┌──────▼───────┐
                 │  acp-worker   │  no ingress, same image, 1–3 replicas
                 │  worker_main  │  python -m worker_main  ·  scanning happens here
                 └──┬───────┬────┘
        ┌───────────┘       └───────────┐
 ┌──────▼───────┐                 ┌──────▼──────────┐
 │ document      │                 │ acpremediated    │
 │ sources       │  Drive ·        │ store (Blob)     │  remediated output,
 │ (read-only)   │  SharePoint ·   │ managed identity │  no keys (ADR 0010)
 └──────────────┘  SMB · local     └─────────────────┘

 managed deps:   Postgres (job queue + state) · Redis (scan tokens) · Log Analytics
 observability:  acp-langfuse-v3 (Azure VM: ClickHouse, PHI-safe traces) · acp-grafana (Postgres mode)
 optional AI:    acp-ollama GPU (T4, scale-to-zero, in-tenant) · RunPod serverless · cloud vision
```

**`acp-app` and `acp-worker` run the identical image** (`acp-app:<sha>-<ts>`) and differ only by
entrypoint. One build, two roles — so a capability fix cannot be live in the API and stale in the
worker, which is where scanning actually happens (ADR 0013, split worker tier). The CPU vision
floor runs **in-process**, not as a standing service; Redis is an **external managed** service via
`REDIS_URL`, not a container app.

---

## The Azure inventory

Resource group `mdk-accessibility`, one managed Container Apps environment, one registry.

| Resource | Type | Notes |
|---|---|---|
| `acp-app` | Container App | External ingress, port 8077. 1 CPU / 2 GiB, single revision |
| `acp-worker` | Container App | No ingress. Same image, `python -m worker_main`. Scale 1–3 (ADR 0013) |
| `acp-langfuse-v3` | **Azure VM** (`Standard_D4s_v3` + 128 GB Premium disk) | Langfuse **v3** — ClickHouse + Redis + MinIO + Postgres via docker-compose behind Caddy/TLS, **eastus2**. PHI-safe AI traces (see Observability). Replaced the v2 Container App |
| `acp-grafana` | Container App | Dashboards — provisioned only in Postgres mode |
| `acp-ollama` | Container App | **Optional / opt-in.** In-tenant **T4 GPU** (`NC8AS_T4`), internal :11434, **scale-to-zero** (`gpu_up.sh`, ADR 0022/0027) |
| `acp-app-staging` / `acp-worker-staging` | Container Apps | **Optional.** Unattended staging tier, separate DB (`staging_up.sh`) — dormant until provisioned |
| `mdkaccessibilityacr` | Container Registry | All first-party images |
| PostgreSQL Flexible Server | Managed DB | Job queue + scan state (or SQLite, single-instance, when `DATABASE_URL` unset) |
| Redis | Managed service | Cross-replica scan-token durability (`REDIS_URL`) — **required** once the worker tier is split |
| `acpremediatedstore` | Storage Account | Remediated output, **managed identity, no keys** (ADR 0010) |
| Log Analytics + alerts | Observability | `acp-5xx-errors`, `acp-replica-restarts`, `acp-dead-letter-jobs` |

No key-based auth to storage: the app's system-assigned identity holds **Storage Blob Data
Contributor**. Secrets (Google ADC, DB URL, Langfuse keys, provider keys) are Container App
secrets referenced as `secretref:`, never baked into the image, and **inherited on a bare
redeploy** so an image-only update never silently drops them.

> Corrections vs. the pre-2026-08 deck: there is **no standing `acp-redis` Container App**
> (managed Redis) and **no always-on `acp-ollama` at 4 CPU / 8 GiB** (the CPU floor is in-process;
> the only `acp-ollama` is the opt-in scale-to-zero GPU app).

---

## Document sources — where a scan starts

A scan reads documents from one of five source kinds, dispatched by `scanner._list(source, …)`.
Every source produces the **same** three-denominator inventory and feeds the **same** downstream
pipeline — a source is a new *adapter*, never a new pipeline.

| Source | Access | Notes |
|---|---|---|
| **Google Drive** | Keyless ADC or per-user "Sign in with Google" | The original source; scheduled sweeps |
| **SharePoint / OneDrive** | Entra (Microsoft Graph) | Multi-document-library per site, OneDrive fallback (`routes/sharepoint.py`) |
| **SMB network drives** | On-prem connector, read-only `svc-acp` | **New** (ADR 0032/0036) — see below |
| **local / folder** | Filesystem | Dev, tests, and the demo corpus |

**SMB is the pilot's third source and the one with the most interesting topology.** The connector
is the worker image + `api/smb_source.py` running **inside** the customer network, talking to
Azure over **outbound-only HTTPS :443**. PHI never leaves the perimeter to be *discovered*; only
findings and coverage egress; remediated bytes (if allowed) are written to a new
`\\…\ACP-Remediated` path, never over the original. Multi-share (`ACP_SMB_SHARES` = a list of UNC
roots), config-only preflight (`describe_smb_readiness`, touches no network), credential from Key
Vault via Managed Identity.

> Status to state honestly on a slide: **discovery logic + adapter shipped** and unit-tested
> against a fake filesystem; the live `smbprotocol` transport is **deployment-gated** and the two
> ADRs are still "Proposed". Frame SMB as "in pilot", not "live".

---

## Why three document engines

This is the part of the architecture that surprises people, so it is worth being direct: ACP
does not have *an* engine. It has three, split by what each format's ecosystem does well.

| Engine | Language | Handles | Why not the others |
|---|---|---|---|
| **Office analysers** | C# / .NET 10 | DOCX, XLSX, PPTX | Open XML SDK is the only first-party OOXML reader; the Python options misread real files (ADR 0012) |
| **PDF engine** | Python | PDF structure, tags, reading order | 41 modules, **now vendored in-tree** at `engine/pdf-analyser` (ADR 0029) |
| **`api/formats/`** | Python | Cross-format + HTML, capability registry | Where new per-format detectors land |

The cost is real — a build needs `dotnet build -c Release` *before* `az acr build`, because the
Dockerfile `COPY`s the compiled analyser. Forget it and the image build fails.

---

## Engine 1 — the .NET Office analysers

`engine/office-analysers/DigitalA11y.Analysers.DotNet`, invoked as a CLI
(`spike/dotnet/AcpScan.Cli`) and shelled out to per file.

- **Target:** `net10.0`
- **`DocumentFormat.OpenXml`** — first-party OOXML. Reads `w:docPr/@descr`, `xdr:cNvPr`,
  `a:rPr/@lang`, style tables, theme colours
- **`Microsoft.Extensions.DependencyInjection`** — rules are DI-registered, so adding a rule is
  a registration rather than an edit to a dispatch table
- **`Microsoft.Extensions.Options`** — per-rule configuration

Rules are one class per criterion — `AltTextRule`, `LinkPurposeRule`, `LanguageOfPartsRule`,
`ColourContrastRule`, `SheetNameUniquenessRule` — each mapped to a WCAG SC in
`config/rule-catalog.json`.

---

## Engine 2 — the PDF stack

PDF has no single library that does everything, so ACP uses several, each for what it is good at.

| Library | Used for |
|---|---|
| **`pikepdf`** (10.8) | Object model: `/StructTreeRoot`, `/AcroForm`, `/Alt`, `/TU`, content-stream rewriting |
| **`pdfplumber`** (0.11) | Geometry: `page.chars`, `page.rects`, per-glyph colour and bbox |
| **`pdfminer.six`** | Text extraction under pdfplumber |
| **`pypdf`** (6.14) | Metadata reads where pikepdf is awkward |
| **`pypdfium2`** | Page render — **pure wheel**, chosen to avoid poppler (GPL) and PyMuPDF (AGPL) |

Plus **`pytesseract`** for images-of-text OCR (1.4.5), wrapping the `tesseract-ocr` binary in the
image. (That apt install is the one flaky CI dependency — an unreachable mirror fails the OCR
criteria's gate, distinctly, without failing the whole suite.)

The **41-module PDF engine is now tracked in-repo** at `engine/pdf-analyser` (ADR 0029), so a CI
checkout can assemble a complete build context. This closed the old "vendored, not versioned"
supply-chain seam the previous deck flagged as a weakness.

---

## Engine 3 — the capability registry

The layer that makes per-format capability explicit rather than implied.

- **`api/capabilities.py`** — a `Capability` enum (TEXT, OCR, STRUCTURE, TAG_TREE, LINKS,
  TABLES, FORMS, ANNOTATIONS, COLOR, FONTS, READING_ORDER, DOM, ARIA, CSS) and a per-format
  baseline, adjusted per file: a PDF with no AcroForm loses FORMS; a scanned one swaps TEXT→OCR
- **`api/assessment.py`** — `Coverage` (UNSUPPORTED < DECLARED < HEURISTIC < PARTIAL < FULL) and
  `Confidence`. Only `FULL` may certify a pass
- **`api/rule_registry.py`** — `register(rule, fmt, detector, requires, coverage, confidence,
  reason)`. Adding a criterion to a format is one call plus a detector module
- **`api/formats/{docx,xlsx,pptx,pdf,html,office}/`** — the detectors themselves

`lxml` underpins the OOXML reading on the Python side. Certification is gated by **coverage, not
confidence** (ADR 0031), and the auto-apply / AI-assessed lanes are gated by ADR 0030.

---

## The control plane

`api/` — ~14 route groups over the FastAPI app.

| Library | Role |
|---|---|
| **FastAPI** 0.137 / **Starlette** / **uvicorn** | HTTP. `uvicorn app:app --port $PORT` |
| **Pydantic** 2.13 | Request/response models |
| **psycopg2-binary** | Postgres — job queue and scan state |
| **APScheduler** | Scheduled Drive sweeps |
| **httpx** | Outbound (Ollama/vision providers, Langfuse, webhooks) — hand-rolled, no vendor SDKs |
| **redis** | Cross-replica scan-token durability |
| **google-api-python-client** / **google-auth** | Drive discovery, keyless ADC |
| **msal / Microsoft Graph (httpx)** | SharePoint / OneDrive discovery, Entra |
| **azure-storage-blob** / **azure-identity** | Remediated output + Key Vault, managed identity |
| **reportlab** | Branded PDF conformance report |
| **langfuse** (>=2,<3) | PHI-safe spans; no-ops when env vars absent |

Route groups: `scans`, `drive`, `sharepoint`, `hitl`, `ai`, `assess`, `analytics`, `disposition`,
`scope`, `control`, `capability`, `rubric`, `campaigns`, `system` (+ the SPA). Grew from 9 with the
Discover/Assess lifecycle, the review copilot, the disposition queue, SharePoint, and per-user scope.

---

## AI — local floor → GPU → governed cloud

**No commercial LLM SDK is in the dependency list.** Every provider adapter is hand-rolled `httpx`
against the vendor API, so there is no key and no SDK in the build. `api/providers.py` defines
**five vision adapters**, each with a declared `privacy_zone`:

| Adapter | Zone | Role |
|---|---|---|
| **Ollama** (local CPU floor) | local 🟢 | Default, keyless; also the vehicle for the in-tenant GPU |
| **Azure OpenAI** | tenant | Enterprise-safe first cloud — model runs in the customer's own Azure |
| **OpenAI** | cloud | Customer-approved public cloud |
| **Anthropic** (Claude) | cloud | Customer-approved public cloud |
| **RunPod serverless** | cloud | Qwen2.5-VL GPU on a scale-to-zero serverless endpoint |

**The escalation ladder (ADR 0019/0022/0030):** a local CPU draft is quality-gated by
`_is_usable_alt` — a weak model that returns non-empty **garbage** (symbol runs, single tokens) is
rejected. The image then escalates to a configured cloud provider **only when an admin enabled one
and its secret is present**; otherwise it defers to a human. Provenance records the transparent
numbered escalation path, the provider that **actually served** (never claims GPU when CPU ran),
and `cost_usd`.

**Two GPU options, chosen by governance posture:**
- **In-tenant Azure T4** (`acp-ollama`, `gpu_up.sh`): Ollama on a Container Apps GPU profile in the
  *same* environment as acp-app, **internal ingress** on :11434, scale-to-zero. Reached by pointing
  `OLLAMA_BASE_URL` at the internal FQDN — no code change, live via a 30s-TTL endpoint refresh.
  Internal ingress is deliberate: it keeps the zone chip **local 🟢** (data never leaves the tenant).
- **RunPod serverless**: burst GPU, `zone=cloud`, out-of-band.

Text model default is **`llama3.2`**; vision floor **`moondream`**. **The standing product rule:**
anything with a model in the decision path caps at *drafted-then-approved* — ACP will not certify a
pass on a generated judgement. That is why the capability grid has a remediation axis at all.

---

## Observability — PHI-safe AI tracing (Langfuse)

Every model call is auditable without any document content leaving the boundary. `api/lf.py` is a
full tracing layer that is an **env-gated no-op** — with `LANGFUSE_SECRET_KEY` absent, every call
returns a `_Noop` and nothing is sent.

**Version — now on Langfuse v3 (ClickHouse).** ACP moved from v2 to **v3** on 2026-08-19. The trigger
was concrete: with the enriched per-file traces, a real **44-document scan hung the v2 Session view**
(v2 on a single Postgres renders every trace in a session at once). v3 re-architects the trace store
onto **ClickHouse** (columnar, built for high-volume, high-cardinality trace/cost queries), with
**Redis** (queue/cache) and **MinIO** (event/media blobs), and splits the app into **web + worker** —
which is what makes the aggregate Session view scale. Project **`acp-compliance`**, wired onto **both**
acp-app and acp-worker (`set_integration_env.sh` — Langfuse on the API alone would miss the worker,
where scanning runs).

**Why a VM, not Container Apps.** ClickHouse needs real local disk and does **not** run reliably on
Azure Container Apps (ACA offers only Azure Files/SMB mounts, which ClickHouse fights). So v3 runs on a
**dedicated Azure VM** — `acp-langfuse-v3`, `Standard_D4s_v3` (16 GB) + a 128 GB Premium data disk —
via docker-compose (ClickHouse · Redis · MinIO · Postgres · web · worker) behind **Caddy** with
automatic Let's Encrypt TLS, at `acp-langfuse-v3.eastus2.cloudapp.azure.com`.

**The cutover was host-only.** v3 was built alongside v2 and verified (health 200, `acp-compliance`
project, ingestion 207) before flipping `acp-app`/`acp-worker`'s `LANGFUSE_HOST` — keys re-seeded, so
**no application change**; ACP's client and PHI invariant are version-independent, so what ACP *sends*
is unchanged. Old v2 (the container app on shared Postgres) was deleted; its trace data was
deliberately not migrated (start-fresh). Runbook + compose live in `deploy/langfuse-v3/` (#447).

**What it captures.**
- **Two traces per scan.** A **Scan/Discover** trace (discovery + optional PII deep-scan, carrying
  file/rule spans) and a **separate Assess** trace (`{scan_id}-assess`) carrying the per-rule ✓/✗
  WCAG assessment and the **0–100 `compliance_score`**.
- **Span tree:** trace → one `file_span` per document → one child `rule_span` per WCAG rule, plus
  `pii_span` (counts + masked samples, ADR 0006) and `error_span` (un-evaluable rule). AI calls are
  Langfuse *generations* carrying model, token usage, **cost**, provider and processing zone.
- **Idempotent:** deterministic observation/score ids, so an Assess re-run or worker retry
  **updates** the existing span instead of appending duplicates.

**The PHI invariant (the headline).** The filename is treated as PHI. `_doc_label` **HMAC-redacts**
every filename to `doc-<6hex>.<ext>`, keyed by a salt (defeats a dictionary attack) — not a bare
hash. Completion text is **never sent**; spans carry **counts and lengths**, not content. It is
**fail-safe redacted by default**; only the public synthetic-corpus demo opts out
(`ACP_TRACE_FILENAMES=plain`). Per-user attribution: trace `user_id` + `user:` tag from the
signed-in email.

---

## One file, end to end

```
Drive / SharePoint / SMB discovery ──► fingerprint ──► enqueue (Postgres) ──► worker claims
                                                                                 │
                       ┌─────────────────────────────────────────────────────────┤
                       ▼                    ▼                    ▼
                 .NET analyser        PDF engine           api/formats
                 (docx/xlsx/pptx)        (pdf)            (html, cross-format)
                       └─────────────────────┬───────────────────┘
                                             ▼
                                       findings + outcome
                                     PASS │ FAIL │ REVIEW
                                             │
                       ┌─────────────────────┴─────────────────┐
                       ▼                                       ▼
                deterministic fix                      HITL review queue
             (written, then re-scanned)         (AI draft — human approves / edits /
                       │                          marks Not applicable)
                       └───────────────┬───────────────────────┘
                                       ▼
                          remediated file ──► Blob (managed identity)
                          + before/after evidence ──► conformance report
```

Discovery and assessment are **separate phases** (ADR 0020) — discovery is cheap and rescannable,
assessment is expensive and idempotent. The review queue is the guided Remediate workspace:
select a finding, understand it, act (approve / edit the draft / defer / mark not applicable),
auto-advance to the next.

---

## Which SC you scan for decides whether the GPU runs

The vision/GPU lane is entered **only** for image‑understanding criteria; everything else is
CPU‑deterministic and never touches a GPU — so a scan's cost and latency track which WCAG SCs are
in scope.

```
  per-file assessment ── each detector = one WCAG SC ──┐
                                                        ▼
        ┌──────────────── which SC? ─────────────────────┐
        ▼                                                 ▼
  DETERMINISTIC SCs                               VISION SCs  (must SEE the image)
  1.4.3 contrast · 3.1.1 lang · 1.3.1 headings    1.1.1 alt text · 1.4.5/1.4.9 images-of-text
  tables · forms · link text                      scanned-PDF reading order (ADR 0027)
        │                                                 │
        ▼                                                 ▼
   CPU analysers only                         ┌────────── VISION LANE ──────────┐
   PASS / FAIL / REVIEW                        │ [1] Ollama moondream   CPU 🟢    │
        │                                      │       │ acceptance gate: weak?  │
        │                                      │       ▼ escalate                │
        │                                      │ [2] GPU: Azure T4 🟢 / RunPod 🟡 │
        │                                      │       ▼ still weak & admin-on    │
        │                                      │ [3] Cloud: AzureOpenAI / OpenAI /│
        │                                      │       Anthropic 🟡               │
        │                                      │       ▼ none enabled             │
        │                                      │ [4] Defer to a human (HITL)      │
        │                                      └────────────────┬─────────────────┘
        └──────────────────► findings ◄─────────────────────────┘
                                 │
      deterministic fix (write → re-scan → certify)   OR   AI-drafted alt text
                                 ▼
      HITL review → Blob (managed identity) + conformance report
      every model call → Langfuse  (cost · zone · filename HMAC-masked, PHI-safe)
```

| Success criterion | Vision? | Compute |
|---|---|---|
| 1.1.1 alt text · 1.4.5 / 1.4.9 images‑of‑text · scanned‑PDF reading order | **Yes** | vision lane: CPU floor → GPU → cloud |
| 1.4.3 contrast · 3.1.1 language · 1.3.1 headings · tables · forms · link text | No | CPU analysers, no model |

---

## The compute & GPU tiers, and why the GPU is region‑bound

```
 ┌──────── REGION A  (US — current) ────────┐   ┌──── REGION B  (e.g. Asia — pattern) ────┐
 │ acp-app · acp-worker    one ACA env       │   │ acp-app · acp-worker  (replicated)      │
 │ acp-ollama (GPU) ─ SAME env/region ─ T4   │   │ acp-ollama (GPU) ─ T4 IF the region      │
 │ Postgres · Redis · Blob  (PHI in-region)  │   │ offers it (SKU discovered, else A100/…)  │
 └───────────────────────────────────────────┘   └──────────────────────────────────────────┘
   acp-langfuse-v3 → eastus2 VM (ClickHouse)       RunPod serverless GPU → GLOBAL  🟡 cloud
```

The in‑tenant GPU **must share the app's ACA environment**, and an environment is one region — so a
GPU app in another region defeats its own in‑tenant/low‑latency purpose (`gpu_up.sh` says exactly
this). Serving **Asia** means replicating the environment there; the T4 SKU is **discovered from that
region's supported list** (`ACP_GPU_FIND_REGION`), not hard‑coded.

| Tier | Where | Hardware / model | Zone | Scale |
|---|---|---|---|---|
| **CPU floor** | in‑process (acp‑app/worker, 1 CPU/2 GiB) | Ollama `moondream` / `llama3.2` | 🟢 local | always |
| **Azure T4 (in‑tenant)** | `acp-ollama`, same env | ACA profile **`NC8AS_T4`** = 8 vCPU + 1× **NVIDIA T4 (16 GB)**; `qwen2.5vl:7b` | 🟢 local | **scale‑to‑zero** |
| **RunPod serverless** | RunPod cloud (global) | serverless GPU (size configurable); `Qwen2.5‑VL‑7B` | 🟡 cloud | scale‑to‑zero |
| **Cloud vision** | provider API | Azure OpenAI (tenant) / OpenAI / Anthropic | 🟡 (Azure = tenant) | n/a |

> **Honest caveats for the slide:** no Asia region is deployed today (a single ACA env + Langfuse in
> eastus2); Region B is the deploy *pattern*, not a live region. And the RunPod GPU **class** is
> configurable — the repo pins the model, not the GPU tier.

---

## The honesty bar & the status model

The design constraint that costs the most and matters the most: **ACP will not report a pass it
cannot evidence.**

- **Three outcomes, not two.** `PASS`, `FAIL`, `REVIEW`. `REVIEW` is "a person must decide", and
  it is structurally enforced — a (rule, format) whose detector cannot certify a pass can never
  return one.
- **Coverage gates certification** (ADR 0031), not confidence. Only `Coverage.FULL` may certify.
- **Fixes are verified, not assumed.** Every deterministic fix re-scans the output and credits the
  criterion only if the finding is gone (ADR 0009); verification (Written → Re-scan → Certified) is
  shown only *after* a fix is saved.
- **The status model is a partition (ADR 0026).** `derive_file_status` splits in-scope criteria into
  mutually-exclusive buckets (auto-verified, human-verified, needs-review, needs-remediation,
  not-automatically-assessable). **`not_applicable` sits OUTSIDE `in_scope`** — a reviewer marking a
  criterion N/A *leaves the coverage denominator*, so it **raises** reported coverage %. Two
  distinct questions are tracked: **coverage** ("did ACP look?") vs **status** ("is it ready?").
- **Three denominators, never one.** Discover the *whole* estate; assess and remediate only what ACP
  supports. An auditor sees discovered → assessable → remediated, not one flattering percentage.

On 2026-07-29 this bar caught a real regression: the PDF contrast fixer assumed a white page and
was rewriting compliant dark-theme documents from 21:1 to 3.66:1. A fixture found it; reading the
diff would not have.

---

## The WCAG capability matrix

A public, per-cell statement of what ACP can do — criteria × formats × 2 axes.

- **Assessment:** A4 Fully Assessed · A3 Potential Issue · A2 Human Assessment Required · N/A
- **Remediation:** R4 Automatically Fixed · R3 AI Generated Fix · R2 Guided · R1 None · N/A

Each cell carries a **rule-inherent ceiling** — the best any tool could do, distinct from what ACP
has built. Cells move on observed detector runs, never on a reading of a diff.

Kept in step by `repository_dispatch` from acp → wcag-matrix on every push to `main`:
`acp-progress-log` (a curated changelog entry) and `acp-capability-change` (re-derive the ceiling
and open a PR for any cell claiming more than the code supports).

---

## The same bar, turned inward — ACP's own conformance report

The capability matrix says what ACP can do to *your* documents. The ACR workspace answers the
question a procurement team actually asks: **what is true of ACP's own UI?** It produces an
Accessibility Conformance Report — the completed-VPAT artifact — about ACP itself.

- **Four editions, built from catalogs.** ITI publishes WCAG, 508, EU and INT; each obliges a
  different requirement set, and the matrix is generated per edition — 55 WCAG criteria, or **489
  rows** for INT (55 WCAG + 120 Section 508 + 314 EN 301 549 clauses). An edition this deployment
  cannot honestly produce is refused, at creation and again at publication.
- **An automated pass is never a "Supports."** An axe-core run is evidence; a person decides. Four
  conformance terms exist and no internal workflow state may appear where one belongs — enforced in
  code, not by convention.
- **Publication is gated, then frozen.** Undecided criteria, stale evidence, missing tester metadata
  or an incomplete manual plan all block it. What ships is an immutable snapshot with a content
  digest re-verified on read; a correction is a new revision, never an edit.
- **The exported report must itself be accessible.** One projection renders to JSON, HTML, PDF and
  DOCX, and the Word export is *refused* if it fails ACP's own docx analyser. A conformance report
  nobody with a screen reader can read is the one document this product cannot hand over.
- **It follows the ITI VPAT® template's structure without vendoring the file** — a licensing
  decision, recorded in ADR 0053. Whether the output may be *called* a VPAT® is a separate
  service-mark question, still open; every format says on its face that it is not one.

Same honesty bar as the scan side, pointed at ourselves: counts rather than a compliance score, and
no claim ACP cannot evidence.

---

## Build & deploy — two automated chains

The deploy is **a pipeline** rather than a laptop (the old #1 weakness); who *starts* it is a
separate setting, and it is currently a person. CalVer `YYYY.M.D.N` — the count of the
day's Pacific-midnight revisions + 1 — is baked in as a build arg and surfaced at `/healthz`;
`/readyz` reports worker-tier heartbeat age and PDF-engine availability separately.

**Chain A — production (manually triggered, human-approved).** `.github/workflows/deploy.yml` is
dispatched by a person at Actions → deploy → Run workflow, gated by the **`production` GitHub
Environment (required-reviewer approval)**. Its `workflow_run`-on-green-`main` trigger is still
declared but skipped unless the repo variable `PRODUCTION_AUTO_DEPLOY_ENABLED` is `1`; it is unset. It runs `deploy/public/redeploy.sh`, which independently **refuses a
sha whose CI isn't green**, builds in ACR from an isolated clone, and updates acp-app + acp-worker
to the **same image** (or the fixes ship nowhere useful).

**Chain B — staging (unattended).** `.github/workflows/deploy-staging.yml` + `staging_up.sh` ship
the **same green commit** to `acp-app-staging` + `acp-worker-staging` (separate DB) with **no
reviewer gate**, so `main`'s tip is live within minutes. Dormant until a `STAGING_FQDN` repo
variable is set; reuses `redeploy.sh` unchanged.

```
# The manual path still exists and is identical to Chain A's core:
./deploy/public/redeploy.sh          # image-only update; env + 27 vars / 9 secrets survive
```

Image-only updates via `az containerapp update --image` — first-deploy `deploy.sh` re-derives all
environment (new access code, DB, secrets) and is the wrong script for a live app.

---

## Where it is weak

Named deliberately — every one of these is real as of 2026-08-19.

- **SMB live transport is deployment-gated.** Discovery logic + the adapter are shipped and tested
  against a fake filesystem, but `smbprotocol` isn't in `requirements.txt` yet and the connector
  runs on-prem — so "network drives" is *in pilot*, not live for a hosted customer.
- **No alerting when a deploy is left unapproved.** Chain A links a merge to the container, but
  nothing pages when the `production` approval sits unactioned or a revision comes up healthy yet
  routes no traffic — prod can fall behind `main` silently.
- **The GPU vision lane is opt-in and cold-starts.** The in-tenant T4 (`acp-ollama`) is
  scale-to-zero, so the first image after idle pays a model-load cost; RunPod serverless has the
  same trade. Quality escalation to cloud needs an admin to enable a provider.
- **Concurrency discipline is documented, not enforced.** `CLAUDE.md` carries hard-won rules for
  many parallel sessions on one checkout; nothing mechanically prevents breaking them.
- **Langfuse v3 is a self-managed VM now.** The trace-volume ceiling is solved (ClickHouse scales the
  Session view), but v3 runs as a **hand-provisioned Azure VM** (docker-compose behind Caddy) rather than
  a managed Container App — a box to patch, back up, and keep TLS current, provisioned by a runbook rather
  than the CI pipeline. Its v2 trace history was not migrated (start-fresh), and stopping the VM to save
  cost also stops tracing.

The retired weaknesses (worth saying, because the old deck led with them): the manual laptop
deploy is **replaced by automated CD**, and the PDF engine is **now versioned in-repo** (ADR 0029).
