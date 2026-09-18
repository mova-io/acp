// Finding/review reconciliation — the classification model (contract C3), on a synthetic
// reproduction of the production population behind scan b3eba56d4d5d (DOCX, one body image).
//
// What the screen said: "All clear — nothing needs your review", 4 of 5 reviewed beside 3 of 5
// actions complete, pills Needs your input 0 / Processing 0 / Status checks 2 / Results 3, and a
// VERIFIED 1.4.3 contrast item under a blue "Status check: This suggestion needs individual review or
// has not been admitted to automatic application." banner. Underneath:
//   - 1.1.1 PENDING, an AI alt-text proposal for the only body image (docx:drawing:1:paragraph:32);
//   - 1.4.5 APPROVED + applied + verified — the OCR text replacement that REMOVED that image, so the
//     corrected copy has no body drawing left and the 1.1.1 proposal has nothing to describe;
//   - verified 1.4.3 and 3.1.1 applied-change evidence (af: rows), plus the 1.4.5 write's own diff.
// Every value below is synthetic; only the shapes follow production (dbItemToUi + /hitl/queue).
import { act, createElement } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
import { automaticReviewQueue } from './automaticReviewQueue.js'
import { automaticReviewResponsibility, matchesAutomaticReview } from './automaticReviewResponsibility.js'
import { TARGET_REPLACED_EXCLUSION, batchDecision, exclusionReason, snapshotFinding } from './batchReviewSelection.js'
import {
  approvalRecordedOn, approvalSuperseded, approvedWriteUnconfirmed, autoFixRows, dedupeReviewTasks, isResolved,
  isTargetReplaced, resultKindOf, reviewProgressOf, workflowStatusOf,
} from './remediationInboxModel.js'
import { remediationRecoveryGuidance } from './remediationRecoveryGuidance.js'
import { remainingReasonOf, reviewBannerOf, reviewQueueAction } from './reviewQueueAction.js'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
afterEach(unmountAll)

const SCAN = 'scan-synthetic-1'
const RUN = 'run-synthetic-1'
const FILE = 'UTSW_Discharge_Summary.docx'
const IMAGE = 'docx:drawing:1:paragraph:32'
const policy = { enabled: true, supported: true, run_id: RUN, source_revision: 1 }
const OLD_GENERIC = 'This suggestion needs individual review or has not been admitted to automatic application.'

// One HITL row in the dbItemToUi shape: server record under _raw, UI status undefined for pending.
function hitl({ id, rule, status = 'pending', applied = false, validated = false, value, source = 'AI vision model (synthetic)', raw = {} }) {
  const proposals = [{ proposed_value: value, source, model: /AI/.test(source) ? 'vision' : undefined, locator: IMAGE, snapshot_id: `snap-${id}` }]
  return {
    id, file: FILE, scanId: SCAN, ruleId: rule, rule_id: rule, title: `DOCX · ${rule}`,
    status: status === 'pending' ? undefined : status, applied, validated,
    hasProposal: true, after: value, proposals, proposalSource: source,
    _raw: { id, scan_id: SCAN, file: FILE, rule_id: rule, status, applied: applied ? 1 : null, validated: validated ? 1 : 0,
      finding_count: 1, proposals, proposal_snapshot_ids: [`snap-${id}`], source_revision: 1, decision_version: 0, ...raw },
  }
}

const EVIDENCE = {
  removed_by_item_id: '9102', removed_by_rule_id: '1.4.5', targets: [IMAGE], finding_ids: ['finding-111'],
  corrected_artifact_sha256: 'c0rrected-synthetic', source_artifact_sha256: 's0urce-synthetic',
  verified_at: '2026-09-17T10:00:00Z', assessment: 'release.corrected_copy_assessed',
  assessment_status: 'analysed', skipped_rules: 0,
}
const altText = hitl({ id: 9101, rule: '1.1.1', value: 'A synthetic line chart of weekly readings.',
  raw: { superseded: true, superseded_reason: 'target_removed_by_verified_fix', superseded_evidence: EVIDENCE } })
const imageOfText = hitl({ id: 9102, rule: '1.4.5', status: 'approved', applied: true, validated: true,
  value: 'Synthetic instructions: take one tablet twice daily.', source: 'OCR (tesseract)' })
