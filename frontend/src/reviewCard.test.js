import { describe, it, expect } from 'vitest'
import { authoringScaffold, buildEvidenceCard, evidenceSignals, explainFinding, formatProposedValue, guidanceSentence, primaryActionLabel, reviewIntent, trustStates, validationChecklist, verificationLadder, whyHumanReview } from './reviewCard.js'

// The comparisonFor and noDraftHint suites are gone with the functions: both existed only
// for Remediate's WhyReview + ReviewItemCard, deleted in #108 as unreachable. The live card
// builds before/after from remediation_diff (BeforeAfterEvidence) and states a missing draft
// through draftMsg, each covered by the EvidenceCard suites.

describe('formatProposedValue — a raw value a reviewer can act on', () => {
  it('turns a bare ISO code into the markup it becomes', () => {
    // 3.1.2 proposes "es". On its own that tells a reviewer nothing about what changes.
    expect(formatProposedValue('3.1.2', 'es')).toBe('lang="es" — Spanish')
    expect(formatProposedValue('3.1.1', 'fr')).toBe('lang="fr" — French')
    expect(formatProposedValue('3.1.2', 'pt-BR')).toBe('lang="pt-BR" — Portuguese')
  })

  it('names the language only when we know it, never inventing one', () => {
    expect(formatProposedValue('3.1.2', 'xx')).toBe('lang="xx"')
  })

  it('leaves prose alone — alt text is already what gets written', () => {
    expect(formatProposedValue('1.1.1', 'A clinician at a desk.')).toBe('A clinician at a desk.')
    expect(formatProposedValue('2.4.4', 'Download the intake form')).toBe('Download the intake form')
    // a two-letter word that is not a lang code for this criterion stays untouched
    expect(formatProposedValue('2.4.4', 'go')).toBe('go')
  })

  it('never throws on an empty or missing value', () => {
    for (const v of [null, undefined, '']) expect(formatProposedValue('3.1.2', v)).toBe('')
  })
})

describe('buildEvidenceCard — the Evidence Card model', () => {
  it('judgement item (contrast) → approval IS the resolution, so it certifies', () => {
    // 1.4.3 has no value to write: a re-scan can never clear a "this ratio is acceptable"
    // sign-off, so approval is legitimately the gate and the criterion flips to Pass.
    const c = buildEvidenceCard({
      id: 'i2', scan_id: 's1', file: 'page.html', rule_id: 'SC_1_4_3',
      rule_name: 'Contrast (Minimum)',
    })
    expect(c.sc).toBe('1.4.3')
    expect(c.certifiesOnApprove).toBe(true)
    expect(c.impact).toEqual({ before: 'Fail', after: 'Pass' })
  })

  it('alt-text item → assisted track, Approve & Apply, real recommendation + confidence basis', () => {
    const c = buildEvidenceCard({
      id: 'i1', scan_id: 's1', file: 'deck.pptx', rule_id: 'SC_1_1_1',
      rule_name: 'Non-text content', approved_value: 'A guide dog in a harness',
    })
    expect(c.sc).toBe('1.1.1')
    expect(c.fmt).toBe('PPTX')
    expect(c.track.track).toBe('assisted')
    expect(c.track.action).toBe('Approve & Apply')
    expect(c.recommendation).toBe('A guide dog in a harness')
    expect(c.confidence.basis).toBeTruthy()          // evidence, never a %
    expect(c.problem).toMatch(/alt-text|description/i)
    // Approving an alt-text value does NOT resolve 1.1.1: routes/hitl.py stores the value as
    // evidence and no remediator ever writes it into the document. The card must not promise a
    // Pass the backend (store.mark_file_compliant_if_reviewed) now refuses to grant.
    expect(c.certifiesOnApprove).toBe(false)
    expect(c.impact).toEqual({ before: 'Fail', after: 'Fail' })
  })

  it('keyboard item → human track (detect ≠ fix)', () => {
    expect(buildEvidenceCard({ rule_id: '2.1.1', file: 'x.pdf' }).track.track).toBe('human')
  })

  it('deterministic item → auto track (auto-applied, not a review card action)', () => {
    expect(buildEvidenceCard({ rule_id: '1.4.3', file: 'x.docx' }).track.track).toBe('auto')
  })

  it('filters before/after diffs to this criterion only', () => {
    const c = buildEvidenceCard({ rule_id: '1.4.3', file: 'x.docx' }, [
      { rule_id: '1.4.3', before: 'faint', after: 'dark' },
      { rule_id: '2.4.2', before: 'no title', after: 'Title' },
    ])
    expect(c.diffs.length).toBe(1)
    expect(c.diffs[0].after).toBe('dark')
  })

  it('no AI draft → recommendation is null (a judgement item)', () => {
    expect(buildEvidenceCard({ rule_id: '2.1.1', file: 'x.pdf' }).recommendation).toBeNull()
  })
})

