import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

afterEach(unmountAll)

const { default: RemediationInbox } = await import('./RemediationInbox.jsx')

// Filenames are prefixed a-/b- so the document sort (alphabetical by file, then id) yields a
// stable order for the interaction assertions below. Workflow stages under the top tabs:
//   id1 automatic record  → Needs review (legacy record without durable application evidence)
//   id2 hasProposal       → Needs review (a fresh AI draft, untouched)
//   id3 manual (no draft) → Manual fixes (needs a human to hand-edit)
const QUEUE = [
  { id: 1, file: 'a-brief.docx', title: 'DOCX · Heading contrast is too low', page: 1, severity: 'SERIOUS', rec: { action: 'auto' }, before: '#D9D9D9', after: '#2F6FED' },
  { id: 2, file: 'a-brief.docx', title: 'DOCX · Image needs alt text', page: 3, severity: 'CRITICAL', hasProposal: true, after: 'A bar chart of revenue' },
  { id: 3, file: 'b-policy.pdf', title: 'PDF · Scanned page, no text', rule_id: '1.1.1', severity: 'SERIOUS' },
]

let container, root
// The workspace layout + pane sizes persist in localStorage; clear it so each test starts from the
// two-panel default rather than inheriting a previous test's choice.
beforeEach(() => { try { localStorage.clear(); sessionStorage.clear() } catch {} ;({ container, root } = createTestRoot()) })

// Interaction tests use a deterministic document sort so the queue order is stable;
// the priority-default ordering (critical-first) is covered by remediationInboxModel.test.js.
const render = async (props) => { await act(async () => { root.render(createElement(RemediationInbox, { legacyApprovalControls: true, initialTab: 'needs-review', initialSort: 'document', initialGroup: 'issue', onOpenWord: () => {}, onRecheck: () => {}, ...props })) }) }
const click = async (el) => { await act(async () => { el.tagName === 'OPTION' ? (el.parentElement.value = el.value, el.parentElement.dispatchEvent(new Event('change', { bubbles: true }))) : el.dispatchEvent(new MouseEvent('click', { bubbles: true })) }) }
const btnByText = (t) => [...container.querySelectorAll('button, select[aria-label="Filter by status"] option')].find((b) => b.textContent.includes(t))
const detailHeading = () => container.querySelector('h3')?.textContent

