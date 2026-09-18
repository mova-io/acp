import { SIM, simIdentity, simGetSources, simStartScan, simGetJob, simGetScan, simListScans, simRules, simRemediationDiffs } from './sim.js'
import { CAPABILITY_FALLBACK } from './capability.js'
import { SCOPE_UNIVERSE } from './scopePresets.js'
import { TRACKED_17 } from './ruleDetails.js'

import { authEpoch, noteAuthChange } from './apiIdentity.js'
import { refreshSPToken } from './spAuth.js'
const BASE = import.meta.env.VITE_API ?? 'http://localhost:8077'

// Per-user tokens (real mode only). In SIM mode nothing touches a real Drive / OneDrive.
let driveToken = null
let spToken = null
let googleToken = null  // GIS Bearer token (auth mode = "gis")
let msToken = null      // Microsoft (Entra) access token — the API Bearer for a Microsoft sign-in
// Identity changes are recorded in apiIdentity.js — see that module for why it is separate.
// Anything caching per-user data keys on it, so a response fetched for one account can never be
// handed to the next.
export const setDriveToken = (t) => { driveToken = t }
export const setSPToken = (t) => { spToken = t }
export const setGoogleToken = (t) => { const w = googleToken; googleToken = t; noteAuthChange(w, t) }
export const setMsToken = (t) => { const w = msToken; msToken = t; noteAuthChange(w, t) }
export const clearAllTokens = () => {
  const had = googleToken || msToken
  googleToken = null; msToken = null; driveToken = null; spToken = null
  noteAuthChange(had, null)
}
// Re-exported deliberately. `SIM` is defined in sim.js and most modules import it from there, but
// the transport wrappers below are the reason a caller cares about it at all — "is there a server
// to talk to?" — so a module that imports one of them reaches for `SIM` here. Without this line
// `import { SIM } from './api.js'` type-checks in vitest (a module mock supplies it) and FAILS THE
// PRODUCTION BUILD with `[MISSING_EXPORT] "SIM" is not exported by "src/api.js"`, which is a green
// suite and no bundle. changeReview.js shipped exactly that shape on this branch.
export { SIM }
export const getToken = () => googleToken || msToken || null
// The Authorization bearer is Google's token when present, else the Microsoft one — tagged with
// X-Auth-Provider so the backend verifies it against the right issuer (Graph, not Google's
// tokeninfo). Without that tag a Microsoft sign-in has no bearer the backend accepts and every
// call 401s the instant the user is in.
const headers = (extra = {}) => ({
  ...extra,
  ...(googleToken ? { 'Authorization': 'Bearer ' + googleToken }
      : msToken ? { 'Authorization': 'Bearer ' + msToken, 'X-Auth-Provider': 'microsoft' } : {}),
  ...(driveToken ? { 'X-Drive-Token': driveToken } : {}),
  ...(spToken ? { 'X-SP-Token': spToken } : {}),
})

// Shadow-only transport wiring. It deliberately reuses the normal ACP bearer/provider headers:
// the browser never receives the gateway's internal credential and never chooses owner_scope.
export const getRealtimeStreamRequest = () => ({
  endpoint: `${BASE}/api/realtime/v1/stream`,
  headers: headers(),
})
export const getRealtimeAuthoritativeSnapshot = () =>
  fetch(`${BASE}/api/realtime/v1/status`, { headers: headers() }).then(j)

// Why the user was bounced to the sign-in screen. Carried on the event so that screen can SAY
// it. Being silently returned to sign-in, mid-task, with no explanation is indistinguishable
// from the app having lost your work: the token lives only in this module, so a reload or an
// expiry drops it, and every subsequent click quietly 401s.
export const SESSION_EXPIRED =
  'Your session expired, so you were signed out. Sign in again to pick up where you left off — anything already saved is safe.'

// Why a scan the client is holding cannot be loaded. Scans are scoped to the user who ran them
// (per-user isolation, 0a1b167), and a per-scan read for somebody else's scan answers 404 — not
// 403 — so an id is not an existence oracle across accounts. That is correct, and it is also
// invisible: found live 2026-07-29, an allow-listed user's client held an id it could never
// load and re-requested /traces and /diff for ~60s with every failure ending in
// `.catch(() => null)`. Assess produced no score and said nothing. An empty score the user
// cannot explain is worse than an error, so say it and move them somewhere that works.
export const SCAN_UNAVAILABLE =
  'That scan isn’t available to your account — scans are private to the person who ran them, and this one may belong to another account or have been removed.'

// Backend detail for exactly this case. Coupled deliberately, and NOT by status alone: a 404
// from a per-file or per-trace route under a scan the user does own is a different problem and
// must not evict their scan. tests/test_scan_not_found_detail.py pins the string server-side so
// this coupling cannot drift silently.
const SCAN_NOT_FOUND_DETAIL = /^scan not found$/i

// Backend marker for "Ollama answered, but the model it would need is not pulled". Coupled
// deliberately and NOT by status alone: /ai/suggest also 503s when the model IS pulled and the
// call merely failed, and that one must stay retryable per card. api/routes/ai.py owns the
// string (MODEL_NOT_PULLED) and tests/test_ai_model_not_pulled_detail.py pins it server-side.
//
// Flagged rather than rewritten: the server's detail already names the model and the `ollama pull`
// that fixes it, and the review card renders it verbatim. This only tells autoDraft.js that
// re-asking is pointless — see the circuit breaker there.
const AI_MODEL_NOT_PULLED_DETAIL = /\bmodel not pulled\b/i
// The scan id out of `<base>/scans/<id>[/...]`. Read off the Response, so it works for every
// wrapper below without threading the id through each one.
const SCAN_URL_RE = /\/scans\/([^/?#]+)/

const j = async (r) => {
  if (r.status === 401) {
    // Only a GATE 401 (the access gate rejected the session's bearer) signs the user out. A
    // ROUTE 401 — an endpoint refusing for its own reason, e.g. a Drive-only route hit by a
    // Microsoft user — must NOT eject an authenticated user or clear their token. The backend
    // marks its gate 401s with `X-Acp-Auth: session`; without it, this is a route error and
    // falls through to the normal !r.ok handling below. Found 2026-08-11: /sources 401'ing a
    // Microsoft user with no Google Drive bounced the whole session to "expired".
    if (r.headers.get('X-Acp-Auth') === 'session') {
      googleToken = null; msToken = null
      noteAuthChange(1, 0)   // same identity change as clearAllTokens; caches must not survive it
      window.dispatchEvent(new CustomEvent('acp:session-expired', { detail: { reason: SESSION_EXPIRED } }))
      throw new Error(SESSION_EXPIRED)
    }
  }
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`
    let payload = null
    try {
      const body = await r.json()
      payload = body
      // `message` is the human sentence when the server wrote one; `detail` is the machine tag.
      // Preferring detail put strings like "database_busy" in front of users — the overload 503
      // carries a written explanation precisely so it does not have to. Fall back to detail for
      // every endpoint that only sends that.
      if (body?.message) detail = body.message
      else if (body?.detail) detail = body.detail
    } catch (_) { /* body wasn't JSON */ }
    if (r.status === 404 && SCAN_NOT_FOUND_DETAIL.test(String(detail).trim())) {
      const m = SCAN_URL_RE.exec(r.url || '')
      const scanId = m ? decodeURIComponent(m[1]) : null
      // Announce BEFORE throwing, so the many callers that end in `.catch(() => null)` — the
      // whole reason this was silent — cannot suppress it.
      window.dispatchEvent(new CustomEvent('acp:scan-unavailable',
        { detail: { scanId, reason: SCAN_UNAVAILABLE } }))
      const e = new Error(SCAN_UNAVAILABLE)
      e.scanUnavailable = true
      e.scanId = scanId
      throw e
    }
    const e = new Error(detail)
    // Carry the HTTP status on the error. Callers that must distinguish "the server proved it
    // did nothing" from "we have no idea what happened" cannot do it from the message text —
    // submitIntent.outcomeIsUncertain() is the first, and it decides whether a retry reuses its
    // idempotency key or mints a new one. Without this the check reads `undefined` every time
    // and silently answers "uncertain" for everything, which is the safe direction but is not a
    // check.
    e.status = r.status
    // Machine-readable fields alongside the sentence, so a caller can branch without parsing
    // prose: `code` (e.g. DB_CAPACITY_BUSY), `changes` ("none" | "unknown" — whether the server
    // can prove nothing was written), and the ids needed to correlate or reconcile.
    if (payload) {
      if (payload.code) e.code = payload.code
      if (payload.detail) e.detail = payload.detail
      if (payload.changes) e.changes = payload.changes
      if (payload.request_id) e.requestId = payload.request_id
      if (payload.occurred_at) e.occurredAt = payload.occurred_at
    }
    // A capability failure, not a transient one: every other card in the inbox would hit the
    // same missing model, so the auto-draft gate stops rather than asking N more times.
    if (r.status === 503 && AI_MODEL_NOT_PULLED_DETAIL.test(String(detail))) e.aiModelNotPulled = true
    throw e
  }
  return r.json()
}
const sim = (value, ms = 220) => new Promise((res) => setTimeout(() => res(value), ms))

// ── Boot reads: the requests the app's FIRST PAINT waits on ──────────────────────────────────────
//
// A request that FAILS rejects, and a caller can show an error. A request that HANGS never settles,
// so a `.catch` never runs and a loading screen gated on it renders forever — no error, no retry,
// nothing but a manual reload. On 2026-09-01 a production dependency stalled for about two minutes
// (`/readyz` timed out at 03:41 while `/healthz` stayed green) and tabs open across that window sat
// on a spinner long after the backend had recovered.
//
// Only the boot reads get this. 154 of the 157 fetches in this file have no timeout and most should
// keep it that way: a background refresh that quietly fails costs nothing, and an 8-second ceiling
// would break a long upload or a scan enqueue. The test is narrow — does a promise that never
// settles leave a person looking at a loading screen they cannot escape?
//
// A shared helper rather than `signal:` repeated per call site, so the set of boot reads is one
// greppable thing a test can assert over instead of a property each new endpoint must remember.
export const BOOT_TIMEOUT_MS = 8000

// The scan enqueue. A WRITE, so it is bounded separately and far more loosely than a boot read —
// but bounded, which it was not.
//
// The gap this closes, found in production 2026-09-01: a Discovery submitted DURING a deployment,
// while the new API replicas were starting. The request never reached a server at all — no
// preflight, no POST /scans, no scan id, no job id — and because the call had no timeout it never
// settled, so the browser sat in its optimistic "Submitting Discovery" state permanently, with no
// console error and nothing to retry.
//
// 30s, not the 8s a boot read gets: this POST only enqueues a job and returns its ids, so it is
// fast by design, but it is still a write and a ceiling tight enough to abort a request the server
// is actually processing would be the worse bug.
//
// Bounding a WRITE is only safe because the caller holds an idempotency key across the failure.
// A timeout tells us the response was lost, NOT that the work was not done — the scan may exist.
// The key is what makes the retry safe: outcomeIsUncertain(undefined) is true (submitIntent.js),
// so the key is held rather than abandoned, and a resubmit reconciles to the same job instead of
// enqueuing a second scan. Never shorten this without re-reading that contract.
export const SCAN_ENQUEUE_TIMEOUT_MS = 30000
// Applying a schedule can wait on several Azure control-plane updates, so it gets more room than
// an ordinary read while still guaranteeing that the Review step eventually leaves "Applying".
// A timeout is an UNKNOWN outcome: the server may have finished after the browser stopped waiting,
// so callers must re-read the schedule instead of claiming that nothing changed or retrying blind.
export const CAPACITY_APPLY_TIMEOUT_MS = 120000
export const CAPACITY_MUTATION_TIMEOUT_MS = 30000
const bootFetch = (url, init = {}) => fetch(url, { ...init, signal: AbortSignal.timeout(BOOT_TIMEOUT_MS) })

// AI provenance (ADR 0019 Phase 0): the active model + local/cloud zone, cached from /config so
// any surface (the review card's "Generated by …" line + privacy badge) can read it without
// threading a prop through the tree. Null until the first getConfig resolves — callers tolerate it.
let _aiProv = null
export const aiProvenance = () => _aiProv

// /config gates the whole sign-in screen, so it is the one request whose FAILURE MODE MATTERS MOST —
// and it had none. There was no timeout, and `AbortSignal.timeout` is what the rest of this module
// already uses for exactly this (see the preflight and folder-name calls). Without it a request that
// HANGS never settles, so SignIn's `if (!cfg)` branch renders "Loading…" under the logo forever: no
// error, no retry, nothing but a reload to escape. A caller's `.catch` cannot help, because a hung
// fetch never rejects.
//
// That is not hypothetical. On 2026-09-01 the production /readyz probe timed out at 03:41 while
// /healthz stayed green — a brief dependency stall — and a tab open across that window sat on
// "Loading…" long after the backend recovered.
//
// 8s matches the other two timed calls in this file. Generous for a small JSON read, short enough
// that a stalled boot becomes a visible, retryable error rather than a dead screen.
export const CONFIG_TIMEOUT_MS = 8000
export const getConfig = () => (SIM ? sim({ google_client_id: null, auth: 'demo', sim: true, ai: { provider: 'ollama', model: 'llama3.1', vision_model: 'llava:13b', zone: 'local', host: 'localhost' }, langfuse_trace_base: 'https://acp-langfuse.demo/project/acp-compliance/traces' }) : fetch(`${BASE}/config`, { signal: AbortSignal.timeout(CONFIG_TIMEOUT_MS) }).then(j)).then((c) => { _aiProv = c.ai || _aiProv; return c })
// Lightweight backend reachability probe. /healthz is always public (no auth header needed).
// Returns true when the backend responds with HTTP 2xx, false on network error or non-2xx.
// SIM mode always reports healthy — there is no real server to probe.
// This one is subtle and matters most. checkHealth is what RAISES the backend-down banner and its
// Retry control, and `backendDown` starts as null — "not known yet" — so the banner renders only
// once a probe answers. A hung /healthz therefore leaves the banner permanently unrendered during
// exactly the stall it exists to report: the rescue mechanism is disabled by the outage. It maps a
// rejection to false already, so a timeout is all that was missing to make a stall report itself.
export const checkHealth = () => (SIM ? Promise.resolve(true)
  : bootFetch(`${BASE}/healthz`).then((r) => r.ok, () => false))
// Scan-infra readiness probe. /readyz is always public. Unlike checkHealth (up/down), this
// reports WHETHER SCANS CAN ACTUALLY RUN right now — the worker tier can be alive at the process
// level while claim_job is failing (DB out of connections, etc.), which /healthz cannot see.
// Returns the parsed body ({ready, degraded, workers, ...}) or null on network error/non-2xx —
// null means "couldn't ask", not "definitely broken", so callers must treat it as inconclusive
// rather than as a failure. SIM mode always reports ready — there is no real server to probe.
export const checkReadiness = () => (SIM ? Promise.resolve({ ready: true, degraded: [] })
  : fetch(`${BASE}/readyz`).then((r) => (r.ok ? r.json() : null), () => null))

// Splits a buffered SSE (text/event-stream) chunk into complete frames plus whatever partial
// frame is still waiting on more bytes. Pure and exported so the parsing logic is unit-testable
// without a real network stream. A frame is one or more lines ending in a blank line; only
// `event:`/`data:` lines are recognised (comments and ids are not — the backend emits neither).
// The backend (api/routes/scans.py's discover/stream) always emits exactly one `data:` line per
// frame with no embedded newlines (json.dumps produces a single-line string), so multi-line
// `data:` joining per the SSE spec is deliberately not implemented — there is nothing that needs it.
export function parseSSEFrames(buffer) {
  const frames = []
  let rest = buffer
  let idx
  while ((idx = rest.indexOf('\n\n')) !== -1) {
    const raw = rest.slice(0, idx)
    rest = rest.slice(idx + 2)
    let event = 'message'
    let data = null
    // `id:` was discarded here until ADR 0051. It is the resume cursor: the server stamps each
    // event frame with its scan_events.seq, the client remembers the last one it rendered, and
    // sends it back as Last-Event-ID on the next connect. Dropping the line meant no client could
    // resume even once the server offered to.
    let id = null
    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data = line.slice(5).trim()
      else if (line.startsWith('id:')) id = line.slice(3).trim()
    }
    if (data !== null) frames.push({ event, data, id })
  }
  return { frames, rest }
}

// Live Discovery progress over SSE — GET /scans/{scanId}/discover/stream. Deliberately NOT the
// browser's native EventSource: EventSource cannot send custom headers at all, and every other
// call in this file authenticates via the Authorization bearer `headers()` builds — sending the
// token as a URL query param instead would put it in server/proxy access logs and browser
// history, which this app avoids everywhere else (compliance-focused, HIPAA/BAA context). This
// reads the same authenticated stream manually via fetch() + a ReadableStream reader instead.
//
// onMessage fires with the parsed job-state object (same shape getJob() returns) on every
// `data:` frame; onDone/onError fire at most once each, whichever way the stream ends.
//
// onError means the CONNECTION failed — a non-ok response, a fetch exception, the reader
// erroring mid-read. onDone means the stream ended cleanly, WHETHER OR NOT it had good news:
// the server's own `event: done` frame (final job state already delivered via onMessage) and its
// `event: error` frame (routes/scans.py's only use of it: "no active job for this scan" — Redis
// genuinely has nothing, which happens routinely once a job finishes and its state ages out, not
// only on a real fault) both count as onDone. Found live 2026-08-26: treating the server's
// `event: error` as a transport failure flipped the caller's sseFailedRef on every scan that
// finished cleanly, degrading the last tick or two to polling getJob(job_id) with a job_id that
// had already gone stale (the job row aged out, or a retry replaced it) — reproducing a handful
// of the exact 404s the scan-ID-anchored stream (#843) was built to eliminate. The backend never
// emits `event: error` for any other reason: a real fault (bad scan_id, wrong owner) is rejected
// as an HTTP error status before the stream ever starts, never as a mid-stream frame.
//
// Returns {close()}; ALWAYS call it once the caller is done (scan settled, cancelled, or
// unmounting) — closing aborts the fetch, which is what stops the server's generator loop, so an
// unclosed stream leaks both the browser connection and the backend coroutine polling Redis
// every 250ms for it.
export function openDiscoverStream(scanId, { onMessage, onDone, onError } = {}) {
  if (SIM) return { close: () => {} }   // SIM has no real server to stream from
  const controller = new AbortController()
  ;(async () => {
    try {
      const res = await fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/discover/stream`,
        { headers: headers(), signal: controller.signal })
      if (!res.ok || !res.body) { onError?.(); return }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const { frames, rest } = parseSSEFrames(buffer)
        buffer = rest
        for (const f of frames) {
          if (f.event === 'done' || f.event === 'error') { onDone?.(); return }
          try { onMessage?.(JSON.parse(f.data)) } catch { /* malformed frame — skip, keep reading */ }
        }
      }
      onDone?.()
    } catch (e) {
      if (e?.name !== 'AbortError') onError?.()   // an abort is our own close(), not a failure
    }
  })()
  return { close: () => controller.abort() }
}
// Langfuse deep-link base (from /config) → "📊 View trace" chips. traceUrl(null) when unset.
let lfTraceBase = null
export const setLangfuseBase = (b) => { lfTraceBase = b || null }
// Direct deep-link to the trace. Safe now that the backend caps per-document spans on the
// Scan trace (ACP_SCAN_TRACE_SPAN_CAP), so traces stay small enough for the Langfuse detail
// view to load even on large estates.
export const traceUrl = (traceId) => (lfTraceBase && traceId ? `${lfTraceBase}/${encodeURIComponent(traceId)}` : null)
// Reliable trace link: route the chip through the backend redirect endpoint. File-centric
// tracing (backend: lf.file_trace) — every file gets its OWN Langfuse trace, with Discover/
// Assess/Remediate as spans inside it, grouped into a session keyed by scan_id:
//   kind='file'    — scanId + file → that ONE file's full Discover→Assess→Remediate trace.
//                     Ensures it exists before 302-ing, so a click never lands on "Not Found".
//   kind='session' — scanId only   → the Langfuse session for the whole scan (every one of
//                     its file traces) — the "view this scan" replacement for the old single
//                     scan/assess/remediate trace. Never 404s, even before any file ran.
// Returns null when tracing isn't configured (no chip). SIM keeps a direct deep-link (no backend).
export const openTraceUrl = (scanId, kind = 'session', file = null) => {
  if (!scanId) return null
  if (SIM) return traceUrl(kind === 'file' && file ? `${scanId}::${file}` : scanId)
  if (!lfTraceBase) return null
  if (kind === 'file' && file) return `${BASE}/scans/${encodeURIComponent(scanId)}/trace/file/${encodeURIComponent(file)}`
  return `${BASE}/scans/${encodeURIComponent(scanId)}/trace/session`
}
export const getTraceStatus = (scanId, kind = 'session', file = null) => {
  if (!scanId || SIM) return Promise.resolve({ available: !!scanId })
  if (!lfTraceBase) return Promise.resolve({ available: false })
  if (kind === 'file' && file)
    return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/trace/file/${encodeURIComponent(file)}/exists`).then(j).catch(() => ({ available: false }))
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/trace/${kind}/exists`).then(j).catch(() => ({ available: false }))
}
// One file's trace, fetched server-side and returned as PHI-safe JSON, for the IN-APP trace
// panel (TracePanel). Langfuse's own trace page hangs for logged-out users on our self-hosted
// v3 and can't be iframed (X-Frame-Options), so ACP renders the trace itself instead of linking
// out. Returns {status: 'ok'|'pending'|'not_configured', trace?}; never rejects — the panel maps
// each state to an honest message. SIM returns a canned trace so the demo needs no backend.
export const getFileTraceData = (scanId, file) => {
  if (SIM) return sim({ status: 'ok', trace: {
    id: `${scanId}::${file}`, document: file || 'doc-3f9a2c.docx', format: 'docx',
    timestamp: '2026-06-29T17:04:10Z',
    result: { score: 82, conformant: false, level: 'AA',
      checks_failed: 2, failing_criteria: { '1.1.1': 1, '1.4.3': 3 }, findings_total: 4,
      checks: { PASS: 12, FAIL: 2, REVIEW: 1, NOT_EVALUATED: 3 },
      pii: { flagged: true, types: ['us_ssn', 'email_address'], findings: 5, critical: false },
      remediation: { remediated: true, written_back: false, published: false } },
    observations: [
      { type: 'SPAN', name: 'Discover', start: '2026-06-29T17:04:10Z', end: '2026-06-29T17:04:11Z', level: 'DEFAULT', model: null, input_tokens: 0, output_tokens: 0, cost: 0 },
      { type: 'SPAN', name: 'Assess · WCAG 2.1 AA', start: '2026-06-29T17:04:12Z', end: '2026-06-29T17:04:18Z', level: 'DEFAULT', model: null, input_tokens: 0, output_tokens: 0, cost: 0 },
      { type: 'GENERATION', name: 'alt-text', start: '2026-06-29T17:04:14Z', end: '2026-06-29T17:04:16Z', level: 'DEFAULT', model: 'llava:13b', input_tokens: 512, output_tokens: 48, cost: 0 },
    ] } })
  if (!scanId || !file) return Promise.resolve({ status: 'not_configured' })
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/trace/file/${encodeURIComponent(file)}/data`, { headers: headers() })
    .then(j).catch(() => ({ status: 'pending' }))
}
// The whole SCAN's session — every file's trace as a PHI-safe list + rollup, for the in-app
// SessionPanel. This is the aggregate that hung Langfuse's own UI on large scans; rendered
// in-app it's a plain capped list. Same {status} contract as getFileTraceData; SIM returns a
// canned session.
export const getSessionTraceData = (scanId) => {
  if (SIM) return sim({ status: 'ok', session: {
    id: scanId, total: 3, truncated: false,
    rollup: { documents: 3, assessed: 2, conformant: 1, avg_score: 74.5, with_failures: 1, with_pii: 1 },
    files: [
      { trace_id: `${scanId}::doc-3f9a2c.docx`, document: 'doc-3f9a2c.docx', format: 'docx',
        result: { score: 67, conformant: false, level: 'AA', failing_criteria: { '1.1.1': 2, '1.4.3': 1 },
          pii: { flagged: true, types: ['us_ssn'], findings: 3, critical: false } } },
      { trace_id: `${scanId}::doc-b7970a.pdf`, document: 'doc-b7970a.pdf', format: 'pdf',
        result: { score: 82, conformant: true, level: 'AA', failing_criteria: {} } },
      { trace_id: `${scanId}::doc-929e23.xlsx`, document: 'doc-929e23.xlsx', format: 'xlsx', result: null },
    ] } })
  if (!scanId) return Promise.resolve({ status: 'not_configured' })
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/trace/session/data`, { headers: headers() })
    .then(j).catch(() => ({ status: 'pending' }))
}
// One document's trace across EVERY scan it appears in — the "this document over time" view
// Langfuse's session-grouped UI can't give (DocumentHistoryPanel). `docLabel` is the trace-facing
// label (TracePanel's trace.document), not a raw filename. Same {status} contract; SIM canned.
export const getDocumentHistory = (scanId, docLabel) => {
  if (SIM) return sim({ status: 'ok', history: {
    document: docLabel || 'doc-3f9a2c.docx', format: 'docx', total: 3,
    scans: [
      { scan_id: 'scan-cur', trace_id: `scan-cur::${docLabel}`, timestamp: '2026-08-19T17:04:10Z',
        result: { score: 82, conformant: false, level: 'AA', failing_criteria: { '1.4.3': 1 } } },
      { scan_id: 'h3', trace_id: `h3::${docLabel}`, timestamp: '2026-06-10T09:00:00Z',
        result: { score: 71, conformant: false, level: 'AA', failing_criteria: { '1.1.1': 2, '1.4.3': 1 } } },
      { scan_id: 'h2', trace_id: `h2::${docLabel}`, timestamp: '2026-06-03T09:00:00Z',
        result: { score: 60, conformant: false, level: 'AA', failing_criteria: { '1.1.1': 3, '1.3.1': 2, '1.4.3': 1 } } },
    ] } })
  if (!scanId || !docLabel) return Promise.resolve({ status: 'not_configured' })
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/trace/file/${encodeURIComponent(docLabel)}/history`, { headers: headers() })
    .then(j).catch(() => ({ status: 'pending' }))
}
// Sensitive-data (PII) findings for a scan (ADR 0006) — rollup + per-type counts (masked).
export const getScanPii = (scanId) => (SIM
  ? sim({ summary: { documents: 6, items: 23, by_type: [
      { pii_type: 'ssn', label: 'US SSN', count: 9, docs: 4 },
      { pii_type: 'credit_card', label: 'Credit card', count: 7, docs: 3 },
      { pii_type: 'email', label: 'Email address', count: 5, docs: 5 },
      { pii_type: 'phone', label: 'Phone number', count: 2, docs: 2 },
    ] }, files: [] })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/pii`, { headers: headers() }).then(j).catch(() => null))
