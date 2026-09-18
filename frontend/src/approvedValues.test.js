// Approving alt text now WRITES it into the document, so what the reviewer saw and what the
// server applies must be the same thing. These pin the two places that could diverge.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { proposalsOf } from './reviewCard.js'

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')

describe('the approve payload carries one value per image', () => {
  it('api.js sends approved_values alongside the single headline approved_value', () => {
    const src = read('api.js')
    expect(src).toMatch(/approved_values: opts\.approvedValues \?\? null/)
    expect(src).toMatch(/approved_value: approvedValue/)   // headline value still logged
  })

  it('EvidenceCard seeds one editor per instance, from that instance\'s own draft', () => {
    const src = read('EvidenceCard.jsx')
    // `instances` is the proposals when the AI drafted any, else the deferred evidence images —
    // so the per-image editor drives both the drafted and the from-scratch (no-draft) case.
    expect(src).toMatch(/useState\(\(\) => seedValues\(instances\)\)/)
    expect(src).toMatch(/const multi = instances\.length > 0/)
  })

  it('EvidenceCard sends the per-image values, and only on approval', () => {
    const src = read('EvidenceCard.jsx')
    // Three approvals author no content, and none of them may send values:
    //   !resolution    — a WCAG exception (decorative / essential logo) applied instead of a fix.
    //   !explainOnly   — a confirmed PDF structure/heading map or reading order, which is evidence
    //                    and a re-authoring instruction, never bytes written into the document.
    //   !decorativeRow — a confirmed decorative image. The value is routed to the marker writer,
    //                    which ignores it; sending it would record an instruction as the
    //                    reviewer's approved TEXT.
    //
    // ADR 0055 adds the ONE exception, and it is an exception to the first clause only:
    // `described_not_replaced` resolves images-of-text by judgement AND carries the reviewer's
    // description, which api/routes/hitl.py refuses the decision without. So the gate reads
    // `(!resolution || describedRow)` — every other resolution still suppresses, which is what
    // keeps #43 fixed.
    expect(src).toMatch(/status === 'approved' && \(!resolution \|\| describedRow\)/)
    expect(src).toMatch(/&& !explainOnly && !decorativeRow/)
    expect(src).toMatch(/const describedRow = resolution === DESCRIBED_NOT_REPLACED/)
    expect(src).toMatch(/approvedValues/)
  })

  it('an explain-only row is confirmed, never edited', () => {
    // The card must not offer a write-back field for a value nothing writes back: that is the
    // promise store._row_approved_values stopped believing, and the UI should not make it either.
    const src = read('EvidenceCard.jsx')
    expect(src).toMatch(/const explainOnly = proposalList\.length > 0 && proposalList\.every\(\(p\) => p\.explain_only\)/)
    expect(src).toMatch(/const editable = !explainOnly && !decorativeRow/)
    // Same single exception as above: a described row DID author a value, so the audit line must
    // carry it rather than reading as a judgement with nothing behind it.
    expect(src).toMatch(/const finalValue = \(\(resolution && !describedRow\) \|\| explainOnly \|\| decorativeRow\)/)
  })

  it('a decorative row is confirmed against the image, never typed into', () => {
    // The draft is "Mark as decorative — no alt text needed". An editable box prefilled with it
    // asks the reviewer to describe an image they are about to declare needs no description, then
    // throws the text away — #43 routes it to the marker writer, which ignores the value. The
    // card shows the picture instead, so what it offers matches what approving does.
    const src = read('EvidenceCard.jsx')
    expect(src).toMatch(/const decorativeRow = proposalList\.length > 0/)
    expect(src).toMatch(/proposalList\.every\(\(p\) => p\.kind === 'decorative'\)/)
    expect(src).toMatch(/decorativeRow \? \(/)          // its own read-only render branch
    expect(src).toMatch(/evcard-decorative-row/)
  })

  it('every instance is rendered beside its own textarea — never a value for unseen evidence', () => {
    const src = read('ProposalEditors.jsx')
    expect(src).toMatch(/proposals\.map\(\(p, i\) =>/)
    expect(src).toMatch(/<ProposalThumb thumb=\{p\.thumb\}/)
    expect(src).toMatch(/onChange=\{\(e\) => onChange\(i, e\.target\.value\)\}/)
  })

  it('each row shows what is changing: the current value, and what gets written', () => {
    // "The AI drafted a fix" told a reviewer nothing. The passage (or image) and the resulting
    // markup are the only things that let them judge the value at all.
    const src = read('ProposalEditors.jsx')
    expect(src).toMatch(/<span className="difftag">current<\/span><code>\{p\.before\}<\/code>/)
    expect(src).toMatch(/<span className="difftag">writes<\/span><code>\{after\}<\/code>/)
    expect(src).toMatch(/formatProposedValue\(sc, values\[i\] \?\? ''\)/)
    expect(src).toMatch(/\{p\?\.rationale && \(/)   // why this value, shown per instance
  })

  it('both review screens use the SAME editor, so the array index means the same proposal', () => {
    // approved_values[i] is proposal i on the server. Two divergent renderings could reorder
    // them and write a description onto the wrong image.
    for (const f of ['EvidenceCard.jsx', 'ReviewDrawer.jsx']) {
      expect(read(f)).toMatch(/from '\.\/ProposalEditors\.jsx'/)
      expect(read(f)).toMatch(/<ProposalEditors /)
    }
  })

  it('the Remediate drawer approves with the array, not just the first image', () => {
    const src = read('ReviewDrawer.jsx')
    expect(src).toMatch(/multi\s*\n?\s*\? onAct\(item\.id, 'approved', values\[0\], values\)/)
  })

  it('Remediate.act forwards approvedValues to the API', () => {
    const src = read('Remediate.jsx')
    // `viewed` is the row the pane rendered — the version the decision is bound to.
    expect(src).toMatch(/const act = \(id, kind, editedValue, approvedValues, resolution = null, frozen = null, viewed = null\)/)
    expect(src).toMatch(/approvedValues: apiStatus === 'approved' \? \(approvedValues \|\| null\) : null/)
    // The out-of-scope / WCAG-exception resolution is forwarded on an approval too.
    expect(src).toMatch(/resolution: apiStatus === 'approved' \? \(resolution \|\| null\) : null/)
  })
})

// The "route a multi-image row to the drawer" pair that used to sit here greppd ReviewItemCard's
// markup, and ReviewItemCard was never rendered. The safety property it described — never approve
// a value for evidence the reviewer was not shown — is enforced in the card that does render, by
// a better mechanism: EvidenceCard gives every instance its own editor rather than sending the
// reviewer elsewhere. That is asserted directly, on mounted output, above:
// "every instance is rendered beside its own textarea — never a value for unseen evidence".

describe('proposalsOf — the list the editors are built from', () => {
  it('is empty for a judgement finding, so no editor and no write-back', () => {
    for (const empty of [null, {}, { proposals: null }, { proposals: [] }]) {
      expect(proposalsOf(empty)).toEqual([])
    }
  })

  it('preserves order, so values[i] lines up with the server\'s proposal i', () => {
    const item = { proposals: [{ locator: 'a' }, { locator: 'b' }, { locator: 'c' }] }
    expect(proposalsOf(item).map((p) => p.locator)).toEqual(['a', 'b', 'c'])
  })
})