describe('RemediationInbox — workflow-status queue', () => {

  it('groups the review queue by document by default', async () => {
    await render({ queue: QUEUE, decisions: {}, initialGroup: 'document' })
    const select = container.querySelector('select[aria-label="Group findings"]')
    expect(select.value).toBe('document')
    expect(container.textContent).toContain('Review queue')
    expect(container.textContent).toContain('📄 a-brief.docx')
  })

  // ── Clustered rows: many like findings, one row, one decision (PRD Tier C) ───────────────────
  // The failure this exists to stop: a production run put 265 findings into this queue, largely for
  // one criterion. A flat list that long is a rubber-stamping machine however good each row is.

  // Five 1.1.1 AI drafts — four .docx and one .pdf — which are ONE cluster, because format is not
  // part of the cluster key. Plus one 2.4.2, which is a different criterion and so its own row.
  const CLUSTERED = [
    { id: 101, file: 'a.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', severity: 'SERIOUS', hasProposal: true, after: 'alt A' },
    { id: 102, file: 'b.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', severity: 'SERIOUS', hasProposal: true, after: 'alt B' },
    { id: 103, file: 'c.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', severity: 'CRITICAL', hasProposal: true, after: 'alt C' },
    { id: 104, file: 'c.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', severity: 'SERIOUS', hasProposal: true, after: 'alt D' },
    { id: 105, file: 'e.pdf', title: 'PDF \u00b7 Image needs alt text', rule_id: '1.1.1', severity: 'SERIOUS', hasProposal: true, after: 'alt E' },
    { id: 106, file: 'f.docx', title: 'DOCX \u00b7 Document has no title', rule_id: '2.4.2', severity: 'MINOR', hasProposal: true, after: 'A title' },
  ]
  const rows = () => [...container.querySelectorAll('.rinbox-row')]

  it('groups like findings into one row by default, and says how big the group is', async () => {
    await render({ queue: CLUSTERED, decisions: {} })
    // Six findings, but five of them are one decision — so two rows, not six.
    expect(rows().length).toBe(2)
    const cluster = rows().find((r) => r.textContent.includes('5 findings'))
    expect(cluster).toBeTruthy()
    expect(cluster.textContent).toContain('4 documents')   // a, b, c (twice), e → 4 distinct files
    expect(cluster.textContent).toContain('1.1.1')
    // Queue chips explain the action; severity remains in priority filters and group detail.
    expect(cluster.textContent).toContain('Review needed')
    expect(cluster.textContent).not.toContain('1 critical')
  })

  it('spans document formats, and says on the row that it does', async () => {
    // Format is deliberately not part of the cluster key (the owner's call, 2026-09-01: keying on
    // it split the large single-criterion runs clustering exists to collapse). The compensating
    // control is disclosure — the breadth is stated, never implied.
    await render({ queue: CLUSTERED, decisions: {} })
    const cluster = rows().find((r) => r.textContent.includes('5 findings'))
    expect(cluster.textContent).toContain('DOCX and PDF')
    // …and the spoken label carries it too, so it is not a sighted-only fact.
    expect(cluster.getAttribute('aria-label')).toContain('DOCX and PDF')
  })

  it('selecting a cluster opens its first undecided finding for review', async () => {
    await render({ queue: CLUSTERED, decisions: {} })
    const cluster = rows().find((r) => r.textContent.includes('5 findings'))
    await click(cluster)
    expect(detailHeading()).toBe('Image needs alt text')
    // The representative is the first UNDECIDED member — id 101.
    expect(container.querySelector('#rinbox-row-101')).toBeTruthy()
  })

  it('the cluster row follows the decisions: its representative moves on as members are decided', async () => {
    await render({ queue: CLUSTERED, decisions: { 101: { state: 'accepted' } } })
    // An accepted finding LEAVES Needs review for Awaiting validation, so the group in this tab is
    // now three, and the row it shows has moved on from 101 to 102. Both halves matter: the count
    // tracks the tab it is in (never claiming work that is no longer here), and the representative
    // is always the next thing actually needing a decision.
    const cluster = rows().find((r) => r.textContent.includes('4 findings'))
    expect(cluster).toBeTruthy()
    expect(rows().some((r) => r.textContent.includes('5 findings'))).toBe(false)
    expect(container.querySelector('#rinbox-row-102')).toBeTruthy()
    expect(container.querySelector('#rinbox-row-101')).toBeFalsy()
  })

  it('expands to the individual findings when the reviewer wants them', async () => {
    await render({ queue: CLUSTERED, decisions: {} })
    const toggle = [...container.querySelectorAll('button')]
      .find((b) => /Expand the 5 findings/.test(b.getAttribute('aria-label') || ''))
    expect(toggle).toBeTruthy()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    // All five members are now individually selectable — including the .pdf one, so a reviewer who
    // does want to treat a format separately can still reach it.
    for (const id of [101, 102, 103, 104, 105]) {
      expect(container.querySelector(`#rinbox-row-${id}`)).toBeTruthy()
    }
  })

  it('a collapsed cluster is ONE step for the keyboard, not five', async () => {
    await render({ queue: CLUSTERED, decisions: {} })
    const list = container.querySelector('[aria-label^="Findings"]')
    // Two rows → "1 of 2", and ArrowDown moves to the next ROW, skipping the cluster's interior.
    expect(container.textContent).toContain('1 of 2')
    await act(async () => {
      list.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    })
    expect(container.textContent).toContain('2 of 2')
  })

  it('still offers the by-document lens, and switching to it un-clusters the queue', async () => {
    await render({ queue: CLUSTERED, decisions: {} })
    const select = container.querySelector('select[aria-label="Group findings"]')
    expect(select).toBeTruthy()
    expect(select.value).toBe('issue')
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set
    await act(async () => { setValue.call(select, 'document'); select.dispatchEvent(new Event('change', { bubbles: true })) })
    // By document, every finding is its own row again (grouped under their files).
    expect(rows().length).toBe(6)
  })

  // ── A decision is not made until it is SAVED (PRD §5.8) ──────────────────────────────────────
  // The failure mode these cover is specific and was real: `onDecide` was fire-and-forget, so a
  // server refusal still auto-advanced the reviewer, and the only signal was a banner rendered
  // outside this component. A reviewer working the queue at speed would never see it.

  it('does NOT advance when the decision fails to save, and says so where they pressed', async () => {
    const seen = []
    await render({ queue: QUEUE, decisions: {},
      onDecide: (f, d) => { seen.push([f.id, d.state]); return Promise.reject(new Error('The server rejected it.')) } })
    expect(detailHeading()).toBe('Heading contrast is too low')     // id1
    await click(btnByText('Yes, apply fix'))
    expect(seen).toEqual([[1, 'accepted']])
    // Still on the SAME finding — the queue did not move on.
    expect(detailHeading()).toBe('Heading contrast is too low')
    // …and the failure is stated inline, in an alert, next to the buttons that failed.
    const alert = container.querySelector('[role=alert]')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toContain('Not saved.')
    expect(alert.textContent).toContain('The server rejected it.')
    expect(alert.textContent).toContain('still waiting for your decision')
    // The decision controls are live again so the reviewer can retry.
    expect(btnByText('Yes, apply fix').disabled).toBe(false)
  })

  it('advances and shows no error when the decision saves', async () => {
    await render({ queue: QUEUE, decisions: {}, onDecide: () => Promise.resolve() })
    await click(btnByText('Yes, apply fix'))
    expect(detailHeading()).toBe('Image needs alt text')            // moved to id2
    expect(container.querySelector('[role=alert]')).toBeNull()
  })

  it('clears a failed decision\u2019s error when the reviewer moves to another finding', async () => {
    await render({ queue: QUEUE, decisions: {}, onDecide: () => Promise.reject(new Error('nope')) })
    await click(btnByText('Yes, apply fix'))
    expect(container.querySelector('[role=alert]')).toBeTruthy()
    await click(btnByText('Image needs alt text'))
    // The message belonged to that decision, not to the page.
    expect(container.querySelector('[role=alert]')).toBeNull()
  })

  // ── Batch decisions are scoped and named (PRD §6) ────────────────────────────────────────────

  it('names the criterion and the exact number of OTHER findings a batch would cover', async () => {
    // Two unresolved 1.1.1 findings in actionable lanes, plus a manual one that must NOT be swept in.
    const q = [
      { id: 10, file: 'a.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', hasProposal: true, after: 'A chart' },
      { id: 11, file: 'b.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', hasProposal: true, after: 'A photo' },
      { id: 12, file: 'c.pdf', title: 'PDF \u00b7 Scanned page, no text', rule_id: '1.1.1' },   // manual — excluded
    ]
    await render({ queue: q, decisions: {} })
    const batch = btnByText('Select matching proposals (2)')
    expect(batch).toBeTruthy()
    expect(batch.disabled).toBe(false)
    // ONE other actionable finding, not two — the manual one is not batchable.
    expect(batch.parentElement.textContent).toContain('this item and 1 similar finding')
    expect(batch.parentElement.textContent).toContain('across 2 files')
    await click(btnByText('Review matching items'))
    expect(container.textContent).toContain('manual, blocked and already-decided findings are excluded')
  })

  it('offers no global \u201capprove all AI drafts\u201d control anywhere', async () => {
    await render({ queue: QUEUE, decisions: {} })
    const labels = [...container.querySelectorAll('button')].map((b) => b.textContent.trim())
    // Every batch control must name its scope; none may sweep the whole queue.
    expect(labels.some((l) => /^Approve all\b/i.test(l))).toBe(false)
    expect(labels.some((l) => /approve all (ai )?drafts/i.test(l))).toBe(false)
    expect(labels.some((l) => /^(Approve|Accept) everything/i.test(l))).toBe(false)
  })

  it('offers a scoped visible-view bulk action with an impact summary', async () => {
    const q = [
      { id: 30, file: 'a.docx', title: 'DOCX · Image needs alt text', rule_id: '1.1.1', hasProposal: true, after: 'A chart' },
      { id: 31, file: 'b.docx', title: 'DOCX · Document has no title', rule_id: '2.4.2', hasProposal: true, after: 'Annual report' },
      { id: 32, file: 'c.pdf', title: 'PDF · Scanned page, no text', rule_id: '1.1.1' },
    ]
    const calls = []
    await render({ queue: q.map(f => ({ ...f, _raw: { corrected_artifact: 'none', proposal_digest: 'digest-test', decision_version: 0, proposal_snapshot_ids: [String(f.id)], source_revision: 'source' } })), decisions: {}, onDecide: (f, d) => calls.push([f.id, d.value]) })
    await click(btnByText('Bulk approve ready proposals'))
    const panel = container.querySelector('[aria-label="Select findings for approval"]')
    expect(panel.textContent).toContain('2 findings ready')
    await click(btnByText('Select all ready'))
    expect(panel.textContent).toContain('2 findings selected (2 review items) · 2 proposals · 2 files')
    await click(btnByText('Approve selected'))
    expect(calls).toEqual([])
    await click(btnByText('Confirm approval'))
    expect(calls).toEqual([[30, 'A chart'], [31, 'Annual report']])
  })

  it('lets reviewers page through matching proposals without approving or narrowing the group', async () => {
    const calls = []
    const queue = Array.from({ length: 8 }, (_, index) => ({
      id: 800 + index, file: `file-${index}.docx`, title: 'Image needs alt text',
      rule_id: '1.1.1', hasProposal: true, after: `Own proposal ${index}`, rationale: `Own reason ${index}`,
    }))
    await render({ queue, decisions: {}, onDecide: (...args) => calls.push(args) })
    await click(btnByText('Review matching items'))
    await click(btnByText('Next proposals'))
    const preview = container.querySelector('.matching-review-preview')
    expect(preview.textContent).toContain('Own proposal 7')
    expect(preview.textContent).toContain('Own reason 7')
    expect(preview.textContent).toContain('6–7 of 7')
    expect(btnByText('Select matching proposals (8)')).toBeTruthy()
    expect(calls).toEqual([])
  })

  it('reports a partial batch failure instead of claiming the whole cluster landed', async () => {
    const q = [
      { id: 20, file: 'a.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', hasProposal: true, after: 'A chart' },
      { id: 21, file: 'b.docx', title: 'DOCX \u00b7 Image needs alt text', rule_id: '1.1.1', hasProposal: true, after: 'A photo' },
    ]
    // The second write is refused; the first succeeds.
    await render({ queue: q.map(f => ({ ...f, _raw: { corrected_artifact: 'none', proposal_digest: 'digest-test', decision_version: 0, proposal_snapshot_ids: [String(f.id)], source_revision: 'source' } })), decisions: {},
      onDecide: (f) => (f.id === 21 ? Promise.reject(Object.assign(new Error('conflict'), { status: 409 })) : Promise.resolve()) })
    await click(btnByText('Select matching proposals (2)'))
    await click(btnByText('Select all ready'))
    await click(btnByText('Approve selected'))
    await click(btnByText('Confirm approval'))
    expect(container.textContent).toContain('Approval finished: 1 approved, 1 failed, 0 uncertain.')
    // Selection sits on the finding that failed, not past the whole cluster.
    expect(detailHeading()).toBe('Image needs alt text')
  })

  // ── Narrow viewports show one panel at a time (PRD §12) ──────────────────────────────────────

  it('at a narrow width shows the queue OR the finding, with a way back', async () => {
    // jsdom has no matchMedia; stub one that reports narrow so the component takes that branch.
    const real = window.matchMedia
    window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} })
    try {
      await render({ queue: QUEUE, decisions: {} })
      const rinbox = () => container.querySelector('.rinbox')
      const queuePane = () => container.querySelector('.rinbox-queuepane')
      const workspace = () => container.querySelector('.rinbox-workspace')
      expect(rinbox().getAttribute('data-narrow')).toBe('queue')
      // Both panels are never squeezed side by side. The review side stays MOUNTED (so the
      // selection and any unsaved edit survive the trip) but is `hidden`, which takes it out of
      // the accessibility tree and the tab order rather than merely shrinking it.
      expect(queuePane().hidden).toBe(false)
      expect(workspace().hidden).toBe(true)
      // No resizer either — there is nothing on screen to resize against.
      expect([...container.querySelectorAll('[role=separator]')]
        .some((n) => n.getAttribute('aria-label') === 'Resize the inbox')).toBe(false)
      await click(btnByText('Image needs alt text'))          // choosing a finding navigates to it
      expect(rinbox().getAttribute('data-narrow')).toBe('detail')
      expect(queuePane().hidden).toBe(true)
      expect(workspace().hidden).toBe(false)
      expect(detailHeading()).toBe('Image needs alt text')
      const back = btnByText('Back to queue')
      expect(back).toBeTruthy()
      await click(back)
      expect(rinbox().getAttribute('data-narrow')).toBe('queue')
      // The preview toggle is not offered — a third pane cannot help where two do not fit.
      expect(btnByText('Full document preview')).toBeFalsy()
    } finally {
      if (real) window.matchMedia = real; else delete window.matchMedia
    }
  })
  it('opens on Needs review and shows its first item', async () => {
    await render({ queue: QUEUE, decisions: {} })
    // Needs review holds the unconfirmed auto-fix (id1) and the AI draft (id2); the manual finding
    // (id3) is in Manual fixes. Document sort → id1 first.
    expect(detailHeading()).toBe('Heading contrast is too low')
    // The badge counts the rows the tab LISTS — both of them, the unconfirmed auto-fix included.
    // It used to show the bulk-approvable count instead (1 here), so the tab read one number and
    // opened onto another. The ready/inspection separation #1888 introduced is still made, in the
    // run-approval ledger and on the approve button; see hitlPanelCounts.test.jsx.
    expect(container.textContent).toContain('Approve AI suggestions 2')
    expect(container.textContent).toContain('Fix manually 1')
    // Progress is a separate lens, over the same three tasks: decisions and final outcomes.
    expect(container.querySelector('.rinbox-progress').textContent).toContain('0 of 3 tasks have a recorded decision')
  })

  it('partitions findings across the workflow tabs by pipeline stage', async () => {
    await render({ queue: QUEUE, decisions: {} })
    // Manual fixes holds only the manual-from-start finding; the needs-review items are not there.
    await click(btnByText('Fix manually'))
    expect(detailHeading()).toBe('Scanned page, no text')
  })

  it('selecting a row populates the detail pane instead of expanding in place', async () => {
    await render({ queue: QUEUE, decisions: {} })
    await click(btnByText('Image needs alt text'))   // id2, in the default Needs review tab
    expect(detailHeading()).toBe('Image needs alt text')
  })

  it('a queue row leads with the issue, shows the SC number as a compact pill, and the lane state quiet', async () => {
    await render({ queue: [{ id: 1, file: 'Clinical-Newsletter-79.docx', title: 'DOCX · Contrast minimum', page: 2, rule_id: '1.4.3', autoApplied: true }], initialTab: 'awaiting-validation', decisions: {} })
    const row = container.querySelector('.rinbox-row')
    expect(row.textContent).toContain('Contrast minimum')             // the issue is the dominant text
    expect(row.textContent).toContain('1.4.3')                        // the compact WCAG pill
    expect(row.textContent).toContain('Automatic fix')                // the lane state, quiet
    expect(row.textContent).not.toContain('Review automatic fix')     // the loud repeated pill is gone
  })

  it('acting on a finding calls onDecide and auto-advances to the next unresolved one', async () => {
    const calls = []
    await render({ queue: QUEUE, decisions: {}, onDecide: (f, d) => calls.push([f.id, d.state]) })
    await click(btnByText('Image needs alt text'))                   // id2, apply lane
    expect(detailHeading()).toBe('Image needs alt text')
    await click(btnByText('Yes, apply fix'))
    expect(calls).toEqual([[2, 'accepted']])
    // auto-advance moved the workspace to the next unresolved needs-review finding without a click
    expect(detailHeading()).toBe('Heading contrast is too low')      // id1, the remaining auto-fix
  })

  it('a manual finding shows guided steps and native-app actions, not an approve button', async () => {
    await render({ queue: QUEUE, decisions: {} })
    await click(btnByText('Fix manually'))
    await click(btnByText('Scanned page, no text'))
    expect(detailHeading()).toBe('Scanned page, no text')
    expect(container.textContent).toContain('Fix this in Acrobat Pro')  // pdf → Acrobat
    expect(btnByText('Upload & recheck')).toBeTruthy()
  })

  it('an approved finding moves to Awaiting validation; a rejected one to Completed', async () => {
    await render({ queue: QUEUE, decisions: { 2: { state: 'accepted' }, 3: { state: 'rejected' } } })
    expect(container.textContent).toContain('Awaiting verification 1')
    expect(container.textContent).toContain('Results 1')           // id3 (rejected → terminal)
    // id2 + id3 are decided (id1 auto-fix still needs review); only the rejection (id3) is a final
    // outcome — the approval (id2) is awaiting its outcome, never counted as done.
    const progress = container.querySelector('.rinbox-progress').textContent
    expect(progress).toContain('2 of 3 tasks have a recorded decision')
    expect(progress).toContain('1 of 3 tasks has a final outcome')
    expect(progress).toContain('1 awaiting outcome')
  })

  it('marks a finding "Not applicable" (out of scope), resolving it without a fix', async () => {
    const calls = []
    await render({ queue: QUEUE, decisions: {}, onDecide: (f, d) => calls.push(d) })
    await click(btnByText('Image needs alt text'))               // id2
    await click(btnByText('Not applicable'))
    expect(calls[0].state).toBe('not_applicable')
  })

  it('lets the reviewer edit the AI draft and save their version before continuing', async () => {
    const calls = []
    await render({ queue: QUEUE, decisions: {}, onDecide: (f, d) => calls.push(d) })
    await click(btnByText('Image needs alt text'))               // id2, apply lane, carries `after`
    expect(detailHeading()).toBe('Image needs alt text')
    const ta = container.querySelector('textarea[aria-label="Edit the proposed fix"]')
    expect(ta).toBeTruthy()
    // Edit through the native setter so React's controlled onChange fires.
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
    await act(async () => { setValue.call(ta, 'A revenue bar chart, 2021–2025'); ta.dispatchEvent(new Event('input', { bubbles: true })) })
    // The guided action carries the edited value and advances only after it is saved.
    await click(btnByText('Yes, apply fix'))
    expect(calls[0].state).toBe('accepted')
    expect(calls[0].value).toBe('A revenue bar chart, 2021–2025')
  })

  it('offers specific decision actions (no bare "Reject") and hides verification until a fix is saved', async () => {
    await render({ queue: QUEUE, decisions: {} })
    await click(btnByText('Image needs alt text'))               // id2, apply lane, unresolved
    expect(detailHeading()).toBe('Image needs alt text')
    expect(btnByText('No, needs manual work')).toBeTruthy()        // the specific outcome
    expect(btnByText('Defer')).toBeTruthy()
    // The ambiguous bare "Reject" button is gone.
    const bareReject = [...container.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Reject')
    expect(bareReject).toBe(false)
    // Verification (Written → Re-scan) is not shown before the decision is saved.
    expect(container.textContent).not.toContain('Re-scan')
  })

  it('shows the verification path (Approved → Re-scan) once a finding is saved, without certifying it', async () => {
    await render({ queue: QUEUE, decisions: { 2: { state: 'accepted' } } })
    await click(btnByText('Awaiting verification'))
    await click(btnByText('Image needs alt text'))
    expect(container.textContent).toContain('Re-scan')
    expect(container.textContent).not.toMatch(/certif/i)
  })

  it('always renders exactly the inbox and review panes', async () => {
    await render({ queue: QUEUE, decisions: {} })
    expect(detailHeading()).toBe('Heading contrast is too low')
    expect(container.textContent).toContain('Guided remediation')
    expect(container.textContent).not.toContain('Document preview')
    expect(container.querySelectorAll('.rinbox-queuepane, .rinbox-workspace').length).toBe(2)
  })

  it('search narrows the queue within the current tab', async () => {
    await render({ queue: QUEUE, decisions: {} })
    const input = container.querySelector('input[type=search]')
    // Drive the controlled input through the native value setter so React's onChange fires.
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    await act(async () => { setValue.call(input, 'alt text'); input.dispatchEvent(new Event('input', { bubbles: true })) })
    const rows = [...container.querySelectorAll('.rinbox-row')]
    expect(rows.some((r) => r.textContent.includes('Image needs alt text'))).toBe(true)
    expect(rows.some((r) => r.textContent.includes('Scanned page'))).toBe(false)
  })

  // ── The workspace remains TWO panels at every desktop layout ──
  const rinbox = () => container.querySelector('.rinbox')
  const sep = (label) => [...container.querySelectorAll('[role=separator]')].find((s) => s.getAttribute('aria-label') === label)

  it('defaults to a two-panel workspace — queue and review, with no third preview pane', async () => {
    await render({ queue: QUEUE, decisions: {} })
    expect(rinbox().getAttribute('data-layout')).toBe('two-column')
    expect(container.textContent).toContain('Guided remediation')
    expect(container.textContent).not.toContain('Document preview')
    expect(container.querySelector('[aria-label="Preview placement"]')).toBeNull()
  })

  it('dividers are keyboard-resizable (role=separator, Arrow keys change the split)', async () => {
    await render({ queue: QUEUE, decisions: {} })
    const d = sep('Resize the inbox')
    expect(d.getAttribute('aria-orientation')).toBe('vertical')
    const before = Number(d.getAttribute('aria-valuenow'))     // 28 by default
    await act(async () => { d.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })) })
    const after = Number(sep('Resize the inbox').getAttribute('aria-valuenow'))
    expect(after).toBeGreaterThan(before)
    // …and the widened inbox width is persisted.
    expect(Number(localStorage.getItem('acp.remediate.leftW'))).toBeGreaterThan(before)
  })

  // ── "Assigned to me" filter (#417 backend: per-file assignee) ──
  const hasBtn = (t) => [...container.querySelectorAll('button')].some((b) => b.textContent.includes(t))

  it('shows the "Assigned to me" control only when a signed-in reviewer + assign action are wired', async () => {
    await render({ queue: QUEUE, decisions: {} })                    // no myEmail / onAssign → dead control avoided
    expect(hasBtn('Assigned to me')).toBe(false)
    await render({ queue: QUEUE, decisions: {}, myEmail: 'me@x.com', onAssign: () => {} })
    expect(hasBtn('Assigned to me')).toBe(true)
  })

  it('assigns the selected document to the reviewer via onAssign(file, myEmail)', async () => {
    const calls = []
    await render({ queue: QUEUE, decisions: {}, myEmail: 'me@x.com', onAssign: (f, e) => calls.push([f, e]) })
    // Default selection is id1 (a-brief.docx), unassigned → the chip offers "+ Assign to me".
    await click(btnByText('Assign to me'))
    expect(calls).toEqual([['a-brief.docx', 'me@x.com']])
  })

  it('"Assigned to me" narrows the queue to documents assigned to the reviewer', async () => {
    const Q = [
      { id: 1, file: 'a.docx', title: 'DOCX · Alpha', hasProposal: true, after: 'x' },   // needs-review, assigned
      { id: 2, file: 'b.docx', title: 'DOCX · Beta', hasProposal: true, after: 'y' },     // needs-review, NOT assigned
    ]
    await render({ queue: Q, decisions: {}, myEmail: 'me@x.com', assignees: { 'a.docx': 'me@x.com' }, onAssign: () => {} })
    let rows = [...container.querySelectorAll('.rinbox-row')].map((r) => r.textContent)
    expect(rows.some((t) => t.includes('Alpha'))).toBe(true)
    expect(rows.some((t) => t.includes('Beta'))).toBe(true)
    await click(btnByText('Assigned to me'))                          // "Assigned to me (1)"
    rows = [...container.querySelectorAll('.rinbox-row')].map((r) => r.textContent)
    expect(rows.some((t) => t.includes('Alpha'))).toBe(true)          // assigned → stays
    expect(rows.some((t) => t.includes('Beta'))).toBe(false)          // unassigned → filtered out
  })

  it('shows an honest empty state when nothing in view is assigned to the reviewer', async () => {
    await render({ queue: QUEUE, decisions: {}, myEmail: 'me@x.com', assignees: {}, onAssign: () => {} })
    await click(btnByText('Assigned to me'))
    expect(container.textContent).toContain('Nothing in this view is assigned to you')
    expect(btnByText('Show all')).toBeTruthy()
  })

  it('isolates AI-assisted drafts without creating another workflow tab', async () => {
    await render({ queue: QUEUE, decisions: {} })
    const select = container.querySelector('select[aria-label="Filter by fix source"]')
    expect(select).toBeTruthy()
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set
    await act(async () => { setValue.call(select, 'ai'); select.dispatchEvent(new Event('change', { bubbles: true })) })
    const rows = [...container.querySelectorAll('.rinbox-row')]
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain('Image needs alt text')
    expect(rows[0].textContent).not.toContain('Heading contrast is too low')
    expect(sessionStorage.getItem('acp.remediate.filters.current.source')).toBe('ai')
  })

  it('keeps automatic and manual work available as the complementary fix-source view', async () => {
    await render({ queue: QUEUE, decisions: {} })
    const select = container.querySelector('select[aria-label="Filter by fix source"]')
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set
    await act(async () => { setValue.call(select, 'other'); select.dispatchEvent(new Event('change', { bubbles: true })) })
    const text = [...container.querySelectorAll('.rinbox-row')].map((row) => row.textContent).join(' ')
    expect(text).toContain('Heading contrast is too low')
    expect(text).not.toContain('Image needs alt text')
  })

  it('does not label a deterministic proposal as AI-assisted', async () => {
    const proposals = [
      { proposed_value: 'A chart summary', source: 'chart data (deterministic — from the document chart XML)' },
    ]
    await render({ queue: [{ id: 9, file: 'chart.docx', title: 'DOCX · Chart alternative',
      hasProposal: true, after: 'A chart summary', proposalSource: proposals[0].source, proposals }], decisions: {} })
    const select = container.querySelector('select[aria-label="Filter by fix source"]')
    expect(select.textContent).toContain('AI-assisted drafts (0)')
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set
    await act(async () => { setValue.call(select, 'ai'); select.dispatchEvent(new Event('change', { bubbles: true })) })
    expect(container.querySelectorAll('.rinbox-row')).toHaveLength(0)
    expect(container.textContent).toContain('No findings match these filters')
  })

  // ── Keyboard + screen-reader accessibility of the review queue ──
  const liveRegion = () => container.querySelector('[aria-live="polite"]')
  const queueList = () => container.querySelector('[aria-label^="Findings"]')
  const key = async (el, k) => { await act(async () => { el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })) }) }

  it('announces the selected finding and its N-of-M place in a polite live region', async () => {
    await render({ queue: QUEUE, decisions: {} })
    // Needs review holds id1 (auto-fix) + id2 (AI draft); document sort → id1 first.
    expect(liveRegion().getAttribute('role')).toBe('status')
    expect(liveRegion().textContent).toContain('Finding 1 of 2')
    expect(liveRegion().textContent).toContain('Heading contrast is too low')
    expect(liveRegion().textContent).toContain('a-brief.docx')
  })

  it('uses a roving tabindex — only the selected row is a Tab stop', async () => {
    await render({ queue: QUEUE, decisions: {} })
    const rows = [...container.querySelectorAll('.rinbox-row')]
    expect(rows.find((r) => r.getAttribute('aria-current') === 'true').tabIndex).toBe(0)
    expect(rows.find((r) => r.getAttribute('aria-current') !== 'true').tabIndex).toBe(-1)
  })

  it('ArrowDown moves the selection to the next finding and re-announces it', async () => {
    await render({ queue: QUEUE, decisions: {} })
    expect(detailHeading()).toBe('Heading contrast is too low')       // id1
    await key(queueList(), 'ArrowDown')
    expect(detailHeading()).toBe('Image needs alt text')              // id2
    expect(liveRegion().textContent).toContain('Finding 2 of 2')
  })

  it('keyboard navigation (j/k) moves focus to the newly-selected row', async () => {
    await render({ queue: QUEUE, decisions: {} })
    await key(queueList(), 'j')                                        // vim-style down
    const focused = container.querySelector('[aria-current="true"]')
    expect(document.activeElement).toBe(focused)
    expect(focused.textContent).toContain('Image needs alt text')     // advanced to id2
  })

  // ── Full-width current / proposed rows preserve long finding values ──
  it('shows current and proposed values for an alt-text finding', async () => {
    await render({ queue: [{ id: 1, file: 'a.docx', title: 'DOCX · Image needs alt text', rule_id: '1.1.1', hasProposal: true, before: '', after: 'A bar chart of Q3 revenue' }], decisions: {} })
    expect(container.textContent).toContain('Current')
    expect(container.textContent).toContain('Not recorded')
    expect(container.textContent).toContain('Proposed')
    expect(container.textContent).toContain('A bar chart of Q3 revenue')
    expect(btnByText('Copy current')).toBeTruthy()
    expect(btnByText('Copy proposed')).toBeTruthy()
  })

  it('filters by priority and format, and remembers the choices for the scan', async () => {
    await render({ scanId: 'scan-filter', queue: QUEUE, decisions: {} })
    const priority = container.querySelector('[aria-label="Filter by priority"]')
    const format = container.querySelector('[aria-label="Filter by file format"]')
    const setSelect = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set
    await act(async () => { setSelect.call(priority, 'critical'); priority.dispatchEvent(new Event('change', { bubbles: true })) })
    expect([...container.querySelectorAll('.rinbox-row')].every((r) => r.textContent.includes('Image needs alt text'))).toBe(true)
    await act(async () => { setSelect.call(format, 'docx'); format.dispatchEvent(new Event('change', { bubbles: true })) })
    expect(sessionStorage.getItem('acp.remediate.filters.scan-filter.priority')).toBe('critical')
    expect(sessionStorage.getItem('acp.remediate.filters.scan-filter.format')).toBe('docx')
    expect(btnByText('Clear filters')).toBeTruthy()
  })

  it('shows the proposed document title without an empty field', async () => {
    await render({ queue: [{ id: 1, file: 'a.docx', title: 'DOCX · Document has no title', rule_id: '2.4.2', hasProposal: true, before: null, after: 'Q3 Report' }], decisions: {} })
    expect(container.textContent).toContain('Current')
    expect(container.textContent).toContain('Not recorded')
    expect(container.textContent).toContain('Proposed')
    expect(container.textContent).toContain('Q3 Report')
  })

  it('does not restore the removed workflow tiles for a sequence finding', async () => {
    await render({ queue: [{ id: 1, file: 'a.docx', title: 'DOCX · Meaningful sequence', rule_id: '1.3.2', hasProposal: true, after: 'x',
      proposals: [{ seq: 1, text: 'Pull quote at the top' }, { seq: 2, text: 'Sidebar callout' }] }], decisions: {} })
    expect(container.textContent).toContain('Current')
    expect(container.textContent).toContain('Proposed')
    expect(container.textContent).not.toContain('Issue found')
  })

  it('distinguishes an empty queue from a filtered view with no matches', async () => {
    await render({ queue: [], decisions: {} })
    expect(container.textContent).toContain('All review items have recorded outcomes')
    await unmountAll(); ({ container, root } = createTestRoot())
    await render({ queue: QUEUE, decisions: {} })
    const input = container.querySelector('input[type=search]')
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    await act(async () => { setValue.call(input, 'definitely absent'); input.dispatchEvent(new Event('input', { bubbles: true })) })
    expect(container.textContent).toContain('No findings match “definitely absent”')
    expect(btnByText('Clear search')).toBeTruthy()
  })
})