const diffs = [
  { file: FILE, rule_id: '1.4.3', seq: 0, before: '#9A9A9A on #FFFFFF', after: '#595959 on #FFFFFF', note: 'contrast', verified: true },
  { file: FILE, rule_id: '3.1.1', seq: 0, before: 'lang missing', after: 'en-US', note: 'language', verified: true },
  { file: FILE, rule_id: '1.4.5', seq: 0, before: '[image]', after: 'Synthetic instructions: take one tablet twice daily.',
    note: `approved by a reviewer · ${IMAGE}`, verified: true },
]
const afRows = autoFixRows(diffs, sc => sc)
const population = () => automaticReviewQueue([altText, imageOfText, ...afRows], policy, {})
const contrastOf = rows => rows.find(r => r.rule_id === '1.4.3')

describe('the production population, reconciled', () => {
  it('counts four tasks, all finished — the 1.4.5 diff is the same object as its HITL row', () => {
    const rows = population()
    expect(rows).toHaveLength(5)
    const tasks = dedupeReviewTasks(rows)
    expect(tasks.map(r => r.id)).toEqual([9101, 9102, afRows[0].id, afRows[1].id])
    expect(reviewProgressOf(rows, {})).toEqual({ total: 4, decided: 4, finished: 4, awaitingOutcome: 0, open: 0 })
    // Same answer whether the caller dedupes first or not.
    expect(reviewProgressOf(tasks, {})).toEqual(reviewProgressOf(rows, {}))
    const owners = tasks.map(r => automaticReviewResponsibility(r, {}))
    expect(owners).toEqual(['results', 'results', 'results', 'results'])
    for (const tab of ['review', 'status-check', 'awaiting-validation']) {
      expect(tasks.filter(r => matchesAutomaticReview(r, tab, {}, true))).toHaveLength(0)
    }
    expect(tasks.filter(r => matchesAutomaticReview(r, 'completed', {}, true))).toHaveLength(4)
  })

  it('a verified 1.4.3 contrast row carries no automatic reason and no banner', () => {
    const contrast = contrastOf(population())
    expect(resultKindOf(contrast, {})).toBe('verified')
    expect(contrast.automaticReason).toBeNull()
    expect(reviewBannerOf(contrast, {}, true)).toBeNull()
    expect(reviewBannerOf(contrast, {}, false)).toBeNull()
    expect(remainingReasonOf(contrast, {}, true)).toMatchObject({ tab: 'results', tabLabel: 'Results', nextAction: 'No action needed' })
    // The same row as a HITL record rather than applied-change evidence.
    const hitlContrast = automaticReviewQueue([hitl({ id: 9103, rule: '1.4.3', status: 'approved', applied: true, validated: true, value: '#595959' })], policy)[0]
    expect(hitlContrast.automaticReason).toBeNull()
    expect(reviewBannerOf(hitlContrast, {}, true)).toBeNull()
  })

  it('renders no owner banner above the verified contrast result', async () => {
    const { container, root } = createTestRoot()
    const contrast = contrastOf(population())
    await act(async () => root.render(createElement(RemediationInbox, {
      queue: [contrast], decisions: {}, autoApprove: true, automaticApprovalPolicy: policy,
      initialTab: 'all', initialGroup: 'document',
    })))
    expect(container.querySelector('.automatic-review-queued')).toBeNull()
    expect(container.textContent).not.toContain(OLD_GENERIC)
  })

  it('the approved, applied, validated 1.4.5 is a verified result', () => {
    const row = population().find(r => r.id === 9102)
    expect(workflowStatusOf(row, {})).toBe('completed')
    expect(resultKindOf(row, {})).toBe('verified')
    expect(automaticReviewResponsibility(row, {})).toBe('results')
    expect(reviewBannerOf(row, {}, true)).toBeNull()
  })
})

