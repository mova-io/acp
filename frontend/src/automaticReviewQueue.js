import { exclusionReason } from './batchReviewSelection.js'
import { AUTO_RULES, normalizedRuleOf } from './automaticReviewResponsibility.js'
import { activeAutomaticStateOf, approvalRecordedOn, approvalSuperseded, approvedWriteUnconfirmed, isAiAssistedDraft, optionalInspectionOf, postApprovalMarkerReason, resultKindOf, workflowStatusOf } from './remediationInboxModel.js'

const hasProposalValue = row => !!(row.hasProposal || (row.after != null && row.after !== '')
  || (row.proposals || row._raw?.proposals || []).some(p => typeof p?.proposed_value === 'string' && p.proposed_value.trim()))

// Why THIS row is not being applied automatically — a specific sentence per state, never one
// generic line stamped on every row. Returns null where the question does not arise: a recorded
// result (verified, rejected, replaced by a verified fix …) has nothing left to admit, so it carries
// no reason and no banner at all.
export function unadmittedReasonOf(row, { marker = null, disposition = null, policy = {}, decisions = {} } = {}) {
  if (optionalInspectionOf(row) || resultKindOf(row, decisions) != null) return null
  // Saved or approved: a pre-approval "person must judge" marker no longer describes the row.
  const pastApproval = row.applied === true || row.autoApplied === true || approvalRecordedOn(row, decisions)
  const markerReason = pastApproval ? postApprovalMarkerReason({ automaticDisposition: disposition }) : disposition?.reason
  if (policy?.enabled !== true || markerReason) return markerReason || null
  if (activeAutomaticStateOf(row)) return 'An automatic job for this change is queued or running.'
  if (approvalSuperseded(row, decisions))
    return 'The approval on record was made against an earlier version of this document or suggestion, so ACP will not write it.'
  if (approvedWriteUnconfirmed(row, decisions))
    return 'Your approval is recorded, but no confirmed write of the approved value exists yet.'
  if (row.applied === true || row.autoApplied === true)
    return 'This change is saved; the re-scan that confirms it has not verified it yet.'
  if (approvalRecordedOn(row, decisions) || workflowStatusOf(row, decisions) === 'awaiting-validation')
    return 'Your approval is recorded; the change has not been confirmed by a re-scan yet.'
  if (!hasProposalValue(row))
    return 'ACP has no proposed value for this finding yet, so there is nothing to admit to automatic application.'
  if (policy.supported === false)
    return 'Automatic approval is not available for this run, so this suggestion needs an individual decision.'
  if (marker)
    return 'The automatic-approval record for this suggestion was made for a different run, document version or suggestion, so it does not cover the current one.'
  const rule = normalizedRuleOf(row)
  if (rule && !AUTO_RULES.has(rule))
    return `Automatic approval does not cover WCAG ${rule} changes, so this suggestion needs an individual decision.`
  if (!isAiAssistedDraft(row))
    return 'Automatic approval covers AI-drafted suggestions only; this suggestion was not drafted by a model.'
  const exclusion = exclusionReason(row, decisions)
  if (exclusion) return `Not eligible for automatic application: ${exclusion}.`
  return 'ACP has no automatic-approval record for this suggestion in the current run yet.'
}

// Only admitted server work changes a pending proposal's UI queue. Standing
// consent alone cannot prove source freshness or the writer's eligibility.
export function automaticReviewQueue(rows = [], policy = {}, decisions = {}) {
  policy ??= {}
  return rows.map(row => {
    const raw = row._raw || row
    const marker = raw.automatic_approval || row.automatic_approval || (raw.auto_approval_status ? {state:raw.auto_approval_status,run_id:raw.auto_approval_run_id,source_revision:raw.auto_approval_source_revision} : null)
    const exactSnapshots = !marker?.proposal_snapshot_ids || JSON.stringify(marker.proposal_snapshot_ids) === JSON.stringify(raw.proposal_snapshot_ids || row.proposal_snapshot_ids || [])
    const scan = raw.scan_id || row.scanId
    const scopeBound = marker?.responsibility ? !!scan && marker.scan_id === scan && Array.isArray(marker.proposal_snapshot_ids) && exactSnapshots : (!marker?.scan_id || !scan || marker.scan_id === scan) && exactSnapshots
    const disposition = scopeBound && marker?.run_id === policy.run_id && marker?.source_revision === policy.source_revision ? marker : null
    const unmarked = {...row, automaticQueued:false, automaticDisposition:disposition}
    if (!row.automaticQueued) delete unmarked.automaticQueued
    unmarked.automaticReason = unadmittedReasonOf(unmarked, {marker, disposition, policy, decisions})
    const admitted = policy.enabled === true && policy.supported !== false && policy.run_id && policy.source_revision != null
      && scopeBound && marker?.run_id === policy.run_id && marker.source_revision === policy.source_revision
      && ['queued','processing','checking','applying','verifying'].includes(marker.state)
    if (!admitted || optionalInspectionOf(row) || workflowStatusOf(unmarked,decisions) !== 'needs-review'
        || !isAiAssistedDraft(row) || exclusionReason(unmarked,decisions)) return unmarked
    return {...row,automaticQueued:true,automaticQueueLabel:({checking:'Checking automatic eligibility',queued:'Queued automatically',processing:'Applying…',applying:'Applying…',verifying:'Verifying…'})[marker.state]}
  })
}