it('shows unavailable and a retry after a failed approval check without claiming On', async () => {
  let retries = 0
  await render({ queue: QUEUE, decisions: {}, legacyApprovalControls: false, autoApprove: null, autoApproveError: 'The approval check timed out.', onAutoApproveRetry: () => { retries += 1 } })
  expect(container.textContent).toContain('Unavailable')
  expect(container.textContent).not.toContain('Checking…')
  const toggle = container.querySelector('[role="switch"]')
  expect(toggle.checked).toBe(false)
  expect(toggle.disabled).toBe(true)
  await click([...container.querySelectorAll('button')].find(button => button.textContent === 'Retry AI approval check'))
  expect(retries).toBe(1)
})

it('explains individual judgment with auto-apply on and preserves its manual action', async () => {
 const policy={enabled:true,supported:true,run_id:'run',source_revision:'source'}
 const row={...QUEUE[1],id:2,status:'pending',_raw:{corrected_artifact:'none',proposal_digest:'digest-test',automatic_approval:{state:'review_required',owner:'You',reason:'Diagram content requires your judgment.',run_id:'run',source_revision:'source'}}}
 await render({queue:[row],automaticApprovalPolicy:policy,autoApprove:true,legacyApprovalControls:false})
 expect(container.textContent).toContain('Diagram content requires your judgment.')
 expect(container.textContent).toContain('ACP automatically applies eligible AI suggestions')
 const apply=[...container.querySelectorAll('button')].find(button=>button.textContent === 'Review and apply')
 expect(apply).toBeTruthy()
 expect(apply.disabled).toBe(false)
})

