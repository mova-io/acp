import { describe, expect, it, afterEach } from 'vitest'
import { act } from 'react'
import { createTestRoot, unmountAll } from './testRoots'
afterEach(unmountAll)
import { remediationEventLine, eventTone } from './remediationEventFeed'
import { Activity } from './RemediationOpsPanel'

describe('vision retry narration', () => {
  it('describes a historical recovered draft neutrally, with no document edit and no invented review gate', () => {
    const line = remediationEventLine({ kind: 'remediate.vision_retry_recovered', document: 'A.docx', detail: { drafts: 1 } })
    expect(line).toBe('AI image description drafted for A.docx · not yet written to the document')
    expect(line).not.toMatch(/saved corrections|recovered|verification|waiting for your review/i)
    expect(eventTone('remediate.vision_retry_recovered')).toBe('neutral')
  })
  it('says waiting for your review only with awaiting_review evidence, and uncertain drafts only need checking', () => {
    const line = detail => remediationEventLine({ kind: 'remediate.vision_retry_recovered', document: 'A.docx', detail })
    expect(line({ drafts: 2, awaiting_review: 2, uncertain: 0, missing: 1, document_write: false }))
      .toBe('2 AI image descriptions drafted for A.docx · waiting for your review — not yet written to the document · 1 image still needs a description')
    expect(line({ drafts: 1, awaiting_review: 0, uncertain: 0, missing: 0 }))
      .toBe('AI image description drafted for A.docx · not yet written to the document')
    expect(line({ drafts: 2, awaiting_review: 0, uncertain: 2, missing: 0 }))
      .toBe('2 AI image descriptions drafted for A.docx · not yet written to the document · 2 drafts need checking')
    expect(line({ drafts: 2, awaiting_review: 0, uncertain: 2, missing: 0 })).not.toMatch(/valid|verified|usable|review/i)
    expect(line({ drafts: 0, awaiting_review: 0, uncertain: 0, missing: 3 }))
      .toBe('AI image-description retry finished for A.docx · no draft was produced · 3 images still need a description')
  })
  it.each([
    ['vision_retry_input_changed', 'the saved input changed (the document or its assessed source was updated)'],
    ['vision_retry_target_replaced', 'its image was replaced by an approved, verified fix'],
    ['vision_retry_review_only', 'each remaining image already has a draft waiting for your review'],
    ['vision_retry_not_needed', 'no image needed a new AI draft'],
    ['vision_retry_review_changed', 'the review item changed'],
    ['vision_retry_run_inactive', 'the run is no longer active'],
  ])('words an obsolete retry (%s) neutrally, and claims no AI request only when recorded', (reason, text) => {
    const obsolete = { kind: 'remediate.vision_retry_obsolete', document: 'A.docx', detail: { retry: 2, reason_code: reason, no_ai_request: true } }
    const projected = { kind: 'remediate.vision_retry_blocked', document: 'A.docx', detail: { reason_code: reason,
      recorded_reason_code: 'vision_recovery_unresolved', projection: 'historical_obsolete_retry' } }
    expect(remediationEventLine(obsolete)).toBe(`Earlier image-description retry for A.docx cancelled · ${text}. No AI request was made.`)
    expect(remediationEventLine(projected)).toBe(`Earlier image-description retry for A.docx cancelled · ${text}.`)
    for (const event of [obsolete, projected]) expect(eventTone(event.kind, event.detail)).toBe('neutral')
    if (reason !== 'vision_retry_target_replaced') expect(text).not.toMatch(/approved|verified fix/)
  })
  it('keeps genuine AI pauses unchanged', () => {
    expect(remediationEventLine({ kind: 'remediate.vision_retry_blocked', document: 'A.docx', detail: { reason_code: 'vision_recovery_unresolved' } }))
      .toBe('AI generation for A.docx paused · check the recorded failure before retrying')
    expect(eventTone('remediate.vision_retry_blocked', { reason_code: 'vision_budget_exhausted' })).toBe('attention')
  })
  it('keeps exhausted descriptions in review', () => {
    expect(remediationEventLine({ kind: 'remediate.vision_retry_blocked', document: 'A.pdf' })).toContain('individual review')
    expect(eventTone('remediate.vision_retry_blocked')).toBe('attention')
  })
  it('explains spending uncertainty without calling it manual document work', () => {
    const line = remediationEventLine({ kind: 'remediate.vision_retry_blocked', document: 'A.docx',
      detail: { reason_code: 'vision_spending_reconciliation_required' } })
    expect(line).toContain('awaiting confirmation of previous AI usage')
    expect(line).not.toContain('individual review')
  })
  it('shows a yellow retry only when newly received', async () => {
    const event = { key: 'retry', documentKey: 'A', kind: 'remediate.vision_retry_pending',
      tone: 'attention', line: 'Image description queued to retry' }
    const { root, container } = createTestRoot()
    await act(async () => root.render(<Activity events={[]} />))
    await act(async () => root.render(<Activity events={[event]} />))
    expect(container.querySelector('.remops-activity-retry.remops-activity-fresh')).not.toBeNull()
    expect(container.textContent).toContain('Image description queued to retry')
  })
})

it('shows empty image response as a specific safe cause', () => {
  expect(remediationEventLine({kind:'remediate.vision_retry_blocked',document:'A.pptx',detail:{reason_code:'vision_response_empty',response:'private text'}})).toContain('AI returned no image description')
  expect(remediationEventLine({kind:'remediate.vision_retry_blocked',document:'A.pptx',detail:{reason_code:'vision_response_empty',response:'private text'}})).not.toContain('private text')
})
