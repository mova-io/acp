import { activeAutomaticStateOf, approvalRecordedOn, approvalSuperseded, approvedWriteUnconfirmed, laneOf, postApprovalMarkerReason, recordedReviewDecision,
  resultKindOf, targetReplacementOf, workflowStatusOf } from './remediationInboxModel.js'
import { CHECK_EXCLUSIONS, automaticReviewResponsibility, normalizedRuleOf } from './automaticReviewResponsibility.js'
import { unadmittedReasonOf } from './automaticReviewQueue.js'
import { exclusionReason } from './batchReviewSelection.js'
import { requiresPdfSourceEditing } from './pdfStructuralProposal.js'
import { remediationRecoveryGuidance } from './remediationRecoveryGuidance.js'

// Action ownership is independent of severity, which remains available for priority filters.
export function reviewQueueAction(row, decisions = {}, automatic = false) {
  const status = workflowStatusOf(row, decisions)
  const owner = automaticReviewResponsibility(row, decisions)
  if (status === 'completed') return { key: 'results', label: 'Result recorded' }
  if (owner === 'acp') return { key: 'processing', label: 'Processing' }
  // Approved, not written, not verified. "Review needed" was the wrong ask — the approval is
  // recorded and no further one will be requested — and the row had no control at all behind it.
  // Naming the state as recoverable is what routes the reviewer to the recovery pane.
  if (approvedWriteUnconfirmed(row, decisions)) return { key: 'recover', label: 'Recovery needed' }
  // An approval that no longer binds is not recovery work and not a status check — both of those
  // say ACP owns the next move. This one wants a person: the earlier approval will not be written
  // into the version the document is at now. Stated ahead of the awaiting-validation line, which
  // an approved row reaches whenever no blocked disposition beat it there.
  if (approvalSuperseded(row, decisions)) return { key: 'review', label: 'Review needed' }
  if (status === 'awaiting-validation' || (automatic && owner === 'check')) return { key: 'check', label: 'Status check' }
  const lane = laneOf(row).key
  const manualReason = ['Manual work or no supported proposal writer',
    'This PDF needs headings or table structure added in the original document. ACP cannot apply this draft automatically.']
    .includes(row.automaticDisposition?.reason)
  if (owner === 'human' && manualReason) return { key: 'edit', label: 'Edit needed' }
  if (['manual', 'handoff'].includes(lane)) return { key: 'edit', label: 'Edit needed' }
  if (lane === 'blocked' && owner !== 'human') return { key: 'check', label: 'Status check' }
  return { key: 'review', label: 'Review needed' }
}

export function reviewQueueActions(rows, decisions = {}, automatic = false) {
  const counts = new Map()
  for (const row of rows) {
    const action = reviewQueueAction(row, decisions, automatic)
    const existing = counts.get(action.key)
    counts.set(action.key, { ...action, count: (existing?.count || 0) + 1 })
  }
  return [...counts.values()]
}

// ── Why an item is still where it is, and what happens next (contract C3) ──────────────────────
// The labels are ReviewQueueTabs' own, per mode, so an explanation names the pill the item is in.
export const REMAINING_TAB_LABELS = {
  automatic: { review: 'Needs your input', processing: 'Processing', 'status-check': 'Status checks', results: 'Results' },
  standard: { review: 'Needs review', processing: 'Processing', results: 'Results' },
}
// The ReviewQueueTabs / matchesAutomaticReview key for each contract tab (for opening an item).
const QUEUE_TAB = { review: 'review', processing: 'awaiting-validation', 'status-check': 'status-check', results: 'completed' }

const hasProposalValue = row => !!(row?.hasProposal || (row?.after != null && row?.after !== '')
  || (row?.proposals || row?._raw?.proposals || []).some(p => typeof p?.proposed_value === 'string' && p.proposed_value.trim()))
const decisionOf = (row, decisions) => decisions[row?.id] ?? decisions[row?.file]

/** The sentence that says what a recorded RESULT is. Never claims certification. */
export function resultReasonOf(row, decisions = {}) {
  const kind = resultKindOf(row, decisions)
  if (kind === 'target-replaced') {
    const rule = targetReplacementOf(row)?.removed_by_rule_id
    return `The content this item referred to was removed by ${rule ? `the verified WCAG ${normalizedRuleOf({ rule_id: rule })} change` : 'another verified change'}, so nothing is left to fix here. No value was written for this item and no approval is needed.`
  }
  if (kind === 'verified') return 'A fresh scan of the corrected copy no longer reports this item.'
  if (kind === 'saved-unverified') return 'The change is saved, but no verification has confirmed it yet.'
  if (kind === 'rejected') return 'The proposed fix was rejected; nothing was written for it.'
  if (kind === 'inspection') return 'Automatic changes were saved; inspecting them is optional.'
  if (kind === 'decision') {
    const decision = recordedReviewDecision(row, decisions)
    return decision === 'not_applicable' ? 'Marked as not applicable to this document.'
      : decision === 'deferred' ? 'Deferred by a reviewer.' : 'A decision is recorded for this item.'
  }
  return null
}