describe('buildEvidenceCard — AI proposals (hitl_queue.proposals)', () => {
  const withProposal = (extra = {}, proposals = [
    { locator: '#l1', before: 'click here', proposed_value: 'Download Annual Report (PDF)',
      rationale: "derived from the download target 'Annual-Report.pdf'", source: 'derived from the link target' },
  ]) => buildEvidenceCard({
    id: 'p1', scan_id: 's1', file: 'page.html', rule_id: 'SC_2_4_4',
    rule_name: 'Link Purpose', proposals, ...extra,
  })

  it('a server-side proposal becomes the recommendation the reviewer confirms', () => {
    const c = withProposal()
    expect(c.recommendation).toBe('Download Annual Report (PDF)')
    expect(c.proposal.list).toHaveLength(1)
    expect(c.proposal.list[0].rationale).toMatch(/derived from the download target/)
  })

  it('a proposal outranks a stale approved_value — it is the current recommendation', () => {
    const c = withProposal({ approved_value: 'an older approved value' })
    expect(c.recommendation).toBe('Download Annual Report (PDF)')
  })

  it('falls back to approved_value when there is no proposal', () => {
    const c = buildEvidenceCard({ id: 'x', rule_id: 'SC_2_4_4', approved_value: 'Read the 2026 report' })
    expect(c.recommendation).toBe('Read the 2026 report')
    expect(c.proposal).toBe(null)
  })

  it('a validated proposal is MEDIUM, never High — an AI proposal is not trusted until approved', () => {
    const c = withProposal({ validated: 1 })
    expect(c.proposal.validated).toBe(true)
    expect(c.confidence.level.label).toBe('Medium')
    expect(c.confidence.basis).toMatch(/validated by re-scan/)
  })

  it('an unvalidated proposal is MEDIUM — approve to apply', () => {
    expect(withProposal().confidence.basis).toMatch(/approve to apply/)
  })

  it('a decorative proposal is subjective → LOW (a re-scan can never validate the call)', () => {
    const c = withProposal({ validated: 1 }, [
      { proposed_value: 'Mark as decorative — no alt text needed', kind: 'decorative',
        rationale: "filename 'site-logo.png' looks decorative" },
    ])
    expect(c.proposal.subjective).toBe(true)
    expect(c.confidence.level.label).toBe('Low')
    expect(c.confidence.basis).toMatch(/human judgement/)
  })

  it('a 1.3.3 sensory rewrite is subjective by criterion, whatever its kind', () => {
    const c = buildEvidenceCard({
      id: 's', rule_id: '1.3.3', validated: 1,
      proposals: [{ proposed_value: 'Select the Submit button', rationale: 'relies on colour' }],
    })
    expect(c.proposal.subjective).toBe(true)
    expect(c.confidence.level.label).toBe('Low')
  })

  it('never emits a fabricated percentage (ADR 0016)', () => {
    for (const c of [withProposal(), withProposal({ validated: 1 })]) {
      expect(JSON.stringify(c.confidence)).not.toMatch(/%/)
    }
  })
})