// AI Compliance Digest (bundle #2) — exec paragraph grounded in real scan data + the facts.
export const getDigest = (scanId, refresh = false) => (SIM
  ? sim({
    headline: '52 of 175 documents conformant (79/100 average).', score: 79,
    narrative: 'Your estate sits at 79/100 with 52 of 175 documents fully conformant. Since the last scan the score rose 4 points, though 3 documents regressed after edits — most notably the public landing page, which lost contrast compliance. The single biggest systemic gap is missing alt text, failing on 89 documents; fixing it would move the most documents toward AA. Recommended: prioritise alt-text remediation across the estate and re-validate the three regressions.',
    changed: ['Estate score rose 4 points to 79/100 since the last scan.', '3 documents regressed — worst: marketing-public-landing-page.html (100→82).', '9 documents improved.', '6 documents contain sensitive data flagged for review.'],
    top_issue: 'Images have alt text (1.1.1) fails on 89 documents — the biggest systemic gap.',
    next_action: 'Fix Images have alt text across the estate — it clears the most documents toward AA.',
    ai: true, model: 'local model',   // SIM must not name a model the product never calls
  }, 1500)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/digest${refresh ? '?refresh=true' : ''}`, { headers: headers() }).then(j))
// Discovery diff vs the previous run OF THE SAME SOURCE (per-source baseline) — what the estate
// gained, lost and changed. Deliberately separate from getScanDiff: that one reads file_records,
// the ASSESSED grain, which an ADR 0020 Discover-only run leaves empty. This reads the inventory,
// so it answers for the runs a source operations panel is about.
//
// Callers pass `vs` explicitly where they already know the baseline (the drawer knows the source's
// prior run from runsForSource), which keeps the number on screen tied to the run named beside it.
export const getInventoryDiff = (scanId, vs = null) => (SIM
  ? sim({
      cur_id: scanId, prev_id: 'h3', cur_at: '2026-08-18T11:50:00Z', prev_at: '2026-08-17T11:48:00Z',
      boundary_changed: false, truncated: false,
      summary: { new: 132, changed: 24, removed: 6, unchanged: 12324, not_listed: 0, indeterminate: 41 },
      new: [], changed: [], removed: [], not_listed: [], indeterminate: [],
    }, 180)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/inventory-diff${vs ? `?vs=${encodeURIComponent(vs)}` : ''}`,
          { headers: headers() }).then(j))
// SIM has no rule engine, so the demo inventory derives its lifecycle from the crawl metadata SIM
// already computes for every file — the same `superseded` / `ageDays` facts the Discover list shows
// beside each row. Two rules, named, so the demo exercises the real shape: a tag that carries the
// rule that produced it and the reason it matched. Precedence matches the backend's — one status
// per file, the reversible one where both could apply.
const simLifecycle = (f) => {
  if (f.superseded) return ['Delete Candidate', 'sim-superseded', "matched delete rule 'Superseded drafts'"]
  if (f.ageDays >= 1825) return ['Archive Candidate', 'sim-legacy',
    `matched archive rule 'Legacy documents (5+ years)' — last modified ${f.modifiedAge}`]
  return ['Active', null, null]
}
// Lifecycle rules #8 overrides recorded so far in SIM mode, keyed by file — simScanInventory is
// recomputed fresh on every call (it derives from f.superseded/f.ageDays, not persisted state),
// so an override needs its own small store to survive across calls the way the real backend's
// scan_inventory row does.
const _simOverrides = new Map()
const simScanInventory = (scanId, offset, limit) => {
  const files = simGetScan(scanId).files || []
  const rows = files.slice(offset, offset + limit).map((f) => {
    const [lifecycle_status, lifecycle_rule_id, lifecycle_reason] = simLifecycle(f)
    const ov = _simOverrides.get(f.file)
    return { scan_id: scanId, file: f.file, path: f.file, size_kb: f.sizeKB ?? null,
             owner: f.owner ?? null, lifecycle_status, lifecycle_rule_id, lifecycle_reason,
             lifecycle_override_reason: ov?.reason ?? null,
             lifecycle_overridden_by: ov?.actor ?? null, lifecycle_overridden_at: ov?.at ?? null }
  })
  return { scan_id: scanId, total: files.length, offset, limit, rows }
}
// One page of the per-file discover inventory — EVERY discovered file with its source metadata
// and, the reason this exists, its LIFECYCLE columns (lifecycle_status / lifecycle_rule_id /
// lifecycle_reason). `GET /scans/{id}` cannot answer for those: it selects from file_records,
// which has no lifecycle columns at all — they live on scan_inventory, which is this route.
//
// Paginated at the route (limit <= 1000) and passed through as such, `total` being the real count.
// Reading one page and counting over it would be a count without its population, so the caller
// pages to completion or treats the read as incomplete — see discoveryInventory.js.
export const getScanInventory = (scanId, { offset = 0, limit = 1000 } = {}) => (SIM
  ? sim(simScanInventory(scanId, offset, limit), 180)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/inventory?offset=${offset}&limit=${limit}`,
          { headers: headers() }).then(j))
export const getLifecycleSummary = (scanId) => (SIM
  ? getScanInventory(scanId).then(({ rows, total }) => {
      const count = (status) => rows.filter((r) => (r.lifecycle_status || 'Active') === status).length
      const counts = { active: count('Active'), already_archived: count('Archived') + count('Already archived'),
        archive_candidate: count('Archive Candidate'), delete_candidate: count('Delete Candidate'), deleted: count('Deleted'),
        exempt: count('Exempted'), reactivated: count('Reactivated'),
        unevaluable: count('Unevaluable') + count('Conflict — review required'), failed: count('Failed') }
      return { scan_id: scanId, total, reconciled_total: Object.values(counts).reduce((a, b) => a + b, 0),
        counts, assessment_excluded: counts.already_archived + counts.archive_candidate + counts.delete_candidate + counts.deleted,
        recommendations_only: true, data_version: null }
    })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/lifecycle/summary`, { headers: headers() }).then(j))
export const getLifecycleRuleResults = (scanId) => (SIM
  ? sim({ scan_id: scanId, data_version: null, rules: [] })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/lifecycle/rules`, { headers: headers() }).then(j))
export const getLifecycleFiles = (scanId, { status = '', policyId = '', candidateOnly = false, offset = 0, limit = 200 } = {}) => {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  if (status) qs.set('status', status)
  if (policyId) qs.set('policy_id', policyId)
  if (candidateOnly) qs.set('candidate_only', 'true')
  return SIM ? getScanInventory(scanId, { offset, limit })
    : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/lifecycle/files?${qs}`, { headers: headers() }).then(j)
}
// PRD §8 grouped approval. Sends the ids the queue DISPLAYED, plus the policy version and
// action they were all queued under — the server refuses the batch if any row disagrees, so the
// client cannot widen a selection by getting its own grouping wrong. Records decisions only; no
// source file is touched here.
// PRD §7.4 timeline. Scan-scoped in the path (that scan is the owner gate) but not in the
// answer: the events cross scans, which is the whole point of showing a history at all.
export const getLifecycleFileHistory = (scanId, file) => (SIM
  ? sim({ scan_id: scanId, document_id: file, events: [] })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/lifecycle/files/${encodeURIComponent(file)}/history`,
          { headers: headers() }).then(j))
export const approveDispositionBatch = ({ auditIds, policyId, policyVersion, action, reason }) => (SIM
  ? sim({ submitted: auditIds.length, approved: auditIds, refused: [], already_decided: [],
          reconciled: true, executed: false })
  : fetch(`${BASE}/disposition/approvals`, {
    method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ audit_ids: auditIds, policy_id: policyId,
                           policy_version: policyVersion, action, reason: reason || null }),
  }).then(j))
export const getLifecycleFileDetail = (scanId, file) => (SIM
  ? getScanInventory(scanId).then(({ rows }) => ({ ...(rows.find((r) => r.file === file) || {}), evaluations: [] }))
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/lifecycle/files/${encodeURIComponent(file)}`, { headers: headers() }).then(j))
// Lifecycle rules #8 — record a human's reasoned disagreement with a rule's Archive/Delete
// Candidate recommendation for ONE file. Rejects (throws) on a blank reason or a file with no
// candidate status to override, matching the real route's 422/409 — the caller (Discover.jsx)
// treats any rejection as "could not record this" and leaves the file unoverridden.
export const overrideLifecycleRecommendation = (scanId, file, reason) => {
  const r = (reason || '').trim()
  if (!r) return Promise.reject(new Error('a reason is required'))
  if (SIM) {
    const row = simScanInventory(scanId, 0, 1e9).rows.find((x) => x.file === file)
    if (!row || !['Archive Candidate', 'Delete Candidate'].includes(row.lifecycle_status)) {
      return Promise.reject(new Error('no recommendation to override'))
    }
    const at = new Date().toISOString()
    _simOverrides.set(file, { reason: r, actor: simIdentity().email, at })
    return sim({ ...row, lifecycle_override_reason: r, lifecycle_overridden_by: simIdentity().email,
                lifecycle_overridden_at: at })
  }
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/lifecycle-override`, {
    method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ reason: r }),
  }).then(j)
}
// Discovery acknowledgement (PRD §EX-10): persist the operator's approval of the snapshot.
export const acknowledgeScan = (scanId) => {
  if (SIM) return sim({ scan_id: scanId, acknowledged: true })
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/acknowledge`, {
    method: 'PUT', headers: headers(),
  }).then(j)
}
export const unacknowledgeScan = (scanId) => {
  if (SIM) return sim({ scan_id: scanId, acknowledged: false })
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/acknowledge`, {
    method: 'DELETE', headers: headers(),
  }).then(j)
}

// Regression diff vs a prior scan (ADR 0009) — which docs got worse/better + criteria that broke.
export const getScanDiff = (scanId, vs = null) => {
  if (SIM) return sim({
    cur_id: scanId, prev_id: 'h3', cur_at: '2026-06-29T17:00:00Z', prev_at: '2026-06-22T09:00:00Z',
    summary: { regressed: 3, improved: 9, new: 2, removed: 1 },
    regressed: [
      { file: 'marketing-public-landing-page.html', prev: 100, cur: 82, delta: -18, broke: [{ sc: '1.4.3', name: 'Contrast (minimum)' }] },
      { file: 'cardiology-patient-handbook.pdf', prev: 91, cur: 78, delta: -13, broke: [{ sc: '1.1.1', name: 'Images have alt text' }, { sc: '2.4.6', name: 'Headings & labels' }] },
      { file: 'q3-board-deck.pptx', prev: 88, cur: 84, delta: -4, broke: [] },
    ],
    improved: [
      { file: 'onboarding.pdf', prev: 72, cur: 100, delta: 28 },
      { file: 'benefits-guide.pdf', prev: 80, cur: 96, delta: 16 },
    ],
    new: [{ file: 'hr-policy-2026.docx', score: 64 }, { file: 'patient-intake-form-v2.pdf', score: 88 }],
    removed: [{ file: 'legacy-archive-page.html', score: 55 }],
  }, 220)
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/diff${vs ? `?vs=${encodeURIComponent(vs)}` : ''}`, { headers: headers() }).then(j)
}
// Per-WCAG-rule outcomes for a scan (PASS/FAIL/SKIP + finding counts), one row per file×rule.
// `file` narrows to one document — the route has always supported it (scans.py: get_scan_traces
// takes file=), the client just never passed it. Assess uses it to name the criteria a document
// failed the moment it finishes, without pulling every file's rows on every poll.
export const getScanTraces = (scanId, file = null) => {
  if (SIM) {
    const SCS = [['1.1.1', 'Images have alt text', 'A'], ['1.3.1', 'Info & relationships', 'A'], ['1.4.3', 'Contrast (minimum)', 'AA'], ['2.4.4', 'Link purpose', 'A'], ['2.4.6', 'Headings & labels', 'AA'], ['3.1.1', 'Language of page', 'A'], ['1.2.2', 'Captions', 'A'], ['4.1.2', 'Name, role, value', 'A'], ['2.4.7', 'Focus visible', 'AA'], ['1.4.11', 'Non-text contrast', 'AA'], ['3.3.2', 'Labels or instructions', 'A'], ['2.1.1', 'Keyboard', 'A']]
    const rows = []
    SCS.forEach(([id, name, level], k) => { for (let f = 0; f < 25; f++) { const r = (f * 7 + k * 3) % 10; const outcome = r < 2 ? 'SKIP' : (r < 5 ? 'FAIL' : 'PASS'); rows.push({ file: `doc-${f}.html`, rule_id: id, plain_name: name, level, outcome, finding_count: outcome === 'FAIL' ? r : 0 }) } })
    return sim(rows, 150)
  }
  const q = file ? `?file=${encodeURIComponent(file)}` : ''
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/traces${q}`, { headers: headers() }).then(j)
}
// The concrete values AI fixes wrote this scan (real vision alt text + a small image
// thumbnail), for the "Recent AI fixes" panel. Best-effort — [] on any error/SIM.
export const getAppliedFixes = (scanId) => (SIM || !scanId
  ? sim([])
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/applied-fixes`, { headers: headers() }).then(j).catch(() => []))
// The AI provenance ledger for a scan (ADR 0019 Phase 0b): one row per model call with surface /
// provider / model / privacy zone / latency / outcome — the auditable "what model saw my document,
// where did it run?" record behind the review card's audit panel. Best-effort — [] on any error/SIM.
export const getScanAiCalls = (scanId) => (SIM || !scanId
  ? sim([])
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/ai_calls`, { headers: headers() }).then(j).catch(() => []))
// Inspect saved outputs without generating drafts. Missing provenance must not hide
// saved proposals or be mistaken for a successful empty history response.
export async function getRemediationAIDetails(scanId) {
  if (!scanId) throw new Error('Select an assessment to inspect AI suggestions.')
  if (SIM) return sim({ items: [], calls: [], callsAvailable: false, available: false })
  const id = encodeURIComponent(scanId)
  const [queue, ledger] = await Promise.allSettled([
    fetch(`${BASE}/hitl/queue?scan_id=${id}&include_superseded=true`, { headers: headers(), cache: 'no-store' }).then(j),
    fetch(`${BASE}/scans/${id}/ai_calls`, { headers: headers(), cache: 'no-store' }).then(j),
  ])
  if (queue.status === 'rejected') throw queue.reason
  if (!Array.isArray(queue.value)) throw new Error('Saved suggestion details are unavailable.')
  const callsAvailable = ledger.status === 'fulfilled' && Array.isArray(ledger.value)
  return { items: queue.value, calls: callsAvailable ? ledger.value : [], callsAvailable, available: true }
}
// Release review uses a server-scoped projection: reviewer and post-write rows are attached only
// through their durable AI call id. An empty outcome array means "not recorded", never zero.
export const getReleaseAiProvenance = (scanId, files = []) => (SIM || !scanId || !files.length
  ? sim([])
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/ai-provenance`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ files }),
    }).then(j))
// Per-fix before→after evidence for one file — the original text/markup → remediated
// version, persisted only for fixes that verifiably cleared. Feeds the certification PDF's
// "Before → After" section, and the review drawer's evidence card. SIM serves the same
// fixtures as getScanRemediationDiffs, narrowed to the one file, on the same "only after the
// demo has actually remediated" gate.
export const getFileRemediationDiffs = (scanId, file, { strict = false } = {}) => {
  if (SIM) return sim(_simRemed.total ? simRemediationDiffs().filter((d) => d.file === file) : [])
  if (!scanId) return sim([])
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/remediation-diffs`,
               { headers: headers() }).then(j).catch(error => { if (strict) throw error; return [] })
}
// ── Server-computed report FACTS (api/report_facts.py) ──────────────────────────────────────
// The report is built from these, not from the client's own rollups: the server owns identity,
// the finding list, the saved-change list (verified AND applied-but-unverified), the recorded
// reviewer decisions, and the accounting rules that say when a number may be stated at all.
// `factsDigest` binds a rendered report to the evidence it was built from — POST /report-render
// answers 409 when they no longer agree.
//
// These THROW on failure (via j), deliberately: a report built from silently-missing facts is
// exactly the failure this endpoint exists to stop. Callers state the gap instead of filling it.
// SIM resolves null — demo mode has no server, and a fabricated digest would be a lie the
// renderer could not detect.
export const getFileReportFacts = (scanId, file) => (SIM || !scanId || !file
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/report-facts`,
          { headers: headers(), cache: 'no-store' }).then(j))
