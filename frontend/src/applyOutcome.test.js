import { describe, it, expect } from 'vitest'
import { applyOutcomeOf, applyOutcomeCopy, buildEvidenceCard, reviewableInPlace, verificationLadder } from './reviewCard.js'

// An approved value that was written to a working copy and refused credit by the re-scan used to
// vanish: the row left the pending inbox, the write was discarded, apply.unverified was logged and
// nothing showed it. These pin the card-side model of that outcome (hitl_queue.apply_outcome).

const refused = (extra = {}) => ({
  id: 7, scan_id: 's1', file: 'deck.pptx', rule_id: '1.4.5', rule_name: 'Images of Text',
  status: 'approved', applied: 0, reviewed_at: '2026-09-07T03:00:00+00:00',
  proposals: [{ locator: 'image 1', before: '', proposed_value: 'Benefits at a glance', rationale: 'OCR' }],
  apply_outcome: { outcome: 'still_failing', criteria: ['1.4.5', '1.4.9'], reason: '', ts: '2026-09-07T03:01:00+00:00' },
  ...extra,
})

describe('applyOutcomeOf — only a backend-attached, recognised outcome becomes a card fact', () => {
  it('normalises the wire shape', () => {
    expect(applyOutcomeOf(refused())).toEqual({
      state: 'still_failing', criteria: ['1.4.5', '1.4.9'], reason: '', ts: '2026-09-07T03:01:00+00:00',
    })
  })
  it('is null for a pending row, a row without the field, and an unknown outcome word', () => {
    expect(applyOutcomeOf({ status: 'pending' })).toBeNull()
    expect(applyOutcomeOf(refused({ apply_outcome: undefined }))).toBeNull()
    expect(applyOutcomeOf(refused({ apply_outcome: { outcome: 'cleared' } }))).toBeNull()
    expect(applyOutcomeOf(null)).toBeNull()
  })
})

describe('reviewableInPlace — what the FileDrawer mounts a card for', () => {
  it('pending rows, and approved-unapplied rows that carry an outcome', () => {
    expect(reviewableInPlace({ status: 'pending' })).toBe(true)
    expect(reviewableInPlace(refused())).toBe(true)
  })
  it('never an applied row, an approved row with nothing to explain, or a rejected one', () => {
    expect(reviewableInPlace(refused({ applied: 1 }))).toBe(false)
    expect(reviewableInPlace(refused({ apply_outcome: undefined }))).toBe(false)
    expect(reviewableInPlace(refused({ status: 'rejected' }))).toBe(false)
    expect(reviewableInPlace(null)).toBe(false)
  })
})

describe('verificationLadder with an apply outcome — stopped at re-scan, and says "working copy"', () => {
  it('still_failing: draft, review and the working-copy write are done; re-scan failed; not certified', () => {
    const l = verificationLadder(buildEvidenceCard(refused()))
    expect(l.map((s) => s.label)).toEqual(['AI draft generated', 'Human review', 'Written to a working copy', 'Re-scan verified', 'Outcome recorded'])
    expect(l.map((s) => s.state)).toEqual(['done', 'done', 'done', 'failed', 'todo'])
    // never a green "Written to document": the document the reviewer has is unchanged
    expect(l.some((s) => s.label === 'Written to document')).toBe(false)
  })
  it('could_not_verify names the re-scan step for what it was', () => {
    const l = verificationLadder(buildEvidenceCard(refused({
      apply_outcome: { outcome: 'could_not_verify', criteria: ['1.4.5'], reason: 'engine missing: tesseract', ts: 't' },
    })))
    expect(l[3]).toEqual({ label: 'Re-scan could not verify', state: 'failed' })
  })
  it('a pending row is untouched: no failed step and no working-copy write (whatever its shape)', () => {
    const l = verificationLadder(buildEvidenceCard(refused({ status: 'pending', apply_outcome: undefined })))
    expect(l.some((s) => s.state === 'failed')).toBe(false)
    expect(l.some((s) => s.label === 'Written to a working copy')).toBe(false)
    expect(l.find((s) => s.label === 'Human review').state).toBe('current')
  })
})

describe('applyOutcomeCopy — the sentence under the ladder', () => {
  it('still_failing names the criteria and says the copy was discarded and nothing credited', () => {
    const c = applyOutcomeCopy(buildEvidenceCard(refused()))
    expect(c.headline).toBe('Written, but 1.4.5, 1.4.9 still fails on re-scan.')
    expect(c.body).toMatch(/not kept/)
    expect(c.body).toMatch(/nothing was credited/)
    expect(c.body).toMatch(/needs a different fix/)
  })
  it('could_not_verify carries the backend reason verbatim', () => {
    const c = applyOutcomeCopy(buildEvidenceCard(refused({
      apply_outcome: { outcome: 'could_not_verify', criteria: ['1.4.5'], reason: 'engine missing: tesseract', ts: 't' },
    })))
    expect(c.headline).toBe('Written, but the re-scan could not verify it.')
    expect(c.body).toMatch(/1\.4\.5 still fails/)
    expect(c.body).toMatch(/Reason: engine missing: tesseract\./)
  })
  it('falls back to the card SC when the decision named no criteria, and is null without an outcome', () => {
    const c = applyOutcomeCopy(buildEvidenceCard(refused({ apply_outcome: { outcome: 'still_failing', criteria: [] } })))
    expect(c.headline).toBe('Written, but 1.4.5 still fails on re-scan.')
    expect(applyOutcomeCopy(buildEvidenceCard(refused({ apply_outcome: undefined })))).toBeNull()
  })
})