describe('exceptions-only automatic review',()=>{
 it('defaults to real human authoring and keeps unknown AI items reachable without claiming fixed',async()=>{
  const queue=[{id:90,file:'manual-crop.docx',rule_id:'1.4.5',title:'Crop needs your description',status:'pending'},
   {id:91,file:'waiting-alt.docx',rule_id:'1.1.1',title:'Caption needs status check',status:'pending'}]
  await render({queue,legacyApprovalControls:false,autoApprove:true,initialTab:'review'})
  expect(container.querySelector('option[value="review"]').textContent).toBe('Needs your input (1)')
  expect(container.querySelector('.rinbox-queuepane').textContent).toContain('manual-crop.docx')
  expect(container.querySelector('.rinbox-queuepane').textContent).not.toContain('waiting-alt.docx')
  const filter=container.querySelector('select[aria-label="Filter by status"]')
  await act(async()=>{filter.value='status-check';filter.dispatchEvent(new Event('change',{bubbles:true}))})
  expect(container.querySelector('.rinbox-queuepane').textContent).toContain('waiting-alt.docx')
  expect(container.textContent).toContain('no confirmed automatic admission')
 })
})
it('does not infer human confirmation from missing recheck action for applied unverified automatic work',async()=>{
 await render({queue:[{id:95,file:'applied.docx',rule_id:'1.1.1',status:'approved',applied:true,validated:false,hasProposal:true,after:'Caption',automaticDisposition:{state:'blocked',responsibility:'check'}}],legacyApprovalControls:false,autoApprove:true,initialTab:'status-check',onRecheck:undefined})
 expect(container.textContent).toContain('Applied · verification incomplete')
 expect(container.textContent).toContain('No additional approval is needed')
 expect(container.textContent).not.toContain('This change requires human confirmation')
 expect(container.textContent).not.toContain('Human confirmation required')
 expect(container.textContent).not.toContain('This finding is verified')
})
it('genuine manual crop authoring remains explicitly a human task',async()=>{
 await render({queue:[{id:96,file:'crop.docx',rule_id:'1.4.5',status:'pending',title:'Describe visible crop'}],legacyApprovalControls:false,autoApprove:true,initialTab:'review',onRecheck:undefined})
 expect(container.textContent).toContain('fix it by hand in the source app')
})
it('admitted automatic fixes do not request human confirmation when no recheck callback is rendered',async()=>{
 const row={id:97,file:'queued.docx',scanId:'scan',rule_id:'1.1.1',status:'pending',hasProposal:true,after:'Caption',proposals:[{proposed_value:'Caption',source:'AI',model:'vision',model_call_id:'call'}],_raw:{corrected_artifact:'none',proposal_digest:'digest-test',finding_count:1,proposal_snapshot_ids:['snap'],source_revision:'source',decision_version:0,automatic_approval:{state:'checking',run_id:'run',source_revision:'source',proposal_snapshot_ids:['snap']}}}
 await render({queue:[row],automaticApprovalPolicy:{enabled:true,run_id:'run',source_revision:'source'},legacyApprovalControls:false,autoApprove:true,initialTab:'awaiting-validation',onRecheck:undefined})
 expect(container.textContent).toContain('No individual approval or human confirmation is needed now')
 expect(container.textContent).not.toContain('Human confirmation required')
})