function processingTextOf(row) {
  const state = activeAutomaticStateOf(row) || 'queued'
  const text = ({ queued: 'Queued for automatic application.', checking: 'Checking automatic eligibility.',
    processing: 'Applying the change.', applying: 'Applying the change.', verifying: 'Verifying the written change.' })[state]
  return `${text} ${state === 'verifying' ? 'It is not verified yet.' : 'This is not yet an applied or verified fix.'}`
}

function humanReasonOf(row, decisions) {
  const decision = decisionOf(row, decisions)
  if (approvalSuperseded(row, decisions)) return row.automaticDisposition?.reason
    || 'The approval on record was made against an earlier version of this document or suggestion, so ACP will not write it into the current version.'
  if (decision?.state === 'assigned' || decision?.state === 'deferred') return 'This was assigned for manual work in the source document.'
  if (row.rejectedFix || decision?.state === 'rejected') return 'The proposed fix was rejected, so a person must make this change.'
  if (row.automaticDisposition?.responsibility === 'human' && row.automaticDisposition.reason) return row.automaticDisposition.reason
  if (requiresPdfSourceEditing(row)) return 'ACP cannot write this PDF heading or table structure from a prose suggestion.'
  if (row.manual === true || ['manual', 'handoff'].includes(laneOf(row).key)) return 'ACP cannot write this change automatically; it needs a manual edit.'
  if (row.automaticReason) return row.automaticReason
  return hasProposalValue(row) ? 'ACP drafted a fix that needs your decision.' : 'This finding needs your review.'
}

// The specific gap for a draft that cannot be decided yet — named ahead of any server reason, which
// (for a human-marked row) describes the decision that comes AFTER the repair, not this check.
const EXCLUSION_STATUS_REASON = {
  'Missing proposal': 'One or more findings do not have a usable proposed value yet.',
  'Version unavailable — review individually': 'The suggestion does not have the saved version information required to approve it.',
  'Stale — refresh and review': 'The suggestion or its source version changed and must be refreshed before anyone can decide on it.',
  'Invalid structural proposal — refresh suggestions': 'The structural suggestion is invalid and must be refreshed.',
  'Blocked or unavailable': 'ACP has no supported route to apply this suggestion as it stands.',
}
function statusReasonOf(row, decisions) {
  const exclusion = exclusionReason(row, decisions)
  if (CHECK_EXCLUSIONS.includes(exclusion) && (row.automaticDisposition?.responsibility === 'human' || !row.automaticDisposition?.reason))
    return EXCLUSION_STATUS_REASON[exclusion]
  if (row.automaticDisposition?.reason) return row.automaticDisposition.reason
  const guidance = remediationRecoveryGuidance(row, decisions)
  if (guidance?.reason && !['Automatic application is not confirmed', 'Your decision is needed'].includes(guidance.title)) return guidance.reason
  if (row.automaticReason) return row.automaticReason
  const marker = row._raw?.automatic_approval || row.automatic_approval || null
  return unadmittedReasonOf(row, { marker, disposition: row.automaticDisposition || null, policy: { enabled: true }, decisions })
    || 'ACP has not confirmed automatic application for this item.'
}

// A saved or approved change is past its approval, so a marker written BEFORE the approval (a
// person-must-judge reason) no longer describes it and is never shown. Only an ACP-side status
// reason survives, after the verification-state sentence.
function verificationReasonOf(row, decisions) {
  const status = postApprovalMarkerReason(row)
  const then = status ? ` ${status}` : ''
  if (row.applied === true || row.autoApplied === true) {
    return approvalRecordedOn(row, decisions)
      ? `Approval is already recorded and the change is saved; no additional approval is needed. The re-scan that confirms it has not verified it yet.${then}`
      : `This change is saved; no additional approval is needed. The re-scan that confirms it has not verified it yet.${then}`
  }
  if (approvalRecordedOn(row, decisions)) return `Approval is already recorded. Verification is still pending; no additional approval is needed.${then}`
  return status || 'The change is waiting for the re-scan that confirms it.'
}

function humanNextActionOf(row, decisions) {
  const decision = decisionOf(row, decisions)
  if (approvalSuperseded(row, decisions)) return 'Review the current suggestion and decide again'
  if (['assigned', 'deferred', 'rejected'].includes(decision?.state) || row.rejectedFix || row.manual === true
      || requiresPdfSourceEditing(row) || ['manual', 'handoff'].includes(laneOf(row).key)) return 'Make the change in the source document'
  if (!hasProposalValue(row)) return 'Review the finding and fix it by hand'
  return normalizedRuleOf(row) === '1.1.1' ? 'Approve or edit the proposed alt text' : 'Approve, edit or reject the proposed fix'
}

