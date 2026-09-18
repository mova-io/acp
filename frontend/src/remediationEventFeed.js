// User-facing projection of the durable remediation lifecycle log. The event is narration, not
// state: counters and terminality continue to come only from the reconciled run snapshot.
// Retained server history is not truncated in the browser. Explicit limits remain supported.
export const MAX_VISIBLE_REMEDIATION_EVENTS = Infinity

const n = (value, noun) => {
  const amount = Number(value)
  const plural = noun.endsWith('fix') ? `${noun}es` : `${noun}s`
  return Number.isFinite(amount) ? `${amount.toLocaleString()} ${amount === 1 ? noun : plural}` : noun
}

// The document's NAME, in the order the server can supply it:
//
//   1. `document` — the structured column (ADR 0052). Every event written since carries it.
//   2. `detail.file` — where the name lived before the column existed. The log is DURABLE, so
//      rows written the old way are still replayed on resume; dropping this fallback would blank
//      the names in exactly the history a reconnecting client came back for.
//   3. a generic noun — used when the run's privacy policy suppressed the name (PRD §22), and
//      when neither field is present.
//
// It never invents a name, and it never treats a suppressed event as an unnamed one: suppression
// is a decision the server made and `documentLabel` says so, so the line reads "a document"
// rather than implying ACP does not know which.
const file = (event) => event?.document || event?.detail?.file
  || (event?.document_suppressed ? 'a document' : 'Document')

// Which document an event belongs to, for grouping several parallel documents' histories apart.
// `document_ref` is a per-run handle that survives suppression — the whole reason it exists — so
// grouping keeps working on a run whose names are withheld.
export const eventDocumentKey = (event) => event?.document_ref || event?.document
  || event?.detail?.file || null

// Only named server reason codes are projected; raw detector errors may contain content.
export function verificationFailureLabel(detail = {}) {
  const labels = {criterion_still_failing: 'still fails on the corrected copy',
    verification_unavailable: 're-scan unavailable; check the saved copy before retrying'}
  const rows = Array.isArray(detail.failed_criteria) ? detail.failed_criteria : []
  return rows.filter(row => /^\d+\.\d+\.\d+$/.test(row?.criterion || '') && labels[row?.reason_code])
    .slice(0, 20).map(row => `WCAG ${row.criterion}: ${labels[row.reason_code]}`).join('; ')
}

// A queued image-description retry that was CANCELLED because it no longer applied — the saved
// input changed, the run stopped, the review item changed, or the draft only needs a person. It is
// neither success nor a provider failure, so it gets its own wording and a neutral tone. Two wire
// forms: the dedicated kind, and a stored `vision_retry_blocked` row the server re-projects at read
// time with a `vision_retry_*` reason code (historical records). The server sends `review_only`
// only when every remaining target is a NAMED human-judgment gate; `not_needed` covers usable
// drafts and drafts held for an unrecognised reason, so it claims no human gate and no validity.
//
// Only `target_replaced` may name an approved, verified fix: `input_changed` means the source
// revision, corrected bytes or parent job changed, which does not by itself prove what caused it.
// "No AI request was made" is said ONLY when the event records `no_ai_request: true`; a historical
// re-projection cannot prove that, so its line says nothing either way.
export const OBSOLETE_VISION_REASONS = {
  vision_retry_input_changed: 'the saved input changed (the document or its assessed source was updated)',
  vision_retry_target_replaced: 'its image was replaced by an approved, verified fix',
  vision_retry_review_only: 'each remaining image already has a draft waiting for your review',
  vision_retry_not_needed: 'no image needed a new AI draft',
  vision_retry_review_changed: 'the review item changed',
  vision_retry_run_inactive: 'the run is no longer active',
}
export const isObsoleteVisionRetry = (kind, detail = {}) => kind === 'remediate.vision_retry_obsolete'
  || (kind === 'remediate.vision_retry_blocked' && typeof detail?.reason_code === 'string' && detail.reason_code.startsWith('vision_retry_'))

const count = value => (value == null || value === '' || !Number.isFinite(Number(value)) || Number(value) < 0) ? null : Math.floor(Number(value))
// Criterion ids arrive dotted ('1.1.1') or as the stored rule id ('SC_1_1_1', 'WCAG_1_1_1'); only
// the three numbers are ever displayed, so nothing else from the field can reach the screen.
const criterionId = value => {
  const match = typeof value === 'string' && /^(?:WCAG_?|SC_)?(\d{1,2})[._](\d{1,2})[._](\d{1,2})$/i.exec(value.trim())
  return match ? `${match[1]}.${match[2]}.${match[3]}` : null
}
// Binding identities: the run (the event's correlation id) and the review item (detail.item_id).
// Opaque handles, never displayed; anything outside a short token shape reads as absent.
const handle = value => (typeof value === 'string' || typeof value === 'number') && /^[A-Za-z0-9_.:-]{1,80}$/.test(String(value)) ? String(value) : null