describe('a pending 1.1.1 whose image was removed by the verified 1.4.5 fix', () => {
  const row = () => population().find(r => r.id === 9101)

  it('is a target-replaced result, never stale, human or a status check', () => {
    const r = row()
    expect(isTargetReplaced(r)).toBe(true)
    expect(workflowStatusOf(r, {})).toBe('completed')
    expect(resultKindOf(r, {})).toBe('target-replaced')
    expect(automaticReviewResponsibility(r, {})).toBe('results')
    expect(matchesAutomaticReview(r, 'review', {}, true)).toBe(false)
    expect(matchesAutomaticReview(r, 'status-check', {}, true)).toBe(false)
    expect(reviewQueueAction(r, {}, true).key).toBe('results')
    expect(r.automaticReason).toBeNull()
    expect(reviewBannerOf(r, {}, true)).toBeNull()
    expect(remediationRecoveryGuidance(r, {})).toBeNull()
    expect(isResolved(r, {})).toBe(true)
    // The audit status is untouched: pending stays pending.
    expect(r._raw.status).toBe('pending')
  })

  it('cannot be approved, and says why', () => {
    const r = row()
    expect(exclusionReason(r, {})).toBe(TARGET_REPLACED_EXCLUSION)
    expect(exclusionReason(r, {})).toMatch(/replaced by a verified change/i)
    expect(() => batchDecision(snapshotFinding(r))).toThrow(TARGET_REPLACED_EXCLUSION)
  })

  it('names the removing criterion and asks for nothing', () => {
    const remaining = remainingReasonOf(row(), {}, true)
    expect(remaining).toMatchObject({ tab: 'results', tabLabel: 'Results', queueTab: 'completed', nextAction: 'No action needed' })
    expect(remaining.reason).toContain('WCAG 1.4.5')
    expect(remaining.reason).not.toMatch(/certif/i)
  })

  it('an APPROVED but unwritten row with the same evidence keeps its approval and is not recoverable', () => {
    const approved = { ...altText, status: 'approved', _raw: { ...altText._raw, status: 'approved', approval_recheck_required: false } }
    expect(approvalRecordedOn(approved, {})).toBe(true)
    expect(approvalSuperseded(approved, {})).toBe(false)
    expect(approvedWriteUnconfirmed(approved, {})).toBe(false)
    expect(remediationRecoveryGuidance(approved, {})).toBeNull()
    expect(automaticReviewResponsibility(approved, {})).toBe('results')
    expect(resultKindOf(approved, {})).toBe('target-replaced')
  })

  it('a criterion-reassessed supersession is still stale work, not a replaced target', () => {
    const reassessed = { ...altText, _raw: { ...altText._raw, superseded_reason: 'criterion_reassessed', superseded_evidence: undefined } }
    expect(isTargetReplaced(reassessed)).toBe(false)
    expect(exclusionReason(reassessed, {})).toBe('Stale — refresh and review')
    expect(resultKindOf(reassessed, {})).toBeNull()
  })
})

describe('a pending 1.1.1 the server says a person must decide', () => {
  const REASON = 'The synthetic image contains words the draft does not transcribe; a person must confirm the description.'
  const marker = { state: 'review_required', responsibility: 'human', owner: 'You', reason: REASON,
    run_id: RUN, scan_id: SCAN, source_revision: 1, proposal_snapshot_ids: ['snap-9201'] }
  const pending = hitl({ id: 9201, rule: '1.1.1', value: 'A synthetic photo of a clinic entrance.', raw: { automatic_approval: marker } })

  it('is "You", with the server reason verbatim', () => {
    const r = automaticReviewQueue([pending], policy)[0]
    expect(automaticReviewResponsibility(r, {})).toBe('human')
    expect(r.automaticReason).toBe(REASON)
    expect(reviewBannerOf(r, {}, true)).toEqual({ owner: 'You', text: REASON })
    expect(remainingReasonOf(r, {}, true)).toEqual({ tab: 'review', tabLabel: 'Needs your input', queueTab: 'review',
      reason: REASON, nextAction: 'Approve or edit the proposed alt text' })
    expect(remainingReasonOf(r, {}, false)).toMatchObject({ tab: 'review', tabLabel: 'Needs review', reason: REASON })
  })

  it('is a status check naming the gap while it cannot be decided (no version lineage), never a generic line', () => {
    // Undecidable drafts stay out of "Needs your input" (actionableReviewInput.test.jsx), but the
    // reason says exactly what is missing — not the old catch-all, and not the human reason.
    const noVersion = { ...pending, _raw: { ...pending._raw, decision_version: undefined } }
    const r = automaticReviewQueue([noVersion], policy)[0]
    expect(exclusionReason(r, {})).toBe('Version unavailable — review individually')
    expect(automaticReviewResponsibility(r, {})).toBe('check')
    const banner = reviewBannerOf(r, {}, true)
    expect(banner.owner).toBe('Status check')
    expect(banner.text).toMatch(/saved version information/)
    expect(banner.text).not.toBe(OLD_GENERIC)
    expect(remainingReasonOf(r, {}, true)).toMatchObject({ tab: 'status-check', nextAction: 'Refresh the suggestion from the remediation plan' })
  })

  it('without any admission record gets a specific status-check reason, never the generic one', () => {
    const unmarked = hitl({ id: 9202, rule: '1.1.1', value: 'A synthetic photo of a clinic entrance.' })
    const r = automaticReviewQueue([unmarked], policy)[0]
    expect(automaticReviewResponsibility(r, {})).toBe('check')
    expect(r.automaticReason).not.toBe(OLD_GENERIC)
    expect(r.automaticReason).toMatch(/no automatic-approval record/i)
    expect(reviewBannerOf(r, {}, true)).toEqual({ owner: 'Status check', text: r.automaticReason })
    expect(remainingReasonOf(r, {}, true)).toMatchObject({ tab: 'status-check', tabLabel: 'Status checks', reason: r.automaticReason })
  })
})