// Scan-level facts. Paginated over the per-file index (`offset`/`limit`/`complete`); the
// top-level totals, `factsDigest`, `previous` and `previousReason` are the same on every page.
export const getScanReportFacts = (scanId, { offset = 0, limit = 200 } = {}) => (SIM || !scanId
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/report-facts?offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(limit)}`,
          { headers: headers(), cache: 'no-store' }).then(j))
// EXACT-BYTES page render: the server rasterises only bytes whose sha256 equals `sha256`, and
// answers 404 when it does not hold them. That is what makes a preview's provenance knowable —
// /files/{file}/page/{page} prefers the original but may fall back to the remediated blob, so
// nothing fetched from it can honestly be captioned "before" or "after".
// Resolves `{ blob, sha256 }` — sha256 being the digest the SERVER says it rendered, read from the
// `X-ACP-Artifact-Sha256` response header — or null (never rejects: a missing preview is said out
// loud, not thrown). The header is what lets a caption naming a digest be checked against the
// bytes actually shown rather than against the digest we happened to ask for.
//
// NOTE: on a cross-origin API host the header is only readable if the server sends
// `Access-Control-Expose-Headers: X-ACP-Artifact-Sha256`. When it is not readable, `sha256` is
// null and the preview is labelled as unconfirmed rather than silently trusted.
export const getFileArtifactPage = (scanId, file, sha256, page = 1) => (SIM || !scanId || !file || !sha256
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/artifact/${encodeURIComponent(sha256)}/page/${encodeURIComponent(page)}`,
          { headers: headers() })
      .then(async (r) => (r.ok ? { blob: await r.blob(), sha256: r.headers?.get?.('X-ACP-Artifact-Sha256') || null } : null))
      .catch(() => null))

// Raw Response (blob body) for the accessible server renderer; null in SIM (no server).
export const postReportRender = (scanId, body) => (SIM ? Promise.resolve(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/report-render`, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }))
// Versioned reviewer decisions on saved changes (api/routes/change_review.py). Real mode only —
// changeReview.js owns the SIM behaviour (local, clearly unsaved), so these never fake a server.
export const fetchChangeReviews = (scanId, file) =>
  fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/change-reviews`,
        { headers: headers(), cache: 'no-store' }).then(j)
export const putChangeReview = (scanId, file, changeId, body) =>
  fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/change-reviews/${encodeURIComponent(changeId)}`,
        { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }).then(j)
// Scan-wide before→after evidence — every verified-cleared fix across all files, so the
// Remediation view can group REAL applied fixes by rule/category without fabricating counts.
// Covers all fix types (reading order, titles, headings, tables), unlike applied-fixes
// (image alt text only). Best-effort — [] on any error.
//
// SIM has no engine, so it serves fixtures — but only once the demo has actually run
// remediation (_simRemed.total). Returning them unconditionally would open the Remediation
// view already claiming "N issues fixed automatically" and marking the step done.
// R15 · undo one deterministic fix ACP claims to have applied. There is nothing to restore on the
// document — remediation only ever produces a separate corrected copy, never overwriting the
// source — so this clears ACP's OWN evidence that the finding is fixed; the underlying assessment
// issue reappears in the worklist as if the fix had never run. SIM: optimistic, matching every
// other write in this module — there is no real store to mutate in demo mode.
export const undoAppliedFix = (scanId, file, ruleId) => (SIM
  ? sim({ undone: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/undo-fix`,
          { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ rule_id: ruleId }) }).then(j))

export const getScanRemediationDiffs = (scanId, includeSummary = false) => {
  if (SIM) return sim(_simRemed.total ? simRemediationDiffs() : [])
  if (!scanId) return sim([])
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation-diffs${includeSummary ? '?include_summary=true' : ''}`,
               { headers: headers() }).then(j).catch(() => [])
}
// Audit trail (maturity Phase 4): chronological provenance of one document in one scan —
// scanned → AI drafted → human decided → fix written → published. Every event is a real
// persisted row.
//
// This one REJECTS on failure rather than resolving to [], which is the opposite of the
// best-effort posture its neighbours take, and deliberately so. Everywhere else `[] on any
// error` is a fair trade: a missing "Recent AI fixes" panel costs a nicety. Here the empty
// array IS the product's claim — an audit trail that renders as "nothing has happened to this
// document" is a statement to an auditor, and a transport failure must never be able to make
// it. The two states are different sentences and only the caller can say which it is in, so
// the API layer hands back what actually happened and lets it.
//
// Callers must therefore carry a .catch — DocumentAudit and FileDrawer both do. The SIM and
// no-argument branches still resolve to [] because those genuinely are empty, not unknown.
export const getDocumentTimeline = (scanId, file) => {
  if (SIM || !scanId || !file) return sim([])
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/timeline?file=${encodeURIComponent(file)}`,
               { headers: headers() }).then(j)
}

// ADR 0042 — this RUN's durable lifecycle history (queued/claimed/listing/saved/discovered, plus
// retries and failures). The run-level sibling of getDocumentTimeline above.
//
// Same deliberate error contract as that one, for the same reason: the endpoint is always-200 and
// degrades to {available:false} for an unknown or foreign scan, so a rejected promise here means
// the REQUEST failed, never that the run has no history. "This run has no recorded history" and
// "we could not reach the server" are different sentences and only the caller can tell which it is
// in. Callers must carry a .catch — ScanHistory does.
//
// `afterSeq` is exclusive: pass the highest seq already held to fetch only what was missed. Not
// used by the first caller (which renders the whole run), and present because it is the endpoint's
// point — a client reconnecting after a gap asks "what did I miss", not "what is the state now".
export const getScanHistory = (scanId, { afterSeq = null, limit = null } = {}) => {
  if (SIM || !scanId) return sim({ available: false, events: [] })
  const qs = new URLSearchParams()
  if (afterSeq != null) qs.set('after_seq', String(afterSeq))
  if (limit != null) qs.set('limit', String(limit))
  const tail = qs.toString() ? `?${qs}` : ''
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/history${tail}`,
               { headers: headers() }).then(j)
}
// R18 · Comments on a finding — the human discussion thread anchored to one finding
// (scan × file × criterion × instance). SIM has no backend, so it keeps the thread in memory for
// the length of the demo session; real mode reads/writes the owner-scoped store. Oldest first.
const _simComments = {}   // `${scanId}::${findingKey}` -> [{id, ts, author, body, file, rule_id}]
export const findingKeyOf = (f) =>
  [f?.file || '', f?.rule_id || f?.ruleId || f?.wcag || '', f?.location || f?.locator || ''].join('||')
export const getFindingComments = (scanId, findingKey) => {
  if (SIM) return sim((_simComments[`${scanId}::${findingKey}`] || []).slice())
  if (!scanId || !findingKey) return Promise.resolve([])
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/comments?finding_key=${encodeURIComponent(findingKey)}`,
               { headers: headers() }).then(j).catch(() => [])
}
export const addFindingComment = (scanId, { findingKey, body, file = '', ruleId = '' }) => {
  if (SIM) {
    const key = `${scanId}::${findingKey}`
    const row = { id: `c${(_simComments[key] || []).length + 1}`, ts: new Date().toISOString(),
                  author: simIdentity().email, body, file, rule_id: ruleId }
    ;(_simComments[key] = _simComments[key] || []).push(row)
    return sim(row)
  }
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/comments`,
               { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
                 body: JSON.stringify({ finding_key: findingKey, body, file, rule_id: ruleId }) }).then(j)
}
export const getMe = () => (SIM ? sim(simIdentity()) : fetch(`${BASE}/me`, { headers: headers() }).then(j))
// Workspace access (PRD §13). The first copy arrives on /workspace/bootstrap so the navigation can
// draw correctly on its first render; this is for re-reading it after an administrator changes a
// role — §9: "Users whose permissions change during an active session receive the new permissions
// on their next API request. Navigation refreshes automatically."
//
// Resolves to `null` in SIM and on any failure, and null means NOT TOLD, which leaves every tab
// visible (see frontend/src/access.js). A failed refresh must not narrow a working session: the
// server is the control, and a network blip is not a permission decision.
export const getMyAccess = () => (SIM
  ? sim(null)
  : fetch(`${BASE}/me/access`, { headers: headers() }).then(j).catch(() => null))
const analyticsQuery = (period, source, options = {}) => {
  const query = new URLSearchParams({ period })
  if (source) query.set('source', source)
  for (const key of ['owner', 'status', 'start', 'end', 'timezone', 'search', 'page', 'page_size', 'basis']) {
    if (options[key] != null && options[key] !== '') query.set(key, String(options[key]))
  }
  return query
}
export const getAdminAnalytics = (period = '30d', source = null, options = {}) =>
  fetch(`${BASE}/admin/analytics/overview?${analyticsQuery(period, source, options)}`,
        { headers: headers(), signal: options.signal, cache: 'no-store' }).then(j)
export const getAdminAnalyticsScan = (scanId, options = {}) =>
  fetch(`${BASE}/admin/analytics/scans/${encodeURIComponent(scanId)}`,
        { headers: headers(), signal: options.signal, cache: 'no-store' }).then(j)
const downloadAnalytics = async (endpoint, filename, period, source, options) => {
  const response = await fetch(`${BASE}/admin/analytics/${endpoint}?${analyticsQuery(period, source, options)}`,
    { headers: headers(), signal: options.signal, cache: 'no-store' })
  if (!response.ok) return j(response)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = filename; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export const downloadAdminAnalyticsExport = (period, source, options = {}) =>
  downloadAnalytics('export', 'scan-register.csv', period, source, options)
export const downloadAdminAnalyticsMethodology = (period, source, options = {}) =>
  downloadAnalytics('methodology', 'scan-methodology.json', period, source, options)
export const getSources = () => (SIM ? sim(simGetSources()) : fetch(`${BASE}/sources`, { headers: headers() }).then(j))
export const getRubric = () => (SIM
  ? sim({ name: 'WCAG 2.1 AA', version: '1', hash: 'e85fcf7e14f9040c', target: 'WCAG 2.1 AA', threshold: 90, criteria: {} })
  : fetch(`${BASE}/rubric`, { headers: headers() }).then(j))
export const getRules = () => (SIM ? sim(simRules()) : fetch(`${BASE}/rules`, { headers: headers() }).then(j))
// Per-(criterion × format) remediation capability — the single source of truth for which
// WCAG criteria are auto-fixable per file format (see capability.js). SIM and any fetch
// failure fall back to the bundled table, so the format-aware UI never regresses to the
// format-blind view even offline.
export const getCapability = () => (SIM
  ? sim({ formats: Object.keys(CAPABILITY_FALLBACK), capability: CAPABILITY_FALLBACK })
  : fetch(`${BASE}/capability`, { headers: headers() }).then(j)
      .catch(() => ({ formats: Object.keys(CAPABILITY_FALLBACK), capability: CAPABILITY_FALLBACK })))
export const updateRubric = (body) => (SIM
  ? sim({ hash: 'e85fcf7e14f9040c', disabled_rules: body.disabled_rules || [], threshold: body.compliant_threshold || 90 })
  : fetch(`${BASE}/rubric`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }).then(j))
export const listScans = () => (SIM ? sim(simListScans()) : fetch(`${BASE}/scans`, { headers: headers() }).then(j))
// In-flight scan (for reconnecting after a reload). Returns {} when nothing is running.
export const getActiveScan = () => (SIM ? sim({}) : fetch(`${BASE}/scans/active`, { headers: headers() }).then(j))
// GET /workspace/bootstrap (workspace-bootstrap redesign, Phase 1) — one request for
// identity/permissions, the picked default scan's id/status and cached Overview snapshot, the
// scan-picker list, and the active-job summary. App.jsx's initial-load effect uses this in place
// of separately calling getMe (the permission fields only — display name/photo still come from
// getMe itself, which the bootstrap endpoint deliberately omits), listScans, and getActiveScan.
//
// SIM has no server-computed snapshot or default-scan pick, so it composes an equivalent shape
// from the existing SIM building blocks: simListScans()[0] IS the current, uncollapsed scan by
// construction (see simListScans), so there is no need to run the collapse-detection logic SIM
// never needs to exercise. `overview` only carries the fields App.jsx's loading screen actually
// reads (estate.discovered, documents.certifiable) — not a full mirror of the real snapshot's
// shape, which nothing in SIM mode consumes.
export const getWorkspaceBootstrap = () => {
  if (!SIM) return bootFetch(`${BASE}/workspace/bootstrap`, { headers: headers() }).then(j)
  const scans = simListScans()
  const cur = scans[0] || null
  return sim({
    me: simIdentity(),
    scan_id: cur?.id ?? null,
    scan_status: cur ? 'done' : null,
    revision: 0,
    overview: cur ? { estate: { discovered: cur.files ?? 0 }, documents: { certifiable: cur.certifiable ?? 0 } } : null,
    scans,
    active_job: {},
    active_workflows: [],
  })
}
export const getActiveWorkflows = () => (SIM
  ? sim({ active_workflows: [] })
  : fetch(`${BASE}/workspace/active-workflows`, { headers: headers() }).then(j))
// ADR 0014: push a freshly-minted GIS token to a running scan so long scans keep Drive auth.
export const refreshScanDriveToken = (scanId) => (SIM
  ? sim({ refreshed: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/drive-token`, { method: 'POST', headers: headers() }).then(j))
// Push a freshly-acquired MSAL token to a running scan so long SharePoint scans keep their auth.
export const refreshScanSPToken = (scanId) => (SIM
  ? sim({ refreshed: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/sp-token`, { method: 'POST', headers: headers() }).then(j))
// Revoke the scan's token store on sign-out so the worker doesn't keep stale credentials.
export const clearScanTokens = (scanId) => (SIM
  ? sim({ cleared: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/tokens`, { method: 'DELETE', headers: headers() }).then(j))
// A caller already holding `run.revision` can pass it as `knownRevision` to send
// `If-None-Match` — the backend bumps scan_runs.revision on every write that would change this
// response (Discover/Assess/Remediate/Publish), so a match means "nothing changed" and the
// backend skips recomputing/reshipping the full file_records+issue_records join. A 304 resolves
// to NOT_MODIFIED rather than throwing, so callers opt in by checking the return value — nothing
// about the plain `getScan(id)` call (no second argument, no If-None-Match header) changes.
export const NOT_MODIFIED = Symbol('scan-not-modified')
// Deliberately NOT BOOT_TIMEOUT_MS. This read is awaited into the boot chain, so a hang strands the
// app on "Loading your latest scan…" — but App.jsx's own comment calls it "a genuinely heavy query
// on a large estate (file_records joined against scan_inventory, shadow-file reconciliation,
// counter aggregation)", and an 8-second ceiling would start failing large estates that are simply
// slow. A ceiling that turns working-but-slow into broken is a worse bug than the one being fixed.
// 45s is far longer than any healthy response and still finite, which is the only property the
// hang needs.
export const SCAN_READ_TIMEOUT_MS = 45000
// The per-rule execution manifest, for the Assessment Run Integrity Gate (runIntegrity.js).
//
// Bounded like every other read added since #1149/#1150: a hang here is worse than a failure,
// because the gate's whole job is to withhold a conformance claim it cannot justify, and a fetch
// that never settles leaves it in `pending` — claiming nothing, but also never telling anyone why.
// A rejection reaches runIntegrity as `unavailable`, which is an answer.
//
// Its own constant rather than SCAN_READ_TIMEOUT_MS's 45s: this endpoint reads two indexed tables
// keyed by scan_id and does no joins or reconciliation, so it has none of the properties that
// argued for the long ceiling there.
export const MANIFEST_READ_TIMEOUT_MS = 15000
export const getScanManifest = (id) => (SIM ? sim(null) : fetch(
  `${BASE}/scans/${encodeURIComponent(id)}/manifest`,
  { headers: headers(), cache: 'no-store', signal: AbortSignal.timeout(MANIFEST_READ_TIMEOUT_MS) },
).then(j))
// One revisioned authority for execution state across Discover → Release. Domain metrics such as
// findings and assessment outcomes stay on their own canonical snapshots; this contract answers
// which stage ran, its work-item partition, integrity, and sealed handoff identity.
export const getStageLineage = (id) => (SIM ? sim({ lineage: null, content_digest: null }) : fetch(
  `${BASE}/scans/${encodeURIComponent(id)}/stage-lineage`,
  { headers: headers(), cache: 'no-store' },
).then(j))
export const getScan = (id, knownRevision = null) => (SIM ? sim(simGetScan(id)) : fetch(`${BASE}/scans/${id}`, {
  headers: headers(knownRevision != null ? { 'If-None-Match': `W/"${knownRevision}"` } : {}),
  cache: 'no-store',
  signal: AbortSignal.timeout(SCAN_READ_TIMEOUT_MS),
}).then((r) => (r.status === 304 ? NOT_MODIFIED : j(r))))
// The merged live-assessment snapshot (KPIs + funnel + worker/queue block) for the running screen.
// SIM has no live pipeline → available:false (the panel renders nothing).
export const getScanLive = (id) => (SIM ? sim({ available: false }) : fetch(`${BASE}/scans/${id}/live`, { headers: headers() }).then(j))
export const getInventory = () => (SIM ? sim([]) : fetch(`${BASE}/inventory`, { headers: headers() }).then(j))
export const reportUrl = (id) => (SIM ? '#' : `${BASE}/scans/${id}/report.pdf`)
// Fetch the report WITH the auth header (owner-scoped) → blob → download. Replaces the
// old tokenless <a href>, which let anyone pull another user's report by scan id.
export const openReport = (id, filename) => {
  if (SIM) return Promise.resolve()
  return fetch(`${BASE}/scans/${encodeURIComponent(id)}/report.pdf`, { headers: headers() })
    .then((r) => { if (!r.ok) throw new Error(`report ${r.status}`); return r.blob() })
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = filename || `acp-report-${id}.pdf`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60000)
    })
}
// The `?folders=` query for a chosen location set. `folder` (singular) stays the one-root form
// so an existing caller is untouched; both may be sent and the server unions them.
// A SharePoint/OneDrive location is `<driveId>/<itemId>` — the pair, because a Graph item id is
// unique only within its drive.
const foldersQ = (folders) => ((folders || []).length
  ? (folders || []).map((f) => `&folders=${encodeURIComponent(typeof f === 'string' ? f : f.id)}`).join('')
  : '')
// Carve-outs beneath the chosen folders. Server-side these are ignored without `folders`, since
// there would be nothing to carve out of and applying them to a whole-estate scan would narrow it
// with nothing on screen to say so.
const excludeQ = (exclude) => ((exclude || []).length
  ? (exclude || []).map((f) => `&exclude_folders=${encodeURIComponent(typeof f === 'string' ? f : f.id)}`).join('')
  : '')
export const startScan = (source = 'local', folder = null, aiEnabled = true, pii = false, excludeRemediated = false, incremental = true, folders = null, exclude = null, includeSubfolders = true) => (SIM ? sim(simStartScan(source), 120) : fetch(`${BASE}/scans?source=${source}${folder ? `&folder=${encodeURIComponent(folder)}` : ''}${foldersQ(folders)}${excludeQ(exclude)}&include_subfolders=${includeSubfolders ? 'true' : 'false'}&ai=${aiEnabled}&pii=${pii}&exclude_remediated=${excludeRemediated}&incremental=${incremental}`, { method: 'POST', headers: headers() }).then(j))
export const getJob = (id) => (SIM ? sim(simGetJob(id), 60) : fetch(`${BASE}/scans/jobs/${id}`, { headers: headers() }).then(j))

// Authenticated stream for the pre-scan job path. Native EventSource cannot carry ACP's bearer
// header, so use fetch + ReadableStream just like the scan-anchored Discover stream above.
export function openJobStream(jobId, { onMessage, onDone, onError } = {}) {
  if (SIM) return { close: () => {} }
  const controller = new AbortController()
  ;(async () => {
    try {
      const res = await fetch(`${BASE}/scans/jobs/${encodeURIComponent(jobId)}/stream`,
        { headers: headers(), signal: controller.signal, cache: 'no-store' })
      if (!res.ok || !res.body) { onError?.(); return }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parsed = parseSSEFrames(buffer)
        buffer = parsed.rest
        for (const frame of parsed.frames) {
          if (frame.event === 'done') { onDone?.(); return }
          if (frame.event === 'error') { onError?.(); return }
          try { onMessage?.(JSON.parse(frame.data)) } catch { /* malformed frame */ }
        }
      }
      onDone?.()
    } catch (e) {
      if (e?.name !== 'AbortError') onError?.()
    }
  })()
  return { close: () => controller.abort() }
}
getJob.openStream = openJobStream

// ── Durable async queue (ADR 0004/0005) ───────────────────────────────────────
// Queued scan: runs in the worker pool, survives restarts, shows in /jobs + Grafana.
// `idempotencyKey` comes from submitIntent.js — one key per submit intent, the SAME key on a
// retry of that intent. enqueue_scan looks it up owner-scoped and returns the original
// (scan_id, job_id) rather than inserting, so a response lost after the commit resolves to the
// job that already exists instead of creating a second scan. Optional so every existing caller
// and test keeps working unchanged; without it the server behaves exactly as before.
export const startScanQueued = (source = 'local', folder = null, aiEnabled = true, pii = false, excludeRemediated = false, incremental = true, folders = null, exclude = null, idempotencyKey = null, replaceActive = false, preferRecent = false, includeSubfolders = true) => (SIM
  ? sim({ scan_id: 'sim-scan', job_id: 'sim-job', queued: true, workers: 4 })
  : fetch(`${BASE}/scans?source=${source}${folder ? `&folder=${encodeURIComponent(folder)}` : ''}${foldersQ(folders)}${excludeQ(exclude)}&include_subfolders=${includeSubfolders ? 'true' : 'false'}&ai=${aiEnabled}&pii=${pii}&exclude_remediated=${excludeRemediated}&incremental=${incremental}&queue=true&fanout=true&replace_active=${replaceActive ? 'true' : 'false'}&prefer_recent=${preferRecent ? 'true' : 'false'}`, { method: 'POST', headers: headers(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}), signal: AbortSignal.timeout(SCAN_ENQUEUE_TIMEOUT_MS) }).then(j))
// Read-only check on the SPECIFIC source + folders about to be scanned — run right before
// doScan actually starts one, so a bad credential, a deleted folder, or a dead worker tier is
// caught before a scan row exists rather than surfacing as "0 documents" after the fact.
// Bound this advisory check to eight seconds. The worker still validates source access.
// Deliberately best-effort at the call site (see doScan): a failed preflight call itself must
// never block a scan the way a 'blocked' verdict correctly does — that would make preflight
// itself a new way to get stuck.
export const checkDiscoveryPreflight = (source = 'local', folder = null, folders = null) => (SIM
  ? sim({ verdict: 'ready', blocked_reasons: [], degraded_reasons: [] })
  : fetch(`${BASE}/discovery/preflight?source=${source}${folder ? `&folder=${encodeURIComponent(folder)}` : ''}${foldersQ(folders)}`, { method: 'POST', headers: headers(), signal: AbortSignal.timeout(8000) }).then(j))
// Stop an in-flight durable scan: kills its outstanding jobs server-side and closes the run
// as 'cancelled' (files already analysed keep their records). Owner-scoped — 409 otherwise.
export const cancelScan = (scanId) => (SIM
  ? sim({ scan_id: scanId, status: 'cancelled' })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/cancel`, { method: 'POST', headers: headers() }).then(j))
// Async server-side remediation: one remediate_file job per HTML file in the scan.
// SIM keeps a tiny drain state so getRemediationStatus ticks down over a few polls —
// the demo then shows the live KPI / progress-bar updates instead of finishing instantly.
let _simRemed = { remaining: 0, total: 0 }
export const remediateScan = (scanId, scope, remediationPolicy) => {
  if (SIM) { const n = scope ? scope.length : 3; _simRemed = { remaining: n, total: n }; return sim({ scan_id: scanId, enqueued: n, job_ids: ['a', 'b', 'c'], workers: 4 }) }
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediate`, { method: 'POST', headers: { ...headers(), 'Content-Type': 'application/json' }, body: JSON.stringify({ ...(scope ? { scope } : {}), ...(remediationPolicy ? { remediation_policy: remediationPolicy } : {}) }) }).then(j)
}
// Access allow-list (who can use the app) — managed from Settings.
export const getAllowlist = () => (SIM
  ? sim({ emails: ['demo@sim'], owner: 'demo@sim', domains: [], invite_enabled: false })
  : fetch(`${BASE}/admin/allowlist`, { headers: headers() }).then(j))
export const setAllowlist = (emails) => (SIM
  ? sim({ emails })
  : fetch(`${BASE}/admin/allowlist`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ emails }) }).then(j))
// Platform-admin management (owner-only PUT). `admins` is the owner-managed set; `env_admins` are
// permanent (ACP_ADMIN_EMAILS, set at deploy); `owner` is immutable.
export const getAdmins = () => (SIM
  ? sim({ owner: 'demo@sim', env_admins: [], admins: [] })
  : fetch(`${BASE}/admin/admins`, { headers: headers() }).then(j))
export const setAdmins = (emails) => (SIM
  ? sim({ owner: 'demo@sim', env_admins: [], admins: emails })
  : fetch(`${BASE}/admin/admins`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ emails }) }).then(j))
// Invite a tester as an Entra B2B guest AND add them to the allowlist in one step (ADR 0033).
// Owner-only; the endpoint 409s (and the UI hides) unless the guest-invite credential is configured.
export const inviteTester = (email) => (SIM
  ? sim({ email, emails: ['demo@sim', email], status: 'PendingAcceptance' })
  : fetch(`${BASE}/admin/invite`, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ email }) }).then(j))
// Unified people onboarding. Authentication stays with Google or Microsoft; ACP stores only
// the access grant, role, and lifecycle state needed to explain who can enter the workspace.
export const getPeople = () => (SIM
  ? sim({ people: [{ email: 'demo@sim', provider: 'google', role: 'owner', status: 'active', protected: true }], invite_enabled: false, domains: [], can_manage: true })
  : fetch(`${BASE}/admin/people`, { headers: headers() }).then(j))
export const addPerson = (person) => (SIM
  ? sim({ person: { ...person, status: person.provider === 'microsoft' ? 'setup_required' : 'access_ready' } })
  : fetch(`${BASE}/admin/people`, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(person) }).then(j))
export const updatePerson = (email, change) => (SIM
  ? sim({ person: { email, ...change } })
  : fetch(`${BASE}/admin/people/${encodeURIComponent(email)}`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(change) }).then(j))
export const removePerson = (email) => (SIM
  ? sim({ people: [] })
  : fetch(`${BASE}/admin/people/${encodeURIComponent(email)}`, { method: 'DELETE', headers: headers() }).then(j))

// ── workspace roles (PRD §13) ───────────────────────────────────────────────
// SIM returns empty rather than a fabricated role set. A demo that invents roles would let the
// Roles screen look finished while nothing behind it works — and this is the screen where
// believing a permission took effect, when it did not, is the whole risk.
export const getWorkspaceRoles = () => (SIM
  ? sim({ roles: [], enforced: false })
  : fetch(`${BASE}/admin/roles`, { headers: headers() }).then(j))
export const putRoleEnforcement = (body) => (SIM
  ? sim({ rollout: { mode: body.enabled ? 'enforce' : 'off' } })
  : fetch(`${BASE}/admin/workspace-roles/enforcement`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }).then(j))
export const getRoleCapabilities = () => (SIM
  ? sim({ tabs: [], levels: [], grants: [], mine: [], ungoverned_tabs: [] })
  : fetch(`${BASE}/admin/capabilities`, { headers: headers() }).then(j))
export const createWorkspaceRole = (body) => (SIM
  ? sim(body)
  : fetch(`${BASE}/admin/roles`, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }).then(j))
// `version` travels in the body, not as a header, because it is part of what is being saved:
// PRD §14's concurrency check refuses a stale one, and the server rejects the request outright if
// it is missing rather than treating absence as "no check".
export const updateWorkspaceRole = (roleId, body) => (SIM
  ? sim(body)
  : fetch(`${BASE}/admin/roles/${encodeURIComponent(roleId)}`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }).then(j))
export const deleteWorkspaceRole = (roleId) => (SIM
  ? sim({ deleted: roleId })
  : fetch(`${BASE}/admin/roles/${encodeURIComponent(roleId)}`, { method: 'DELETE', headers: headers() }).then(j))
// Owner-only rollout controls. Preflight is read once on demand (never polled); bootstrap is dry
// by default and writes only when the caller sends the literal boolean `true`.
export const getWorkspaceRolePreflight = () => (SIM
  ? sim({ rollout: { mode: 'off', next: 'observe' }, ready: false, blockers: 1, warnings: 0,
      findings: [{ code: 'roles_not_seeded', severity: 'blocker', detail: 'Built-in roles are not seeded.' }] })
  : fetch(`${BASE}/admin/workspace-roles/preflight`, { headers: headers() }).then(j))
export const bootstrapWorkspaceRoles = (apply = false) => (SIM
  ? sim({ dry_run: !apply, roles_created: [], assignments: [] })
  : fetch(`${BASE}/admin/workspace-roles/bootstrap`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ apply: apply === true }),
    }).then(j))
export const assignWorkspaceRole = (email, roleId, platformRole) => (SIM
  ? sim({ person: { email, workspace_role_id: roleId, ...(platformRole === undefined ? {} : { role: platformRole }) } })
  : fetch(`${BASE}/admin/people/${encodeURIComponent(email)}/role`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ role_id: roleId, ...(platformRole === undefined ? {} : { platform_role: platformRole }) }) }).then(j))
// What changes if this person is given this role — PRD §9's confirmation. Computed by the server
// from the same resolver the gate uses, NOT diffed in the browser from two capability lists: a
// confirmation that disagrees with what actually happens is worse than none, because it is read
// and approved before the change.
export const roleImpact = (email, roleId) => (SIM
  ? sim({ gains: [], loses: [] })
  : fetch(`${BASE}/admin/people/${encodeURIComponent(email)}/role-impact?role_id=${encodeURIComponent(roleId || '')}`, { headers: headers() }).then(j))
// The immutable decision log for one scan (system.py GET /decisions). The drawer reads the
// `scan.file_error` rows out of it to say WHY a document failed, instead of guessing "unreadable"
// — handlers records the verbatim exception per file and nothing was showing it.
export const listScanDecisions = (scanId, limit = 500) => (SIM
  ? sim([])
  : fetch(`${BASE}/decisions?scan_id=${encodeURIComponent(scanId)}&limit=${limit}`,
          { headers: headers() }).then(j).catch(() => []))
// Per-scan decision snapshots (PRD: time-travel) — restore/persist triage + action decisions.
export const getDecisions = (scanId) => (SIM
  ? sim({})
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/decisions`, { headers: headers() }).then(j))
export const saveDecision = (scanId, file, kind, value) => (SIM
  ? sim({ ok: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/decisions/${encodeURIComponent(file)}?kind=${kind}`,
          { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ value }) }).then(j))
export const saveDecisionsBatch = (scanId, items) => (SIM
  ? sim({ ok: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/decisions`,
          { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ items }) }).then(j))
