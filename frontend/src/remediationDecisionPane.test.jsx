import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import axe from 'axe-core'
import { createTestRoot, unmountAll } from './testRoots.js'

afterEach(unmountAll)

// The reviewer's DECISION experience: the redesigned guided pane (decision-first ordering, grounded
// before/after, obvious auto-fix approve/reject) and the copy-bug fix in the preview. Kept in its own
// file so it doesn't collide with RemediationInbox.test.jsx. Rendered WITHOUT a scanId — the
// deterministic, API-free path the inbox tests already rely on.
const { default: RemediationInbox } = await import('./RemediationInbox.jsx')
const { default: RemediationPreview } = await import('./RemediationPreview.jsx')

let container, root
beforeEach(() => { try { localStorage.clear() } catch {} ;({ container, root } = createTestRoot()) })
const renderInbox = async (props) => { await act(async () => { root.render(createElement(RemediationInbox, { initialTab: 'active', initialSort: 'document', onOpenWord: () => {}, ...props })) }) }
const renderPreview = async (props) => { await act(async () => { root.render(createElement(RemediationPreview, props)) }) }
const click = async (el) => { await act(async () => { el.tagName === 'OPTION' ? (el.parentElement.value = el.value, el.parentElement.dispatchEvent(new Event('change', { bubbles: true }))) : el.dispatchEvent(new MouseEvent('click', { bubbles: true })) }) }
const btnByText = (t) => [...container.querySelectorAll('button, select[aria-label="Filter by status"] option')].find((b) => b.textContent.includes(t))
const tab = (label) => [...container.querySelectorAll('[role=tab]')].find((b) => b.textContent.trim() === label)

// A docx contrast finding (1.4.3) with NO geometry — no page, locator or thumb. The colours match the
// worked example: #EEEEEE→#767676 is ~1.2:1 → ~4.5:1 on white.
const CONTRAST_APPLY = {
  id: 1, file: 'brief.docx', title: 'DOCX · Heading contrast is too low', rule_id: '1.4.3',
  hasProposal: true, before: '#EEEEEE on #FFFFFF', after: '#767676',
}
const CONTRAST_AUTO = { ...CONTRAST_APPLY, id: 2, hasProposal: false, autoApplied: true }

describe('Preview — the "invisible structure/metadata" copy bug is fixed', () => {
  it('a VISUAL finding with no geometry is NOT labelled "structure or metadata"', async () => {
    await renderPreview({ finding: CONTRAST_APPLY })   // 1.4.3, no page/locator/thumb, no scan
    expect(container.textContent).not.toContain('structure or metadata')
    // It says something honest about lacking coordinates instead.
    expect(container.textContent).toContain('can’t pinpoint this on the page')
    // …and does NOT offer a Structure mode for a visible finding.
    expect(tab('Structure')).toBeFalsy()
  })

  it('a genuinely STRUCTURAL finding keeps its structure framing', async () => {
    await renderPreview({ finding: { id: 9, file: 'annual-report.docx', rule_id: '2.4.2', after: 'Annual Report 2026' } })
    expect(container.textContent).toContain('structure or metadata')
    expect(tab('Structure')).toBeTruthy()
  })

  it('surfaces grounded contrast swatches (real colours + computed ratios) for the contrast finding', async () => {
    await renderPreview({ finding: CONTRAST_APPLY })
    expect(container.textContent).toContain('The quick brown fox')  // the affected text, at each colour
    expect(container.textContent).toContain('#EEEEEE')
    expect(container.textContent).toContain('#767676')
    expect(container.textContent).toContain('1.2:1')
    expect(container.textContent).toContain('4.5:1')
    expect(container.textContent).toContain('Fails')                // before, from the real ratio
    expect(container.textContent).toContain('Passes')               // after
  })
})

