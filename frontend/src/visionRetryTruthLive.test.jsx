// The live Remediation activity panel, mounted for real, fed the production sequence (sanitized,
// synthetic): a DOCX's single 1.1.1 image gets an AI draft that only needs a person to confirm
// its meaning, a second retry is queued, a person approves the 1.4.5 replacement of that same
// image, the saved copy is verified, and the queued retry then finds its input changed and is
// cancelled. The screen used to call the draft "saved corrections" and the cancelled retry
// "AI generation paused", and kept a stale notice for the document.
//
// Events go through mergeRemediationEvents / addRemediationEvent — the same ingestion
// useRemediationRun uses for the history page and the stream — in the wire shape the server
// projects: seq, kind, document, document_ref, correlation_id (the run), detail (item_id binds the
// review item), occurred_at, activity_stage.
import { act } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import { RemediationActivityPanel } from './RemediationOpsPanel.jsx'
import { addRemediationEvent, mergeRemediationEvents } from './remediationEventFeed.js'

afterEach(unmountAll)

const FILE = 'synthetic-brochure.docx'
const REF = 'docref-synthetic-1'
const RUN = 'run-synthetic-1'
const OTHER_RUN = 'run-synthetic-2'
const ITEM = 'item-synthetic-111'
const OTHER_ITEM = 'item-synthetic-222'
const snapshot = { scan_id: 'scan-synthetic', batch_id: RUN, run_id: RUN, state: 'processing', terminal: false }
const at = seq => `2026-09-10T10:${String(seq).padStart(2, '0')}:00Z`
const wire = (seq, kind, detail = {}, extra = {}) => ({ seq, kind, document: FILE, document_ref: REF, correlation_id: RUN,
  detail, occurred_at: at(seq), material: true, ...extra })
const replacedWire = (seq, item = ITEM, extra = {}) => wire(seq, 'remediate.review_target_replaced',
  { rule_id: '1.1.1', removed_by_rule_id: '1.4.5', finding_count: 1, item_id: item }, { activity_stage: 'review', ...extra })

// Current emitters: recovered / pending / obsolete carry item_id; the replacement carries it too.
const generated = [
  wire(1, 'remediate.ai_request_started', { processing_zone: 'cloud', model: 'synthetic-vision', provider: 'synthetic' }, { activity_stage: 'draft_generation' }),
  wire(2, 'remediate.ai_request_finished', { processing_zone: 'cloud', model: 'synthetic-vision', provider: 'synthetic', status: 'ok' }, { activity_stage: 'draft_generation' }),
  wire(3, 'remediate.vision_retry_recovered', { retry: 1, drafts: 1, awaiting_review: 1, uncertain: 0, missing: 0, coverage_complete: true, document_write: false, item_id: ITEM }, { activity_stage: 'draft_generation' }),
  wire(4, 'remediate.vision_retry_pending', { retry: 2, item_id: ITEM }, { activity_stage: 'draft_generation' }),
  wire(5, 'remediate.fix_applied', { fixes: 1, criteria: ['1.4.5'] }, { activity_stage: 'document_write' }),
  wire(6, 'remediate.verified', { fixes: 1, criteria: ['1.4.5'] }, { activity_stage: 'verification' }),
]
const obsolete = wire(8, 'remediate.vision_retry_obsolete', { retry: 2, reason_code: 'vision_retry_input_changed', no_ai_request: true, item_id: ITEM }, { activity_stage: 'draft_generation' })
const replaced = replacedWire(9)

