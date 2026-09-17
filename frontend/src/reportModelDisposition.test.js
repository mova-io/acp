import { describe, it, expect } from 'vitest'
import { buildFileCertificationModel } from './reportModel.js'

// W4 — dispositions (manual attestation / out-of-scope) flow into the certification report:
// the denominator, the executive summary, a dedicated Dispositions section, the full-coverage
// table, the audit trail, and the conformance statement all reflect them — and the report is
// byte-for-byte unchanged when no row carries a disposition.

// A minimal but realistic coverage row set: two passes, one open finding, and two dead-end
// (UNCHECKED) criteria that a human then disposes.
const baseRows = () => ([
  { id: '1.4.3', name: 'Contrast (Minimum)', plain: 'Contrast', level: 'AA', fix: '⚡ auto', outcome: 'PASS', count: 0 },
  { id: '2.4.2', name: 'Page Titled', plain: 'Document title', level: 'A', fix: '⚡ auto', outcome: 'PASS', count: 0 },
  { id: '1.1.1', name: 'Non-text Content', plain: 'Image alt text', level: 'A', fix: '✎ AI', outcome: 'FAIL', count: 3, fileIssues: [{ detail: 'image missing alt' }] },
  { id: '1.2.1', name: 'Audio-only / Video-only', plain: 'Transcript', level: 'A', fix: '✋ human', outcome: 'UNCHECKED', count: 0 },
  { id: '1.2.2', name: 'Captions', plain: 'Captions', level: 'A', fix: '✋ human', outcome: 'UNCHECKED', count: 0 },
])

const data = (rows) => ({ file: 'deck.pptx', score: 72, targetLevel: 'AA', rows, date: '2026-08-18', timestamp: '2026-08-18 10:00' })
const flat = (model) => model.blocks.flatMap((b) => [
  b.text, ...(b.items || []), ...((b.rows || []).flat()), ...((b.cards || []).map((c) => `${c.label} ${c.value}`)),
]).filter((x) => typeof x === 'string').join('\n')
const findTable = (model, caption) => model.blocks.find((b) => b.k === 'table' && b.caption === caption)

describe('reportModel — W4 dispositions in the certification report', () => {
  it('is unchanged when no row carries a disposition', () => {
    const model = buildFileCertificationModel(data(baseRows()))
    const txt = flat(model)
    expect(txt).not.toMatch(/attested/i)
    expect(txt).not.toMatch(/Manual attestations & dispositions/i)
    // 2 pass of 5 in-scope, phrased against the full denominator
    expect(txt).toMatch(/2 of 5 in-scope criteria pass/)
    expect(findTable(model, 'Manual attestations and out-of-scope dispositions')).toBeUndefined()
  })

  it('an ATTESTED dead-end criterion no longer counts as "not auto-checked" and appears in Dispositions', () => {
    const rows = baseRows()
    rows[3].disposition = { kind: 'attested', reason: 'Verified transcript with NVDA', actor: 'qa@acme.co' }
    const model = buildFileCertificationModel(data(rows))
    const txt = flat(model)
    // still 5 in scope (attested stays in scope), but only 1 unchecked now (1.2.2)
    expect(txt).toMatch(/2 of 5 in-scope criteria pass/)
    expect(txt).toMatch(/1 criterion manually attested/)
    expect(txt).toMatch(/1 criterion not auto-checked/)
    // dedicated section + row with the reason
    const tbl = findTable(model, 'Manual attestations and out-of-scope dispositions')
    expect(tbl).toBeTruthy()
    expect(tbl.rows.flat().join(' ')).toMatch(/Verified transcript with NVDA/)
    // audit trail records the attestation
    const audit = findTable(model, 'Audit trail')
    expect(audit.rows.flat().join(' ')).toMatch(/Manually attested/)
  })

  it('an OUT_OF_SCOPE criterion leaves the in-scope denominator, with its reason on the record', () => {
    const rows = baseRows()
    rows[4].disposition = { kind: 'out_of_scope', reason: 'No audio content in this engagement' }
    const model = buildFileCertificationModel(data(rows))
    const txt = flat(model)
    // denominator drops from 5 to 4
    expect(txt).toMatch(/2 of 4 in-scope criteria pass/)
    expect(txt).toMatch(/1 criterion recorded out of scope/)
    const audit = findTable(model, 'Audit trail')
    expect(audit.rows.flat().join(' ')).toMatch(/Marked out of scope/)
  })

  it('out-of-scope does not change the OPEN-findings verdict — a real FAIL still blocks certification', () => {
    const rows = baseRows()
    rows[4].disposition = { kind: 'out_of_scope', reason: 'x' }
    const model = buildFileCertificationModel(data(rows))
    expect(model.fullyConformant).toBe(false)   // 1.1.1 is still FAIL
  })

  it('a fully-attested/out-of-scoped set with no open findings reads as conformant', () => {
    const rows = [
      { id: '1.4.3', name: 'Contrast', plain: 'Contrast', level: 'AA', fix: '⚡ auto', outcome: 'PASS', count: 0 },
      { id: '1.2.1', name: 'Transcript', plain: 'Transcript', level: 'A', fix: '✋ human', outcome: 'UNCHECKED', count: 0, disposition: { kind: 'attested', reason: 'manual audit' } },
      { id: '1.2.2', name: 'Captions', plain: 'Captions', level: 'A', fix: '✋ human', outcome: 'UNCHECKED', count: 0, disposition: { kind: 'out_of_scope', reason: 'no audio' } },
    ]
    const model = buildFileCertificationModel(data(rows))
    expect(model.fullyConformant).toBe(true)
    // in-scope denominator is 2 (out-of-scope excluded), all resolved
    // Was /meets all 2 in-scope/. "Meets" read as a conformance claim; the house position (a64142c1)
    // is that ACP reports what it checked, changed and verified, so the ready wording now says
    // there is nothing outstanding among the criteria checked — against the same denominator.
    expect(flat(model)).toMatch(/No outstanding items for "deck\.pptx" among the 2 in-scope/)
  })
})