// A recovered retry saved a DRAFT; it never writes to the document. Auto-apply exists, so a draft
// is not assumed to need a person: "waiting for your review" appears only when the record counts
// drafts awaiting human confirmation (awaiting_review > 0). Historical records carry only {drafts}
// and get the neutral draft wording. Drafts whose blocker is unknown are "needs checking" — never
// valid, usable or verified.
export const UNCERTAIN_DRAFT_KEY = 'uncertain'
// Coverage is UNKNOWN only when the record says so: `missing: null` (the row's total is unknown).
// `coverage_complete: false` alongside a numeric `missing` is KNOWN incompleteness (missing > 0),
// not unknown coverage. A historical record without the key says nothing either way.
const recoveredCoverageUnknown = detail => (Object.prototype.hasOwnProperty.call(detail, 'missing') && detail.missing === null)
  || (detail.coverage_complete === false && count(detail.missing) == null)
function recoveredLine(event, detail) {
  const drafts = count(detail.drafts)
  const awaiting = count(detail.awaiting_review)
  const missing = count(detail.missing)
  const uncertain = count(detail[UNCERTAIN_DRAFT_KEY])
  const usable = count(detail.usable)
  const unknownCoverage = recoveredCoverageUnknown(detail)
  const counted = awaiting != null || missing != null || uncertain != null || usable != null || unknownCoverage
  const parts = []
  const noun = amount => amount > 1 ? `${amount.toLocaleString()} AI image descriptions` : 'AI image description'
  if (awaiting > 0) {
    parts.push(`${noun(awaiting)} drafted for ${file(event)}`)
    parts.push('waiting for your review — not yet written to the document')
  } else if (!counted || drafts > 0 || uncertain > 0 || usable > 0) {
    parts.push(`${noun(Math.max(drafts ?? 1, uncertain ?? 0, 1))} drafted for ${file(event)}`)
    parts.push('not yet written to the document')
  } else parts.push(`AI image-description retry finished for ${file(event)} · no draft was produced`)
  if (uncertain > 0) parts.push(`${n(uncertain, 'draft')} ${uncertain === 1 ? 'needs' : 'need'} checking`)
  if (missing > 0) parts.push(`${n(missing, 'image')} still ${missing === 1 ? 'needs' : 'need'} a description`)
  if (unknownCoverage) parts.push(missing > 0 ? 'other images may also need one' : 'whether every image has a description is not known')
  return parts.join(' · ')
}

// `not_needed` and `review_only` carry the settled-draft counts. `uncertain` drafts are held for a
// reason ACP could not classify: never described as valid, verified or merely awaiting confirmation.
function obsoleteReason(detail) {
  const base = OBSOLETE_VISION_REASONS[detail.reason_code] || 'it no longer applied'
  if (detail.reason_code !== 'vision_retry_not_needed' && detail.reason_code !== 'vision_retry_review_only') return base
  const usable = count(detail.usable), awaiting = count(detail.awaiting_review), uncertain = count(detail[UNCERTAIN_DRAFT_KEY])
  const parts = [usable > 0 && `${n(usable, 'draft')} ready`, awaiting > 0 && `${awaiting.toLocaleString()} waiting for your review`,
    uncertain > 0 && `${uncertain.toLocaleString()} held for a reason ACP could not classify`].filter(Boolean)
  return parts.length ? `${base} (${parts.join(', ')})` : base
}