describe('Guided pane — decision-first ordering + grounded evidence', () => {
  it('puts the decision actions before the detail so no scrolling is needed to act', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {} })
    const detail = container.querySelector('.remediation-detail')
    const content = container.querySelector('.remediation-detail-content')
    const actions = container.querySelector('.remediation-detail-actions')
    expect(detail).toBeTruthy()
    expect(actions?.nextElementSibling).toBe(content)
    expect(detail.style.height).toBe('')
    expect(content.style.flex).toBe('')
    expect(content.style.overflowY).toBe('')
  })

  it('orders the pane Your task → Current / Proposed → Why this matters, with evidence collapsed', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {} })
    const text = container.textContent
    const iTask = text.indexOf('Your task')
    const iCurrent = text.indexOf('Current')
    const iWhy = text.indexOf('Why this matters')
    expect(iTask).toBeGreaterThan(-1)
    expect(iCurrent).toBeGreaterThan(iTask)
    expect(iWhy).toBeGreaterThan(iCurrent)
    const evDetails = [...container.querySelectorAll('details')]
      .find((d) => d.querySelector('summary')?.textContent.includes('Detection'))
    expect(evDetails).toBeTruthy()
    expect(evDetails.open).toBe(false)
    expect(text).not.toContain('Issue found')
  })

  it('the task line is plain, imperative and criterion-aware (a contrast decision)', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {} })
    expect(container.textContent).toContain('Review ACP’s contrast fix')      // criterion-aware
    expect(container.textContent).toContain('apply it')                        // imperative, apply lane
  })

  it('shows full-width current/proposed rows and a plain change sentence with the real values', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {} })
    expect(container.querySelector('.remediation-comparison')).toBeTruthy()
    expect(container.textContent).toContain('Current')
    expect(container.textContent).toContain('Proposed')
    expect(container.textContent).toContain('Text color changed from #EEEEEE to #767676')
    expect(container.textContent).toContain('Contrast increased from 1.2:1 to 4.5:1')
    expect(container.textContent).toContain('No text or layout changed')
    expect(container.querySelector('mark.remediation-change')?.textContent).toBe('767676')
  })

  it('uses criterion-specific impact guidance when the finding has no custom rationale', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {} })
    expect(container.textContent).toContain('Sufficient contrast makes text easier to read')
  })
})

describe('Guided pane — preserves the #412/#415 behaviours', () => {
  it('keeps the editable draft in the guided save-and-continue flow', async () => {
    const calls = []
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {}, onDecide: (f, d) => calls.push(d) })
    const ta = container.querySelector('textarea[aria-label="Edit the proposed fix"]')
    expect(ta).toBeTruthy()
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
    await act(async () => { setValue.call(ta, '#595959'); ta.dispatchEvent(new Event('input', { bubbles: true })) })
    await click(btnByText('Apply this fix'))
    expect(calls[0].state).toBe('accepted')
    expect(calls[0].value).toBe('#595959')
  })


  it('shows Yes and No at the top and collapses additional decisions under More options', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {}, onDecide: () => {} })
    const actions = container.querySelector('[role="group"][aria-label^="Decision actions for"]')
    expect(actions).toBeTruthy()
    const more = [...actions.querySelectorAll('details')].find(d => d.querySelector('summary')?.textContent === 'More options')
    expect(more.open).toBe(false)
    expect(btnByText('Apply this fix').closest('details')).toBeNull()
    expect(btnByText('Needs manual work').closest('details')).toBeNull()
    expect(btnByText('Edit proposed fix').closest('details')).toBe(more)
    await click(more.querySelector('summary'))
    for (const label of ['Apply this fix', 'Edit proposed fix', 'Needs manual work']) {
      expect([...actions.querySelectorAll('button')].some((button) => button.textContent.includes(label))).toBe(true)
    }
    expect(btnByText('Defer')).toBeFalsy()
    expect(btnByText('Not applicable')).toBeFalsy()
    await click(btnByText('Edit proposed fix'))
    expect(document.activeElement).toBe(container.querySelector('textarea[aria-label="Edit the proposed fix"]'))
  })

  it('keeps injected audit evidence but does not add a pipeline to the decision content', async () => {
    await renderInbox({
      queue: [CONTRAST_APPLY], decisions: {},
      renderDetailExtra: () => createElement('details', { 'aria-label': 'Audit trail' },
        createElement('summary', null, 'Audit trail'), createElement('p', null, 'Decision saved by reviewer')),
    })
    const audit = container.querySelector('details[aria-label="Audit trail"]')
    expect(audit).toBeTruthy()
    expect(audit.open).toBe(false)
    expect(container.querySelector('.remediation-doc-progress')).toBeNull()
  })

  it('has no automated accessibility violations in the active decision workspace', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {}, onDecide: () => {} })
    const results = await axe.run(container, { rules: { region: { enabled: false } } })
    expect(results.violations).toEqual([])
  })

  it('hides verification until saved, then shows Re-scan without a certification claim', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: {} })
    expect(container.textContent).not.toContain('Re-scan')                    // capital-R only after save
    await unmountAll(); ({ container, root } = createTestRoot())
    await renderInbox({ queue: [CONTRAST_APPLY], decisions: { 1: { state: 'accepted' } } })
    await click(btnByText('Awaiting verification'))
    await click(btnByText('Heading contrast is too low'))
    expect(container.textContent).toContain('Re-scan')
    expect(container.textContent).not.toMatch(/certif/i)
  })
})