// Run the WCAG assessment into Langfuse on demand (separate from the scan trace).
// `includeLifecycleFlagged` is the authorized override (PRD §4.5): by default the run SKIPS files a
// discovery rule flagged for archival or deletion (Archive/Delete Candidate, Archived, Deleted) —
// pass true to pull them back into the assessment anyway.
// Assessment scope belongs to this owned scan, not administrative workspace settings.
export const getAssessmentScope = (scanId) => (SIM
  ? getSettings()
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/assessment-scope`, { headers: headers() }).then(j))
export const putAssessmentScope = (scanId, body) => (SIM
  ? sim({ simulated: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/assessment-scope`, {
      method: 'PUT', headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j))
export const assessScan = (scanId, level = 'AA', includeLifecycleFlagged = false, fixApprovalPolicy = null) => (SIM
  ? sim({ ok: true })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/assess?level=${encodeURIComponent(level)}&include_lifecycle_flagged=${includeLifecycleFlagged ? 'true' : 'false'}`,
          { method: 'POST', headers: fixApprovalPolicy ? { ...headers(), 'Content-Type': 'application/json' } : headers(),
            ...(fixApprovalPolicy ? { body: JSON.stringify({ fix_approval_policy: fixApprovalPolicy }) } : {}) }).then(j))
// Live remediation progress: in-flight jobs + latest fixed file (drives the Remediate bar).
export const getRemediationStatus = (scanId) => {
  if (SIM) {
    const step = Math.max(1, Math.ceil(_simRemed.total / 4))   // drain over ~4 polls
    _simRemed.remaining = Math.max(0, _simRemed.remaining - step)
    const fixedSoFar = _simRemed.total - _simRemed.remaining
    return sim({ in_flight: _simRemed.remaining, failed: 0,
                 latest_file: _simRemed.remaining ? `report-${fixedSoFar}.html` : `report-${_simRemed.total}.html` }, 120)
  }
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation-status`,
               { headers: headers(), cache: 'no-store' }).then(j)
}

// The reconciled, revisioned run snapshot (PRD "Remediation Real-Time Operations Panel" §8) —
// run state, the six-way document partition, phase rail, and the server's own invariant check.
// Distinct from getRemediationStatus, which stays as-is and still feeds the progress bar.
//
// SIM RETURNS NOTHING, deliberately. Every other SIM answer is a plausible literal; a plausible
// literal here would be a hand-written run state and a hand-balanced partition — i.e. a second,
// fabricated implementation of the one contract this endpoint exists to make impossible to fake.
// The panel renders nothing rather than a demo of a guarantee that was not checked.
export const getRemediationSnapshot = (scanId) => (SIM
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/snapshot`,
          { headers: headers(), cache: 'no-store' }).then(j))
export const getRemediationAutomationPolicy = (scanId) => (SIM
  ? sim({ policy: { level: 3, revision: 0 }, run_policy_snapshot: null,
          capabilities: { apply_waiting: false } })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/automation-policy`,
          { headers: headers(), cache: 'no-store' }).then(j))
// This forecast comes from the complete stored run, never a browser's partial review queue.
export const getRemediationImpact = (scanId, policy, scope) => (SIM
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/impact-preview`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ ...policy, ...(scope ? { scope } : {}) }), cache: 'no-store',
    }).then(j))
export const saveRemediationImpactPolicy = (scanId, policy, expectedRevision) => (SIM
  ? Promise.reject(new Error('Saving remediation settings requires a connected backend.'))
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/impact-policy`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ ...policy, expected_revision: expectedRevision }),
    }).then(j))
export const assignRemediationImpact = (scanId, files, assignee, policy) => (SIM
  ? Promise.reject(new Error('Assigning remediation work requires a connected backend.'))
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/impact-assign`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ files, assignee, policy }),
    }).then(j))
export const submitRemediationPolicyAction = (scanId, action, level, expectedRevision, idempotencyKey) => (SIM
  ? sim({ action, policy: { level, revision: expectedRevision + 1 }, duplicate: false })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/automation-policy/actions`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }),
      body: JSON.stringify({ action, level, expected_revision: expectedRevision }),
    }).then(j))
export const getFindingDispositions = (scanId, disposition = null) => (SIM
  ? sim({ scan_id: scanId, batch_id: null, disposition, items: [], available: false })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/finding-dispositions${
      disposition ? `?disposition=${encodeURIComponent(disposition)}` : ''}`,
    { headers: headers(), cache: 'no-store' }).then(j))
// ── Exceptions and scoped recovery (PRD §6E, §11) ─────────────────────────────
//
// SIM RETURNS AN EMPTY VIEW, deliberately, exactly as getRemediationSnapshot returns null. A
// simulated exception group would be a hand-written set of refusal codes and destinations — a
// second, fabricated implementation of the one contract these endpoints exist to make impossible
// to fake — and its retry buttons would appear to write to a customer's library and do nothing.
export const getRemediationExceptions = (scanId) => (SIM
  ? sim({ run_id: scanId, groups: [], controls: [] })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/exceptions`,
          { headers: headers(), cache: 'no-store' }).then(j))
