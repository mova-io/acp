import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import QualityReviewEvidence from './QualityReviewEvidence.jsx'
import RemediationInbox from './RemediationInbox.jsx'
afterEach(unmountAll)
const thumb = 'data:image/png;base64,aGVsbG8='
async function render(Component, props) {
  const view = createTestRoot()
  await act(async () => view.root.render(createElement(Component, props)))
  return view
}
describe('Source comparison', () => {
  it('pairs each recorded source with its own proposal and displays explicit uncertainty', async () => {
    const { container } = await render(QualityReviewEvidence, { finding: { proposals: [
      { locator: 'slide 1 chart', thumb, proposed_value: 'Revenue fell in 2024', review_status: 'needs_review' },
      { locator: 'slide 2 picture', thumb, proposed_value: 'A tree', agreement: { verdict: 'different', second_opinion: 'A bush' } },
    ] } })
    const rows = container.querySelectorAll('article')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('Revenue fell in 2024')
    expect(rows[0].textContent).not.toContain('A tree')
    expect(rows[0].textContent).toContain('Needs review:')
    expect(rows[1].textContent).toContain('A bush')
    expect(container.querySelectorAll('img')).toHaveLength(2)
    expect(container.textContent).toContain('Recorded thumbnail')
  })
  it('rejects remote images and honestly reports missing source evidence', async () => {
    const { container } = await render(QualityReviewEvidence, { finding: { proposals: [{ kind: 'image', thumb: 'https://tracking.example/image', proposed_value: 'Draft' }] } })
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('Image preview unavailable')
    expect(container.textContent).not.toContain('Needs review:')
  })
  it('does not describe a text excerpt as a missing image', async () => {
    const { container } = await render(QualityReviewEvidence, { finding: { proposals: [{ before: 'Click here', proposed_value: 'Read the report' }] } })
    expect(container.textContent).toContain('Click here')
    expect(container.textContent).not.toContain('Image preview unavailable')
  })
  it.each([true, false])('is mounted in the real inbox and preserves editing and approval (legacy %s)', async (legacyApprovalControls) => {
    localStorage.clear(); sessionStorage.clear()
    const onDecide = vi.fn().mockResolvedValue(undefined)
    const finding = { id: 4, file: 'chart.docx', title: 'Image description', rule_id: '1.1.1', hasProposal: true, after: 'A chart', proposals: [{ thumb, locator: 'image 1', proposed_value: 'A chart', review_status: 'needs_review' }] }
    const { container } = await render(RemediationInbox, { queue: [finding], decisions: {}, onDecide, legacyApprovalControls, initialTab: 'needs-review' })
    expect(container.querySelector('[aria-label="Source evidence and proposed fixes"] img')).not.toBeNull()
    const editor = container.querySelector('[aria-label="Edit the proposed fix"]')
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(editor, 'Revenue decreased in 2024')
      editor.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(container.querySelector('[aria-label="Source comparison 1"]').textContent).toContain('Revenue decreased in 2024')
    const button = [...container.querySelectorAll('button')].find(button => button.textContent.includes(legacyApprovalControls ? 'Yes, apply fix' : 'Apply this fix'))
    await act(async () => button.click())
    expect(onDecide).toHaveBeenCalledWith(expect.objectContaining({ id: 4 }), expect.objectContaining({ state: 'accepted', value: 'Revenue decreased in 2024' }))
  })
})

it('distinguishes independently checked pixels from saved document verification', async () => {
  const {container} = await render(QualityReviewEvidence, {finding: {rule_id: '1.1.1', proposals: [{
    locator: 'pdf:fig:0:0', proposed_value: 'A blue circle',
    caption_validation: {approved: true, status: 'validated'},
  }]}})
  expect(container.textContent).toContain('Source meaning: independently checked')
  expect(container.textContent).toContain('Saved document verification is tracked separately')
})

it('does not call a blocked caption independently verified', async () => {
  const {container} = await render(QualityReviewEvidence, {finding: {rule_id: '1.1.1', proposals: [{
    locator: 'pdf:fig:0:0', proposed_value: 'Chart', automatic_write_blocked: true,
    caption_validation: {approved: true, status: 'validated'},
  }]}})
  expect(container.textContent).toContain('Source meaning: not independently verified')
})

it('labels source-grounded AI approval as judgment rather than verification', async () => {
  const {container} = await render(QualityReviewEvidence, {finding: {rule_id:'1.1.1', proposals:[{
    proposed_value:'A brown dog beside a tree', quality_source_review:{status:'ai_reviewed'},
  }]}})
  expect(container.textContent).toContain('AI reviewed against the source with supported claims')
  expect(container.textContent).toContain('not proof of semantic correctness or full accessibility')
  expect(container.textContent).not.toContain('Source meaning: independently checked')
})

