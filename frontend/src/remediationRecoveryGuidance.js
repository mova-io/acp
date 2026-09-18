import { exclusionReason } from './batchReviewSelection.js'
import { requiresPdfSourceEditing } from './pdfStructuralProposal.js'
import { activeAutomaticStateOf, approvalSuperseded, approvedWriteUnconfirmed, isTargetReplaced, postApprovalMarkerReason } from './remediationInboxModel.js'

// Use persisted eligibility and actual proposal lineage. Consent alone is not
// evidence that an automatic job exists, and a document retry rewrites fixes.
export function remediationRecoveryGuidance(row, decisions = {}) {
  if (!row || row.validated || row.automaticQueued) return null
  // Replaced by a verified fix: a result, with nothing to refresh, retry or decide.
  // A running writer/retry job: ACP's Processing work — offering a second retry would duplicate it.
  if (isTargetReplaced(row) || activeAutomaticStateOf(row)) return null
  // An approval is on record and no longer binds. Stated FIRST, and with no retry offer: the
  // branch below offers a version-checked retry, which a superseded approval cannot authorize.
  // The backend's retry
  // gate refuses this row too ("The document has changed since this was approved"), so a button
  // here would only produce that refusal.
  if (approvalSuperseded(row, decisions)) return {
    title: 'Approved earlier — this has changed since',
    reason: row.automaticDisposition?.reason
      || 'The approval on record was made against a version of this document or this suggestion that is no longer the current one.',
    next: 'This one may need a fresh decision: what was approved then may not describe what the '
      + 'document says now, and ACP will not write the earlier approval into the current version. '
      + 'Review the current suggestion and decide again, or open the remediation plan to see what '
      + 'changed. Until then this finding stays unresolved and is still reported as remaining.',
    plan: true,
    staleApproval: true,
  }
  // Approved, and the approved value was never written. The reviewer's approval is recorded and
  // must be checked against the current version, so this pane offers a retry of
  // the WRITE, and states the writer's own recorded reason rather than inventing one.
  if (approvedWriteUnconfirmed(row, decisions)) return {
    title: 'Approved — the change has not been written yet',
    reason: postApprovalMarkerReason(row)
      || 'ACP has your approval on record, but no confirmed write of the approved value exists for this finding.',
    next: 'Your approval stays recorded. Retry checks that the document and suggestion still match '
      + 'the approved versions. Matching records need no second approval; changed or missing version '
      + 'records require review. Retry writing the approved value, or open the remediation '
      + 'plan to read the writer’s recorded outcome for this item. Until a write is confirmed this '
      + 'finding stays unresolved and is still reported as remaining.',
    plan: true,
    retryApproved: true,
  }
  if (requiresPdfSourceEditing(row)) return {
    title: 'Edit the source document',
    reason: 'ACP cannot write this PDF heading or table structure from a prose suggestion.',
    next: 'Add the structure in the original document or a PDF accessibility editor, then assess the updated copy.',
  }
  const marker = row.automaticDisposition
  if (row.applied && !row.validated || ['verification_failed', 'verification_pending'].includes(row.status)) return {
    title: 'Check the saved correction',
    // A pre-approval "person must judge" marker no longer describes a saved correction.
    reason: postApprovalMarkerReason(row) || 'The recorded correction has not passed independent verification.',
    next: 'Check the saved result and failed criterion. A document retry reapplies fixes; it is not a verification-only retry.',
    plan: true,
  }
  const exclusion = exclusionReason(row, decisions)
  if (exclusion === 'Missing proposal' || exclusion?.includes('refresh') || exclusion === 'Version unavailable — review individually') return {
    title: exclusion === 'Missing proposal' ? 'A complete suggestion is needed' : 'Refresh the suggestion',
    reason: exclusion === 'Missing proposal' ? 'One or more findings do not have a usable proposed value.'
      : exclusion === 'Version unavailable — review individually' ? 'The suggestion does not have the saved version information required for automatic approval.'
      : 'The suggestion or its source version is stale or invalid.',
    next: 'Open the remediation plan to check generation and refresh options. The current item remains unresolved.',
    plan: true,
  }
  if (marker?.responsibility === 'human') return {
    title: 'Your decision is needed',
    reason: marker.reason || 'The saved policy requires individual review of this change.',
    next: 'Review the proposed value, edit it if needed, then apply it or send it for manual work.',
  }
  if (row.automaticReason) return {
    title: 'Automatic application is not confirmed',
    reason: row.automaticReason,
    next: 'Check the remediation plan for eligibility and generation status. No automatic job is claimed for this item.',
    plan: true,
  }
  return null
}