// `files` is the SELECTION — the documents the user can currently see and has chosen. Always sent
// explicitly, never omitted to mean "all": the server acts on exactly what it is given, so a
// group action can never reach a row that scrolled out of view or was filtered away.
const remediationAction = (scanId, path, files) => (SIM
  ? sim({ requested: 0, started: 0, refused: 0, failed: 0, duplicate: 0, results: [],
          complete_success: false, summary: 'Not available in the demo' })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/${path}`,
          { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(files === undefined ? {} : { files }) }).then(j))
export const retryRemediationDelivery = (scanId, files) =>
  remediationAction(scanId, 'exceptions/retry-delivery', files)
export const retryRemediationDocuments = (scanId, files) =>
  remediationAction(scanId, 'exceptions/retry-documents', files)
export const cancelRemediationRun = (scanId) => remediationAction(scanId, 'cancel')
export const pauseRemediationRun = (scanId) => remediationAction(scanId, 'pause')
export const resumeRemediationRun = (scanId) => remediationAction(scanId, 'resume')
export const cancelLiveOpsStage = (scanId, stage) => fetch(
  `${BASE}/admin/activity/workflows/${encodeURIComponent(scanId)}/stages/${encodeURIComponent(stage)}/cancel`,
  { method: 'POST', headers: headers() }).then(j)
export const resumeLiveOpsRemediation = (scanId) => fetch(
  `${BASE}/admin/activity/workflows/${encodeURIComponent(scanId)}/stages/remediate/resume`,
  { method: 'POST', headers: headers() }).then(j)
// Authenticated Remediate progress stream.  Native EventSource cannot send ACP's bearer header,
// so this shares Discover's fetch + ReadableStream SSE parser and exposes the same close contract.
// `lastEventId` resumes the durable lifecycle log (ADR 0051): pass the last id this client
// rendered and the server replays the scan_events it missed, oldest first, before any live frame.
// Sent as a REQUEST HEADER, not a query param, for the reason the bearer token is — request URLs
// reach proxy access logs and browser history.
//
// It is sent explicitly because nothing sends it for us: `Last-Event-ID` is automatic only for
// native EventSource, which this function deliberately does not use (see the Discover stream's
// note above — EventSource cannot carry the Authorization header at all).
//
// `onEvent` receives one lifecycle event per call, with its `id`. `onReconcile` fires when the
// server declines to replay — a cursor ahead of the log, a pruned log, a malformed cursor — and
// means: fetch a fresh snapshot before applying anything later (PRD §17.6). A caller that passes
// neither behaves exactly as this function did before resume existed.
export function openRemediationStream(scanId, { onMessage, onDone, onError,
                                                onEvent, onReconcile, lastEventId = null } = {}) {
  if (SIM) return { close: () => {} }
  const controller = new AbortController()
  ;(async () => {
    try {
      const h = headers()
      if (lastEventId !== null && lastEventId !== undefined) h['Last-Event-ID'] = String(lastEventId)
      const res = await fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/stream`,
        { headers: h, signal: controller.signal, cache: 'no-store' })
      if (!res.ok || !res.body) { onError?.(); return }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const { frames, rest } = parseSSEFrames(buffer)
        buffer = rest
        for (const frame of frames) {
          if (frame.event === 'done') { onDone?.(); return }
          if (frame.event === 'error') { onError?.(); return }
          if (frame.event === 'reconciliation-required') {
            try { onReconcile?.(JSON.parse(frame.data)) } catch { onReconcile?.({}) }
            continue
          }
          if (frame.event === 'remediation-event') {
            // The id rides alongside the parsed event so a caller can persist the cursor without
            // reaching into the payload — the frame's id is the authority, not a field inside it.
            try { onEvent?.(JSON.parse(frame.data), frame.id) } catch { /* keep listening */ }
            continue
          }
          try { onMessage?.(JSON.parse(frame.data)) } catch { /* malformed frame: keep listening */ }
        }
      }
      onDone?.()
    } catch (e) {
      if (e?.name !== 'AbortError') onError?.()
    }
  })()
  return { close: () => controller.abort() }
}
// Per-violation remediation state (ADR 0003 Phase 2) for one file — which rule_ids were
// actually auto-fixed, so the rule coverage table can say "pass — remediated" instead of
// just "pass" for a criterion that used to fail. SIM has no per-violation state to draw
// on, so it returns nothing rather than fabricate it.
export const getFileRemediationState = (scanId, file) => (SIM
  ? sim([])
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/remediation-state`,
          { headers: headers() }).then(j))
// The backend owns primary-reason precedence and integrity; callers render this response and
// must not recreate its classification in the browser.
export const previewRemediationAutomationPolicy = (findings, level) => (SIM
  ? Promise.resolve(null)
  : fetch(`${BASE}/remediation/automation-policy/preview`, {
      method: 'POST', headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ findings, level }),
    }).then(j))
// Platform settings (admin) — includes ADR 0010's Drive-mirror on/off + folder name.
// SIM has no backend, so this is a browser-local store rather than a fresh literal each read,
// and every answer it gives carries `simulated: true`. That flag is not decoration: see
// updateSettings below for what it exists to stop.
const _simSettings = { ai_enabled: true, drive_mirror_enabled: true, drive_mirror_folder: 'Remediated' }
export const getSettings = () => (SIM
  ? sim({ ..._simSettings, simulated: true })
  : fetch(`${BASE}/settings`, { headers: headers() }).then(j))
// Source-staleness (Release Center): did each file's SOURCE change in Drive since ACP scanned it?
// headers() attaches X-Drive-Token, which the endpoint needs to read the source's current state.
export const getSourceStatus = (scanId) => (SIM
  ? sim({ scan_id: scanId, stale_count: 0, untracked_count: 0, unavailable_count: 0, files: [] })
  : fetch(`${BASE}/scans/${scanId}/source-status`, { headers: headers() }).then(j))
// AI usage + cost governance rollup (ADR 0019 Phase 1) — today / month / all-time.
const _emptyRoll = { calls: 0, ok: 0, failed: 0, cost_usd: 0, avg_latency_ms: 0, scans: 0, by_provider: [], by_model: [], by_zone: [], by_surface: [] }
export const getAiCosts = () => (SIM
  ? sim({ today: _emptyRoll, month: _emptyRoll, all_time: _emptyRoll, shadow_rollout: null })
  : fetch(`${BASE}/ai/costs`, { headers: headers() }).then(j).catch(() => ({ today: _emptyRoll, month: _emptyRoll, all_time: _emptyRoll, shadow_rollout: null })))
// AI provider gateway config (ADR 0019 §6). The API returns only SAFE views — never a key value,
// just whether the referenced secret is present. putAiProvider sends the secret's reference NAME,
// never a key (the backend rejects a pasted key).
// The error is NOT swallowed here: a 403 means this signed-in user is not the owner, so the
// admin forms must be disabled rather than presented as editable fields whose save can only
// fail. Callers that just want a list can still .catch() themselves.
export const getAiProviders = () => (SIM
  ? sim({ providers: [] })
  : fetch(`${BASE}/ai/providers`, { headers: headers() }).then(j))
export const getSecondOpinionPolicy = () => (SIM
  ? sim({ enabled: false, criteria: ['1.3.5'], confidence_threshold: 'low',
      max_requests_per_scan: 25, max_requests_per_day: 250, max_daily_cost_usd: 10,
      estimated_cost_per_request_usd: 0.01 })
  : fetch(`${BASE}/ai/second-opinion-policy`, { headers: headers() }).then(j))
export const putSecondOpinionPolicy = (policy) => (SIM
  ? sim({ ...policy, simulated: true })
  : fetch(`${BASE}/ai/second-opinion-policy`, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(policy),
    }).then(j))
export const getRemediationPilot = () => (SIM
  ? sim({ enabled: false, running: false, categories: ['docx:2.4.4', 'html:2.4.4'],
      provider: 'anthropic', model: 'claude-sonnet-5', stop_reasons: ['Pilot is off'],
      max_calls: 100, max_spend_usd: 5, min_sample: 10, max_failure_rate: .1,
      min_acceptance_rate: .9, max_edit_rate: .2, min_validation_clear_rate: .95,
      metrics: { calls: 0, failed: 0, cost_usd: 0, decisions: 0, accepted: 0,
        edited: 0, validated: 0, cleared: 0, regressed: 0, newly_failing: 0 } })
  : fetch(`${BASE}/ai/remediation-pilot`, { headers: headers() }).then(j))
export const putRemediationPilot = (policy) => (SIM
  ? sim({ ...policy, running: !!policy.enabled, stop_reasons: policy.enabled ? [] : ['Pilot is off'], simulated: true })
  : fetch(`${BASE}/ai/remediation-pilot`, {
      method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(policy),
    }).then(j))
export const getAiProvidersHealth = (windowHours = 24) => (SIM
  ? sim({ window_hours: windowHours, providers: {} })
  : fetch(`${BASE}/ai/providers/health?window_hours=${windowHours}`, { headers: headers() }).then(j))
export const putAiProvider = (patch) => (SIM
  ? sim({ providers: [], simulated: true })
  : fetch(`${BASE}/ai/providers`, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(patch),
    }).then(j))
// The ONE call that carries a key value, and only to hand it to the deployment's Key Vault: the
// backend stores the resulting reference name and never the value, and no read path returns it.
// Available only where GET /ai/providers reports secret_write.available — a deployment without a
// vault refuses (422) rather than storing the value anywhere else, so the panel hides the field
// instead of offering a box that cannot work.
export const putAiProviderSecret = (provider, value) => (SIM
  ? sim({ providers: [], simulated: true })
  : fetch(`${BASE}/ai/providers/${encodeURIComponent(provider)}/secret`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ value }),
    }).then(j))
// Ask the server to send its own SYNTHETIC probe image to one provider and report what came back.
// The body carries a provider NAME and nothing else — no key, and no document: the image is
// generated server-side (providers.probe_image_bytes) precisely so pressing this cannot send
// customer content to a third party. In the simulated build it reports that it did nothing,
// rather than a cheerful ✓ for a call that never left the browser.
export const testAiProvider = (provider) => (SIM
  ? sim({ ok: false, provider, reason: 'simulated', detail: 'simulated build — no call was made' })
  : fetch(`${BASE}/ai/providers/test`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ provider }),
    }).then(j))

// ADR 0021 · Review memory — the org's house-style rules, and the decisions on derived proposals.
//
// The READ is open to any signed-in user (the backend's GET has no admin gate), because seeing
// which house style shaped a draft is not an admin privilege. Every WRITE is admin-gated server
// side by `_require_admin`, so ReviewMemory.jsx renders those controls only for `me?.is_admin` —
// the #952 lesson, where WorkerReplicaControl showed buttons to everyone and a non-admin's click
// optimistically updated then silently reverted on the 403, with no message.
//
// Errors are NOT swallowed. "This org has no house style" and "we could not reach the server" are
// different sentences, and only the caller can say which it is in — the same contract
// getDocumentTimeline and getAiProviders already hold. Callers must carry a .catch.
//
// The three writes send their arguments as QUERY PARAMS, not a JSON body: the backend declares
// them as `Query(...)`, so a JSON body would be silently ignored and every call would 422.
export const getOrgMemory = () => (SIM
  ? sim({ rules: [], enabled: false })
  : fetch(`${BASE}/org-memory`, { headers: headers() }).then(j))

export const addOrgMemoryRule = ({ kind, guidance, ruleId = null, format = null }) => {
  if (SIM) return sim({ id: 'sim', status: 'active', simulated: true })
  const qs = new URLSearchParams({ kind, guidance })
  if (ruleId) qs.set('rule_id', ruleId)
  if (format) qs.set('format', format)
  return fetch(`${BASE}/org-memory?${qs}`, { method: 'POST', headers: headers() }).then(j)
}

export const setOrgMemoryStatus = (id, status) => (SIM
  ? sim({ id, status, simulated: true })
  : fetch(`${BASE}/org-memory/${encodeURIComponent(id)}/status?status=${encodeURIComponent(status)}`,
          { method: 'PUT', headers: headers() }).then(j))

// Runs the derivation job for this org NOW. Returns {proposed, count} — `count: 0` is a real,
// common answer (not enough review signal yet), and the panel says so rather than reading as a
// failed request.
export const deriveOrgMemory = () => (SIM
  ? sim({ proposed: [], count: 0, simulated: true })
  : fetch(`${BASE}/org-memory/derive`, { method: 'POST', headers: headers() }).then(j))

// A SIM write is not a write, and this stub used to be unable to say so. It returned
// `{ ...defaults, ...patch }` — the caller's own request handed back as the server's answer.
// Settings.jsx guards against a silent no-op by comparing the RESPONSE to the REQUEST, so a
// response BUILT FROM the request cleared that guard every time: `drift` was necessarily empty,
// and the panel printed "✓ endpoint switched" for a PUT that never left the browser. SIM is the
// one build where a fake success is possible and it was the one build the guard could not see.
//
// That cost two debugging cycles (2026-07-30, 2026-07-31) on a demo-blocking vision model. SIM is
// ON by default (sim.js: `VITE_SIM !== 'false'`), and only deploy/public/Dockerfile sets it false —
// so the Netlify site and `npm run dev` are both SIM, and both were clicked for a production fix.
//
// The local store still takes the patch, so the demo's toggles still move on screen. `simulated`
// is what stops a caller calling that a save: every caller MUST read it before reporting success.
export const updateSettings = (patch) => (SIM
  ? sim((() => { Object.assign(_simSettings, patch); return { ..._simSettings, simulated: true } })())
  : fetch(`${BASE}/settings`, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(patch),
    }).then(j))
// Per-user scan-scope override (ADR 0035 stage 2). A signed-in user's OWN override — the server keys it
// to their identity and only ever lets it WIDEN the owner default (the union lives in active_scope), so
// this surface can never narrow below what the owner mandated. `owner_default` is returned so the editor
// can show the baseline the override adds onto. PUT sends `{scan_scope: <map | "">}`; DELETE clears the
// override so scans fall back to the owner default (distinct from PUT "" which is a real "no restriction").
const _simMyScope = { scan_scope: '', owner_default: '' }
export const getMyScope = () => (SIM
  ? sim({ ..._simMyScope, simulated: true })
  : fetch(`${BASE}/settings/mine`, { headers: headers() }).then(j))
export const putMyScope = (payload) => (SIM
  ? sim((() => { _simMyScope.scan_scope = typeof payload === 'string' ? payload : JSON.stringify(payload); return { scan_scope: _simMyScope.scan_scope, simulated: true } })())
  : fetch(`${BASE}/settings/mine`, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ scan_scope: payload }),
    }).then(j))
export const clearMyScope = () => (SIM
  ? sim((() => { _simMyScope.scan_scope = ''; return { scan_scope: '', simulated: true } })())
  : fetch(`${BASE}/settings/mine`, { method: 'DELETE', headers: headers() }).then(j))
export const putMyReleaseTimezone = (releaseTimezone) => (SIM
  ? sim({ release_timezone: releaseTimezone, simulated: true })
  : fetch(`${BASE}/settings/mine`, {
      method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ release_timezone: releaseTimezone }),
    }).then(j))
export const putMyReleaseDestination = (releaseDestination) => (SIM
  ? sim({ release_destination: releaseDestination, simulated: true })
  : fetch(`${BASE}/settings/mine`, {
      method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ release_destination: releaseDestination }),
    }).then(j))
export const putMyReleaseTemplates = (releaseTemplates) => (SIM
  ? sim({ release_templates: releaseTemplates, simulated: true })
  : fetch(`${BASE}/settings/mine`, {
      method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ release_templates: releaseTemplates }),
    }).then(j))
// Download a remediated file's fixed bytes (ADR 0010) — Blob primary, Drive-mirror
// fallback server-side. Authenticated fetch → blob → download, same pattern as
// openReport (a bare <a href> would drop the Authorization header).
export const downloadRemediated = (scanId, file) => {
  if (SIM) return Promise.resolve()
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/remediated`,
               { headers: headers() })
    .then((r) => { if (!r.ok) throw new Error(`download ${r.status}`); return r.blob() })
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = file
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60000)
    })
}
// Status of one durable-queue job — real progress for a single-file remediation
// (queued → running → done/dead) instead of a timed guess. SIM scripts a quick
// two-poll running → done sequence per job id.
const _simJobs = {}
export const getQueueJob = (jobId) => {
  if (SIM) {
    _simJobs[jobId] = (_simJobs[jobId] ?? 3) - 1
    return sim({ id: jobId, type: 'remediate_file', status: _simJobs[jobId] > 0 ? 'running' : 'done' }, 120)
  }
  return fetch(`${BASE}/jobs/${encodeURIComponent(jobId)}`, { headers: headers() }).then(j)
}
// Send this scan's review-needing findings to the server HITL queue (idempotent
// POST /hitl/queue/{sid}/auto) — called after a single-file remediate-now so an
// AI-assisted fix still gets human sign-off instead of silently skipping review.
// Re-check this scan's open review rows against the RECORDED assessment of the saved corrected
// copy (contract C7/C8). Owner-scoped server route; no AI call, no document write, no change to any
// row's review status — it only marks rows whose target a verified change already removed.
// Resolves { scan_id, superseded_count, files: [{ file, superseded, unchanged, skipped: [{item_id, reason}] }] }.
export const reconcileReviewTargets = (scanId) => (SIM
  ? sim({ scan_id: scanId, superseded_count: 0, files: [] })
  : fetch(`${BASE}/hitl/queue/${encodeURIComponent(scanId)}/reconcile-targets`, { method: 'POST', headers: headers(), cache: 'no-store' }).then(j))
export const queueHitlReview = (scanId) => (SIM
  ? sim({ queued: 1 })
  : fetch(`${BASE}/hitl/queue/${encodeURIComponent(scanId)}/auto`, { method: 'POST', headers: headers() }).then(j))
// ── Phased remediation campaigns (ADR 0003 Phase 4) ─────────────────────────────
export const createCampaign = (scanId, name, deadline = null) => (SIM
  ? sim({ campaign_id: 'sim-campaign', name, status: 'active', batches: [] }, 200)
  : fetch(`${BASE}/campaigns`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ scan_id: scanId, name, deadline }),
    }).then(j))
export const listCampaigns = (scanId) => (SIM
  ? sim([])
  : fetch(`${BASE}/campaigns?scan_id=${encodeURIComponent(scanId)}`, { headers: headers() }).then(j))
export const getCampaign = (campaignId) => (SIM
  ? sim(null)
  : fetch(`${BASE}/campaigns/${encodeURIComponent(campaignId)}`, { headers: headers() }).then(j))
export const setCampaignStatus = (campaignId, status) => (SIM
  ? sim({ campaign_id: campaignId, status })
  : fetch(`${BASE}/campaigns/${encodeURIComponent(campaignId)}/status`, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ status }),
    }).then(j))
// ── HITL review queue ─────────────────────────────────────────────────────────
// Auto-populate HITL queue from AI-assisted FAILs in a scan (idempotent).
export const autoPopulateHitlQueue = (scanId) => (SIM
  ? sim({ queued: 0, items: [] })
  : fetch(`${BASE}/hitl/queue/${encodeURIComponent(scanId)}/auto`, { method: 'POST', headers: headers() }).then(j))
// List ALL HITL items across scans (the global "Review queue" bell). No scan_id → the
// backend returns every item; status omitted → every status (metrics need resolved ones).
export const listAllHitl = (status = null) => (SIM
  ? sim([], 60)
  : fetch(`${BASE}/hitl/queue${status ? `?status=${status}` : ''}`, { headers: headers() }).then(j))
// Items whose PUT is in flight: keyed by item id, set BEFORE the fetch so that a
// listHitlQueue reload triggered by the acp:hitl-changed event (which Remediate.jsx fires
// before calling updateHitlItem) cannot resurrect an item the reviewer just actioned.
// The entry is cleared when the PUT settles — success or failure — so an undo after a
// network error correctly re-surfaces the item on the next reload.
const _pendingActs = new Map()

// List HITL items for a scan, optionally filtered by status.
//
// `includeTargetReplaced` asks for superseded rows too, then keeps ONLY those whose target was
// removed by another verified fix (superseded_reason 'target_removed_by_verified_fix'). The review
// workspace needs them to show that terminal result; without the flag the server drops them and
// the finding the ledger now reports as replaced has no row to explain it. Every other superseded
// row stays filtered, exactly as the server's default read does.
export const listHitlQueue = (scanId, status = null, options = {}) => (SIM
  ? sim([])
  : fetch(`${BASE}/hitl/queue?scan_id=${encodeURIComponent(scanId)}${status ? `&status=${status}` : ''}${options.includeTargetReplaced ? '&include_superseded=true' : ''}`, { headers: headers(), cache: 'no-store', signal: options.signal }).then(j)
      .then((items) => (items || []).filter((it) => !_pendingActs.has(it.id)
        && (!it.superseded || it.superseded_reason === 'target_removed_by_verified_fix'))))
// Update a HITL item (approved / rejected / skipped) with an optional reviewer note
// and/or the reviewer's final (AI-drafted or hand-edited) approved_value.
// opts (optional) carries review telemetry for the Intelligent Review Workspace:
// { edited } — reviewer changed the AI draft before approving (confidence-calibration signal);
// { reviewMs } — time from card-open to decision (the reviewer-time-saved metric);
// { aiValue } — the AI-proposed value shown, so the server stores proposed-vs-final.
// opts.approvedValues — one final text per proposal, positionally (the row holds one proposal
// per image). A null/'' entry accepts that proposal's own draft. The server writes them into
// the document; approved_value stays the single headline value, for the audit log.
export const updateHitlItem = (itemId, status, reviewerNote = null, approvedValue = null, opts = {}) => {
  if (SIM) return sim({ id: itemId, status })
  // Register the item SYNCHRONOUSLY before the fetch so that any listHitlQueue call
  // resolving while the PUT is still in flight (the race that caused I.2) filters it out.
  _pendingActs.set(itemId, Date.now())
  const requestId = opts.requestId || globalThis.crypto?.randomUUID?.()
    || `hitl-${Date.now()}-${Math.random().toString(16).slice(2)}`
  return fetch(`${BASE}/hitl/queue/${encodeURIComponent(itemId)}`, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ status, reviewer_note: reviewerNote, approved_value: approvedValue,
        approved_values: opts.approvedValues ?? null,
        edited: !!opts.edited, review_ms: opts.reviewMs ?? null, ai_value: opts.aiValue ?? null,
        model_call_id: opts.modelCallId ?? null,
        model_call_ids: opts.modelCallIds ?? null,
        request_id: requestId,
        expected_version: opts.expectedVersion ?? null,
        expected_proposal_snapshot_ids: opts.expectedProposalSnapshotIds ?? null,
        expected_source_revision: opts.expectedSourceRevision ?? null,
        // 'single' — one reviewer's decision on the row on screen: the server compares the viewed
        // binding above under the row lock and allows edited values. Omitted (null) by a frozen batch
        // selection, which keeps the batch guard. See viewedApprovalBinding.js.
        approval_scope: opts.approvalScope ?? null,
        // The corrected copy the reviewer was looking at: the row's `corrected_artifact` VERBATIM (its
        // sha256, or "none" when no corrected copy exists). Required on every approval, single and batch.
        expected_corrected_sha256: opts.expectedCorrectedSha256 ?? null,
        // The row's reviewable content the reviewer was looking at: its opaque `proposal_digest`, VERBATIM.
        // Required on every approval, single and batch (D -> F phase 6).
        expected_proposal_digest: opts.expectedProposalDigest ?? null,
        // Feedback intelligence: WHY a rejection happened (enum; bulk/keyboard paths send 'unspecified')
        reject_reason: opts.rejectReason ?? null,
        // WCAG exception the reviewer applied instead of writing a fix: 'decorative' (1.1.1 — image
        // conveys nothing, no text alternative required) or 'essential_exception' (1.4.5/1.4.9 —
        // a logo/brand mark is exempt). Recorded in the audit trail as WHY the finding is resolved.
        resolution: opts.resolution ?? null }),
    }).then(j).then(
      (result) => { _pendingActs.delete(itemId); return result },
      (err)    => { _pendingActs.delete(itemId); throw err },
    )
}
export const assignHitlItem = (itemId, assignee) =>
  fetch(`${BASE}/hitl/queue/${encodeURIComponent(itemId)}/assign`, {
    method: 'PATCH',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ assignee: assignee || null }),
  }).then(j)
// Retry one version-bound approval. The response distinguishes active jobs from refusals.
export const retryApprovedWrite = (itemId) => (SIM
  ? sim({ accepted: true, in_flight: true, status: 'queued', job_id: `sim-retry-${itemId}`, reason: null })
  : fetch(`${BASE}/hitl/queue/${encodeURIComponent(itemId)}/retry-write`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({}),
    }).then(j))
// HITL review telemetry for the workspace dashboard — decisions by action, approval rate,
// edit rate (confidence-calibration signal), avg review time (reviewer-time-saved). Scan-scoped.
export const getHitlAnalytics = (scanId = null) => (SIM
  ? sim({ total: 0, by_action: {}, reviewed: 0, approval_rate: null, edit_rate: null, avg_review_ms: null })
  : fetch(`${BASE}/hitl/analytics${scanId ? `?scan_id=${encodeURIComponent(scanId)}` : ''}`, { headers: headers() }).then(j).catch(() => null))
// Draft a concrete fix value (alt text / link text / title) via the local AI model
// for one semantic finding. Reviewer accepts/edits it — never auto-applied here.
//
// `locator` names WHICH embedded image to describe (hitl_queue.evidence[].locator). Without it
// a 1.1.1 suggestion can only ever be a fill-in template: the endpoint has no image to hand a
// vision model, so the text model guesses from the filename. Omitted for the other criteria,
// which need no picture.
// `style` ('shorter' | 'detailed' | 'regenerate') lets a reviewer re-draft an image description at a
// different length (#131) — a bounded steer the backend allowlists; anything else is ignored.
// Second-opinion cross-check of a drafted alt text (#123): the model independently re-describes the
// image and we compare → {verdict: 'consistent'|'divergent', second_opinion, shared_terms}. {} when
// no image / model down / SIM. Never a score.
export const validateAlt = (scanId, file, ruleId, locator, alt) => (SIM || !alt
  ? sim({})
  : fetch(`${BASE}/ai/validate?scan_id=${encodeURIComponent(scanId)}&file=${encodeURIComponent(file)}&rule_id=${encodeURIComponent(ruleId)}&alt=${encodeURIComponent(alt)}`
      + (locator ? `&locator=${encodeURIComponent(locator)}` : ''),
      { headers: headers() }).then(j).catch(() => ({})))

export const suggestFix = (scanId, file, ruleId, locator = null, style = null) => (SIM
  ? sim({ suggestion: 'AI draft unavailable in demo mode — edit manually', kind: 'fix', is_template: true })
  : fetch(`${BASE}/ai/suggest?scan_id=${encodeURIComponent(scanId)}&file=${encodeURIComponent(file)}&rule_id=${encodeURIComponent(ruleId)}`
      + (locator ? `&locator=${encodeURIComponent(locator)}` : '')
      + (style ? `&style=${encodeURIComponent(style)}` : ''),
      { headers: headers() }).then(j))

export const getCopilotGuidance = (scanId, file, ruleId, locator = null) => (SIM
  ? sim({ guidance: 'This chart compares quarterly revenue across regions — focus on the trend and which region leads.', provider: 'demo' }, 800)
  : fetch(`${BASE}/ai/copilot?scan_id=${encodeURIComponent(scanId)}&file=${encodeURIComponent(file)}&rule_id=${encodeURIComponent(ruleId)}`
      + (locator ? `&locator=${encodeURIComponent(locator)}` : ''),
      { headers: headers() }).then(j))

// Re-download and re-score ONE file the user fixed externally. Returns {job_id} for polling.
export const rescoreFile = (scanId, file) => (SIM
  ? sim({ job_id: 'sim-rescore', workers: 1 }, 200)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/rescore?file=${encodeURIComponent(file)}`, { method: 'POST', headers: headers() }).then(j))
// Record a file (or files) as published back to its source. Body drives both forms.
export const publishFile = (scanId, file, destination = null) => (SIM
  ? sim({ published: [{ file, published_at: new Date().toISOString() }] }, 150)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/publish`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ file, ...(destination ? { destination } : {}) }),
    }).then(j))
export const publishAllFiles = (scanId, files, releaseFolderName = '', options = {}) => (SIM
  ? sim({ published: files.map((f) => ({ file: f, published_at: new Date().toISOString() })) }, 150)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/publish`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ files, ...(releaseFolderName.trim() ? { release_folder_name: releaseFolderName.trim() } : {}),
        ...(options.destination ? { destination: options.destination } : {}),
        ...(options.allowRemainingIssues ? { allow_remaining_issues: true } : {}),
        ...(options.expectedArtifacts ? { expected_artifacts: options.expectedArtifacts, expected_destination: options.destination || null } : {}) }),
    }).then(j))
export const getReleaseStatus = (scanId) => (SIM
  ? sim({ release_id: null, roots: [], documents: [], documents_total: 0, published: 0, failed: 0, remaining: 0 }, 50)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release`, { headers: headers() }).then(j))
// Verify only the exact saved artifact. This never regenerates or reapplies fixes.
export const verifySavedCopy = (scanId, artifact) => (SIM
  ? Promise.reject(new Error('Saved-copy verification is unavailable in demo mode.'))
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/verify-saved-copy`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(artifact),
    }).then(j))
export const listReleaseHistory = (limit = 50) => (SIM
  ? sim({ releases: [] }, 50)
  : fetch(`${BASE}/releases?limit=${encodeURIComponent(limit)}`, { headers: headers() }).then(j))
export const previewReleaseDestination = (scanId, files, releaseFolderName = '', preserveHierarchy = true, destination = null, options = {}) => (SIM
  ? sim({ folder_name: releaseFolderName || '2026-09-06 12-00 UTC', folder_state: 'proposed', provider: 'drive',
      documents: files.map((file) => ({ file, provider_location: 'google:me', destination_path: `Remediated/${releaseFolderName || '2026-09-06 12-00 UTC'}/${file}`, action: 'create' })),
      blockers: [], can_release: true, collision_policy: 'Existing files are not overwritten.', original_files_unchanged: true }, 80)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/preview`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ files, preserve_hierarchy: preserveHierarchy, ...(releaseFolderName.trim() ? { release_folder_name: releaseFolderName.trim() } : {}),
        ...(destination ? { destination } : {}),
        ...(options.allowRemainingIssues ? { allow_remaining_issues: true, expected_artifacts: options.expectedArtifacts || {} } : {}) }),
    }).then(j))