it.each([true, false])('pairs recorded saved changes with explicit verification %s', async validated => {
 const { container } = await render(QualityReviewEvidence, { finding: { autoApplied: true, validated,
  rule_id: '1.3.2', before: 'Footer, heading, body', after: 'Heading, body, footer' } })
 expect(container.querySelector('[aria-label="Recorded before and after"]').textContent).toContain('Heading, body, footer')
 expect(container.textContent).toContain(validated ? 'recorded checks passed' : 'verification not confirmed')
 expect(container.textContent).toContain('does not demonstrate screen-reader order')
})
it('does not turn proposal validation into saved-document verification', async () => {
 const { container } = await render(QualityReviewEvidence, { finding: { validated: true,
  proposals: [{ proposed_value: 'A chart', quality_source_review: {status:'ai_reviewed'} }] } })
 expect(container.textContent).toContain('Proposed change · not a saved result')
 expect(container.textContent).not.toContain('Saved change · recorded checks passed')
})
it('shows a structured table proposal as a plan rather than a fabricated table image', async () => {
 const { container } = await render(QualityReviewEvidence, { finding: { rule_id:'1.3.1', proposals:[{
  kind:'pdf-table-header-scope', locator:'pdf:struct:1', subject_text:'Year',
  proposed_value: JSON.stringify({op:'header-scope',scope:'Column'}),
 }] } })
 expect(container.textContent).toContain('Associate “Year” with its column')
 expect(container.textContent).toContain('does not render its accessibility tags')
 expect(container.querySelector('img')).toBeNull()
})
it('mounts recorded saved comparisons in the real inbox without demanding approval', async () => {
 const { container } = await render(RemediationInbox, { queue:[{ id:'af:table', file:'table.docx',
  rule_id:'1.3.1', autoApplied:true, inspectionOnly:true, validated:true, before:'Header not associated', after:'Column header associated' }],
  decisions:{}, initialTab:'completed' })
 expect(container.querySelector('[aria-label="Recorded before and after"]')).not.toBeNull()
 expect(container.textContent).toContain('Column header associated')
 expect(container.textContent).toContain('inspection is required')
})
it('uses actual applied record evidence rather than relabelling a proposal as saved', async () => {
 const { container } = await render(QualityReviewEvidence, { finding:{ applied:true, after:'Unapplied proposed caption',
  proposals:[{proposed_value:'Unapplied proposed caption'}], _raw:{applied:true, approved_value:'Saved edited caption'} } })
 const saved = container.querySelector('[aria-label="Recorded before and after"]')
 expect(saved.textContent).toContain('Saved edited caption')
 expect(saved.textContent).not.toContain('Unapplied proposed caption')
 expect(container.textContent).toContain('Saved change · verification not confirmed')
})
it('never treats a verified flag without application as a saved change', async () => {
 const { container } = await render(QualityReviewEvidence, { finding:{verified:true, proposals:[{proposed_value:'Draft'}]} })
 expect(container.textContent).toContain('Proposed change · not a saved result')
 expect(container.querySelector('[aria-label="Recorded before and after"]')).toBeNull()
})
it('does not claim an auto inspection proposal is its saved excerpt', async () => {
 const { container } = await render(QualityReviewEvidence, { finding:{autoApplied:true, after:'Proposal only',
  _raw:{inspection_only:true}, proposals:[{proposed_value:'Proposal only'}]} })
 expect(container.querySelector('[aria-label="Recorded before and after"]').textContent).toContain('Saved change excerpt unavailable')
})
it('labels a saved change Original / Corrected, never Current or Proposed, and can defer the pair to its host', async () => {
 const finding = { applied:true, validated:true, rule_id:'1.4.5', before:'[image of text]', proposals:[{proposed_value:'Checklist text', review_status:'needs_review'}],
  _raw:{applied:true, validated:true, approved_value:'Checklist text'} }
 const { container } = await render(QualityReviewEvidence, { finding })
 const pair = container.querySelector('[aria-label="Recorded before and after"]')
 expect([...pair.querySelectorAll('strong')].map(s => s.textContent)).toEqual(['Original', 'Corrected'])
 expect(container.textContent).not.toMatch(/Current|Proposed|before approving/)
 const hosted = await render(QualityReviewEvidence, { finding, showSavedPair: false })
 expect(hosted.container.querySelector('[aria-label="Recorded before and after"]')).toBeNull()
})