describe('verificationLadder — the honest connected pipeline', () => {
  it('value-fix, unvalidated: write + re-scan are still ahead (todo), never a green pass', () => {
    const l = verificationLadder({ certifiesOnApprove: false, proposal: { list: [{}], validated: false } })
    expect(l.map((s) => s.label)).toEqual(['AI draft generated', 'Human review', 'Written to document', 'Re-scan verified', 'Outcome recorded'])
    expect(l.map((s) => s.state)).toEqual(['done', 'current', 'todo', 'todo', 'todo'])
  })

  it('value-fix, validated: write + re-scan already done, the human is the last gate', () => {
    const l = verificationLadder({ certifiesOnApprove: false, proposal: { list: [{}], validated: true } })
    expect(l.map((s) => s.label)).toEqual(['AI draft generated', 'Written to document', 'Re-scan verified', 'Human review', 'Outcome recorded'])
    expect(l.map((s) => s.state)).toEqual(['done', 'done', 'done', 'current', 'todo'])
  })

  it('judgement finding: short pipeline — the sign-off IS the resolution', () => {
    const l = verificationLadder({ certifiesOnApprove: true })
    expect(l.map((s) => s.label)).toEqual(['Detected', 'Human review', 'Decision recorded'])
  })

  it('never claims certification for a single finding on any path', () => {
    const cards = [{ certifiesOnApprove: true }, { proposal: { list: [{}], validated: true } }, { proposal: { list: [{}] } },
      { applyOutcome: { state: 'nothing_written' } }, { applyOutcome: { state: 'still_failing' } }]
    for (const card of cards) expect(verificationLadder(card).map((s) => s.label).join(' ')).not.toMatch(/certif/i)
  })
})

describe('evidenceSignals — grouped, checkable, never a fabricated score', () => {
  it('groups the detection basis, the reasoning, and the empty prior state', () => {
    const sig = evidenceSignals({
      confidence: { basis: 'AI / heuristic detection — semantic judgement' },
      rationale: 'OCR read the title text inside the image',
      proposal: { list: [{ before: '(no alt text)' }], subjective: true },
      diffs: [],
    })
    expect(sig.find((s) => s.text.includes('heuristic')).group).toBe('Detection')
    expect(sig.find((s) => s.text.includes('OCR')).group).toBe('Reasoning')
    expect(sig.find((s) => s.text.includes('No existing value')).group).toBe('Document state')
    // the subjective-wording caveat is NOT here — it's the dedicated whyHumanReview panel
    expect(sig.some((s) => /human wording/i.test(s.text))).toBe(false)
  })

  it('is empty when the finding carried no evidence — never invents one', () => {
    expect(evidenceSignals({})).toEqual([])
  })
})

describe('explainFinding — deterministic, keyless, honest', () => {
  it('composes the requirement + who is blocked + what ACP did + the next step', () => {
    const e = explainFinding(
      { sc: '1.1.1', recommendation: 'The chart compares quarterly revenue.', certifiesOnApprove: false },
      { trust: { grounding: { state: 'grounded' }, validation: { state: 'not_yet_written' } },
        whyReview: 'The wording is a judgement call.' })
    expect(e).toMatch(/WCAG 1\.1\.1/)
    // `screen[- ]reader`: the per-criterion impact line (wcagWhy.js) hyphenates it, the
    // principle fallback does not. Both name the same blocked user, which is what this asserts.
    expect(e).toMatch(/assistive technology|screen[- ]reader/i)
    expect(e).toMatch(/ACP drafted/)
    expect(e).toMatch(/anchored in text read/i)
    expect(e).toMatch(/judgement call/)
    expect(e).toMatch(/writes it and re-scans/i)
  })

  it('says so plainly when ACP could not draft a value', () => {
    expect(explainFinding({ sc: '1.1.1', recommendation: null, certifiesOnApprove: false }, {}))
      .toMatch(/could not draft/i)
  })

  it('never emits a percentage or a confidence-level word', () => {
    const e = explainFinding({ sc: '1.4.3', certifiesOnApprove: true },
      { trust: { validation: { state: 'deterministic_passed' } } })
    // The guard is against a FABRICATED confidence — "82%", "High confidence", a bare "Low"
    // used as this product's confidence label. It was written as a case-insensitive
    // /\b(high|medium|low)\b/, which also matches the clinical term "low vision": nine of the
    // 87 per-criterion impact lines say "low-vision users" or "users with low vision", so
    // wiring those lines in turned a correct explanation into a failure. Asserted as the
    // capitalised LABEL instead, which is how confidence.js actually renders a level.
    expect(e).not.toMatch(/%|confidence/i)
    expect(e).not.toMatch(/(?<![-\w])(High|Medium|Low)(?![-\w])/)
  })
})