export const getReleaseManifest = (scanId) => (SIM
  ? sim({ manifest: { schema_version: 1, scan_id: scanId, documents: [] },
      content_digest: { algorithm: 'SHA-256', value: 'simulation' },
      digest_note: 'Simulation manifest.' }, 50)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/manifest`, { headers: headers() }).then(j))
export const previewReleasePackage = (scanId, files, options = {}) => (SIM
  ? sim({ files: files.length, paths: files, estimated_bytes: null, estimate_complete: false,
      preserve_hierarchy: options.preserveHierarchy !== false, include_manifest: options.includeManifest !== false,
      blockers: [], can_download: true, recommended_format: files.length === 1 ? 'original' : 'zip' }, 80)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/package/preview`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ files, preserve_hierarchy: options.preserveHierarchy !== false,
        include_manifest: options.includeManifest !== false }),
    }).then(j))
export const downloadReleasePackage = (scanId, files, packageName = '', options = {}) => {
  if (SIM) return Promise.resolve()
  const downloadFormat = options.downloadFormat || 'zip'
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/package`, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ files, preserve_hierarchy: options.preserveHierarchy !== false,
      include_manifest: options.includeManifest !== false, download_format: downloadFormat,
      ...(packageName.trim() ? { package_name: packageName.trim() } : {}) }),
  })
    .then(async (r) => {
      if (!r.ok) {
        const detail = await r.json().then((body) => body?.detail).catch(() => null)
        throw new Error(detail || `package download ${r.status}`)
      }
      return { blob: await r.blob(), disposition: r.headers.get('Content-Disposition') || '' }
    })
    .then(({ blob, disposition }) => {
      const match = disposition.match(/filename="?([^";]+)"?/i)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const requestedName = packageName.trim().replace(/\.zip$/i, '')
      a.download = downloadFormat === 'original'
        ? (match?.[1] || files[0]?.split('/').pop() || 'corrected-file')
        : requestedName ? `${requestedName}.zip` : (match?.[1] || `acp-release-${scanId}.zip`)
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60000)
    })
}
export const prepareReleasePackage = (scanId, files, packageName = '', options = {}) => (SIM
  ? sim({ job_id: `package-${scanId}`, status: 'queued', files: files.length }, 100)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/package/prepare`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ files, preserve_hierarchy: options.preserveHierarchy !== false,
        include_manifest: options.includeManifest !== false, download_format: 'zip',
        ...(packageName.trim() ? { package_name: packageName.trim() } : {}) }),
    }).then(j))
export const downloadPreparedReleasePackage = (scanId, jobId, packageName = '') => {
  if (SIM) return Promise.resolve()
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/package/jobs/${encodeURIComponent(jobId)}/download`,
    { headers: headers() })
    .then(async (r) => {
      if (!r.ok) {
        const detail = await r.json().then((body) => body?.detail).catch(() => null)
        throw new Error(detail || `prepared package download ${r.status}`)
      }
      return r.blob()
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url
      const requestedName = packageName.trim().replace(/\.zip$/i, '')
      a.download = requestedName ? `${requestedName}.zip` : `acp-release-${scanId}.zip`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60000)
    })
}

// Queue state: depth by status + recent jobs (drives the in-app queue panel).
export const getJobs = (status = null) => (SIM
  ? sim({ workers: 4, stats: { done: 12, running: 1, queued: 3 }, dead_letters: { by_type: {}, top_errors: [] }, jobs: [
    { id: 'j1a2', type: 'remediate_file', status: 'running', scan_id: 'sim-all', payload: '{"file":"cardiology-policy.html"}', created_at: '2026-06-29T17:05:01Z', updated_at: '2026-06-29T17:05:04Z' },
    { id: 'j1a3', type: 'scan_file', status: 'done', scan_id: 'sim-all', payload: '{"file":"patient-intake-form.pdf"}', created_at: '2026-06-29T17:04:40Z', updated_at: '2026-06-29T17:04:43Z' },
    { id: 'j1a4', type: 'assess_trace', status: 'done', scan_id: 'sim-all', payload: '{}', created_at: '2026-06-29T17:04:10Z', updated_at: '2026-06-29T17:04:12Z' },
    { id: 'j1a5', type: 'scan_batch', status: 'queued', scan_id: 'sim-all', payload: '{"items":[]}', created_at: '2026-06-29T17:05:05Z', updated_at: '2026-06-29T17:05:05Z' },
  ] })
  : fetch(`${BASE}/jobs${status ? `?status=${status}` : ''}`, { headers: headers() }).then(j))

const _simActivity = () => ({
  generated_at: new Date().toISOString(),
  summary: { active_runs: 3, queued: 4, running: 7, completed_jobs: 427, worker_slots: 10, worker_tier_alive: true },
  runs: [
    { scan_id: 'sim-assess', owner: 'jeremy@example.com', source: 'drive', stage: 'assess', completed: 427, total: 986, running: 4, queued: 555, current_file: 'Clinical Trial Protocol.docx', updated_at: new Date().toISOString() },
    { scan_id: 'sim-discover', owner: 'devanathan@example.com', source: 'sharepoint', stage: 'discover', completed: 6910, total: 6916, running: 2, queued: 4, current_file: 'Policies', updated_at: new Date().toISOString() },
    { scan_id: 'sim-remediate', owner: 'ana@example.com', source: 'drive', stage: 'remediate', completed: 18, total: 32, running: 1, queued: 13, current_file: 'Patient Education.pdf', updated_at: new Date().toISOString() },
  ],
})

export const getAdminActivity = () => (SIM
  ? sim(_simActivity(), 80)
  : fetch(`${BASE}/admin/activity`, { headers: headers(), cache: 'no-store' }).then(j))

/**
 * The live map's stream. Two event types on one connection:
 *
 *   `activity` — the job topology, emitted whenever it changes (several times a minute under load)
 *   `azure`    — the Azure Monitor capacity block, emitted when the reading is RE-MEASURED
 *
 * They are separate because the Azure block is a comparatively large payload (fourteen metrics,
 * each with its own fifteen-minute series) that Azure itself only resamples once a minute.
 * Attaching it to every activity frame would multiply the stream's size for data that had not
 * changed. `onAzure` is optional — a caller that does not want it simply omits it.
 */
export function openAdminActivityStream({ onMessage, onAzure, onError } = {}) {
  if (SIM) {
    onMessage?.(_simActivity())
    return { close: () => {} }
  }
  const controller = new AbortController()
  ;(async () => {
    try {
      const res = await fetch(`${BASE}/admin/activity/stream`,
        { headers: headers(), signal: controller.signal, cache: 'no-store' })
      if (!res.ok || !res.body) { onError?.(new Error(`activity stream ${res.status}`)); return }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parsed = parseSSEFrames(buffer)
        buffer = parsed.rest
        for (const frame of parsed.frames) {
          const handler = frame.event === 'azure' ? onAzure
            : (frame.event === 'activity' || frame.event === 'message') ? onMessage : null
          if (!handler) continue
          try { handler(JSON.parse(frame.data)) } catch { /* skip malformed event */ }
        }
      }
      if (!controller.signal.aborted) onError?.(new Error('activity stream disconnected'))
    } catch (e) {
      if (e?.name !== 'AbortError') onError?.(e)
    }
  })()
  return { close: () => controller.abort() }
}
// "When will my work actually begin?" for one scan's Discover/Assess/Remediate job — backs the
// pickup-estimate line in each tab's Processing status panel. `kind` is 'discover'|'assess'|
// 'remediate'. Always resolves (never throws for a missing/foreign scan or one with no live job
// of this kind — the route degrades to {available:false}, same shape as getScanLive), so callers
// can treat the result as "is there something to show" without a .catch.
export const getQueueEstimate = (scanId, kind) => (SIM ? sim({ available: false })
  : fetch(`${BASE}/scans/${scanId}/queue-estimate?kind=${kind}`, { headers: headers() }).then(j))
// Delete unrecoverable dead-lettered jobs (signed-in admins only).
export const clearDeadJobs = () => (SIM
  ? sim({ purged: 0 })
  : fetch(`${BASE}/admin/jobs/clear-dead`, { method: 'POST', headers: headers() }).then(j))
// Live-scale the in-process worker pool (0–16). Persisted server-side.
export const setWorkers = (count) => (SIM
  ? sim({ workers: count })
  : fetch(`${BASE}/workers?count=${count}`, { method: 'PUT', headers: headers() }).then(j))
// Azure Container App replica control — returns {configured, min_replicas, max_replicas} or
// {configured: false} when AZURE_SUBSCRIPTION_ID is unset in the backend.
export const getWorkerReplicas = () => (SIM
  ? sim({ configured: false, min_replicas: null, max_replicas: null })
  : fetch(`${BASE}/control/workers/replicas`, { headers: headers() }).then(j))
export const setWorkerReplicas = (minReplicas) => (SIM
  ? sim({ configured: false, min_replicas: null, max_replicas: null })
  : fetch(`${BASE}/control/workers/replicas`, {
      method: 'PATCH',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ min_replicas: minReplicas }),
    }).then(j))
// The capacity SCHEDULE — what warm capacity ACP intends and when, plus what Azure actually
// runs and whether the two agree. Read-only in Phase 2: this endpoint writes nothing, and the
// schedule it returns is the PRD's proposal, which is why `applied` is false and `drift` is
// empty. A caller must not render this as a live schedule — see CapacitySchedule.jsx.
//
// SIM returns the same SHAPE with nothing configured, so the demo renders the tab's honest
// "not configured" state rather than a blank panel or a fabricated schedule.
export const getCapacitySchedule = () => (SIM
  ? sim({ enabled: false, timezone: 'America/Los_Angeles', days: [], start: null, end: null,
          business_hours: {}, off_hours: {}, maximums: {}, effective_mode: 'off_hours',
          next_transition_at: null, next_transition_to: null, version: 0, applied: false,
          validation: null, scalers: {}, observed: {}, drift: [], drift_evaluated: false,
          azure_configured: false })
  : bootFetch(`${BASE}/control/capacity-schedule`, { headers: headers() }).then(j))
// Phase 3's writes. All admin-only at the API (each handler runs _require_admin); the SPA hides
// the controls too, which is convenience, not the gate.
//
// SIM RESOLVES RATHER THAN REJECTS, returning the shape the caller expects with nothing changed.
// A demo that threw here would render an error the demo cannot explain; one that pretended to
// save would be worse — see SIM_NOT_WRITTEN in Settings.jsx for the same reasoning.
export const putCapacitySchedule = (body) => (SIM
  ? sim({ ...body, version: body.version, applied: false, azure_applied: false })
  : fetch(`${BASE}/control/capacity-schedule`, {
      method: 'PUT',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(CAPACITY_MUTATION_TIMEOUT_MS),
    }).then(j))
// The dry run. Returns findings and the projected fleet cost; saves nothing, on any path.
export const validateCapacitySchedule = (body) => (SIM
  ? sim({ blocked: false, findings: [], capacity: null, proposed: null })
  : fetch(`${BASE}/control/capacity-schedule/validate`, {
      method: 'POST',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(CAPACITY_MUTATION_TIMEOUT_MS),
    }).then(j))
// Publish one already-saved version to Azure. The version is part of the body so the server can
// reject a stale Review screen rather than applying a newer schedule the administrator did not
// approve. SIM preserves the response contract but explicitly says no external write occurred.
export const applyCapacitySchedule = (body) => (SIM
  ? sim({ simulated: true, correlation_id: null, schedule_version: body.version,
          application: { state: 'not_applied', applied_version: null } })
  : fetch(`${BASE}/control/capacity-schedule/apply`, {
      method: 'POST',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(CAPACITY_APPLY_TIMEOUT_MS),
    }).then(j))
export const createCapacityOverride = (body) => (SIM
  ? sim({ override: null, correlation_id: null })
  : fetch(`${BASE}/control/capacity-schedule/override`, {
      method: 'POST',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(CAPACITY_MUTATION_TIMEOUT_MS),
    }).then(j))
export const deleteCapacityOverride = () => (SIM
  ? sim({ cleared: false })
  : fetch(`${BASE}/control/capacity-schedule/override`, {
      method: 'DELETE', headers: headers(),
      signal: AbortSignal.timeout(CAPACITY_MUTATION_TIMEOUT_MS),
    }).then(j))
// What ACP would apply to Azure under the current schedule — rendered, never applied. Read-only,
// same grant as the schedule it derives from.
export const getCapacityPolicy = () => (SIM
  ? sim({ schedule_version: 0, applied: false, apps: [], az_commands: [],
          transitions_create_no_revision: true })
  : fetch(`${BASE}/control/capacity-schedule/policy`, { headers: headers() }).then(j))
// Azure-side capacity EVIDENCE — how many replicas are actually running right now and recent
// CPU/memory utilization — distinct from getWorkerReplicas' CONFIGURED min/max. Read-only, open
// to any signed-in user (same reasoning as getWorkerReplicas). Individual fields (current_replicas,
// cpu_percent, memory_percent) may be null even when configured:true — that's the backend's own
// per-field graceful degradation, not a fetch failure; never rendered as a fabricated 0/'—'.
export const getWorkerCapacity = () => (SIM
  ? sim({ configured: false, current_replicas: null, min_replicas: null, max_replicas: null,
          cpu_percent: null, memory_percent: null, cpu_cores_per_replica: null,
          memory_per_replica: null, ephemeral_storage_per_replica: null,
          workload_profile_name: null, active_revision_name: null,
          metrics_available: false, measured_at: null })
  : fetch(`${BASE}/control/workers/capacity`, { headers: headers() }).then(j))
// Cost transparency is deliberately separate from capacity telemetry: it distinguishes an
// operations-configured estimate from delayed Azure billing actuals and always returns provenance.
export const getLiveOpsCosts = () => (SIM
  ? sim({ configured: false, services: [], estimated_hourly_usd: null, estimated_daily_usd: null,
          billing: { configured: false, freshness_label: 'Azure billing feed not configured' } })
  : fetch(`${BASE}/control/costs`, { headers: headers() }).then(j))
// The FULL deploy/revision history for the acp-worker Container App — every revision, not just
// the active one getWorkerCapacity() extracts a handful of fields from. Read-only, open to any
// signed-in user, same reasoning as getWorkerCapacity/getWorkerReplicas. Revisions change only
// on deploy, not continuously, so unlike getWorkerCapacity there is no polling store for this —
// callers fetch on demand (mount + manual refresh).
export const getWorkerRevisions = () => (SIM
  ? sim({ configured: false, revisions: [] })
  : fetch(`${BASE}/control/workers/revisions`, { headers: headers() }).then(j))
// Reset demo data — clears scan results (Grafana) and/or Langfuse traces. Keeps settings.
export const resetDemoData = (scope = 'all') => (SIM
  ? sim({ scope, cleared_tables: [], langfuse_traces_deleted: 0 })
  : fetch(`${BASE}/admin/reset?scope=${scope}&confirm=true`, { method: 'POST', headers: headers() }).then(j))
// Self-service reset — clears only the SIGNED-IN USER'S OWN scans, so two people testing
// concurrently never clear each other's work. No scope choice (unlike resetDemoData): it is
// always "everything of mine". DB rows only — does not purge Blob/Drive copies.
export const resetMyData = () => (SIM
  ? sim({ owner: 'demo', cleared_tables: [] })
  : fetch(`${BASE}/me/reset-data?confirm=true`, { method: 'POST', headers: headers() }).then(j))
// Graph folder picker (OneDrive / SharePoint). Returns ids ALREADY in `<driveId>/<itemId>` form,
// which is what ?folders= expects — re-attaching the drive at the call site is how the download
// path once fetched the signed-in user's file of that id instead.
export const listSpFolders = (parent = 'root', driveId = '', site = '') => (SIM
  ? sim({ drive_id: 'sim-drive', parent, folders: [] })
  : fetch(`${BASE}/sharepoint/folders?parent=${encodeURIComponent(parent)}${driveId ? `&drive_id=${encodeURIComponent(driveId)}` : ''}${site ? `&site=${encodeURIComponent(site)}` : ''}`, { headers: headers() }).then(j))

// The folders each source is scoped to — a property of the CONNECTION, not of one scan, which is
// why it is stored server-side rather than re-picked on every scan.
export const getScanLocations = () => (SIM ? sim({ locations: {} })
  : fetch(`${BASE}/sources/locations`, { headers: headers() }).then(j))
export const setScanLocations = (source, folders, exclude = []) => (SIM ? sim({ ok: true, source, folders, exclude })
  : fetch(`${BASE}/sources/locations`, { method: 'PUT', headers: { ...headers(), 'Content-Type': 'application/json' },
                                         body: JSON.stringify({ source, folders, exclude }) }).then(j))

export const listFolders = (parent = 'root') => (SIM ? sim({ parent, name: 'My Drive', folders: [] }) : fetch(`${BASE}/folders?parent=${encodeURIComponent(parent)}`, { headers: headers() }).then(j))
export const getSchedule = () => (SIM
  ? sim({ enabled: false, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', local_time: '09:00', days: [0, 1, 2, 3, 4], next_at: null, last_at: null,
      source: null, scope: { include: [], exclude: [] }, notifications: { on_failure: true, on_delay: false, on_change: false, channel: 'in_app' },
      execution: { defer_when_busy: true, queue_threshold: 20, prewarm: true, prewarm_minutes: 10 }, history: [], reliability: null })
  : fetch(`${BASE}/schedule`, { headers: headers() }).then(j))
export const putSchedule = (body) => (SIM
  ? sim({ ...body, next_at: null, last_at: null })
  : fetch(`${BASE}/schedule`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) }).then(j))

export const markRemediated = (scanId, file) => (SIM
  ? sim({ remediated_at: new Date().toISOString() })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/remediate`, { method: 'POST', headers: headers() }).then(j))

export const getFileContent = (scanId, file) => (SIM
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/content`, { headers: headers() }).then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.arrayBuffer() }))

// The same bytes as a Blob that KEEPS the response's Content-Type — which is the whole point of
// api/media.py's media_mime(): a <video> handed `application/octet-stream` refuses to play, and a
// Blob rebuilt from the ArrayBuffer above would drop the type again. Goes through this module
// rather than a bare fetch in the component so it carries what every other call carries: BASE
// (a relative /scans/... URL would resolve against the WEB origin on a deployed frontend, not the
// API), the Authorization bearer, X-Auth-Provider (without which a Microsoft sign-in 401s) and
// the Drive token the backend needs to reach a Drive-backed original. Rejects on a non-2xx so the
// caller can say the media could not be played rather than showing a dead transport.
export const getFileContentBlob = (scanId, file) => (SIM
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/content`, { headers: headers() })
      .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.blob() }))

// Page-1 preview image (ADR 0015). Resolves to a PNG Blob, or null when there's no preview
// (unsupported type, source unreachable, render failed, or SIM mode). NEVER rejects — a
// missing thumbnail must degrade silently, never surface as an error to the caller.
export const getFileThumbnail = (scanId, file) => (SIM
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/thumbnail`, { headers: headers() })
      .then(r => (r.ok ? r.blob() : null))
      .catch(() => null))
// Rendered PNG of page N — the "locate in document" evidence primitive. The backend clamps
// `page` to the document's range; null (→ placeholder) for non-PDF, SIM, or any failure.
export const getFilePage = (scanId, file, page = 1) => (SIM || !scanId
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/page/${page}`, { headers: headers() })
      .then(r => (r.ok ? r.blob() : null))
      .catch(() => null))

// Normalized bounding box {page,x,y,w,h} for the shape a finding's `part#rId` locator names —
// the rectangle the review card overlays on the page render (ADR 0018 Slice 2). null (→ no box,
// plain preview) for SIM, a non-pptx format, an unattributable shape, or any failure: a box is
// only ever a real measured rectangle (ADR 0016), never guessed.
export const getFileGeometry = (scanId, file, locator) => (SIM || !scanId || !file || !locator
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/geometry?locator=${encodeURIComponent(locator)}`, { headers: headers() })
      .then(r => (r.ok ? r.json() : null))
      .then(d => (d && d.bbox) || null)
      .catch(() => null))

// Deep link back to the source document at the given slide/page. Returns {url, label} on success,
// {url: null} when no link is possible (local, missing SP token, Graph error). Non-blocking.
export const getSourceLink = (scanId, file, page = 1) => (SIM || !scanId || !file
  ? sim({ url: null })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/source_link?page=${page}`, { headers: headers() })
      .then(r => (r.ok ? r.json() : { url: null }))
      .catch(() => ({ url: null })))

// The docx heading outline {before,after} for a heading finding's Structure evidence — computed on
// demand from the document (mirrors getFileGeometry). null when unavailable (non-docx, <2 headings,
// already-clean outline, or any failure), so the card degrades to the honest generic note.
export const getHeadingOutline = (scanId, file) => (SIM || !scanId || !file
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/heading-outline`, { headers: headers() })
      .then(r => (r.ok ? r.json() : null))
      .then(d => (d && d.outline) || null)
      .catch(() => null))

// The docx table(s) with header-row associations for a 1.3.1 finding's Structure evidence — computed
// on demand (mirrors getHeadingOutline). null when unavailable (non-docx, no qualifying table, or any
// failure), so the card degrades to the honest generic note.
export const getTableStructure = (scanId, file) => (SIM || !scanId || !file
  ? sim(null)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/table-structure`, { headers: headers() })
      .then(r => (r.ok ? r.json() : null))
      .then(d => (d && d.tables) || null)
      .catch(() => null))

