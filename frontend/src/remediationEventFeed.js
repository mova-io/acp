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

export function remediationEventLine(event) {
  const detail = event?.detail || {}
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
      return `Image description recovered for ${file(event)} · saved corrections still need verification`
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
            reasonCode: ['vision_spending_reconciliation_required', 'vision_permission_or_budget_blocked', 'vision_generated_output_unusable', 'vision_local_endpoint_required', 'vision_recovery_unresolved', 'vision_response_empty', 'vision_provider_access_denied', 'vision_budget_admission_denied', 'vision_budget_exhausted', 'vision_run_permission_unavailable', 'vision_ai_disabled_or_budget_zero', 'vision_pricing_not_verified', 'vision_provider_limit_exceeded', 'vision_provider_request_rejected'].includes(event.detail?.reason_code) ? event.detail.reason_code : null,
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

// Group the bounded recent history, retaining actionable exceptions ahead of routine milestones.
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
        && group.rows.slice(0, index).some(newer => newer.kind === 'remediate.delivered')))
      || group.rows.find(row => row.tone === 'success') || group.rows[0],
  }))
}