describe('trustStates — verifiable states, never a confidence score', () => {
  it('OCR-anchored vision alt → grounded, not yet written', () => {
    const t = trustStates({ sc: '1.1.1', rationale: 'anchored in text read from the image (OCR: "Revenue")',
      proposal: { list: [{}], validated: false }, certifiesOnApprove: false })
    expect(t.grounding.state).toBe('grounded')
    expect(t.validation.state).toBe('not_yet_written')
  })

  it('ungrounded vision guess → visual interpretation (warn)', () => {
    const t = trustStates({ sc: '1.1.1', rationale: 'vision description only — no text in the image to anchor it',
      proposal: { list: [{}], validated: false }, certifiesOnApprove: false })
    expect(t.grounding.state).toBe('visual_only')
    expect(t.grounding.tone).toBe('warn')
  })

  it('an explicit grounded boolean on the proposal wins over the text heuristic', () => {
    const t = trustStates({ sc: '1.1.1', rationale: 'ambiguous prose',
      proposal: { list: [{ grounded: true }], validated: true }, certifiesOnApprove: false })
    expect(t.grounding.state).toBe('grounded')
    expect(t.validation.state).toBe('re_scan_passed')
  })

  it('a text finding with a value → grounded in document text', () => {
    const t = trustStates({ sc: '2.4.4', recommendation: 'Download the 2025 annual report', certifiesOnApprove: false })
    expect(t.grounding.state).toBe('document_text')
  })

  it('a judgement finding → deterministic validation, makes no grounding claim', () => {
    const t = trustStates({ sc: '1.4.3', certifiesOnApprove: true })
    expect(t.validation.state).toBe('deterministic_passed')
    expect(t.grounding).toBeNull()
  })

  it('never emits a percentage or a confidence level', () => {
    const t = trustStates({ sc: '1.1.1', rationale: 'OCR text', proposal: { list: [{}] }, certifiesOnApprove: false })
    expect(JSON.stringify(t)).not.toMatch(/%|confidence|\b(high|medium|low)\b/i)
  })
})

describe('validationChecklist — machine-verified receipt for an applied fix', () => {
  it('is null for a pending value-fix (nothing applied yet)', () => {
    expect(validationChecklist({ sc: '1.1.1', diffs: [], proposal: { list: [{}], validated: false } })).toBeNull()
  })
  it('shows the write + re-scan + clear receipt when a remediation_diff exists', () => {
    const c = validationChecklist({ sc: '1.4.3', diffs: [{ before: 'x', after: 'y' }] })
    expect(c.every((s) => s.done)).toBe(true)
    expect(c.map((s) => s.label).join(' ')).toMatch(/written.*re-opened.*cleared/is)
    expect(c.some((s) => /1\.4\.3/.test(s.label))).toBe(true)
  })
  it('also fires for a validated proposal (applied + re-scan-cleared)', () => {
    expect(validationChecklist({ sc: '2.4.4', diffs: [], proposal: { list: [{}], validated: true } })).toHaveLength(3)
  })
})

describe('reviewIntent — the one plain-language sentence at the top (Review queue)', () => {
  const withDraft = { proposals: [{ proposed_value: 'a chart of revenue' }] }
  const noDraft = { proposals: [] }
  it('a drafted fix → “review and approve”, flavoured by the SC noun', () => {
    expect(reviewIntent(withDraft, '1.1.1')).toMatch(/drafted a fix for this image/i)
    expect(reviewIntent(withDraft, '2.4.4')).toMatch(/link/i)
  })
  it('no draft → “couldn’t generate a trustworthy fix, write one”', () => {
    expect(reviewIntent(noDraft, '1.1.1')).toMatch(/couldn.t generate|needs you to write/i)
  })
  it('a deterministic (auto/verify) item → “applied and verified — confirm”', () => {
    expect(reviewIntent({ rule_id: 'auto/verify' })).toMatch(/applied and verified/i)
  })
})

describe('primaryActionLabel — the button reads by workflow, not internal state', () => {
  it('proposal → Approve AI fix · confirm → Confirm fix · author → Approve description', () => {
    expect(primaryActionLabel({ proposals: [{ proposed_value: 'x' }] })).toBe('Approve AI fix')
    expect(primaryActionLabel({ rule_id: 'auto/verify' })).toBe('Confirm fix')
    expect(primaryActionLabel({ proposals: [] })).toBe('Approve description')
  })
})