// ADR 0024 Tier B.1 — render-verified 1.4.3-hybrid contrast, ON DEMAND. With no locator the
// backend re-derives every text-over-picture/gradient shape and MEASURES each from real pixels,
// returning {measured:true, worst_ratio, any_fail_aa, shapes:[…], checked, total} or an honest
// {measured:false, reason:…}. Upgrades a 🟡 flag to a measured value; never a certified pass.
export const getFileContrast = (scanId, file) => (SIM || !scanId || !file
  ? sim({ measured: false, reason: 'unavailable' })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/verify-contrast`, { headers: headers() })
      .then(r => (r.ok ? r.json() : { measured: false, reason: 'error' }))
      .catch(() => ({ measured: false, reason: 'error' })))

// ADR 0024 Tier B.2 — render-verified 1.4.4 Resize Text, ON DEMAND. Renders each fixed-size
// (no-autofit) text box and MEASURES how much of it the text fills; enlarging to 200% needs ≥2×
// that height. Returns {measured:true, worst_fill, any_overflow_at_200, boxes:[…]} or an honest
// {measured:false, reason:…}. Upgrades a 🟡 flag to a measured overflow/headroom; never a pass.
export const getFileResize = (scanId, file) => (SIM || !scanId || !file
  ? sim({ measured: false, reason: 'unavailable' })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/verify-resize`, { headers: headers() })
      .then(r => (r.ok ? r.json() : { measured: false, reason: 'error' }))
      .catch(() => ({ measured: false, reason: 'error' })))

// ADR 0025 Tier B — render-verified 1.4.3 text-over-image contrast for PDF, ON DEMAND. The scan
// flags text sitting over a raster image (declared colour can't prove contrast there); this renders
// the page (pdfium, no LibreOffice) and MEASURES the text-vs-image contrast per run. Returns
// {measured:true, worst_ratio, any_fail_aa, runs:[…], checked, total} or {measured:false, reason:…}.
// Upgrades a 🟡 flag to a measured value; never a certified pass.
export const getFilePdfContrast = (scanId, file) => (SIM || !scanId || !file
  ? sim({ measured: false, reason: 'unavailable' })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/verify-pdf-contrast`, { headers: headers() })
      .then(r => (r.ok ? r.json() : { measured: false, reason: 'error' }))
      .catch(() => ({ measured: false, reason: 'error' })))

// ADR 0026 — the authoritative Accessibility Status for one file. Derived-at-read from the same
// certification facts the coverage matrix uses, so the hero card never disagrees with the detail.
// Returns the six-bucket model + coverage + state + one CTA, or {available:false}.
export const getFileStatus = (scanId, file) => (SIM
  ? sim({
      available: true, in_scope: 20, coverage: { evaluable: 18, total: 20 }, resolved: 16,
      automatically_verified: 16, human_verified: 0, needs_review: 2, needs_remediation: 0,
      not_automatically_assessable: 2, not_applicable: 0, unapplied_approved: 0,
      est_review_secs: 120, state: 'ready_after_review', cta: 'Review Findings',
    })
  : (!scanId || !file
      ? sim({ available: false })
      : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/status`, { headers: headers() })
          .then((r) => (r.ok ? r.json() : { available: false }))
          .catch(() => ({ available: false }))))

// ADR 0026 PR 3 — the scan-scope Accessibility Status: per-file models summed server-side, state
// re-derived over the totals. Powers the scan-level card + the Confidence Dashboard.
export const getScanStatus = (scanId) => (SIM
  ? sim({ available: true, scope: 'scan', documents: 12, in_scope: 240,
      coverage: { evaluable: 210, total: 240 }, resolved: 190,
      automatically_verified: 180, human_verified: 10, needs_review: 14, needs_remediation: 6,
      not_automatically_assessable: 30, not_applicable: 0, unapplied_approved: 0,
      est_review_secs: 630, state: 'needs_remediation', cta: 'Start Remediation' })
  : (!scanId ? sim({ available: false })
      : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/status`, { headers: headers() })
          .then((r) => (r.ok ? r.json() : { available: false }))
          .catch(() => ({ available: false }))))

// Examined-element denominators (ADR 0026 Epic 2): the engine-reported inventory counts from
// classify() at scan time — real walks, so "of N images examined" is a count, never an estimate.
export const getExamined = (scanId, file) => (SIM
  ? sim({ available: true, pages: 12, images: 18, has_text: true, is_scanned: false })
  : (!scanId || !file
      ? sim({ available: false })
      : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/examined`, { headers: headers() })
          .then((r) => (r.ok ? r.json() : { available: false }))
          .catch(() => ({ available: false }))))

// Confirm-the-pass (ADR 0026 / Epic 3): record a human's verification of a 🟡 review criterion.
// Writes the same immutable hitl.approved decision the review queue writes; the backend refuses
// anything whose outcome isn't REVIEW (a FAIL needs a fix, not a signature).
export const confirmCriterion = (scanId, file, sc, note) => (SIM || !scanId || !file
  ? sim({ ok: true, sc })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/confirm`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ sc, note }),
    }).then((r) => (r.ok ? r.json() : { ok: false, reason: 'error' }))
      .catch(() => ({ ok: false, reason: 'error' })))

// W4 — disposition lane for a dead-end criterion (UNCHECKED / GAP / AT). Records a human's
// documented resolution: either a manual attestation ("verified out-of-band — reason") or an
// out-of-scope decision ("not in this engagement's target — reason"), so the item reaches a
// recorded, resolved state instead of sitting terminal. Mirrors confirmCriterion's shape.
//
// BACKEND DEFERRED: the persistence endpoint (POST /scans/{sid}/files/{file}/dispose, and its
// read side below) is NOT yet implemented server-side. Against a real backend this call returns
// { ok: false } and the disposition is not persisted across reload; in SIM it resolves ok so the
// demo flow is complete. This is deliberately not faked — see the PR body for what the backend
// needs (an immutable decision written via store.log_decision, guarded to dispositionable
// outcomes, echoed back by a /dispositions read).
export const disposeCriterion = (scanId, file, sc, kind, reason) => (SIM || !scanId || !file
  ? sim({ ok: true, sc, kind, reason, actor: 'you', at: new Date().toISOString() })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/dispose`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ sc, kind, reason }),
    }).then((r) => (r.ok ? r.json() : { ok: false, reason: 'error' }))
      .catch(() => ({ ok: false, reason: 'error' })))

// Read the recorded dispositions for a file so they survive a drawer reopen. Backend deferred
// (see disposeCriterion): returns [] until the endpoint exists, so the drawer simply shows no
// recorded dispositions rather than erroring.
export const listDispositions = (scanId, file) => (SIM || !scanId || !file
  ? sim([])
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/files/${encodeURIComponent(file)}/dispositions`, { headers: headers() })
      .then((r) => (r.ok ? r.json() : []))
      .catch(() => []))

export const uploadToDrive = (scanId, file, blob, contentType) => {
  if (SIM) return sim({ url: 'https://drive.google.com/file/d/sim/view', file_id: 'sim' })
  const fd = new FormData()
  fd.append('scan_id', scanId)
  fd.append('file', file)
  fd.append('blob', new File([blob], file, { type: contentType }))
  return fetch(`${BASE}/drive/upload`, { method: 'POST', headers: headers(), body: fd }).then(j)
}

export const explainFinding = (scanId, file, ruleId) => (SIM
  ? sim({ why: 'Screen readers cannot announce this element — blind users get no information about it.', fix: 'Add a descriptive alt attribute: <img src="logo.png" alt="Company logo">', model: 'llama3.2 (simulated)' })
  : fetch(`${BASE}/ai/explain?scan_id=${encodeURIComponent(scanId)}&file=${encodeURIComponent(file)}&rule_id=${encodeURIComponent(ruleId)}`, { headers: headers() }).then(j))

// SIM is the Netlify/offline build. Its AI, when keys are configured, is the serverless
// functions in frontend/netlify/functions — which POST to api.anthropic.com and
// api.openai.com. It is NOT a local backend, and must never claim to be: PrivateAiBadge
// renders its "no OpenAI/Anthropic, no external tokens" promise off `backend === 'ollama'`,
// so a fabricated 'ollama' here printed a false privacy guarantee on the one deployment
// that actually sends document content to a third party.
// The stub must carry EVERY field the panel reads, including the negative ones. It omitted
// vision_available and vision_unavailable_reason, and the Settings warning is gated on both —
// so on a Netlify preview the "genuine alt text is off" line could not render, and the panel
// looked healthy no matter what. On 2026-07-30 that read as a fix having worked: the button was
// clicked here, the warning vanished, and production still had the stale override pinned.
// A stub that answers fewer questions than the real endpoint reports good news it cannot know.
export const getAiStatus = () => (SIM
  ? sim({ available: false, base_url: null, model: null, vision_model: null, ai_enabled: true,
          backend: 'netlify-functions', model_available: false, vision_available: false,
          vision_unavailable_reason: 'this demo has no local Ollama — genuine image alt text needs one, '
                                    + 'so 1.1.1 findings get a fill-in template for a human to complete',
          config_source: { base_url: 'env', vision_model: 'env', model: 'env' } })
  : fetch(`${BASE}/ai/status`, { headers: headers() }).then(j))

// ── Disposition policies (ADR 0003 Phase 3) — admin-only lifecycle ────────────
// Policies are created DISABLED and approval-gated by default; delete is always
// Drive trash, never permanent. SIM keeps a tiny in-memory store so the whole
// create → enable → execute → approve loop is drivable in the demo.
const _simDisp = { policies: [], approvals: [], seq: 0 }
export const listDispositionPolicies = () => (SIM
  ? sim([..._simDisp.policies])
  : fetch(`${BASE}/disposition/policies`, { headers: headers() }).then(j))
export const createDispositionPolicy = (name, match, action, actionConfig = {}, requiresApproval = true) => (SIM
  ? sim((() => {
      const p = { policy_id: `sim-p${++_simDisp.seq}`, name, match: JSON.stringify(match), action,
                  action_config: JSON.stringify(actionConfig), requires_approval: requiresApproval ? 1 : 0, enabled: 0 }
      _simDisp.policies.push(p); return p
    })())
  : fetch(`${BASE}/disposition/policies`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ name, match, action, action_config: actionConfig, requires_approval: requiresApproval }),
    }).then(j))
// Edit a saved rule (lifecycle rules #2 — the create/duplicate/delete trio had no "edit in
// place"). `updates` is a partial patch — {name?, match?, action?, action_config?,
// requires_approval?} — mirroring PUT /disposition/policies/{id}'s own PolicyUpdate model: every
// field optional, an omitted one left as it is. The route 409s if the rule already has recorded
// history AND the caller is changing its match/action/action_config (never its name/approval) —
// that refusal reaches the caller as a rejected promise, same as every other write here.
export const updateDispositionPolicy = (policyId, updates) => (SIM
  ? sim((() => {
      const p = _simDisp.policies.find((x) => x.policy_id === policyId)
      if (!p) return null
      if (updates.name !== undefined) p.name = updates.name
      if (updates.match !== undefined) p.match = JSON.stringify(updates.match)
      if (updates.action !== undefined) p.action = updates.action
      if (updates.action_config !== undefined) p.action_config = JSON.stringify(updates.action_config)
      if (updates.requires_approval !== undefined) p.requires_approval = updates.requires_approval ? 1 : 0
      return p
    })())
  : fetch(`${BASE}/disposition/policies/${encodeURIComponent(policyId)}`, {
      method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(updates),
    }).then(j))
// `previewedMatchCount` is required by the server before a rule that CHANGES FILES may be turned
// on (PRD §7.5): the activator states what the preview showed them, and the server re-derives the
// count at activation and refuses if it has moved. Omitted for tag/leave and for disabling, which
// are not gated.
export const setDispositionPolicyEnabled = (policyId, enabled, previewedMatchCount) => (SIM
  ? sim((() => {
      const p = _simDisp.policies.find((x) => x.policy_id === policyId)
      if (p) p.enabled = enabled ? 1 : 0
      return p
    })())
  : fetch(`${BASE}/disposition/policies/${encodeURIComponent(policyId)}/enabled?enabled=${enabled}`
          + (previewedMatchCount == null ? '' : `&previewed_match_count=${previewedMatchCount}`),
          { method: 'PUT', headers: headers() }).then(j))
export const deleteDispositionPolicy = (policyId) => (SIM
  ? sim((() => {
      const i = _simDisp.policies.findIndex((x) => x.policy_id === policyId)
      if (i >= 0) _simDisp.policies.splice(i, 1)
      return { deleted: policyId }
    })())
  : fetch(`${BASE}/disposition/policies/${encodeURIComponent(policyId)}`, { method: 'DELETE', headers: headers() }).then(j))
// Reorder = the whole new order, not a single move — SIM just re-sorts its array to match, since
// _simDisp.policies' own array order already IS the "priority" order the real backend tracks
// with an explicit column.
export const reorderDispositionPolicies = (policyIds) => (SIM
  ? sim((() => {
      const byId = new Map(_simDisp.policies.map((p) => [p.policy_id, p]))
      _simDisp.policies = policyIds.map((id) => byId.get(id)).filter(Boolean)
      return [..._simDisp.policies]
    })())
  : fetch(`${BASE}/disposition/policies/reorder`, {
      method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ policy_ids: policyIds }),
    }).then(j))
export const listDispositionConflicts = () => (SIM
  ? sim({ conflicts: [] })
  : fetch(`${BASE}/disposition/policies/conflicts`, { headers: headers() }).then(j))

// ── Safe lifecycle archive auto-fire (R9) ────────────────────────────────────────────────────
//
// SIM RETURNS THE DISABLED, UNCONFIGURED SHAPE AND NEVER AN ELIGIBLE ITEM, deliberately. The demo
// runs against no tenant, so there is no source system in which a move could be proven to have
// happened — and a demo that showed "Automatically archived" would be showing the one state this
// whole feature exists to make honest. Recommendation-only is both the truthful demo answer and
// the shipped default, so SIM and a fresh real tenant agree.
const ARCHIVE_SIM_POLICY = {
  configured: false,
  policy: { enabled: false, kill_switch: false, dry_run: true, source_connections: [], rule_ids: [],
            required_evidence: ['metadata_link'], confirmed_families: [], min_replacement_age_days: 30,
            archive_root: '', preserve_hierarchy: true, max_actions_per_run: 25, max_actions_per_day: 100 },
  snapshot_id: null, updated_at: null, updated_by: null,
  evidence_types: [
    { type: 'metadata_link', label: 'Replacement metadata names this document (retentionOf / supersedes)' },
    { type: 'rule_family', label: 'A lifecycle rule identifies a document family and a strictly newer version' },
    { type: 'sp_version', label: 'SharePoint version metadata names a newer approved replacement' },
    { type: 'admin_mapping', label: 'An administrator confirmed this document-family mapping' },
  ],
  auto_sources: ['sharepoint', 'onedrive'],
  problem: '',
  notice: 'Age, filename similarity and inactivity never authorize an automatic move. A document is '
        + 'archived automatically only when durable evidence shows a newer item supersedes it and '
        + 'this policy permits it.',
}
export const getArchivePolicy = () => (SIM
  ? sim(JSON.parse(JSON.stringify(ARCHIVE_SIM_POLICY)))
  : fetch(`${BASE}/lifecycle/archive/policy`, { headers: headers() }).then(j))
export const updateArchivePolicy = (patch) => (SIM
  ? sim(JSON.parse(JSON.stringify(ARCHIVE_SIM_POLICY)))
  : fetch(`${BASE}/lifecycle/archive/policy`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }),
                                                body: JSON.stringify(patch) }).then(j))
export const setArchiveKillSwitch = (on) => (SIM
  ? sim(JSON.parse(JSON.stringify(ARCHIVE_SIM_POLICY)))
  : fetch(`${BASE}/lifecycle/archive/kill-switch`, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
                                                     body: JSON.stringify({ on: !!on }) }).then(j))
export const getArchiveCandidates = (scanId) => (SIM
  ? sim({ scan_id: scanId, snapshot_id: null, dry_run: true, counts: {}, progress: '', items: [] })
  : fetch(`${BASE}/lifecycle/archive/candidates?scan_id=${encodeURIComponent(scanId)}`,
          { headers: headers() }).then(j))
export const runArchiveAutofire = (scanId) => (SIM
  ? Promise.reject(new Error('Automatic archival is not available in the demo — it would move real files.'))
  : fetch(`${BASE}/lifecycle/archive/run?scan_id=${encodeURIComponent(scanId)}`,
          { method: 'POST', headers: headers() }).then(j))
export const listArchiveExecutions = (scanId = null) => (SIM
  ? sim({ executions: [] })
  : fetch(`${BASE}/lifecycle/archive/executions${scanId ? `?scan_id=${encodeURIComponent(scanId)}` : ''}`,
          { headers: headers() }).then(j))
export const previewDispositionPolicy = (policyId) => (SIM
  ? sim({ policy_id: policyId, would_match: 3, documents: [
      { doc_id: 'drive:sim1', path: 'HR Handbook 2019.pdf', department: 'HR', age_days: 1460 },
      { doc_id: 'drive:sim2', path: 'Old Expense Policy.docx', department: 'Finance', age_days: 1220 },
      { doc_id: 'drive:sim3', path: 'Legacy Onboarding.pptx', department: 'HR', age_days: 1105 }] })
  : fetch(`${BASE}/disposition/policies/${encodeURIComponent(policyId)}/preview`, { method: 'POST', headers: headers() }).then(j))
// Lifecycle rules #4 — preview a rule that does not exist yet: how many documents would
// {match, action} select, right now? Same shape as previewDispositionPolicy but policy_id: null
// (this draft has no id, not because one was lost) — POST /disposition/preview, api/routes/
// disposition.py's preview_draft, which runs the SAME disposition.matches over the SAME documents
// table, whole-estate, as the saved-policy preview above (the two share one evaluator on purpose,
// so the count shown while typing can't drift from what the saved rule will produce).
export const previewDispositionDraft = (match, action, actionConfig = {}) => (SIM
  ? sim({ policy_id: null, would_match: 3, documents: [
      { doc_id: 'drive:sim1', path: 'HR Handbook 2019.pdf', department: 'HR', age_days: 1460 },
      { doc_id: 'drive:sim2', path: 'Old Expense Policy.docx', department: 'Finance', age_days: 1220 },
      { doc_id: 'drive:sim3', path: 'Legacy Onboarding.pptx', department: 'HR', age_days: 1105 }] }, 180)
  : fetch(`${BASE}/disposition/preview`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ match, action, action_config: actionConfig }),
    }).then(j))
export const executeDispositionPolicy = (policyId) => {
  if (SIM) {
    const p = _simDisp.policies.find((x) => x.policy_id === policyId)
    if (!p || !p.enabled) return Promise.reject(new Error('policy is disabled — enable it before executing'))
    const already = _simDisp.approvals.filter((a) => a.policy_id === policyId && a.result !== 'rejected').length
    let queued = 0
    if (p.requires_approval && already === 0) {
      ;['HR Handbook 2019.pdf', 'Old Expense Policy.docx', 'Legacy Onboarding.pptx'].forEach((f, i) => {
        _simDisp.approvals.push({ id: `sim-a${++_simDisp.seq}`, doc_id: `drive:sim${i + 1}`, policy_id: policyId,
                                  action: p.action, result: 'pending_approval',
                                  detail: `queued by policy '${p.name}' — awaiting approval (${f})` })
        queued += 1
      })
    }
    return sim({ policy_id: policyId, matched: 3, pending_approval: queued,
                 applied: 0, failed: 0, skipped: 3 - queued })
  }
  return fetch(`${BASE}/disposition/policies/${encodeURIComponent(policyId)}/execute`, { method: 'POST', headers: headers() }).then(j)
}
export const listDispositionApprovals = () => (SIM
  ? sim(_simDisp.approvals.filter((a) => a.result === 'pending_approval'))
  : fetch(`${BASE}/disposition/approvals`, { headers: headers() }).then(j))
// `execute` decides whether the FILE is touched. The source panel passes false: it records the
// decision and leaves the document alone, because execute_action is Drive-only (a SharePoint row
// cannot execute at all) and ACP holds read-only scopes — an Approve button that claimed to move
// a file would describe a capability the deployment does not have. Default stays true so existing
// callers keep the behaviour they were written against.
export const approveDisposition = (auditId, { execute = true } = {}) => (SIM
  ? sim((() => {
      const a = _simDisp.approvals.find((x) => x.id === auditId)
      if (a) {
        a.result = execute ? 'applied' : 'approved'
        a.detail = execute ? 'applied (simulated)' : 'approved by admin — decision recorded, file not touched'
      }
      return a
    })())
  : fetch(`${BASE}/disposition/approvals/${encodeURIComponent(auditId)}/approve?execute=${execute}`,
          { method: 'POST', headers: headers() }).then(j))
export const rejectDisposition = (auditId) => (SIM
  ? sim((() => { const a = _simDisp.approvals.find((x) => x.id === auditId); if (a) { a.result = 'rejected'; a.detail = 'declined by admin' } return a })())
  : fetch(`${BASE}/disposition/approvals/${encodeURIComponent(auditId)}/reject`, { method: 'POST', headers: headers() }).then(j))

export const listDispositionAudit = (limit = 100) => (SIM
  ? sim([..._simDisp.approvals].slice().reverse())
  : fetch(`${BASE}/disposition/audit?limit=${limit}`, { headers: headers() }).then(j))

// The control plane's estate view. `dept`/`owner` narrow WITHIN the caller's tenant; there is
// deliberately no tenant parameter, because a caller-settable tenant is not a filter — the
// server takes it from the request (see api/routes/control.py).
//
// The SIM shape is DERIVED from simGetScan's own files rather than from a second synthetic
// estate. A prettier fake here would be the one place a viewer could catch the demo
// contradicting itself: Discover and this tab would disagree about how many documents exist,
// in a product whose entire pitch is that its numbers reconcile.
export const getEstate = (dept = '', owner = '') => (SIM
  ? sim((() => {
      const files = (simGetScan('scan-cur').files || [])
      const D = (f) => f.department || '(unassigned)'
      const O = (f) => f.owner || '(unassigned)'
      const kept = files.filter((f) => (!dept || D(f) === dept) && (!owner || O(f) === owner))

      const byDept = {}
      for (const f of kept) {
        const k = D(f)
        byDept[k] = byDept[k] || { department: k, documents: 0, _owners: new Set(), avg_triage: null }
        byDept[k].documents += 1
        byDept[k]._owners.add(O(f))
      }
      const departments = Object.values(byDept)
        .map(({ _owners, ...r }) => ({ ...r, owners: _owners.size }))
        .sort((a, b) => b.documents - a.documents)

      // Owners are NOT narrowed by the filters: the panel exists to let a reader pick one, and
      // a list that shrinks to match the current selection cannot be used to change it.
      const byOwner = {}
      for (const f of files) {
        const k = O(f)
        byOwner[k] = byOwner[k] || { owner: k, documents: 0, _depts: new Set() }
        byOwner[k].documents += 1
        byOwner[k]._depts.add(D(f))
      }
      const owners = Object.values(byOwner)
        .map(({ _depts, ...o }) => ({ ...o, departments: _depts.size }))
        .sort((a, b) => b.documents - a.documents)

      return {
        tenant: 'demo',
        departments,
        owners,
        documents: departments.reduce((n, r) => n + r.documents, 0),
        filters: { department: dept || null, owner: owner || null },
        filtered: Boolean(dept || owner),
      }
    })())
  : fetch(`${BASE}/control/estate?dept=${encodeURIComponent(dept)}&owner=${encodeURIComponent(owner)}`,
          { headers: headers() }).then(j))

// ── SharePoint (#156, #157) ───────────────────────────────────────────────────
// Both routes shipped without a client, so site enumeration and server-side write-back existed
// and could not be invoked by a person. These are those clients.
//
// The token rides on `headers()` as X-SP-Token, which the routes read — the SAME header the
// scan path uses, deliberately: a site the browser can see with one set of scopes is not proof
// the scan token can read it, and discovering through the path the scan takes is what makes the
// picker's list honest (api/routes/sharepoint.py says so at length).
//
// NO SIM BRANCH, and that is the honest shape rather than an omission. There is no synthetic
// SharePoint tenant to enumerate, and a fabricated site list would be the one thing a demo
// viewer could act on and find missing — "Policies (9,870 files)" that does not exist is worse
// than an empty state that explains itself. In SIM these reject with a message saying so.
const SP_SIM = () => Promise.reject(new Error(
  'SIM build — no SharePoint tenant to enumerate. Run against a real backend with a Microsoft '
  + 'sign-in to list sites.'))

export const listSharePointSites = (q = '') => (SIM
  ? SP_SIM()
  : fetch(`${BASE}/sharepoint/sites?q=${encodeURIComponent(q)}`, { headers: headers() }).then(j))

// The site id is compound — `contoso.sharepoint.com,<guid>,<guid>` — which is why the route
// declares `{site_id:path}`. encodeURIComponent would percent-encode the commas and the server
// would then look up a site whose id contains "%2C"; the path converter expects them raw.
export const listSharePointDrives = (siteId) => (SIM
  ? SP_SIM()
  : fetch(`${BASE}/sharepoint/sites/${siteId}/drives`, { headers: headers() }).then(j))

// `driveId` is REQUIRED and comes from the SCAN, not from the browser's idea of where the file
// lives: a Graph item id is unique only within its drive, so writing to the wrong one does not
// error — it succeeds, into somebody else's library.
// `itemId` selects the mode the server takes. Absent, the file lands in the mirror folder.
// Present, the remediated bytes REPLACE that item in place — the original archived first, and a
// failed archive aborting the write. Both decisions are the server's; this only names the target.
export const uploadToSharePoint = ({ scanId, file, driveId, itemId, blob, score }) => {
  // Required for a MIRROR write only. Replacing names an existing item, and an item listed from
  // OneDrive carries no driveId — the server resolves that to /me/drive, the same convention the
  // download path uses for the very same items.
  if (!driveId && !itemId) {
    return Promise.reject(new Error(
      'No drive id for this file. A SharePoint item id is only unique within its drive, so the '
      + 'write target has to be named explicitly — re-scan the site so the item carries it.'))
  }
  if (SIM) return SP_SIM()
  const form = new FormData()
  form.append('scan_id', scanId || '')
  form.append('file', file || '')
  if (driveId) form.append('drive_id', driveId)
  if (itemId) form.append('item_id', itemId)
  if (score != null) form.append('score', String(score))
  form.append('blob', blob, file || 'remediated')
  // No Content-Type header: the browser sets multipart/form-data WITH the boundary, and setting
  // it by hand omits the boundary and the server cannot parse the body.
  return fetch(`${BASE}/sharepoint/upload`, { method: 'POST', headers: headers(), body: form }).then(j)
}

export const queueHitlVerify = (scanId, file) => (SIM
  ? sim({ queued: 1 })
  : fetch(`${BASE}/hitl/queue/${encodeURIComponent(scanId)}/verify?file=${encodeURIComponent(file)}`, { method: 'POST', headers: headers() }).then(j))

// ── Assess code-set + eligibility (Phase C2, Discover/Assess PRD) ───────────────────
// The two READ-ONLY previews the Assess scope picker calls before a run starts:
//   fetchCodeset()          → the Core-17 catalog [{code, name, formats:[...]}] — the selectable
//                             set the WCAG picker renders as "1.4.3 — Contrast (Minimum)".
//   fetchEligibility(codes) → {discovered, eligible, by_format, formats, codes:[…]} for the current
//                             selection, so the picker can show "N files eligible" before running.
// Neither starts a scan or mutates state, so both are safe to call live as the selection changes.
//
// The endpoint wraps the catalog as {codes:[…]}; fetchCodeset unwraps it so callers always get the
// array. SIM has no backend, so it projects the SAME generated Core-17 tables the rest of the UI
// reads (SCOPE_UNIVERSE ∩ TRACKED_17) over a small fixed demo inventory — the picker and its count
// then behave offline exactly as they do against the API, instead of a second hand-typed list.
const _SIM_CORE17 = SCOPE_UNIVERSE
  .filter((r) => TRACKED_17.has(r.sc))
  .map((r) => ({ code: r.sc, name: r.name, formats: [...r.formats].sort() }))
// A representative discovered estate for the demo count — docx-heavy, the engine's strongest lane.
const _SIM_INVENTORY = { discovered: 175, by_format: { docx: 92, pdf: 48, pptx: 21, xlsx: 14 } }

export const fetchCodeset = () => (SIM
  ? sim({ codes: _SIM_CORE17 })
  : fetch(`${BASE}/assess/codeset`, { headers: headers() }).then(j)
).then((r) => (Array.isArray(r) ? r : (r?.codes || [])))

const _simEligibility = (codes) => {
  const sel = (codes && codes.length) ? codes : _SIM_CORE17.map((c) => c.code)
  const byCode = Object.fromEntries(_SIM_CORE17.map((c) => [c.code, c.formats]))
  const fmts = new Set()
  for (const code of sel) for (const f of (byCode[code] || [])) fmts.add(f)
  const by_format = {}
  for (const f of [...fmts].sort()) { const n = _SIM_INVENTORY.by_format[f] || 0; if (n > 0) by_format[f] = n }
  const eligible = Object.values(by_format).reduce((a, b) => a + b, 0)
  const codes_out = sel.map((code) => {
    const cf = byCode[code] || []
    return { code, name: _SIM_CORE17.find((c) => c.code === code)?.name || code, formats: cf,
             eligible: cf.reduce((a, f) => a + (_SIM_INVENTORY.by_format[f] || 0), 0) }
  })
  return { discovered: _SIM_INVENTORY.discovered, eligible, by_format, formats: [...fmts].sort(), codes: codes_out }
}

export const fetchEligibility = (codes = null) => {
  const list = (Array.isArray(codes) ? codes : (codes ? String(codes).split(',') : []))
    .map((c) => String(c).trim()).filter(Boolean)
  if (SIM) return sim(_simEligibility(list))
  const q = list.join(',')
  return fetch(`${BASE}/assess/eligibility${q ? `?codes=${encodeURIComponent(q)}` : ''}`, { headers: headers() }).then(j)
}

// ── Scope rules (Phase C4d, Discover/Assess PRD §4.4 / AC-09) ────────────────────────
// Per-file WCAG scope rules: admins target files by folder / owner / department / SharePoint
// Content Type and assign a
// Core-17 subset, with union / override precedence (see api/scope_resolver.py). The editor UI
// (ScopeRules.jsx) reads the selector+catalog from /scope/selectors, lists/creates/toggles/deletes
// rules over /scope/rules, and shows the scope-aware eligible-file count from
// /assess/eligibility/scoped. Writes are owner-gated server-side; a validation failure is a 400
// whose `detail` (the resolver's own message) `j` surfaces as the thrown Error — the form renders
// it inline. SIM has no backend, so it keeps an in-memory rule store that validates the same way
// the server does, so the demo behaves offline exactly as it does against the API.

// The Core-17 catalog the SIM selector endpoint serves — same generated tables the rest of the UI
// reads, so the picker shows "1.4.3 — Contrast (Minimum)" identically online and offline.
const _SIM_SCOPE_SELECTORS = ['folder', 'owner', 'department', 'content_type']
let _simScopeRules = []          // in-memory rule store for the demo build
let _simScopeSeq = 0
const _simAllowedCodes = () => new Set(_SIM_CORE17.map((c) => c.code))
// Mirror api/scope_resolver.validate_scope_rule so a SIM create surfaces the same inline 400s.
const _simValidateRule = (r) => {
  if (!_SIM_SCOPE_SELECTORS.includes(r.selector))
    throw new Error(`selector must be one of ${JSON.stringify(_SIM_SCOPE_SELECTORS)}, got ${JSON.stringify(r.selector)}`)
  if (!String(r.value || '').trim()) throw new Error('scope rule value must be non-empty')
  const codes = Array.isArray(r.codes) ? r.codes : []
  if (!codes.length) throw new Error('scope rule must select at least one WCAG code')
  const allowed = _simAllowedCodes()
  const bad = codes.filter((c) => !allowed.has(c))
  if (bad.length) throw new Error(`codes not in the allowed Core-17 set: ${JSON.stringify(bad.sort())}`)
}

// The building blocks the rule editor renders: the allowed selectors + the Core-17 catalog to
// pick codes from. Read-only, no gate. Shape: {selectors:[...], codes:[{code, name, formats}]}.
export const fetchScopeSelectors = () => (SIM
  ? sim({ selectors: [..._SIM_SCOPE_SELECTORS], codes: _SIM_CORE17 })
  : fetch(`${BASE}/scope/selectors`, { headers: headers() }).then(j))

// All scope rules, priority-desc / name order (the store's ordering, passed through). Read-only.
export const fetchScopeRules = () => (SIM
  ? sim({ rules: [..._simScopeRules].sort((a, b) => (b.priority - a.priority) || String(a.name).localeCompare(b.name)) })
  : fetch(`${BASE}/scope/rules`, { headers: headers() }).then(j)
).then((r) => (Array.isArray(r) ? r : (r?.rules || [])))

// Create a rule (owner-only). Validates against Core-17 before persisting; a malformed rule is a
// 400 whose message `j` throws — the form catches and renders it inline. Resolves to the new rule.
export const createScopeRule = (body) => {
  if (SIM) {
    return new Promise((res, rej) => setTimeout(() => {
      try { _simValidateRule(body) } catch (e) { rej(e); return }
      const rule = {
        rule_id: `sim-rule-${++_simScopeSeq}`, name: body.name || '', selector: body.selector,
        value: body.value, codes: [...(body.codes || [])], priority: Number(body.priority) || 0,
        is_override: !!body.is_override, enabled: body.enabled !== false,
        created_at: new Date().toISOString(), created_by: 'demo',
      }
      _simScopeRules.push(rule); res(rule)
    }, 180))
  }
  return fetch(`${BASE}/scope/rules`, {
    method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      name: body.name || '', selector: body.selector, value: body.value,
      codes: [...(body.codes || [])], priority: Number(body.priority) || 0,
      is_override: !!body.is_override, enabled: body.enabled !== false,
    }),
  }).then(j)
}

// Enable / disable one rule (owner-only). 404 if it does not exist. Resolves to the updated rule.
export const setScopeRuleEnabled = (id, enabled) => {
  if (SIM) {
    return sim((() => { const r = _simScopeRules.find((x) => x.rule_id === id); if (r) r.enabled = !!enabled; return r || { rule_id: id, enabled: !!enabled } })())
  }
  return fetch(`${BASE}/scope/rules/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ enabled: !!enabled }),
  }).then(j)
}