describe('Guided pane — auto-fix rows get an obvious, honestly-labelled decision', () => {
  it('opens Publish without recording skipped inspection as acceptance', async () => {
    const decisions = {}, writes = [], destinations = []
    await renderInbox({ queue: [CONTRAST_AUTO], decisions,
      onDecide: (f, d) => writes.push([f.id, d]), legacyApprovalControls: true, onPublish: () => destinations.push('publish') })
    const publish = btnByText('Skip inspection and publish')
    expect(container.querySelector('.rinbox-wrap > div').firstElementChild.contains(publish)).toBe(true)
    expect([...container.querySelectorAll('button')].filter(button => button.textContent.includes('Skip inspection and publish'))).toHaveLength(1)
    await click(publish)
    expect(destinations).toEqual(['publish'])
    expect(writes).toEqual([])
    expect(decisions).toEqual({})
    expect(btnByText('Mark inspected')).toBeTruthy()
  })
  it('offers Publish beside pending work but never from read-only history', async () => {
    await renderInbox({ queue: [CONTRAST_APPLY], legacyApprovalControls: true, onPublish: () => {} })
    expect(btnByText('Skip inspection and publish')).toBeTruthy()
    await renderInbox({ queue: [CONTRAST_AUTO], readOnly: true, legacyApprovalControls: true, onPublish: () => {} })
    expect(btnByText('Skip inspection and publish')).toBeFalsy()
  })

  it('offers mark-inspected and a "This looks wrong" flag (no editable draft)', async () => {
    const calls = []
    // An UNacknowledged auto-fix awaits the reviewer's confirmation, so it sits in Needs review (the
    // default tab) — not Awaiting validation — and is selected on open.
    await renderInbox({ queue: [CONTRAST_AUTO], decisions: {}, onDecide: (f, d) => calls.push([f.id, d.state]) })
    expect(btnByText('Mark inspected \u2192')).toBeTruthy()
    expect(btnByText('This looks wrong')).toBeTruthy()
    // The change is already applied — there is no edit-and-apply draft for it.
    expect(container.querySelector('textarea[aria-label="Edit the proposed fix"]')).toBeNull()
    await click(btnByText('Mark inspected \u2192'))
    expect(calls).toContainEqual([2, 'accepted'])
  })

  it('the "This looks wrong" flag routes through onDecide as a rejection', async () => {
    const calls = []
    await renderInbox({ queue: [CONTRAST_AUTO], decisions: {}, onDecide: (f, d) => calls.push([f.id, d.state]) })
    await click(btnByText('This looks wrong'))
    expect(calls).toContainEqual([2, 'rejected'])
  })
})

