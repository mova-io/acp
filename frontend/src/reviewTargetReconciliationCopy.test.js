import { describe, expect, it } from 'vitest'
import { reasonText, reconciliationSummary } from './reviewTargetReconciliationCopy.js'

const file = (over) => ({ file: 'synthetic.docx', superseded: [], unchanged: [], skipped: [], ...over })

describe('reconcile-targets result copy', () => {
  it('names closed items and why the rest stayed open', () => {
    expect(reconciliationSummary({ superseded_count: 2, files: [file({ superseded: ['a', 'b'],
      skipped: [{ item_id: 'c', reason: 'target_remains' }] })] }))
      .toBe('2 items closed: another verified change replaced their target. Not changed: its target is still in the saved copy.')
  })
  it('groups codes that mean the same thing and counts them', () => {
    expect(reconciliationSummary({ superseded_count: 0, files: [file({ skipped: [
      { item_id: 'a', reason: 'criterion_unknown' }, { item_id: 'b', reason: 'criterion_not_assessed' }] })] }))
      .toBe('Nothing changed: the saved copy’s check did not cover this item’s criterion (2 items).')
  })
  it('reports a file-level refusal without inventing an item count', () => {
    expect(reconciliationSummary({ superseded_count: 0, files: [file({ skipped: [{ item_id: null, reason: 'verification_partial' }] })] }))
      .toBe('Nothing changed: the saved copy has no complete recorded check to rely on.')
  })
  it('shows an unknown server code as recorded rather than rewording it', () => {
    expect(reasonText('brand_new_reason')).toBe('recorded reason “brand new reason”')
  })
  it('says when nothing applied at all', () => {
    expect(reconciliationSummary({ superseded_count: 0, files: [] })).toBe('Nothing changed: no open item in this scan is one this re-check applies to.')
    // `unchanged` = retired by an EARLIER re-check (the server's contract), never "still open".
    expect(reconciliationSummary({ superseded_count: 0, files: [file({ unchanged: [{ item_id: 'a' }],
      skipped: [{ item_id: null, reason: 'nothing_to_reconcile' }] })] }))
      .toBe('Nothing changed: 1 item was already closed by a verified target replacement. Not changed: no open item in this scan is one this re-check applies to.')
  })
})

it('never describes an already-closed item as still holding its target', () => {
  const text = reconciliationSummary({ superseded_count: 1, files: [file({ superseded: [{ item_id: 'b' }], unchanged: [{ item_id: 'a' }] })] })
  expect(text).toBe('1 item closed: another verified change replaced its target. 1 item was already closed by a verified target replacement.')
  expect(text).not.toMatch(/still ha(s|ve)/)
})

it('explains a bounded run in plain words', () => {
  expect(reconciliationSummary({ superseded_count: 0, files: [file({ skipped: [{ item_id: null, reason: 'deferred_bounded' }] })] }))
    .toBe('Nothing changed: this re-check stopped at its per-request limit — run it again to check the rest.')
})

it('an already-closed item with nothing else to report says exactly that, with no new changes', () => {
  expect(reconciliationSummary({ superseded_count: 0, files: [file({ unchanged: [{ item_id: 'a' }] })] }))
    .toBe('Nothing changed: 1 item was already closed by a verified target replacement.')
})

it('keeps the skip reasons for items that really remain open beside an already-closed one', () => {
  const text = reconciliationSummary({ superseded_count: 0, files: [file({ unchanged: [{ item_id: 'a' }],
    skipped: [{ item_id: 'b', reason: 'target_remains' }] })] })
  expect(text).toBe('Nothing changed: 1 item was already closed by a verified target replacement. Not changed: its target is still in the saved copy.')
})

it('does not blame the corrected copy when either stored document may be missing', () => {
  expect(reasonText('bytes_unavailable')).not.toMatch(/corrected/)
})