export function remediationEventLine(event) {
  const detail = event?.detail || {}
  if (isObsoleteVisionRetry(event?.kind, detail)) {
    return `Earlier image-description retry for ${file(event)} cancelled · ${obsoleteReason(detail)}.${detail.no_ai_request === true ? ' No AI request was made.' : ''}`
  }
  switch (event?.kind) {
    case 'remediate.ai_request_started':
    case 'remediate.ai_request_finished': {
      const zone = {local: 'Local AI', cloud: 'Cloud AI', tenant: 'Tenant AI'}[detail.processing_zone] || 'AI'
      const safe = value => typeof value === 'string' && /^[a-zA-Z0-9_.:/@+ -]{1,128}$/.test(value) && !/https?:|@/.test(value) ? value : null
      const model = safe(detail.model) || 'Model not recorded'
      const provider = safe(detail.provider)
      const action = event.kind.endsWith('_started') ? 'Request sent' : detail.status === 'failed' ? 'Request failed' : 'Response received'
      return `${zone} · ${model}${provider ? ` (${provider})` : ''} · ${action} for ${file(event)}`
    }
    case 'remediate.accepted':
      return `Remediation accepted${Number.isFinite(Number(detail.documents)) ? ` for ${n(detail.documents, 'document')}` : ''}`
    case 'remediate.fix_applied':
      return Number(detail.fixes) === 0 ? null : `${n(detail.fixes, 'recorded change')} applied to ${file(event)}`
    case 'remediate.verified':
      return `${n(detail.fixes, 'fix')} independently verified for ${file(event)}`
    case 'remediate.verification_failed':
      return verificationFailureLabel(detail) ? `${file(event)} · ${verificationFailureLabel(detail)}`
        : `${n(detail.fixes, 'fix')} did not pass re-scan for ${file(event)}`
    case 'remediate.delivered':
      return `Corrected copy of ${file(event)} saved to the source provider`
    case 'remediate.delivery_failed':
      if (detail.reason === 'delivery_disabled') return `Corrected copy of ${file(event)} saved in ACP · source delivery is disabled`
      if (detail.delivery_status === 'saved_in_acp') return `Corrected copy of ${file(event)} saved in ACP · publication is handled in Release`
      if (detail.reason === 'write_permission_required') return `Corrected copy of ${file(event)} retained in ACP · provider write permission required`
      return `Corrected copy of ${file(event)} retained in ACP; provider delivery failed`
    case 'remediate.review_requested':
      if (detail.reason_code === 'pdf_structure_tagging_required') return `${file(event)} · WCAG 1.3.1: Add PDF accessibility tags in a document editor; automatic metadata fixes cannot create the structure tree`
      return `Manual review requested for ${file(event)}${detail.criterion ? ` · WCAG ${detail.criterion}` : ''}`
    case 'remediate.document_completed':
      return `${file(event)} remediation finished`
    case 'scan.interrupted':
      return `A worker stopped without reporting · attempt ${event?.attempt || 'unknown'} safely queued to resume`
    case 'remediate.vision_retry_pending':
      return `Image description for ${file(event)} queued to retry · attempt ${detail.retry || 1} of 2`
    case 'remediate.vision_retry_recovered':
      return recoveredLine(event, detail)
    case 'remediate.review_target_replaced': {
      const rule = criterionId(detail.rule_id)
      const by = criterionId(detail.removed_by_rule_id)
      const items = count(detail.finding_count)
      return `${rule ? `WCAG ${rule} review` : 'Review item'} for ${file(event)} no longer applies${items > 1 ? ` (${items.toLocaleString()} items)` : ''} · its target was removed by a verified${by ? ` WCAG ${by}` : ''} fix`
    }
    case 'remediate.vision_retry_blocked':
      if (detail.reason_code === 'vision_spending_reconciliation_required') return `Image description for ${file(event)} paused · awaiting confirmation of previous AI usage before another paid request`
      if (detail.reason_code === 'vision_permission_or_budget_blocked') return `Image description for ${file(event)} paused · saved AI permission or spending limit needs attention`
      if (detail.reason_code === 'vision_provider_access_denied') return `Image description for ${file(event)} paused · saved AI provider access was denied`
      if (detail.reason_code === 'vision_budget_admission_denied') return `Image description for ${file(event)} paused · the spending ledger did not admit another request`
      if (detail.reason_code === 'vision_budget_exhausted') return `Image description for ${file(event)} paused · the saved AI spending allowance is exhausted`
      if (detail.reason_code === 'vision_run_permission_unavailable') return `Image description for ${file(event)} paused · the saved run does not authorize another AI request`
      if (detail.reason_code === 'vision_ai_disabled_or_budget_zero') return `Image description for ${file(event)} paused · AI is disabled or the saved allowance is zero`
      if (detail.reason_code === 'vision_pricing_not_verified') return `Image description for ${file(event)} paused · model pricing is not verified`
      if (detail.reason_code === 'vision_provider_limit_exceeded') return `Image description for ${file(event)} paused · the AI provider limit was reached`
      if (detail.reason_code === 'vision_provider_request_rejected') return `Image description for ${file(event)} paused · the AI provider rejected the request`
      if (detail.reason_code === 'vision_local_endpoint_required') return `Local AI for ${file(event)} unavailable · the saved local-only plan requires a private endpoint; cloud processing was not authorized`
      if (detail.reason_code === 'vision_response_empty') return `AI returned no image description for ${file(event)} · automatic prompt retry exhausted; provide the description or retry after checking local AI`
      if (detail.reason_code === 'vision_generated_output_unusable') return `AI response for ${file(event)} could not be used · automatic generation attempts stopped; check AI activity for the validation reason`
      if (detail.reason_code === 'vision_recovery_unresolved') return `AI generation for ${file(event)} paused · check the recorded failure before retrying`
      return `Image description for ${file(event)} still needs individual review`
    case 'scan.retrying':
      return `A processing attempt failed and was scheduled to retry${event?.attempt ? ` · attempt ${event.attempt}` : ''}`
    // ── human actions on the run ──────────────────────────────────────────────
    // The ACTOR is deliberately absent from these lines, and from the events behind them: the
    // feed is replayed to every authorised viewer of the run, and naming who pressed the button
    // would put one user's identity on another's screen. The audit trail records it (PRD §13);
    // the narrative records that it happened.
    case 'remediate.delivery_retry_requested':
      return `Re-sending the corrected copy of ${file(event)}${detail.destination_provider ? ` to ${detail.destination_provider}` : ''} — no fix re-applied`
    case 'remediate.delivery_retry_refused':
      return `Delivery of ${file(event)} was not retried${detail.reason ? ` · ${detail.reason.replace(/_/g, ' ')}` : ''}`
    case 'remediate.cancel_requested':
      return 'Stopping the run · corrected copies already made are kept'
    case 'remediate.paused':
      return 'Run paused · work not yet started is held'
    case 'remediate.resumed':
      return 'Run resumed'
    default:
      return null
  }
}