it('unifies applying, publishing, and a real green automatic approval switch', async () => {
  const changes=[]
  await renderInbox({ queue:[CONTRAST_APPLY], autoApprove:true, onAutoApproveChange:enabled=>changes.push(enabled), onPublish:()=>{}, onDecide:()=>{} })
  expect(btnByText('Skip inspection and publish')).toBeFalsy()
  expect(btnByText('Publish saved copies')).toBeFalsy()
  expect(btnByText('View run readiness')).toBeFalsy()
  const control=container.querySelector('[role="switch"][aria-label="Auto-apply AI fixes"]')
  expect(control.checked).toBe(true)
  expect(control.closest('label').classList.contains('is-on')).toBe(true)
  expect(control.closest('.run-approval-actions')).toBeTruthy()
  await click(control)
  expect(changes).toEqual([false])
})
it('shows admitted automatic checking as Processing while retaining manual human input', async () => {
 const policy={enabled:true,supported:true,run_id:'run',source_revision:'source'}
 const proposal={...CONTRAST_APPLY,rule_id:'1.1.1',ruleId:'1.1.1',status:'pending',proposals:[{proposed_value:'#767676',source:'AI'}],_raw:{finding_count:1,proposal_snapshot_ids:['snapshot'],source_revision:'source',decision_version:0,auto_approval_status:'checking',auto_approval_run_id:'run',auto_approval_source_revision:'source'}}
 const manual={id:'manual',file:'manual.docx',rule_id:'1.4.5',title:'Needs manual fix',status:'pending',hasProposal:false}
 await renderInbox({queue:[proposal,manual],autoApprove:true,automaticApprovalPolicy:policy})
 const queues=container.querySelector('[aria-label="Review queues"]')
 expect(queues.textContent).toContain('Needs your input1')
 expect(queues.textContent).toContain('Processing1')
 expect(queues.textContent).toContain('Results0')
 await click([...queues.querySelectorAll('button')].find(button=>button.textContent.startsWith('Processing')))
 expect(container.textContent).toContain('Checking automatic eligibility')
 expect(container.textContent).toContain('not yet an applied or verified fix')
 await renderInbox({queue:[{...proposal,status:'verification_failed'},manual],autoApprove:true,automaticApprovalPolicy:policy})
 expect(container.querySelector('[aria-label="Review queues"]').textContent).toContain('Needs your input1')
 // Contract C3: a failed row whose exact-scope marker says a job is running ('checking') is ACP's
 // retry — one task, in Processing — not also a status check.
 expect(container.querySelector('[aria-label="Review queues"]').textContent).toMatch(/Processing✓?1/)
 expect(container.querySelector('[aria-label="Review queues"]').textContent).toContain('Status checks0')
 expect(container.querySelector('[aria-label="Review queues"]').textContent).toContain('Results0')
})
it('does not claim review decisions or verified fixes when only automatic checks are queued', async () => {
 const policy={enabled:true,supported:true,run_id:'run',source_revision:'source'}
 const proposal={...CONTRAST_APPLY,rule_id:'1.1.1',ruleId:'1.1.1',status:'pending',proposals:[{proposed_value:'#767676',source:'AI'}],_raw:{finding_count:1,proposal_snapshot_ids:['snapshot'],source_revision:'source',decision_version:0,auto_approval_status:'checking',auto_approval_run_id:'run',auto_approval_source_revision:'source'}}
 await renderInbox({queue:[proposal],autoApprove:true,automaticApprovalPolicy:policy,initialTab:'review'})
 expect(container.textContent).toContain('Automatic checks are queued.')
 expect(container.textContent).not.toContain('Review decisions saved')
 expect(container.textContent).not.toContain('All review items are complete')
})