function checkNextActionOf(row, decisions) {
  const exclusion = exclusionReason(row, decisions)
  if (exclusion === 'Missing proposal' || (row.aiDraftable === true && !hasProposalValue(row))) return 'Generate a complete suggestion from the remediation plan'
  if (exclusion === 'Stale — refresh and review' || exclusion === 'Version unavailable — review individually'
      || exclusion === 'Invalid structural proposal — refresh suggestions') return 'Refresh the suggestion from the remediation plan'
  if (!row.automaticDisposition) return 'Check automatic eligibility in the remediation plan'
  return 'Check the recorded status in the remediation plan'
}

/**
 * Where an item sits and why: { tab, tabLabel, queueTab, reason, nextAction }.
 * `tab` is 'review' | 'processing' | 'status-check' | 'results' ('status-check' only in automatic
 * mode, matching the pills on screen). `reason` is the exact recorded reason where one exists
 * (server marker, recovery reason, exclusion); `nextAction` is an imperative. `queueTab` is the key
 * ReviewQueueTabs / matchesAutomaticReview use for the same pill.
 */
export function remainingReasonOf(row, decisions = {}, automatic = false) {
  const status = workflowStatusOf(row, decisions)
  const tab = automatic
    ? ({ results: 'results', acp: 'processing', human: 'review', check: 'status-check' })[automaticReviewResponsibility(row, decisions)]
    : status === 'completed' ? 'results' : status === 'awaiting-validation' ? 'processing' : 'review'
  const labels = automatic ? REMAINING_TAB_LABELS.automatic : REMAINING_TAB_LABELS.standard
  const out = (reason, nextAction) => ({ tab, tabLabel: labels[tab], queueTab: QUEUE_TAB[tab], reason, nextAction })
  if (tab === 'results') {
    return out(resultReasonOf(row, decisions) || 'A result is recorded for this item.',
      resultKindOf(row, decisions) === 'saved-unverified' ? 'No approval needed — wait for the confirming re-scan' : 'No action needed')
  }
  const active = activeAutomaticStateOf(row)
  if (tab === 'processing' && active) {
    return out(processingTextOf(row), active === 'verifying' ? 'Wait for ACP to finish verifying'
      : active === 'checking' ? 'Wait for ACP to finish checking eligibility' : 'Wait for ACP to finish applying')
  }
  if (approvalSuperseded(row, decisions)) return out(humanReasonOf(row, decisions), 'Review the current suggestion and decide again')
  if (approvedWriteUnconfirmed(row, decisions)) {
    return out(remediationRecoveryGuidance(row, decisions)?.reason || row.automaticReason
      || 'Your approval is recorded, but no confirmed write of the approved value exists yet.', 'Retry writing the approved fix')
  }
  // A saved change is a verification question whether its stage reads awaiting or (jobless) blocked.
  if (status === 'awaiting-validation' || (tab === 'status-check' && (row.applied === true || row.autoApplied === true))) {
    return out(verificationReasonOf(row, decisions), row.applied === true || row.autoApplied === true
      ? 'Wait for the re-scan to verify the saved change' : 'Wait for the re-scan to confirm the approved change')
  }
  if (tab === 'status-check') return out(statusReasonOf(row, decisions), checkNextActionOf(row, decisions))
  return out(humanReasonOf(row, decisions), humanNextActionOf(row, decisions))
}

/**
 * The owner banner above the guided pane: null | { owner: 'You' | 'ACP' | 'Status check', text }.
 * Null for every recorded result — a verified, rejected or replaced item needs no owner line.
 * 'You' only when a person must act, and then the text is the human reason.
 */
export function reviewBannerOf(row, decisions = {}, automatic = false) {
  if (!row || resultKindOf(row, decisions) != null) return null
  const active = activeAutomaticStateOf(row)
  if (!automatic && !active && !row.automaticReason) return null
  const remaining = remainingReasonOf(row, decisions, automatic)
  if (remaining.tab === 'results') return null
  if (remaining.tab === 'processing' && active) return { owner: 'ACP', text: remaining.reason }
  if (remaining.tab === 'review') return { owner: 'You', text: remaining.reason }
  if (approvedWriteUnconfirmed(row, decisions)) {
    const reason = postApprovalMarkerReason(row) || row.automaticReason
    return { owner: 'Status check', text: `Approval is already recorded. Retry checks the approved versions before writing.${reason ? ` ${reason}` : ''}` }
  }
  return { owner: 'Status check', text: remaining.reason }
}