export function eventTone(kind, detail = {}) {
  if (isObsoleteVisionRetry(kind, detail || {})) return 'neutral'
  if (kind === 'remediate.ai_request_finished' && detail.status === 'failed') return 'attention'
  if (kind === 'remediate.delivery_failed' && detail.delivery_status === 'saved_in_acp') return 'neutral'
  if (kind === 'remediate.verification_failed' || kind === 'remediate.delivery_failed') return 'error'
  if (kind === 'remediate.delivery_retry_requested' || kind === 'remediate.review_requested' || kind === 'remediate.delivery_retry_refused'
      || kind === 'remediate.cancel_requested' || kind === 'remediate.paused'
      || kind === 'scan.interrupted' || kind === 'scan.retrying' || kind === 'remediate.vision_retry_pending'
      || kind === 'remediate.vision_retry_blocked') return 'attention'
  if (kind === 'remediate.verified' || kind === 'remediate.delivered' || kind === 'remediate.document_completed') return 'success'
  return 'neutral'
}

export function addRemediationEvent(previous, event, id, limit = MAX_VISIBLE_REMEDIATION_EVENTS) {
  const line = remediationEventLine(event)
  if (!line) return previous
  const key = id == null ? `${event.kind}:${event.occurred_at || ''}:${line}` : String(id)
  if (previous.some((row) => row.key === key)) return previous
  return [{ key, id: id == null ? null : String(id), line, kind: event.kind,
            tone: eventTone(event.kind, event.detail), occurredAt: event.occurred_at || null,
            documentKey: eventDocumentKey(event),
            documentSuppressed: event.document_suppressed === true,
            documentName: event.document_suppressed ? null : (event.document || event.detail?.file || null),
            // The SERVER classifies material vs lease/heartbeat activity; the browser must not
            // re-derive it from the kind string, or the two ends drift the moment a kind is
            // added. Absent (an older server, or a replayed row) reads as unknown — which is
            // neither true nor false, and is why this is `?? null` rather than `|| false`.
            material: event.material == null ? null : !!event.material,
            reasonCode: ['vision_spending_reconciliation_required', 'vision_permission_or_budget_blocked', 'vision_generated_output_unusable', 'vision_local_endpoint_required', 'vision_recovery_unresolved', 'vision_response_empty', 'vision_provider_access_denied', 'vision_budget_admission_denied', 'vision_budget_exhausted', 'vision_run_permission_unavailable', 'vision_ai_disabled_or_budget_zero', 'vision_pricing_not_verified', 'vision_provider_limit_exceeded', 'vision_provider_request_rejected', ...Object.keys(OBSOLETE_VISION_REASONS)].includes(event.detail?.reason_code) ? event.detail.reason_code : null,
            // A cancelled retry that no longer applied (either wire form). Never a current notice.
            obsolete: isObsoleteVisionRetry(event.kind, event.detail || {}),
            // Server-classified lifecycle stage (additive; absent from older servers → null).
            activityStage: ['draft_generation', 'review', 'document_write', 'verification', 'delivery', 'run'].includes(event.activity_stage ?? event.detail?.activity_stage) ? (event.activity_stage ?? event.detail.activity_stage) : null,
            // Which rule a review-target replacement retired; only a 1.1.1 replacement clears an image notice.
            ruleId: event.kind === 'remediate.review_target_replaced' ? criterionId(event.detail?.rule_id) : null,
            // Images still without a draft after a recovered retry; null when the record predates the count.
            missing: event.kind === 'remediate.vision_retry_recovered' ? count(event.detail?.missing) : null,
            // The record SAYS coverage is unknown (missing: null). A historical
            // record that predates the count says nothing either way and is not flagged.
            coverageUnknown: event.kind === 'remediate.vision_retry_recovered' && recoveredCoverageUnknown(event.detail || {}),
            // Proven complete coverage — the only recovery that may settle a block.
            coverageComplete: event.kind === 'remediate.vision_retry_recovered' && event.detail?.coverage_complete === true,
            // Binding for clearing notices: the run the event belongs to and the review item it concerns.
            runId: handle(event.correlation_id),
            itemId: handle(event.detail?.item_id),
            attempt: event.attempt == null ? null : Number(event.attempt),
            evidenceIds: typeof event.detail?.evidence_id === 'string' && /^[a-f0-9]{12}$/.test(event.detail.evidence_id) ? [event.detail.evidence_id] : [],
            evidenceAvailable: typeof event.detail?.evidence_id === 'string' && /^[a-f0-9]{12}$/.test(event.detail.evidence_id),
            snapshotSha256: /^[a-f0-9]{64}$/i.test(event.detail?.artifact_sha256 || '') ? event.detail.artifact_sha256 : null,
            activityDetails: {
              criterion: (Array.isArray(event.detail?.criteria) ? event.detail.criteria : (Array.isArray(event.detail?.failed_criteria) ? event.detail.failed_criteria : []).map(row => row?.criterion)).filter(value => /^\d+\.\d+\.\d+$/.test(value)).join(', '),
              location: !event.document_suppressed && typeof event.detail?.location === 'string' ? event.detail.location.slice(0, 300) : null,
              nextAction: event.kind === 'remediate.verification_failed' ? 'Inspect the failed criterion before retrying.' : event.kind === 'remediate.review_requested' && event.detail?.reason_code === 'pdf_structure_tagging_required' ? 'Add PDF accessibility tags in a document editor.' : null,
            },
            processingZone: ['local', 'cloud', 'tenant'].includes(event.detail?.processing_zone) ? event.detail.processing_zone : null,
            phase: event.phase || null,
            correlationId: event.correlation_id || null }, ...previous]
    .sort((a, b) => {
      const left = Number(a.id), right = Number(b.id)
      return a.id != null && b.id != null && Number.isFinite(left) && Number.isFinite(right) ? right - left : 0
    })
    .slice(0, limit)
}