describe('historical review controls', () => {
  it('disables application rather than offering an action that cannot save', async () => {
    const onDecide = () => { throw new Error('Historical decisions must not run') }
    await render({ queue: [QUEUE[1]], decisions: {}, readOnly: true, onDecide })
    const button = btnByText('Yes, apply fix')
    expect(button).toBeTruthy()
    expect(button.disabled).toBe(true)
  })
})

it('keeps a current-run review actionable after publication and explains the new version boundary', async () => {
  const calls = []
  await render({ queue: [QUEUE[1]], decisions: {}, afterRelease: true, readOnly: false,
    onDecide: async (finding, decision) => { calls.push([finding.id, decision.state]) },
  })
  expect(container.textContent).toContain('a new publication authorization')
  const button = btnByText('Yes, apply fix')
  expect(button.disabled).toBe(false)
  await click(button)
  expect(calls).toEqual([[2, 'accepted']])
})

it('shows a prose PDF tagging outline as manual guidance rather than an executable approval', async () => {
  await render({ queue: [{ id: 81, file: 'untagged.pdf', rule_id: '1.3.1', title: 'Info and Relationships',
    hasProposal: true, before: 'untagged PDF', after: 'Heading 1: Introduction; Heading 2: Details',
  }], decisions: {}, initialTab: 'manual' })
  expect(btnByText('Yes, apply fix')).toBeFalsy()
  expect(container.textContent).toContain('PDF accessibility editor')
  expect(container.textContent).toContain('approving it does not write a PDF structure tree')
})