// Historical records: no item_id, recovered carries only {drafts}.
const historical = [
  wire(3, 'remediate.vision_retry_recovered', { retry: 1, drafts: 1 }),
  wire(4, 'remediate.vision_retry_pending', { retry: 2 }),
  generated[4], generated[5],
]
const storedBlocked = wire(7, 'remediate.vision_retry_blocked', { retry: 2, reason_code: 'vision_recovery_unresolved' })
// The same stored row as the server projects it at read time. The projection cannot prove that no
// provider request was made, so it carries no no_ai_request.
const projectedBlocked = wire(7, 'remediate.vision_retry_blocked', { retry: 2, reason_code: 'vision_retry_input_changed',
  recorded_reason_code: 'vision_recovery_unresolved', projection: 'historical_obsolete_retry' }, { activity_stage: 'draft_generation' })

async function mount(wireEvents, rows = []) {
  const events = mergeRemediationEvents([], wireEvents)
  const { root, container } = createTestRoot()
  await act(async () => root.render(<RemediationActivityPanel snapshot={snapshot} events={events} rows={rows} />))
  return { container, events, root }
}
const history = container => [...container.querySelectorAll('ol[aria-label="Recent remediation activity"] > li')].map(li => li.textContent)
const notices = container => [...container.querySelectorAll('.remaining-work-status li')].map(li => li.textContent)