// Replay a whole cursor page with one sort; a long history must not sort itself
// again for every event in the page. Existing objects survive replay unchanged.
export function mergeRemediationEvents(previous, events = []) {
  const keys = new Set(previous.map(row => row.key))
  const added = []
  for (const event of events) {
    const row = addRemediationEvent([], event, event.seq)[0]
    if (!row || keys.has(row.key)) continue
    keys.add(row.key)
    added.push(row)
  }
  if (!added.length) return previous
  return [...added, ...previous].sort((a, b) => {
    const left = Number(a.id), right = Number(b.id)
    return a.id != null && b.id != null && Number.isFinite(left) && Number.isFinite(right) ? right - left : 0
  })
}

// The per-document histories PRD §6D needs: several documents remediating at once, each with its
// own ordered account, from one interleaved feed.
//
// ORDER IS `id` (the event's seq), not arrival and not `occurredAt`. Arrival order is wrong after
// a resume — replayed history arrives after live frames a client already had — and `occurred_at`
// is a wall clock written by whichever replica ran the job, which ADR 0042 rejected as a cursor
// for exactly this reason. `seq` is the per-scan monotonic the stream resumes on, so ordering by
// it makes each document's history identical whether it was streamed live or replayed.
export function documentHistories(rows = []) {
  const byDocument = new Map()
  for (const row of rows) {
    if (!row?.documentKey) continue
    if (!byDocument.has(row.documentKey)) byDocument.set(row.documentKey, [])
    byDocument.get(row.documentKey).push(row)
  }
  for (const history of byDocument.values()) {
    history.sort((a, b) => {
      const left = Number(a.id), right = Number(b.id)
      if (Number.isFinite(left) && Number.isFinite(right)) return left - right
      return 0
    })
  }
  return byDocument
}