it('can collapse and reopen the card without saving a decision', async () => {
 await render({queue: QUEUE, onDecide: () => { throw new Error('Must not save') }})
 const toggle = btnByText('Collapse remediation card')
 await click(toggle)
 expect(toggle.getAttribute('aria-expanded')).toBe('false')
 expect(container.querySelector('.rinbox-wrap > div').hidden).toBe(true)
 await click(toggle)
 expect(toggle.getAttribute('aria-expanded')).toBe('true')
 expect(container.querySelector('.rinbox-wrap > div').hidden).toBe(false)
})

it('does not offer apply for an unsupported PDF outline with a stale applied flag', async () => {
 await render({queue: [{id: 900, file: 'untagged.pdf', rule_id: 'WCAG 1.3.1', hasProposal: true, applied: true, validated: false, after: 'Heading 1 · Introduction (p.1)', status: 'pending'}], legacyApprovalControls: false, initialTab: 'all'})
 expect(btnByText('Review and apply')).toBeUndefined()
 expect(btnByText('Apply this fix')).toBeUndefined()
 expect(btnByText('Open in Word')).toBeTruthy()
})

it('removes saved AI language fixes with stale review reasons from Needs your input', async () => {
 const marker={state:'review_required',responsibility:'human',reason:'Proposal requires individual judgment or has no exact AI provenance',scan_id:'scan',run_id:'run',source_revision:'source',proposal_snapshot_ids:['snap']}
 const saved={id:701,file:'rights-notice.docx',scanId:'scan',rule_id:'3.1.2',status:'approved',applied:true,validated:false,hasProposal:true,after:'es',_raw:{corrected_artifact:'none',proposal_digest:'digest-test',scan_id:'scan',proposal_snapshot_ids:['snap'],automatic_approval:marker}}
 const manual={id:702,file:'manual-crop.pdf',rule_id:'1.4.5',status:'pending',manual:true,title:'Describe crop'}
 await render({queue:[saved,manual],automaticApprovalPolicy:{enabled:true,run_id:'run',source_revision:'source'},legacyApprovalControls:false,autoApprove:true,initialTab:'review'})
 expect(container.querySelector('.rinbox-queuepane').textContent).not.toContain('rights-notice.docx')
 expect(container.querySelector('.rinbox-queuepane').textContent).toContain('manual-crop.pdf')
 expect(container.querySelector('option[value="review"]').textContent).toBe('Needs your input (1)')
 const filter=container.querySelector('select[aria-label="Filter by status"]')
 await act(async()=>{filter.value='status-check';filter.dispatchEvent(new Event('change',{bubbles:true}))})
 expect(container.querySelector('.rinbox-queuepane').textContent).toContain('rights-notice.docx')
 expect(container.textContent).toContain('no additional approval is needed')
 expect(container.textContent).not.toContain('Proposal requires individual judgment')
 expect(container.textContent).not.toContain('This finding is verified')
})