// Delete one rule (owner-only). 404 if it does not exist.
export const deleteScopeRule = (id) => {
  if (SIM) { _simScopeRules = _simScopeRules.filter((x) => x.rule_id !== id); return sim({ deleted: id }) }
  return fetch(`${BASE}/scope/rules/${encodeURIComponent(id)}`, { method: 'DELETE', headers: headers() }).then(j)
}

// Scope-aware eligibility: how many discovered files are eligible once enabled scope rules apply,
// per file. Shape: {discovered, eligible, by_format, rules_applied}. Read-only; zeros when there is
// no discovery run yet. SIM projects the demo inventory and counts how many rules would reach a file.
export const fetchScopedEligibility = (codes = null) => {
  const list = (Array.isArray(codes) ? codes : (codes ? String(codes).split(',') : []))
    .map((c) => String(c).trim()).filter(Boolean)
  if (SIM) {
    const base = _simEligibility(list)
    const enabled = _simScopeRules.filter((r) => r.enabled)
    return sim({
      discovered: base.discovered, eligible: base.eligible, by_format: base.by_format,
      rules_applied: enabled.length,
    })
  }
  const q = list.join(',')
  return fetch(`${BASE}/assess/eligibility/scoped${q ? `?codes=${encodeURIComponent(q)}` : ''}`, { headers: headers() }).then(j)
}

// Only displayed result folders are resolved, after results render; this never delays a scan.
export const getDriveFolderName = (id) => (SIM ? sim({ id, name: 'Demo folder' })
  : fetch(`${BASE}/drive/folder-name?id=${encodeURIComponent(id)}`, { headers: headers(), signal: AbortSignal.timeout(8000) }).then(j))


export const planReleaseContinuation = (scanId, files, destination, releaseFolderName = '', options = {}) => (SIM
  ? sim({ id: 'simulation', intent: { files: {} }, status: 'draft' }, 50)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/continuation/plan`, {
      method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), signal: options.signal,
      body: JSON.stringify({ files, destination, release_folder_name: releaseFolderName }),
    }).then(j))
export const getReleaseContinuation = scanId => (SIM ? sim(null, 50)
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/continuation`, { headers: headers() }).then(j))
export const authorizeReleaseContinuation = (scanId, intentId) => fetch(
  `${BASE}/scans/${encodeURIComponent(scanId)}/release/continuation/${encodeURIComponent(intentId)}/authorize`,
  { method: 'POST', headers: headers() }).then(j)
export const resumeReleaseContinuation = (scanId, intentId) => fetch(
  `${BASE}/scans/${encodeURIComponent(scanId)}/release/continuation/${encodeURIComponent(intentId)}/resume`,
  { method: 'POST', headers: headers() }).then(j)


export const getRecentRemediationActivity = (scanId, { afterSeq = null, limit = null } = {}) => {
  if (SIM || !scanId) return sim({ available: false, events: [] })
  const query = new URLSearchParams()
  if (afterSeq != null) query.set('after_seq', String(afterSeq))
  if (limit != null) query.set('limit', String(limit))
  const suffix = query.size ? `?${query}` : ''
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/activity${suffix}`, { headers: headers() }).then(j)
}

// Separate per-run release consent. AI approval settings never enable publication.
export const getAutomaticRelease = (scanId, files = [], options = {}) => {
  if (SIM) return sim({ available: false, reason: 'Automatic release requires a connected remediation run.', authorization: null }, 50)
  const query = new URLSearchParams()
  files.forEach(file => query.append('files', file))
  if (options.destination) query.set('destination', JSON.stringify(options.destination))
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/automatic?${query}`, {
    headers: headers(), signal: options.signal,
  }).then(j)
}
// Metadata-only recovery for older default-drive scans. Never replay this POST:
// the server re-derives its frozen Discover scope and validates live source identity.
export const repairAutomaticReleaseSourceIdentity = async (scanId, options = {}) => {
  if (SIM) throw new Error('Source identity repair requires a connected Microsoft source.')
  const identity = authEpoch()
  options.signal?.throwIfAborted()
  let token
  try { token = await refreshSPToken({ interactive: false }) }
  catch (cause) {
    options.signal?.throwIfAborted()
    const failure = new Error(cause?.message || 'Microsoft connection required', { cause })
    failure.code = 'microsoft_connection_required'
    failure.sourceIdentityRequestSent = false
    throw failure
  }
  options.signal?.throwIfAborted()
  if (authEpoch() !== identity) throw new Error('The signed-in account changed.')
  return fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/automatic/repair-source-identity`, {
    method: 'POST', headers: { ...headers(), 'X-SP-Token': token }, signal: options.signal,
  }).then(j)
}
export const enableAutomaticRelease = (scanId, intent) => fetch(
  `${BASE}/scans/${encodeURIComponent(scanId)}/release/automatic`, {
    method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(intent),
  }).then(j)
export const stopAutomaticRelease = (scanId, authorizationId) => fetch(
  `${BASE}/scans/${encodeURIComponent(scanId)}/release/automatic/${encodeURIComponent(authorizationId)}/stop`, {
    method: 'POST', headers: headers(),
  }).then(j)

export const getReleaseReports = (scanId, options = {}) => (SIM
  ? sim({ status: 'not_started', reports: [] })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/reports`, { headers: headers(), signal: options.signal }).then(j))
export const retryReleaseReports = scanId => fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/reports/retry`, { method: 'POST', headers: headers() }).then(j)
export const downloadReleaseReport = async (scanId, bundleId, assetIndex, name) => {
  const response = await fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/release/reports/${encodeURIComponent(bundleId)}/${assetIndex}`, { headers: headers() })
  if (!response.ok) throw new Error('The report could not be downloaded.')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const resumeAutomaticRelease = (scanId, authorizationId) => fetch(
  `${BASE}/scans/${encodeURIComponent(scanId)}/release/automatic/${encodeURIComponent(authorizationId)}/resume`, {
    method: 'POST', headers: headers(),
  }).then(j)

export const getAcceptedRemediationPlan = (scanId, batchId) => SIM
  ? sim({ scan_id: scanId, run_id: batchId, policy: null, available: false })
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/accepted-plan/${encodeURIComponent(batchId)}`, { headers: headers(), cache: 'no-store' }).then(j)

export const createReleaseFolder = (provider, parent, name) => fetch(`${BASE}/release/folders`, {
  method: 'POST', headers: headers({ 'Content-Type': 'application/json' }),
  body: JSON.stringify({ provider, parent, name }),
}).then(j)

const simulatedRunAiApproval = new Map()
const simulatedApproval = (scanId, runId) => simulatedRunAiApproval.get(`${scanId}:${runId}`)
  || { supported: true, enabled: false, revision: 0, run_id: runId, source_revision: `sim:${scanId}:${runId}` }
export const getRunAiApproval = (scanId, runId, options = {}) => SIM ? sim(simulatedApproval(scanId, runId)) : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/ai-approval/${encodeURIComponent(runId)}`, {
  headers: headers(), cache: 'no-store', signal: options.signal,
}).then(j)

export const setRunAiApproval = (scanId, runId, setting, options = {}) => SIM ? (() => {
  const current = simulatedApproval(scanId, runId)
  if (setting.expected_revision !== current.revision || setting.expected_source_revision !== current.source_revision) return Promise.reject(new Error('Automatic approval setting changed; refresh and try again'))
  const updated = { ...current, enabled: setting.enabled === true, revision: current.revision + 1 }
  simulatedRunAiApproval.set(`${scanId}:${runId}`, updated)
  return sim(updated)
})() : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/ai-approval/${encodeURIComponent(runId)}`, {
  method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(setting), signal: options.signal,
}).then(j)


export const getStageProgressQueue = (executionId, bucket) => (SIM
  ? sim({ execution_id: executionId, bucket, available: false, files: [], count: null })
  : fetch(`${BASE}/stage-executions/${encodeURIComponent(executionId)}/queue?bucket=${encodeURIComponent(bucket)}`,
    { headers: headers(), cache: 'no-store' }).then(j))

// Activity evidence is bound to a recorded event and saved-byte hash. Keep
// downloads authenticated instead of opening a URL without the user's headers.
export const getRemediationActivityEvidence = (scanId, seq, options = {}) => (SIM || !scanId
  ? sim({available: false, reason: 'Evidence requires a recorded remediation event.'})
  : fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/activity/${encodeURIComponent(seq)}/evidence`, {headers: headers(), signal: options.signal}).then(j))
const downloadActivityBlob = (blob, name) => {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export const downloadRemediationActivitySavedCopy = async (scanId, seq) => {
  const identity = authEpoch()
  const response = await fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/remediation/activity/${encodeURIComponent(seq)}/copy`, {headers: headers()})
  if (!response.ok) { await j(response); throw new Error('The matching saved copy is unavailable.') }
  const blob = await response.blob()
  if (authEpoch() !== identity) throw new Error('The signed-in account changed.')
  const disposition = response.headers.get('Content-Disposition') || ''
  const encoded = /filename\*=UTF-8''([^;\r\n]+)/i.exec(disposition)
  const match = /filename="([^"\r\n]+)"/.exec(disposition)
  let name = match?.[1] || `saved-copy-${seq}`
  if (encoded) { try {name = decodeURIComponent(encoded[1])} catch {} }
  name = name.split(/[\\/]/).at(-1).replace(/[\x00-\x1f\x7f]/g, '') || `saved-copy-${seq}`
  downloadActivityBlob(blob, name)
}
export const downloadRemediationActivityEvidence = async (scanId, seq) => {
  const identity = authEpoch()
  const evidence = await getRemediationActivityEvidence(scanId, seq)
  if (authEpoch() !== identity) throw new Error('The signed-in account changed.')
  if (evidence?.available !== true) throw new Error('Evidence is not recorded for this activity.')
  downloadActivityBlob(new Blob([JSON.stringify(evidence, null, 2)], {type: 'application/json'}), `activity-evidence-${seq}.json`)
}