describe('vision retry truth in the mounted live activity panel', () => {
  it('describes a historical draft neutrally: no document edit, and no invented review gate', async () => {
    const { container } = await mount(historical.slice(0, 1))
    const [line] = history(container)
    expect(line).toContain(`AI image description drafted for ${FILE} · not yet written to the document`)
    expect(line).not.toMatch(/waiting for your review|saved corrections|recovered|verif/i)
  })

  it('says "waiting for your review" only with awaiting_review evidence, and uncertain drafts need checking', async () => {
    const { container } = await mount([wire(3, 'remediate.vision_retry_recovered', { retry: 1, drafts: 2, awaiting_review: 1, uncertain: 1, missing: 2, document_write: false, item_id: ITEM })])
    const [line] = history(container)
    expect(line).toContain(`AI image description drafted for ${FILE} · waiting for your review — not yet written to the document`)
    expect(line).toContain('1 draft needs checking')
    expect(line).toContain('2 images still need a description')
    expect(line).not.toMatch(/valid|verified|usable/i)
    const current = notices(container).join(' | ')
    expect(current).toContain('Image description still needed')
    expect(current).toContain('2 images in this document still need a description')
    const zero = await mount([wire(3, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 0, uncertain: 0, missing: 0, item_id: ITEM })])
    expect(history(zero.container)[0]).toContain(`AI image description drafted for ${FILE} · not yet written to the document`)
    expect(history(zero.container)[0]).not.toContain('waiting for your review')
  })

  it('the full sequence ends with no pause, no saved-corrections claim and no current notice, and keeps the obsolete line', async () => {
    const { container, events } = await mount([...generated, obsolete, replaced])
    const text = container.textContent
    expect(text).not.toMatch(/paused/i)
    expect(text).not.toMatch(/saved corrections/i)
    expect(container.querySelector('.remaining-work-status')).toBeNull()
    const lines = history(container)
    expect(lines).toHaveLength(8)
    expect(lines.filter(line => line.includes(`Earlier image-description retry for ${FILE} cancelled · the saved input changed (the document or its assessed source was updated). No AI request was made.`))).toHaveLength(1)
    expect(lines.some(line => line.includes(`WCAG 1.1.1 review for ${FILE} no longer applies`))).toBe(true)
    expect(lines.some(line => /cancelled.*approved/.test(line))).toBe(false)
    expect(events.find(event => event.obsolete).tone).toBe('neutral')
    expect(container.querySelector('li[data-activity-stage="draft_generation"]')).not.toBeNull()
    expect(container.querySelector('li[data-activity-stage="document_write"]')).not.toBeNull()
  })

  it('the historical sequence, as re-projected, ends settled and never claims a request was or was not made', async () => {
    const { container, events } = await mount([...historical, projectedBlocked])
    expect(container.textContent).not.toMatch(/paused|saved corrections/i)
    expect(container.querySelector('.remaining-work-status')).toBeNull()
    const [line] = history(container)
    expect(line).toContain(`Earlier image-description retry for ${FILE} cancelled · the saved input changed (the document or its assessed source was updated).`)
    expect(line).not.toContain('No AI request was made')
    expect(events.find(event => event.id === '7').tone).toBe('neutral')
  })

  it('grouping by document keeps the obsolete line and does not lead with the settled retry', async () => {
    const { container } = await mount([...generated, obsolete, replaced])
    await act(async () => [...container.querySelectorAll('button')].find(b => b.textContent === 'Group by document').click())
    const groups = container.querySelectorAll('ol[aria-label="Recent remediation activity"] > li')
    expect(groups).toHaveLength(1)
    expect(groups[0].className).not.toMatch(/attention|error/)
    expect(groups[0].textContent).toContain('No AI request was made.')
    expect(groups[0].textContent).toContain('7 other updates for this document')
  })

  it('a stored genuine block stays paused and current until its own later same-run retry record settles it', async () => {
    const before = await mount([...historical, storedBlocked])
    expect(before.container.textContent).toContain(`AI generation for ${FILE} paused`)
    expect(notices(before.container).join(' ')).toContain('AI generation needs checking')
    // Another run's cancelled retry for the same image does not settle this run's notice.
    const otherRun = await mount([...historical, storedBlocked, { ...wire(8, 'remediate.vision_retry_obsolete', { reason_code: 'vision_retry_run_inactive', no_ai_request: true }), correlation_id: OTHER_RUN }])
    expect(notices(otherRun.container).join(' ')).toContain('AI generation needs checking')
    // A record from no identifiable run is not proof either.
    const runless = await mount([...historical, storedBlocked, { ...obsolete, correlation_id: null }])
    expect(notices(runless.container).join(' ')).toContain('AI generation needs checking')
    // Its own later same-run chain record settles it. Blocks never record an item id, so the
    // successor's item id (the run's one 1.1.1 chain for this document) is not a mismatch.
    // A recovery without proven coverage is not enough.
    const unproven = await mount([...historical, storedBlocked, wire(8, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 0, uncertain: 0, missing: 0, item_id: ITEM })])
    expect(notices(unproven.container).join(' ')).toContain('AI generation needs checking')
    for (const later of [obsolete, wire(8, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 0, uncertain: 0, missing: 0, coverage_complete: true, item_id: ITEM })]) {
      const own = await mount([...historical, storedBlocked, later])
      expect(own.container.querySelector('.remaining-work-status')).toBeNull()
      expect(own.container.textContent).toContain(`AI generation for ${FILE} paused`) // history is kept
    }
  })

  it('a genuine budget block still reads as paused and stays current', async () => {
    const { container, events } = await mount([...generated, wire(7, 'remediate.vision_retry_blocked', { retry: 2, reason_code: 'vision_budget_exhausted' }), replaced])
    expect(container.textContent).toContain(`Image description for ${FILE} paused · the saved AI spending allowance is exhausted`)
    expect(events.find(event => event.id === '7').tone).toBe('attention')
    expect(notices(container).join(' ')).toContain('AI spending allowance exhausted')
  })

  it('an unrelated 1.4.3 review notice for the same file stays after the image notice settles', async () => {
    const rows = [{ id: 31, file: FILE, rule_id: '1.4.3', status: 'pending', hasProposal: true, after: 'Darker text colour' }]
    const { container } = await mount([...generated, obsolete, replaced], rows)
    const current = notices(container).join(' | ')
    expect(current).toContain('Your review needed')
    expect(current).toContain('1 review item')
    expect(current).not.toMatch(/AI retry queued|AI generation|Image description still needed|paused/)
    expect(container.querySelector('[aria-label="Remaining work item counts"]').textContent).toContain('1 need your input')
  })

  it('mixed targets: replacing one image never settles another image in the same document', async () => {
    const { container } = await mount([
      wire(3, 'remediate.vision_retry_recovered', { drafts: 0, awaiting_review: 0, uncertain: 0, missing: 1, item_id: ITEM }),
      wire(4, 'remediate.vision_retry_recovered', { drafts: 0, awaiting_review: 0, uncertain: 0, missing: 2, item_id: OTHER_ITEM }),
      replacedWire(5, ITEM),
    ])
    const current = notices(container).join(' | ')
    expect(current).not.toContain('1 image in this document')
    expect(current).toContain('2 images in this document still need a description')
    // A replacement for another rule, even naming the other item, settles nothing.
    const otherRule = await mount([
      wire(4, 'remediate.vision_retry_recovered', { drafts: 0, awaiting_review: 0, missing: 2, item_id: OTHER_ITEM }),
      wire(5, 'remediate.review_target_replaced', { rule_id: 'SC_1_4_3', removed_by_rule_id: '1.4.5', finding_count: 1, item_id: OTHER_ITEM }),
    ])
    expect(notices(otherRule.container).join(' ')).toContain('2 images in this document still need a description')
  })

  it('cross-run: a later event from another run never settles this run\'s notice', async () => {
    const pending = wire(4, 'remediate.vision_retry_pending', { retry: 2, item_id: ITEM })
    for (const later of [replacedWire(9, ITEM, { correlation_id: OTHER_RUN }), { ...obsolete, correlation_id: OTHER_RUN }]) {
      const { container } = await mount([pending, later])
      expect(notices(container).join(' ')).toContain('AI retry queued')
      expect(history(container)).toHaveLength(2)
    }
    // A replacement with no run binding at all is not proof either.
    const unbound = await mount([pending, replacedWire(9, ITEM, { correlation_id: null })])
    expect(notices(unbound.container).join(' ')).toContain('AI retry queued')
    const same = await mount([pending, replacedWire(9, ITEM)])
    expect(same.container.querySelector('.remaining-work-status')).toBeNull()
  })

  it('out-of-order arrival: only LATER bound events settle a current notice', async () => {
    let events = []
    events = addRemediationEvent(events, wire(8, 'remediate.vision_retry_pending', { retry: 2, item_id: ITEM }), 8)
    events = addRemediationEvent(events, wire(5, 'remediate.vision_retry_obsolete', { reason_code: 'vision_retry_review_changed', no_ai_request: true, item_id: ITEM }), 5)
    events = addRemediationEvent(events, replacedWire(6), 6)
    events = addRemediationEvent(events, wire(7, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 1, missing: 0, item_id: ITEM }), 7)
    const { root, container } = createTestRoot()
    await act(async () => root.render(<RemediationActivityPanel snapshot={snapshot} events={events} />))
    expect(notices(container).join(' ')).toContain('AI retry queued')
    expect(history(container)).toHaveLength(4)
    events = addRemediationEvent(events, wire(9, 'remediate.vision_retry_obsolete', { reason_code: 'vision_retry_run_inactive', no_ai_request: true, item_id: ITEM }), 9)
    await act(async () => root.render(<RemediationActivityPanel snapshot={snapshot} events={events} />))
    expect(container.querySelector('.remaining-work-status')).toBeNull()
    expect(history(container)[0]).toContain('the run is no longer active. No AI request was made.')
  })

  it('a later event for ANOTHER document never settles this document', async () => {
    const otherObsolete = { ...obsolete, document: 'synthetic-other.docx', document_ref: 'docref-synthetic-2' }
    const { container } = await mount([wire(4, 'remediate.vision_retry_pending', { retry: 2, item_id: ITEM }), otherObsolete])
    expect(notices(container).join(' ')).toContain('AI retry queued')
  })
})

