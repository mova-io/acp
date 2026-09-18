import { reviewWorkBreakdown } from './reviewWorkBreakdown.js'
import { unresolvedWorkSummary } from './unresolvedWorkSummary.js'
import { pendingReviewRows } from './remediationCountSummary.js'
import { matchesAutomaticReview } from './automaticReviewResponsibility.js'

// Recent event evidence is narration, never a substitute for the reconciled counters.
const VISION = new Set(['remediate.vision_retry_pending', 'remediate.vision_retry_blocked', 'remediate.vision_retry_recovered', 'remediate.delivered'])
export function remainingWorkStatus({ events = [], rows = [], decisions = {}, snapshot = null, automatic = false } = {}) {
  const latest = new Map()
  const ordered = [...events].sort((a, b) => Number(b.id) - Number(a.id))
  for (const event of ordered) {
    if (!event.documentKey || !VISION.has(event.kind) || latest.has(event.documentKey)) continue
    latest.set(event.documentKey, event)
  }
  const notices = []
  const spendingFiles = new Set()
  const blockedCaptionFiles = new Set()
  const now = Date.parse(snapshot?.generated_at || '')
  for (const event of latest.values()) {
    if (event.kind === 'remediate.vision_retry_pending') notices.push({ key: event.key, label: 'AI retry queued', responsibility: 'ACP will retry automatically. No individual approval is needed for this retry.', tone: 'automatic' })
    if (event.kind === 'remediate.vision_retry_blocked') {
      if (event.documentName && ['vision_permission_or_budget_blocked', 'vision_spending_reconciliation_required', 'vision_local_endpoint_required', 'vision_provider_access_denied', 'vision_budget_admission_denied', 'vision_budget_exhausted', 'vision_run_permission_unavailable', 'vision_ai_disabled_or_budget_zero', 'vision_pricing_not_verified', 'vision_provider_limit_exceeded', 'vision_provider_request_rejected'].includes(event.reasonCode)) blockedCaptionFiles.add(event.documentName)
      if (event.reasonCode === 'vision_spending_reconciliation_required') {
        const recorded = Date.parse(event.occurredAt || '')
        const withinChecks = Number.isFinite(now) && Number.isFinite(recorded) && now >= recorded && now - recorded < 40 * 60 * 1000
        if (withinChecks && event.documentName) spendingFiles.add(event.documentName)
        notices.push({ key: event.key, label: withinChecks ? 'AI usage confirmation pending' : 'AI usage confirmation needs attention', responsibility: 'ACP checks previous usage only while a reconciliation retry is scheduled (up to eight checks). Another paid request waits for confirmation; unresolved spending may need attention.', tone: 'waiting' })
      }
      else if (event.reasonCode === 'vision_permission_or_budget_blocked') notices.push(snapshot?.ai_policy?.zone === 'local'
        ? { key: event.key, label: 'Local AI setup needs attention', responsibility: 'The saved plan permits local AI only. Ask an administrator to check the configured private endpoint and models, then recheck readiness before starting a new remediation plan. This earlier record does not identify the exact local failure; increasing a cloud spending limit will not fix a local-only plan.', tone: 'review' }
        : { key: event.key, label: 'AI permission or spending limit needs attention', responsibility: 'Check the saved AI permission and available spending limit. ACP cannot send another request yet.', tone: 'review' })
      else if (event.reasonCode === 'vision_provider_access_denied') notices.push({ key: event.key, label: 'AI provider access denied', responsibility: 'Check access to the saved AI provider. ACP will not repeat the rejected request automatically.', tone: 'review' })
      else if (event.reasonCode === 'vision_budget_admission_denied') notices.push({ key: event.key, label: 'AI request not admitted by budget', responsibility: 'Check the recorded spending decision. ACP will not send another paid request automatically.', tone: 'review' })
      else if (event.reasonCode === 'vision_budget_exhausted') notices.push({ key: event.key, label: 'AI spending allowance exhausted', responsibility: 'Create a new approved plan with sufficient allowance before retrying. ACP will not exceed the saved limit.', tone: 'review' })
      else if (event.reasonCode === 'vision_run_permission_unavailable') notices.push({ key: event.key, label: 'Saved run does not permit another AI request', responsibility: 'Review or replace the saved plan before retrying. ACP will not broaden its permission.', tone: 'review' })
      else if (event.reasonCode === 'vision_ai_disabled_or_budget_zero') notices.push({ key: event.key, label: 'AI disabled or no allowance saved', responsibility: 'Enable AI through an approved plan with a spending allowance before retrying.', tone: 'review' })
      else if (event.reasonCode === 'vision_pricing_not_verified') notices.push({ key: event.key, label: 'AI model pricing not verified', responsibility: 'Select a model with verified pricing before starting a new paid attempt.', tone: 'review' })
      else if (event.reasonCode === 'vision_provider_limit_exceeded') notices.push({ key: event.key, label: 'AI provider limit reached', responsibility: 'Check provider capacity or limits before retrying. ACP will not retry this limit automatically.', tone: 'review' })
      else if (event.reasonCode === 'vision_provider_request_rejected') notices.push({ key: event.key, label: 'AI provider rejected the request', responsibility: 'Check AI activity for the recorded rejection before retrying.', tone: 'review' })
      else if (event.reasonCode === 'vision_local_endpoint_required') notices.push({ key: event.key, label: 'Local AI endpoint unavailable', responsibility: 'This run permits local AI only. Configure a private local AI endpoint, or start a new plan that explicitly permits cloud AI and its spending allowance. Increasing the budget alone will not fix a local-only run.', tone: 'review' })
      else if (event.reasonCode === 'vision_response_empty') notices.push({ key: event.key, label: 'AI returned no image description', responsibility: 'The automatic prompt retry was exhausted. Provide the missing description or retry generation after checking local AI.', tone: 'review' })
      else if (event.reasonCode === 'vision_generated_output_unusable') notices.push({ key: event.key, label: 'AI response could not be used', responsibility: 'Automatic generation attempts have stopped. Check AI activity for the validation reason; review an available suggestion or provide the missing content.', tone: 'review' })
      else if (event.reasonCode === 'vision_recovery_unresolved') notices.push({ key: event.key, label: 'AI generation needs checking', responsibility: 'Check the recorded AI failure before retrying. A spending problem or human decision has not been confirmed.', tone: 'waiting' })
      else notices.push({ key: event.key, label: 'Your review needed', responsibility: 'Automatic image-description attempts have stopped. Review the suggestion or provide an authored description.', tone: 'review' })
    }
  }
  // Only the missing caption draft belongs to this spending pause. Other
  // criteria, manual assignments and already authored proposals remain actionable.
  const counts = reviewWorkBreakdown(rows.filter(row => {
    const criterion = String(row.rule_id || row.ruleId || row.sc || '').replace(/^(WCAG_?|SC_)/, '').replace(/_/g, '.')
    const missingCaption = criterion === '1.1.1' && row.status === 'pending'
      && row.hasProposal !== true && !row.after && !(row.proposals || []).some(p => p.proposed_value || p.proposed)
      && !row.rejectedFix && !decisions[row.id] && !decisions[row.file]
    return !(spendingFiles.has(row.file) && missingCaption)
  }), decisions, blockedCaptionFiles)
  const descriptions = [
    ['missing-proposals', 'Suggestion not ready', 'need a usable proposal before a fix can be applied. Check AI activity for generation status; no application job is confirmed.', 'waiting'],
    ['blocked-ai', 'AI request blocked', snapshot?.ai_policy?.zone === 'local'
      ? 'cannot obtain a new local AI suggestion until the saved local permission, private endpoint, and configured models are checked. Existing rule-based fixes can continue.'
      : 'cannot obtain a new AI suggestion until the saved AI permission, required endpoint, verified pricing, or available spending is resolved. Existing rule-based fixes can continue.', 'waiting'],
    ['failed-checks', 'Saved fix needs recovery', 'have a recorded application or verification failure. Check the failed criterion and reason before retrying that operation.', 'review'],
    ['status-checks', 'Recorded status needs checking', 'have a blocker without a confirmed failure reason. Check saved evidence; these are not automatically classified as human decisions.', 'waiting'],
    ['review', 'Your review needed', 'need a decision on an available suggestion. Auto-apply does not bypass requirements for individual judgment.', 'review'],
    ['manual', 'Manual document edit needed', 'need a person to edit or resolve the document. These do not drain through AI automatically.', 'manual'],
    ['processing', 'Saved change awaiting its check', 'have a saved change that has not been re-checked yet. The corrected copy still has to be assessed; no further approval is needed.', 'waiting'],
  ]
  // The human population is exactly the one used by the review tab badge.
  // Status checks are a separate population, not additional human decisions.
  const humanRows = pendingReviewRows(rows, decisions, automatic)
  const humanIds = new Set(humanRows)
  // Under automatic approval the status population is exactly the Status checks pill's — including
  // a saved change awaiting its re-check, which the pill lists and the old pending-only filter did
  // not (production: pill 2, this panel 1, for the same two tasks). Optional inspection of an
  // already-applied change stays out, as it does from every pending count (#1888).
  const statusRows = automatic
    ? rows.filter(row => !humanIds.has(row) && !row.autoApplied && matchesAutomaticReview(row, 'status-check', decisions, true))
    : pendingReviewRows(rows, decisions, false).filter(row => !humanIds.has(row))
  const humanCounts = reviewWorkBreakdown(humanRows, decisions, blockedCaptionFiles)
  const statusCounts = reviewWorkBreakdown(statusRows, decisions, blockedCaptionFiles)
  for (const [population, breakdown] of [['human', humanCounts], ['status', statusCounts]]) {
    for (const [key, label, responsibility, tone] of descriptions) {
      const count = breakdown[key]
      if (!count) continue
      const status = population === 'status'
      const statusLabel = key === 'review' || key === 'manual' ? 'Recorded status needs checking' : label
      notices.push({key: status ? `status:${key}` : key, population, label: status ? statusLabel : label, count,
        responsibility: `${count.toLocaleString()} ${status ? 'status-check' : 'review'} item${count === 1 ? '' : 's'} ${status && ['review', 'manual'].includes(key) ? 'have not been admitted to automatic processing. Check the saved eligibility or recovery reason; these are outside the human-input tab.' : responsibility}`, tone: status ? 'waiting' : tone})
    }
  }
  const age = snapshot?.progress?.material_age_s
  const checkpoint = typeof age === 'number' && Number.isFinite(age) && age >= 0 ? `Last saved progress ${Math.floor(age / 60)}m ${Math.floor(age % 60)}s ago.` : null
  const stalled = snapshot?.state === 'stalled'
  const recovery = stalled ? 'ACP has detected a stall. Check Live Operations for the worker or retry blocker; automatic recovery is not yet confirmed.'
    : snapshot?.retry_at ? 'A retry is scheduled. ACP will resume eligible work automatically.'
      : snapshot?.progress?.lease_healthy === true && !snapshot?.terminal ? 'A worker is still active. ACP continues monitoring its checkpoints.' : null
  // Consolidate repeated per-document operational warnings, without merging counted review categories.
  const grouped = new Map()
  for (const notice of notices) {
    const key = notice.count == null ? `${notice.label}:${notice.responsibility}` : notice.key
    const previous = grouped.get(key)
    if (previous) previous.affectedDocuments = (previous.affectedDocuments || 1) + 1
    else grouped.set(key, { ...notice })
  }
  const consolidated = [...grouped.values()].map(notice => notice.affectedDocuments ? { ...notice, responsibility: `${notice.affectedDocuments} documents affected. ${notice.responsibility}` } : notice)
    .map(notice => ({ ...notice, presentation:
      notice.tone === 'automatic'
      || notice.population === 'status'
      || ['missing-proposals', 'status-checks'].includes(notice.key)
      || notice.label === 'AI usage confirmation pending'
      || notice.label === 'AI generation needs checking'
        ? 'detail' : 'action' }))
  return { summary: unresolvedWorkSummary(rows, decisions), notices: consolidated, checkpoint, recovery, stalled, counts, humanCounts, statusCounts, humanTotal: humanRows.length, statusTotal: statusRows.length }
}