// IMAGE-DESCRIPTION NOTICE BINDING. A notice (queued retry, genuine block, recovered with images
// missing or with coverage unknown) is settled only by a LATER event (higher seq) for the same
// document, bound to the SAME run, that proves the notice no longer describes current work:
//
//   * a queued retry ("AI retry queued") is superseded by any later event of its own retry chain
//     (pending / blocked / recovered / obsolete): the retry has run or been replaced, so "queued"
//     is no longer true. That event then speaks for itself;
//   * a block, or a recovery with images missing / coverage unknown, is settled by a later event of
//     the chain that reports the newer state — an obsolete retry, a newer block, a newer queued
//     retry, or a recovery. A recovery settles it ONLY when it records coverage_complete === true:
//     `missing: 0` alone, unknown coverage (`missing: null`) and historical {drafts}-only records
//     never settle anything;
//   * a `review_target_replaced` for rule 1.1.1 retires it only when both records name the same run
//     AND the same item. A replaced image never settles another image's failure.
//
// Within the chain an item id recorded on only one side is not a mismatch (a run schedules one
// retry chain per document, for its single 1.1.1 row, and historical records carry no item id);
// two different item ids always are. Missing RUN binding never matches, and another run's event
// never settles this run's notice. DELIVERY IS NOT IN THE CHAIN: publication may deliver a copy
// with issues remaining, so a delivered copy proves nothing about an image description.
const CHAIN = new Set(['remediate.vision_retry_pending', 'remediate.vision_retry_blocked', 'remediate.vision_retry_recovered',
  'remediate.vision_retry_obsolete'])
export const VISION_NOTICE_KINDS = CHAIN
export function visionSettles(open, later) {
  if (!open || !later || open === later || !open.documentKey || open.documentKey !== later.documentKey) return false
  const earlier = Number(open.id), next = Number(later.id)
  if (!Number.isFinite(earlier) || !Number.isFinite(next) || next <= earlier) return false
  if (!open.runId || open.runId !== later.runId) return false
  if (later.kind === 'remediate.review_target_replaced') {
    return later.ruleId === '1.1.1' && !!open.itemId && open.itemId === later.itemId
  }
  if (!CHAIN.has(later.kind)) return false
  if (open.itemId && later.itemId && open.itemId !== later.itemId) return false
  if (open.kind === 'remediate.vision_retry_pending') return true
  return later.kind !== 'remediate.vision_retry_recovered' || later.coverageComplete === true
}
// Whether a record is itself a current notice once nothing later settles it.
export function visionNoticeOpen(row) {
  if (!row || row.obsolete) return false
  if (row.kind === 'remediate.vision_retry_pending' || row.kind === 'remediate.vision_retry_blocked') return true
  return row.kind === 'remediate.vision_retry_recovered' && (row.missing > 0 || row.coverageUnknown === true)
}
const VISION_ATTENTION = new Set(['remediate.vision_retry_pending', 'remediate.vision_retry_blocked'])

// Group the bounded recent history, retaining actionable exceptions ahead of routine milestones.
// Rows are newest first; an attention row already settled by a NEWER row does not lead its group.
export function activityGroups(rows = []) {
  const groups = new Map()
  for (const row of rows) {
    const key = row.documentKey || `run:${row.key}`
    if (!groups.has(key)) groups.set(key, { key, rows: [] })
    groups.get(key).rows.push(row)
  }
  return [...groups.values()].map(group => ({ ...group,
    lead: group.rows.find((row, index) => (row.tone === 'error' || row.tone === 'attention')
      && !(row.kind === 'remediate.delivery_failed'
        && group.rows.slice(0, index).some(newer => newer.kind === 'remediate.delivered'))
      && !(VISION_ATTENTION.has(row.kind) && group.rows.slice(0, index).some(newer => visionSettles(row, newer))))
      || group.rows.find(row => row.tone === 'success') || group.rows[0],
  }))
}