// Phase 3: the parent's reproduced sequences, through the real reducers and the mounted panel.
describe('phase 3 binding: run and target identity, delivery, unknown coverage', () => {
  const at = (seq, kind, detail, run) => ({ seq, kind, document: FILE, document_ref: REF, correlation_id: run, detail, occurred_at: `2026-09-10T11:${String(seq).padStart(2, '0')}:00Z` })
  const denied = (run, item) => at(1, 'remediate.vision_retry_blocked', { reason_code: 'vision_provider_access_denied', item_id: item }, run)
  const replacedBy = (run, item) => at(2, 'remediate.review_target_replaced', { rule_id: '1.1.1', removed_by_rule_id: '1.4.5', finding_count: 1, item_id: item }, run)

  it('the parent sequence: another run replacing another image keeps the block current, in the DOM and as group lead', async () => {
    const { container } = await mount([denied('run-A', '101'), replacedBy('run-B', '202')])
    expect(notices(container).join(' ')).toContain('AI provider access denied')
    expect(history(container)).toHaveLength(2)
    await act(async () => [...container.querySelectorAll('button')].find(b => b.textContent === 'Group by document').click())
    const [group] = container.querySelectorAll('ol[aria-label="Recent remediation activity"] > li')
    expect(group.className).toContain('remops-activity-attention')
    expect(group.querySelector('.remops-activity-event').textContent).toContain('paused · saved AI provider access was denied')
  })

  it.each([
    ['same run, different image', 'run-A', '202'],
    ['different run, same image', 'run-B', '101'],
    ['replacement with no run binding', null, '101'],
    ['replacement with no item binding', 'run-A', undefined],
  ])('%s never settles the block', async (_, run, item) => {
    const { container } = await mount([denied('run-A', '101'), replacedBy(run, item)])
    expect(notices(container).join(' ')).toContain('AI provider access denied')
  })

  it('the same run replacing the same image settles it, and the grouped lead is no longer the block', async () => {
    const { container } = await mount([denied('run-A', '101'), replacedBy('run-A', '101')])
    expect(container.querySelector('.remaining-work-status')).toBeNull()
    await act(async () => [...container.querySelectorAll('button')].find(b => b.textContent === 'Group by document').click())
    const [group] = container.querySelectorAll('ol[aria-label="Recent remediation activity"] > li')
    expect(group.className).not.toMatch(/attention|error/)
    expect(group.textContent).toContain('paused') // the block stays in the group's history
  })

  it('a blocked image is settled by its own later recovery, not by a recovery of another image', async () => {
    const recovered = (seq, item) => at(seq, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 0, uncertain: 0, missing: 0, coverage_complete: true, document_write: false, item_id: item }, 'run-A')
    const other = await mount([denied('run-A', '101'), recovered(3, '202')])
    expect(notices(other.container).join(' ')).toContain('AI provider access denied')
    const own = await mount([denied('run-A', '101'), recovered(3, '101')])
    expect(own.container.querySelector('.remaining-work-status')).toBeNull()
  })

  it('delivery of a copy with an unresolved caption settles nothing', async () => {
    const rows = [{ id: 101, file: FILE, rule_id: '1.1.1', status: 'pending', aiDraftable: true, hasProposal: false }]
    const { container } = await mount([
      at(1, 'remediate.vision_retry_recovered', { drafts: 0, awaiting_review: 0, uncertain: 0, missing: 1, coverage_complete: false, item_id: '101' }, 'run-A'),
      denied('run-A', '101'),
      at(3, 'remediate.delivered', {}, 'run-A'),
    ].map((event, index) => ({ ...event, seq: index + 1 })), rows)
    expect(history(container)[0]).toContain(`Corrected copy of ${FILE} saved to the source provider`)
    const current = notices(container).join(' | ')
    expect(current).toContain('AI provider access denied')
    // The unresolved caption row is still counted, under the block that is still current.
    expect(current).toContain('AI request blocked1 review item')
    await act(async () => [...container.querySelectorAll('button')].find(b => b.textContent === 'Group by document').click())
    const [group] = container.querySelectorAll('ol[aria-label="Recent remediation activity"] > li')
    expect(group.className).toContain('remops-activity-attention')
  })

  it('unknown coverage is worded as unknown, keeps a notice, and never settles an earlier block', async () => {
    const unknown = at(2, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 0, uncertain: 1, missing: null, coverage_complete: false, document_write: false, item_id: '101' }, 'run-A')
    const { container } = await mount([denied('run-A', '101'), unknown])
    const [line] = history(container)
    expect(line).toContain(`AI image description drafted for ${FILE} · not yet written to the document · 1 draft needs checking · whether every image has a description is not known`)
    expect(line).not.toMatch(/valid|verified|usable|complete|waiting for your review/i)
    const current = notices(container).join(' | ')
    expect(current).toContain('AI provider access denied')
    expect(current).toContain('Image description coverage not confirmed')
    // Alone, unknown coverage is still a current notice rather than silence.
    const alone = await mount([unknown])
    expect(notices(alone.container).join(' ')).toContain('Image description coverage not confirmed')
  })

  it('known incompleteness (numeric missing, coverage_complete false) is counted, not called unknown', async () => {
    const { container } = await mount([at(2, 'remediate.vision_retry_recovered', { drafts: 1, awaiting_review: 1, uncertain: 0, missing: 2, coverage_complete: false, document_write: false, item_id: '101' }, 'run-A')])
    const [line] = history(container)
    expect(line).toContain('waiting for your review — not yet written to the document · 2 images still need a description')
    expect(line).not.toMatch(/not known|may also/)
    const current = notices(container).join(' | ')
    expect(current).toContain('Image description still needed')
    expect(current).not.toContain('coverage not confirmed')
  })

  it('the historical obsolete projection names no approved fix and makes no AI-request claim', async () => {
    const { container } = await mount([at(1, 'remediate.vision_retry_blocked', { reason_code: 'vision_retry_input_changed',
      recorded_reason_code: 'vision_recovery_unresolved', projection: 'historical_obsolete_retry' }, 'run-A')])
    const [line] = history(container)
    expect(line).toContain(`Earlier image-description retry for ${FILE} cancelled · the saved input changed (the document or its assessed source was updated).`)
    expect(line).not.toMatch(/approved fix|No AI request/)
    expect(container.querySelector('.remaining-work-status')).toBeNull()
  })

  it('not-needed and review-only cancellations are worded from their counts', async () => {
    const { container } = await mount([
      at(1, 'remediate.vision_retry_obsolete', { retry: 2, reason_code: 'vision_retry_not_needed', usable: 1, awaiting_review: 1, uncertain: 2, no_ai_request: true, item_id: '101' }, 'run-A'),
      at(2, 'remediate.vision_retry_obsolete', { retry: 2, reason_code: 'vision_retry_review_only', usable: 0, awaiting_review: 2, uncertain: 0, item_id: '102' }, 'run-A'),
    ])
    const [reviewOnly, notNeeded] = history(container)
    expect(notNeeded).toContain('cancelled · no image needed a new AI draft (1 draft ready, 1 waiting for your review, 2 held for a reason ACP could not classify). No AI request was made.')
    expect(notNeeded).not.toMatch(/only needs|confirmation|valid|verified/i)
    expect(reviewOnly).toContain('cancelled · each remaining image already has a draft waiting for your review (2 waiting for your review).')
    expect(reviewOnly).not.toContain('No AI request')
    expect(container.querySelector('.remaining-work-status')).toBeNull()
  })

  it('a neutral not-needed cancellation claims no human gate', async () => {
    const { container } = await mount([at(1, 'remediate.vision_retry_obsolete', { retry: 2, reason_code: 'vision_retry_not_needed', no_ai_request: true, item_id: '101' }, 'run-A')])
    const [line] = history(container)
    expect(line).toContain('cancelled · no image needed a new AI draft. No AI request was made.')
    expect(line).not.toMatch(/review|confirm|valid|verified/i)
  })
})