describe('authoringScaffold — never a blank box', () => {
  it('gives image-authoring an outline, links a link outline, and null for an unknown SC', () => {
    expect(authoringScaffold('1.1.1')).toEqual(expect.arrayContaining([expect.stringMatching(/kind of image/i)]))
    expect(authoringScaffold('2.4.4')).toEqual(expect.arrayContaining([expect.stringMatching(/where the link goes/i)]))
    expect(authoringScaffold('9.9.9')).toBeNull()
  })
})

describe('guidanceSentence — context-aware natural sentence for 1.1.1', () => {
  it('returns null for non-1.1.1 criteria', () => {
    expect(guidanceSentence({ sc: '1.4.5', file: 'doc.pdf' })).toBeNull()
    expect(guidanceSentence({ sc: '2.4.4', file: 'doc.pptx' })).toBeNull()
    expect(guidanceSentence({})).toBeNull()
  })
  it('uses "slide" noun for pptx files', () => {
    const s = guidanceSentence({ sc: '1.1.1', file: 'deck.pptx' })
    expect(s).toMatch(/This slide has/)
  })
  it('uses "worksheet" noun for xlsx files', () => {
    const s = guidanceSentence({ sc: '1.1.1', file: 'report.xlsx' })
    expect(s).toMatch(/This worksheet has/)
  })
  it('uses "page" noun for pdf and docx files', () => {
    expect(guidanceSentence({ sc: '1.1.1', file: 'doc.pdf' })).toMatch(/This page has/)
    expect(guidanceSentence({ sc: '1.1.1', file: 'doc.docx' })).toMatch(/This page has/)
  })
  it('names the image kind when describedImageType detects one', () => {
    const card = { sc: '1.1.1', file: 'doc.pdf', proposals: [{ proposed_value: 'a comparison chart showing revenue' }] }
    const s = guidanceSentence(card)
    expect(s).toMatch(/a chart/)
  })
  it('falls back to "N images" when multiple proposals and no detected kind', () => {
    const card = { sc: '1.1.1', file: 'doc.pdf', proposals: [{ proposed_value: '' }, { proposed_value: '' }] }
    const s = guidanceSentence(card)
    expect(s).toMatch(/2 images/)
  })
  it('falls back to "an image" when no kind detected and single image', () => {
    const s = guidanceSentence({ sc: '1.1.1', file: 'doc.pdf' })
    expect(s).toMatch(/an image/)
  })
  it('gives "ACP drafted" resolution when proposals have a value', () => {
    const card = { sc: '1.1.1', file: 'doc.pdf', proposals: [{ proposed_value: 'Logo of the company.' }] }
    expect(guidanceSentence(card)).toMatch(/ACP drafted a description/)
  })
  it('gives "could not verify" resolution when no draft', () => {
    const card = { sc: '1.1.1', file: 'doc.pdf', proposals: [{ proposed_value: '' }] }
    expect(guidanceSentence(card)).toMatch(/could not verify/)
  })
})

describe('whyHumanReview — the honest reason a human is in the loop', () => {
  it('subjective wording → several valid descriptions', () => {
    expect(whyHumanReview({ proposal: { subjective: true } })).toMatch(/judgement call|valid descriptions/i)
  })
  it('medium (heuristic) confidence → a human confirms the call', () => {
    expect(whyHumanReview({ confidence: { level: { key: 'medium' } } })).toMatch(/heuristic/i)
  })
  it('deterministic high-confidence → nothing to explain', () => {
    expect(whyHumanReview({ confidence: { level: { key: 'high' } } })).toBeNull()
  })
  it('the HITL-six get a criterion-specific reason, not a generic hedge', () => {
    expect(whyHumanReview({ sc: '1.2.1' })).toMatch(/transcript/i)
    expect(whyHumanReview({ sc: '1.2.2' })).toMatch(/caption/i)
    expect(whyHumanReview({ sc: '1.2.3' })).toMatch(/audio description|media alternative/i)
    expect(whyHumanReview({ sc: '1.3.2' })).toMatch(/reading order/i)
    expect(whyHumanReview({ sc: '1.3.3' })).toMatch(/shape, colour|editorial/i)
    expect(whyHumanReview({ sc: '1.4.5' })).toMatch(/essential/i)
    expect(whyHumanReview({ sc: '1.4.9' })).toMatch(/essential/i)
    expect(whyHumanReview({ sc: '2.4.6' })).toMatch(/heading|intent/i)
    expect(whyHumanReview({ sc: '2.4.10' })).toMatch(/section structure|logically/i)
  })
  it('the criterion-specific reason wins over the generic confidence reason', () => {
    // A 1.3.3 finding detected at medium confidence must say WHY 1.3.3 is human, not "heuristic".
    expect(whyHumanReview({ sc: '1.3.3', confidence: { level: { key: 'medium' } } })).toMatch(/editorial/i)
  })
  it('an SC with no special reason still falls back to the confidence-based one', () => {
    expect(whyHumanReview({ sc: '4.1.2', confidence: { level: { key: 'medium' } } })).toMatch(/heuristic/i)
  })
})