describe('an approved row whose write retry is queued', () => {
  const retry = hitl({ id: 9301, rule: '1.4.5', status: 'approved', value: 'Synthetic transcribed text.', source: 'OCR (tesseract)',
    raw: { automatic_approval: { state: 'queued', responsibility: 'acp', owner: 'ACP', run_id: RUN, scan_id: SCAN,
      source_revision: 1, proposal_snapshot_ids: ['snap-9301'] } } })

  it('is ACP Processing once — not recovery, not human, not a status check', () => {
    const r = automaticReviewQueue([retry], policy)[0]
    expect(automaticReviewResponsibility(r, {})).toBe('acp')
    expect(approvedWriteUnconfirmed(r, {})).toBe(false)
    expect(remediationRecoveryGuidance(r, {})).toBeNull()
    expect(reviewQueueAction(r, {}, true)).toEqual({ key: 'processing', label: 'Processing' })
    const tabs = ['review', 'awaiting-validation', 'status-check', 'completed'].filter(t => matchesAutomaticReview(r, t, {}, true))
    expect(tabs).toEqual(['awaiting-validation'])
    expect(reviewBannerOf(r, {}, true)?.owner).toBe('ACP')
    expect(remainingReasonOf(r, {}, true)).toMatchObject({ tab: 'processing', tabLabel: 'Processing', nextAction: 'Wait for ACP to finish applying' })
    // A decision is recorded; the outcome is not in yet.
    expect(reviewProgressOf([r], {})).toEqual({ total: 1, decided: 1, finished: 0, awaitingOutcome: 1, open: 0 })
  })

  it('without the job it is write recovery again', () => {
    const idle = { ...retry, _raw: { ...retry._raw, automatic_approval: { ...retry._raw.automatic_approval, state: 'blocked', responsibility: 'check' } } }
    const r = automaticReviewQueue([idle], policy)[0]
    expect(automaticReviewResponsibility(r, {})).toBe('check')
    expect(approvedWriteUnconfirmed(r, {})).toBe(true)
    expect(remainingReasonOf(r, {}, true)).toMatchObject({ tab: 'status-check', nextAction: 'Retry writing the approved fix' })
  })
})

describe('a saved AI fix still carrying its pre-approval "individual judgment" marker', () => {
  const STALE = 'Proposal requires individual judgment or has no exact AI provenance'
  const marker = { state: 'review_required', responsibility: 'human', reason: STALE, run_id: RUN, scan_id: SCAN,
    source_revision: 1, proposal_snapshot_ids: ['snap-9501'] }
  const saved = hitl({ id: 9501, rule: '3.1.2', status: 'approved', applied: true, validated: false, value: 'es',
    raw: { automatic_approval: marker } })

  it('shows its verification state, never the stale judgment reason', () => {
    const r = automaticReviewQueue([saved], policy)[0]
    expect(r.automaticDisposition?.reason).toBe(STALE)
    expect(automaticReviewResponsibility(r, {})).toBe('check')
    expect(r.automaticReason).not.toBe(STALE)
    const banner = reviewBannerOf(r, {}, true)
    expect(banner.owner).toBe('Status check')
    expect(banner.text).toContain('no additional approval is needed')
    expect(banner.text).not.toContain(STALE)
    const remaining = remainingReasonOf(r, {}, true)
    expect(remaining).toMatchObject({ tab: 'status-check', nextAction: 'Wait for the re-scan to verify the saved change' })
    expect(remaining.reason).not.toContain(STALE)
    expect(remediationRecoveryGuidance(r, {})?.reason ?? '').not.toContain(STALE)
  })

  it('keeps an ACP-side status reason on a saved change', () => {
    const NO_JOB = 'No active verification job is recorded.'
    const withStatus = { ...saved, _raw: { ...saved._raw, automatic_approval: { ...marker, state: 'blocked', responsibility: 'check', reason: NO_JOB } } }
    const r = automaticReviewQueue([withStatus], policy)[0]
    expect(reviewBannerOf(r, {}, true).text).toContain(NO_JOB)
    expect(reviewBannerOf(r, {}, true).text).toContain('no additional approval is needed')
  })

  it('a PENDING row keeps the same judgment reason — it has not been approved yet', () => {
    const pending = hitl({ id: 9502, rule: '3.1.2', value: 'es', raw: { automatic_approval: { ...marker, proposal_snapshot_ids: ['snap-9502'] } } })
    const r = automaticReviewQueue([pending], policy)[0]
    expect(r.automaticReason).toBe(STALE)
    expect(reviewBannerOf(r, {}, true)).toEqual({ owner: 'You', text: STALE })
  })
})

