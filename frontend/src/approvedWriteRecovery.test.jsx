// The production defect, reproduced from the row that caused it.
//
// Build 2026.9.17.3, file UTSW_Discharge_Summary.docx, scan 6f07d85b39b8, run
// 280a6fa56f4e307e386e9cb0, `auto_approve_ai` ON. A 1.4.5 (image-of-text) HITL row carried
// `status='approved'`, `applied=null`, one proposal, and an `automaticDisposition` the BACKEND had
// already marked `responsibility: 'check'`. The writer job (apply_approved_values 38f509d88951469b)
// ran and completed `done`, having REFUSED to write the value — "The image is cropped in Word …
// Credit withheld; the approved value is kept for retry".
//
// The screen nonetheless said "Needs your input (1)", the row's action read "Review needed", its
// lane read "Blocked", and selecting it produced a pane with the sentence "another approval is not
// needed" above NO CONTROLS AT ALL. Three frontend steps produced that, each individually plausible:
//
//   1. workflowStatusOf returns 'blocked' for a blocked disposition BEFORE its `approved` branch,
//      so the row never reached 'awaiting-validation';
//   2. automaticReviewResponsibility therefore skipped the branch whose own comment says "A
//      saved/accepted fix has no approval left to request", and fell through to a rule-list
//      fallback that bills any non-AUTO_RULES criterion — 1.4.5 is not in that set — to a human,
//      overriding the backend's explicit 'check';
//   3. reviewQueueAction's blocked-lane guard is skipped when the owner IS 'human', so the row got
//      the amber "Review needed" pill, and the detail pane suppressed its recovery section because
//      `resolved` (status === 'approved') was true.
//
// What must remain true, and is asserted below: the finding STAYS unresolved/blocked. Draining
// "Needs your input" must not be done by marking anything resolved — the corrected-copy assessment
// genuinely still lists this criterion as remaining.
import { act, createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
import { automaticReviewResponsibility, matchesAutomaticReview } from './automaticReviewResponsibility.js'
import { exclusionReason } from './batchReviewSelection.js'
import { approvalRecordedOn, approvedWriteUnconfirmed, workflowStatusOf, workflowCounts, isAiAssistedDraft } from './remediationInboxModel.js'
import { reviewQueueAction } from './reviewQueueAction.js'
import { unresolvedWorkSummary } from './unresolvedWorkSummary.js'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
afterEach(unmountAll)

// The backend's own sentence at the time of the incident. Agent B is correcting it to state the
// writer's refusal; this test pins the FRONTEND's behaviour, which must not depend on the wording.
const REASON = 'Approved, application pending. No active writer job is recorded; another approval is not needed.'
const row = {
  id: 501, file: 'UTSW_Discharge_Summary.docx', scanId: '6f07d85b39b8',
  rule_id: '1.4.5', status: 'approved', applied: null, validated: false,
  hasProposal: true, after: 'Discharge instructions: take one tablet twice daily.',
  // OCR, not a model: `ai_calls` is EMPTY for this run. Provenance is what it is.
  proposalSource: 'OCR', proposals: [{ proposed_value: 'Discharge instructions: take one tablet twice daily.', source: 'OCR' }],
  automaticDisposition: { state: 'blocked', responsibility: 'check', owner: 'ACP', reason: REASON },
  automaticReason: REASON,
  // The persisted marker, in the shape automaticReviewQueue requires. RemediationInbox re-projects
  // its queue through that function, which NULLS `automaticDisposition` for any row without a
  // scope-bound marker — so a fixture that carried only the derived field mounted as a plain
  // approved row and never reproduced the defect at all. Exactly the production lineage instead.
  proposal_snapshot_ids: ['snap'],
  _raw: {
    finding_count: 1, proposal_snapshot_ids: ['snap'], source_revision: 1, decision_version: 0,
    scan_id: '6f07d85b39b8',
    automatic_approval: { state: 'blocked', responsibility: 'check', owner: 'ACP', reason: REASON,
      run_id: 'run-280a6fa5', scan_id: '6f07d85b39b8', source_revision: 1, proposal_snapshot_ids: ['snap'] },
  },
}
const policy = { enabled: true, supported: true, run_id: 'run-280a6fa5', source_revision: 1 }

describe('an approved 1.4.5 whose approved value was never written', () => {
  it('is ACP status-check work, never a second request for the reviewer', () => {
    expect(automaticReviewResponsibility(row)).toBe('check')
    expect(matchesAutomaticReview(row, 'review', {}, true)).toBe(false)
    expect(matchesAutomaticReview(row, 'status-check', {}, true)).toBe(true)
    // The count behind "Needs your input" is this filter, verbatim from RemediationInbox.
    expect([row].filter(r => automaticReviewResponsibility(r, {}) === 'human')).toHaveLength(0)
  })

  it('stays unresolved and blocked — the queue drains by ownership, not by hiding the finding', () => {
    expect(workflowStatusOf(row)).toBe('blocked')
    expect(workflowCounts([row])).toMatchObject({ blocked: 1, completed: 0, 'needs-review': 0 })
    expect(matchesAutomaticReview(row, 'blocked', {}, true)).toBe(true)
    expect(approvedWriteUnconfirmed(row)).toBe(true)
  })

  it('offers recovery rather than a review it cannot action', () => {
    expect(reviewQueueAction(row, {}, true).key).not.toBe('review')
    expect(reviewQueueAction(row, {}, true)).toEqual({ key: 'recover', label: 'Recovery needed' })
    expect(reviewQueueAction(row, {}, false).key).not.toBe('review')
  })

  it('counts as potential recovery, not as a human decision', () => {
    const summary = unresolvedWorkSummary([row])
    expect(summary.total).toBe(1)
    const count = key => summary.groups.find(g => g.key === key).count
    expect(count('recovery')).toBe(1)
    expect(count('human')).toBe(0)
  })

  it('keeps genuinely human-owned work where it is', () => {
    // No approval recorded: a pending 1.4.5 crop really is the reviewer's.
    expect(automaticReviewResponsibility({ ...row, status: 'pending', automaticDisposition: undefined, automaticReason: null })).toBe('human')
    // A decision that is not an approval is not an approval.
    expect(automaticReviewResponsibility(row, { 501: { state: 'assigned' } })).toBe('human')
    // A defer is a RECORDED outcome on this repo's model (recordedReviewDecision), so it leaves the
    // queue as a result rather than as human work. Asserted as it is, not as it might be nicer.
    expect(automaticReviewResponsibility(row, { 501: { state: 'deferred' } })).toBe('results')
    expect(automaticReviewResponsibility({ ...row, rejectedFix: true })).toBe('human')
    expect(automaticReviewResponsibility({ ...row, manual: true, status: 'pending',
      automaticDisposition: { state: 'blocked', responsibility: 'human', reason: 'Manual work or no supported proposal writer' } })).toBe('human')
    expect(approvedWriteUnconfirmed({ ...row, status: 'pending' })).toBe(false)
    // Written but unverified stays a verification question, not a write recovery.
    expect(approvedWriteUnconfirmed({ ...row, applied: true })).toBe(false)
    expect(approvedWriteUnconfirmed({ ...row, validated: true })).toBe(false)
  })

  // ── Each classification branch, pinned on its own ────────────────────────────────────────────
  //
  // The fix has TWO independent branches — `approvalRecordedOn` on the awaiting-validation line,
  // and the backend's `responsibility: 'check'` ahead of the rule-list fallback — and on the
  // production row either one alone repairs the symptom. Measured: reverting just one left this
  // file 8/8 green, and only reverting both turned it red. Defence in depth is wanted here, but a
  // branch no test can tell apart from its neighbour is a branch that gets deleted later with the
  // suite still green. These two cases fail if EITHER branch is removed, one each.

  it('routes an approved row on its recorded approval alone, with no help from the disposition', () => {
    // The branch under test is `approvalRecordedOn`. Nothing here says `responsibility: 'check'`:
    // the disposition is stale and still says a human owns it — precisely the state the user was
    // in, and the one regression that would put "Needs your input (1)" back on their screen.
    const staleHuman = { ...row, automaticDisposition: { state: 'blocked', responsibility: 'human', reason: REASON } }
    expect(staleHuman.automaticDisposition.responsibility).not.toBe('check')
    expect(automaticReviewResponsibility(staleHuman)).toBe('check')
    expect(matchesAutomaticReview(staleHuman, 'review', {}, true)).toBe(false)

    // And with no disposition at all: an in-session approval whose write then failed. `apply_failed`
    // is what makes workflowStatusOf answer 'blocked' here, so the awaiting-validation branch cannot
    // cover for a missing `approvalRecordedOn`.
    const approvedThenFailed = { ...row, status: 'apply_failed', automaticDisposition: undefined, automaticReason: null,
      _raw: { ...row._raw, automatic_approval: undefined } }
    expect(workflowStatusOf(approvedThenFailed, { 501: { state: 'approved' } })).toBe('blocked')
    expect(automaticReviewResponsibility(approvedThenFailed, { 501: { state: 'approved' } })).toBe('check')
  })

  it('honours a backend "check" on a row that carries no approval at all', () => {
    // The branch under test is `assigned === 'check'`. This row is PENDING, so `approvalRecordedOn`
    // is false and cannot cover for it; its proposal and lineage are complete, so `exclusionReason`
    // returns null and cannot either. 1.4.5 is outside AUTO_RULES, so the rule-list fallback is what
    // claims it — and the backend has already said otherwise.
    const pending = {
      id: 502, file: 'UTSW_Discharge_Summary.docx', scanId: '6f07d85b39b8', rule_id: '1.4.5',
      status: 'pending', applied: null, validated: false, hasProposal: true, after: 'Transcribed text',
      proposals: [{ proposed_value: 'Transcribed text', source: 'OCR' }],
      automaticDisposition: { state: 'blocked', responsibility: 'check', owner: 'ACP', reason: REASON },
      _raw: { finding_count: 1, proposal_snapshot_ids: ['snap'], source_revision: 1, decision_version: 0 },
    }
    expect(approvalRecordedOn(pending)).toBe(false)
    expect(exclusionReason(pending)).toBeNull()
    expect(automaticReviewResponsibility(pending)).toBe('check')
    expect(matchesAutomaticReview(pending, 'review', {}, true)).toBe(false)
    // The fallback still owns rows the backend did NOT classify — removing it must also go red.
    expect(automaticReviewResponsibility({ ...pending, automaticDisposition: undefined })).toBe('human')
  })

  it('renders an actionable recovery affordance in the detail pane, not a dead end', async () => {
    const { container, root } = createTestRoot()
    const onOpenPlan = vi.fn()
    await act(async () => root.render(createElement(RemediationInbox, {
      queue: [row], decisions: {}, autoApprove: true,
      automaticApprovalPolicy: policy,
      initialTab: 'all', initialGroup: 'document', onOpenPlan,
    })))
    const pane = container.querySelector('section[aria-label="What this item needs"]')
    expect(pane).not.toBeNull()
    expect(pane.textContent).toContain('Approved — the change has not been written yet')
    // The backend's recorded reason is surfaced, not paraphrased or replaced.
    expect(pane.textContent).toContain(REASON)
    // This legacy row has no complete version binding: approval is not a permanent promise.
    expect(pane.textContent).toContain('changed or missing version records require review')
    expect(pane.textContent).not.toMatch(/not ask for it again|no further approval will/i)
    // An affordance exists: the plan button is real and wired.
    const plan = [...pane.querySelectorAll('button')].find(b => b.textContent === 'Open remediation plan')
    expect(plan).not.toBeNull()
    await act(async () => plan.click())
    expect(onOpenPlan).toHaveBeenCalledOnce()
    // Nothing on this pane asks for an approval that is already recorded.
    expect([...container.querySelectorAll('button')].map(b => b.textContent))
      .not.toContain('Apply this fix')
    // "Needs your input" has drained, and the finding is still in the Blocked tab.
    expect(container.querySelector('option[value="review"]').textContent).toBe('Needs your input (0)')
    expect(container.querySelector('option[value="blocked"]').textContent).toContain('1')
    // The lane stays Blocked; the ACTION is no longer a review request.
    expect(container.querySelector('#rinbox-row-501 .rinbox-action-chip--review')).toBeNull()
    expect(container.querySelector('#rinbox-row-501 .rinbox-action-chip--recover')?.textContent).toBe('Recovery needed')
  })

  it('does not claim the change was written, and does not credit a model for an OCR value', async () => {
    const { container, root } = createTestRoot()
    await act(async () => root.render(createElement(RemediationInbox, {
      queue: [row], decisions: {}, autoApprove: true,
      automaticApprovalPolicy: policy,
      initialTab: 'all', initialGroup: 'document',
    })))
    const detail = container.querySelector('.remediation-detail')
    // The photographed contradiction: a "Saved / Written" banner beside "Proposed change not saved".
    expect(detail.textContent).not.toContain('✓ Saved.')
    expect(detail.textContent).toContain('✓ Approval recorded.')
    expect(detail.textContent).toContain('Write not confirmed')
    // Provenance: ai_calls is empty for this run. Nothing may credit a model.
    expect(isAiAssistedDraft(row)).toBe(false)
    expect(detail.querySelector('.remediation-category-pill--ai_applied')).toBeNull()
    expect(detail.textContent).not.toMatch(/\bAI applied\b/)
  })

  it('offers a write retry when the host supplies one, and never a second approval', async () => {
    const { container, root } = createTestRoot()
    const onRetryApproved = vi.fn()
    await act(async () => root.render(createElement(RemediationInbox, {
      queue: [row], decisions: {}, autoApprove: true,
      automaticApprovalPolicy: policy,
      initialTab: 'all', initialGroup: 'document', onRetryApproved,
    })))
    const retry = [...container.querySelectorAll('button')].find(b => b.textContent === 'Retry writing the approved fix')
    expect(retry).not.toBeNull()
    await act(async () => retry.click())
    expect(onRetryApproved).toHaveBeenCalledWith(expect.objectContaining({ id: 501 }))
  })
})

it('a document-level acceptance cannot approve a different finding', () => {
  const pending = {...row,status:'pending', automaticDisposition:undefined}
  expect(approvalRecordedOn(pending, {[row.file]:{state:'accepted'}})).toBe(false)
  expect(approvalRecordedOn(pending, {[row.file]:{state:'accepted',findingId:501}})).toBe(true)
})
it.each([
  {approved_source_revision:'old',source_revision:'new'},
  {approved_proposal_snapshot_ids:['old'],proposal_snapshot_ids:['new']},
  {approval_recheck_required:true},
])('changed approval needs a fresh human check: %j', binding => {
  const changed={...row,_raw:{...row._raw,...binding}}
  expect(approvalRecordedOn(changed)).toBe(false)
  expect(automaticReviewResponsibility(changed)).toBe('human')
})
it('an explicit rejection overrides an earlier recorded approval',()=>{
  expect(approvalRecordedOn(row,{501:{state:'rejected'}})).toBe(false)
})