describe('card.thumb — the reviewer always sees the image, draft or not', () => {
  const T1 = 'data:image/png;base64,PROPOSAL'
  const T2 = 'data:image/png;base64,EVIDENCE'

  it('prefers the proposal thumb when the AI produced a draft', () => {
    const c = buildEvidenceCard({ id: 'a', rule_id: '1.1.1',
      proposals: [{ proposed_value: 'A nurse at a workstation.', thumb: T1 }],
      evidence: [{ locator: 'ppt/slides/slide2.xml#rId3', thumb: T2 }] })
    expect(c.thumb).toBe(T1)
  })

  it('falls back to the evidence thumb when vision returned no draft', () => {
    // The empty-card case: image bytes captured to evidence[], no proposal. Before the fallback
    // this rendered no picture at all (the server thumbnail route is PDF-only, so a deck image
    // was invisible).
    const c = buildEvidenceCard({ id: 'b', rule_id: '1.1.1',
      evidence: [{ locator: 'ppt/slides/slide2.xml#rId3', thumb: T2 }] })
    expect(c.thumb).toBe(T2)
  })

  it('is null when there is neither a proposal nor evidence image', () => {
    expect(buildEvidenceCard({ id: 'c', rule_id: '1.1.1' }).thumb).toBeNull()
  })
})

// ── which document is this card about ─────────────────────────────────────────────────
//
// The card showed format, criterion and severity but never the filename. On the deployed app a
// card read "HTML — Automatic fix applied — verify the result" with no document anywhere on it,
// and axe-core confirmed the same gap in the accessible name: every card's aria-label was
// literally "Review —", so a screen-reader user was told nothing about what they were approving.
describe('buildEvidenceCard names the document', () => {
  it('splits a bare filename into a name and no folder', () => {
    const c = buildEvidenceCard({ file: 'Clinical-FAQ-39.html', rule_id: '1.1.1' })
    expect(c.fileName).toBe('Clinical-FAQ-39.html')
    expect(c.fileDir).toBe('')
  })

  it('splits a path, keeping the folder that tells two same-named documents apart', () => {
    const a = buildEvidenceCard({ file: 'Patient Education/2024/Clinical-FAQ-39.html' })
    const b = buildEvidenceCard({ file: 'Clinical/Archive/Clinical-FAQ-39.html' })
    expect(a.fileName).toBe('Clinical-FAQ-39.html')
    expect(a.fileDir).toBe('Patient Education/2024')
    expect(b.fileName).toBe(a.fileName)
    expect(b.fileDir).not.toBe(a.fileDir)   // the name alone could not distinguish these
  })

  it('invents nothing when the row carries no file', () => {
    const c = buildEvidenceCard({ rule_id: '1.1.1' })
    expect(c.fileName).toBe('')
    expect(c.fileDir).toBe('')
  })

  it('keeps the full reference intact for the title attribute', () => {
    expect(buildEvidenceCard({ file: 'a/b/c.pdf' }).file).toBe('a/b/c.pdf')
  })

  it('tolerates a trailing slash and duplicate separators rather than yielding an empty name', () => {
    expect(buildEvidenceCard({ file: 'a//b/c.pdf' }).fileName).toBe('c.pdf')
    expect(buildEvidenceCard({ file: 'a/b/c.pdf/' }).fileName).toBe('c.pdf')
  })
})