describe('dedupeReviewTasks proves a duplicate before dropping it', () => {
  it('drops an af row naming the same target as a HITL row, and keeps the HITL row', () => {
    const [dup] = autoFixRows([diffs[2]], sc => sc)
    expect(dup.targetLocator).toBe(IMAGE)
    expect(dedupeReviewTasks([imageOfText, dup]).map(r => r.id)).toEqual([9102])
    expect(dedupeReviewTasks([dup, imageOfText]).map(r => r.id)).toEqual([9102])
  })

  it('keeps a distinct saved change that only shares file and criterion', () => {
    const [other, anonymous] = autoFixRows([
      { ...diffs[2], note: 'approved by a reviewer · docx:drawing:2:paragraph:40' },
      { ...diffs[2], note: 'office retry synthetic-writer' },
    ], sc => sc)
    expect(dedupeReviewTasks([imageOfText, other, anonymous]).map(r => r.id)).toEqual([9102, other.id, anonymous.id])
  })

  it('collapses af rows for the same target, keeps af rows for distinct ones, never drops HITL rows', () => {
    const [a, b, c] = autoFixRows([diffs[2], diffs[2], { ...diffs[2], note: 'approved by a reviewer · docx:drawing:3:paragraph:9' }], sc => sc)
    expect(dedupeReviewTasks([a, b, c]).map(r => r.id)).toEqual([a.id, c.id])
    const twin = { ...altText, id: 9109 }
    expect(dedupeReviewTasks([altText, twin, imageOfText]).map(r => r.id)).toEqual([9101, 9109, 9102])
    expect(dedupeReviewTasks([altText, altText]).map(r => r.id)).toEqual([9101])
  })

  it('matches by item id and finding id as well as locator', () => {
    const byItem = { ...autoFixRows([{ ...diffs[2], note: '', item_id: 9102 }], sc => sc)[0] }
    const byFinding = autoFixRows([{ ...diffs[2], note: '', finding_id: 'finding-145' }], sc => sc)[0]
    const withFinding = { ...imageOfText, _raw: { ...imageOfText._raw, proposals: [], finding_ids: ['finding-145'] }, proposals: [] }
    expect(dedupeReviewTasks([imageOfText, byItem]).map(r => r.id)).toEqual([9102])
    expect(dedupeReviewTasks([withFinding, byFinding]).map(r => r.id)).toEqual([9102])
  })
})

describe('reviewProgressOf keeps saved-but-unverified work out of "finished"', () => {
  it('an approved, written, unverified change is decided and awaiting its outcome', () => {
    const saved = hitl({ id: 9401, rule: '1.4.5', status: 'approved', applied: true, validated: false, value: 'Synthetic text.' })
    expect(resultKindOf(saved, {})).toBeNull()
    expect(reviewProgressOf([saved, ...afRows.slice(0, 1)], {})).toEqual({ total: 2, decided: 2, finished: 1, awaitingOutcome: 1, open: 0 })
  })
  it('an open proposal is open', () => {
    const open = hitl({ id: 9402, rule: '1.1.1', value: 'Synthetic alt text.' })
    expect(reviewProgressOf([open], {})).toEqual({ total: 1, decided: 0, finished: 0, awaitingOutcome: 0, open: 1 })
  })
})
