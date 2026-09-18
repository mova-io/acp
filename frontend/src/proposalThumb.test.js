import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { isSafeThumb } from './ProposalThumb.jsx'
import { firstThumb, firstProposed, firstRationale, firstSource, buildEvidenceCard } from './reviewCard.js'

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='

describe('isSafeThumb — an untrusted string must never reach an <img src>', () => {
  it('accepts the data URLs _thumb_b64 produces', () => {
    expect(isSafeThumb(PNG)).toBe(true)
    expect(isSafeThumb('data:image/jpeg;base64,/9j/4AAQ')).toBe(true)
    expect(isSafeThumb('data:image/webp;base64,UklGRg==')).toBe(true)
  })

  it('rejects anything that is not an image data URL', () => {
    // proposals arrive as JSON from the database; a bad value must render nothing, not execute.
    expect(isSafeThumb('javascript:alert(1)')).toBe(false)
    expect(isSafeThumb('data:text/html;base64,PHNjcmlwdD4=')).toBe(false)
    expect(isSafeThumb('https://evil.example/x.png')).toBe(false)
    expect(isSafeThumb('data:image/png;base64,abc"onerror=alert(1)')).toBe(false)
    expect(isSafeThumb('')).toBe(false)
    expect(isSafeThumb(null)).toBe(false)
    expect(isSafeThumb(42)).toBe(false)
  })
})

describe('proposal accessors', () => {
  const item = { rule_id: '1.1.1', file: 'deck.pptx', proposals: [
    { locator: 'ppt/slides/slide3.xml#rId4', before: '(no alt text)',
      proposed_value: 'A clinician reviewing intake forms with a parent.',
      rationale: 'No text found in the image; described from visual content.',
      source: 'AI vision model (llava)', thumb: PNG },
  ] }

  it('reads the model draft, the image, the rationale and the model', () => {
    expect(firstProposed(item)).toMatch(/clinician/)
    expect(firstThumb(item)).toBe(PNG)
    expect(firstRationale(item)).toMatch(/No text found/)
    expect(firstSource(item)).toBe('AI vision model (llava)')
  })

  it('returns null when there is no proposal, rather than inventing one', () => {
    for (const empty of [{}, { proposals: [] }, { proposals: null }, null]) {
      expect(firstThumb(empty)).toBeNull()
      expect(firstProposed(empty)).toBeNull()
    }
  })

  it('the evidence card carries the image and the reasoning', () => {
    const card = buildEvidenceCard(item)
    expect(card.thumb).toBe(PNG)
    expect(card.rationale).toMatch(/No text found/)
    expect(card.proposalSource).toBe('AI vision model (llava)')
  })
})

describe('the review screens render the proposal, not a template', () => {
  it('Remediate never falls back to a canned "after" string', () => {
    // `after: it.approved_value || template` always hit the template: nothing server-side ever
    // writes approved_value, so every image in every document showed the same sentence. The
    // fallback is gone — a missing draft must read as missing, not as a fix already applied.
    const src = read('Remediate.jsx')
    expect(src).toMatch(/after: firstProposed\(it\) \|\| it\.approved_value \|\| null/)
    expect(src).not.toMatch(/after: firstProposed\(it\)[^\n]*ba\.after/)
    expect(src).toMatch(/thumb: firstThumb\(it\)/)
  })

  it('a template is never recorded as an AI suggestion', () => {
    // Was a grep of Remediate's WhyReview ('AI suggested value' : 'Next step'), which never
    // rendered. EvidenceCard enforces the same thing where it counts: a template sets the
    // message and deliberately does NOT set aiDraft, so approving it verbatim counts as
    // human-authored and reviewTelemetry cannot log it as an accepted AI value.
    const card = read('EvidenceCard.jsx')
    expect(card).toMatch(/if \(r\.is_template\) \{[\s\S]{0,900}?setDraftMsg\(\{ kind: 'template'/)
    // aiDraft is assigned only on the genuine-value branch, after the template branch closes.
    expect(card).toMatch(/\} else \{\s*\n\s*aiDraft\.current = s/)
  })

  it('no canned "a fix was applied" string survives in either queue builder', () => {
    // Both the DB-backed queue and the SIM queue used to hand the card a constant sentence
    // claiming alt text had been added. Neither ITEM_BA nor its fallback carries an `after`.
    const src = read('Remediate.jsx')
      .replace(/\/\*[\s\S]*?\*\//g, '')                              // block + JSX comments
      .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')
    expect(src).not.toMatch(/AI-generated alt text added/)
    expect(src).not.toMatch(/'AI fix applied — review before certifying'/)
    expect(src).not.toMatch(/after: \(\) =>/)
  })

  it('Remediate renders the master/detail RemediationInbox, fed the live queue', () => {
    // The Review queue is now a master/detail queue (RemediationInbox): selecting a finding
    // populates a detail pane whose before/after shows the ACTUAL proposed value (the mapped
    // item's `after`, from firstProposed), never a template. The proposal-rendering is
    // render-tested in RemediationInbox.test.jsx; here we pin the wiring.
    const src = read('Remediate.jsx')
    expect(src).toMatch(/<RemediationInbox/)
    // fed the combined queue: the human review items PLUS the auto-applied fixes folded in as
    // green review-lane rows (autoFixRows).
    expect(src).toMatch(/queue=\{inboxQueue\}/)
    // …plus the rejected-fix handoff rows (W2) and the rows that already carry a decision, so a
    // decided item stays accounted for instead of leaving the page (hitlDecidedTracking).
    expect(src).toContain('inboxQueue = automaticReviewQueue(reviewTasks, runAiApproval.policy')
    expect(src).toMatch(/reviewQueue = reviewableRemediationItems\(dedupeById\(\[\.\.\.queue, \.\.\.rejectedItems, \.\.\.decidedItems, \.\.\.autoFixItems\]\)/)
    expect(src).toMatch(/proposals: it\.proposals/)   // dbItemToUi still carries proposals through
  })

  it('EvidenceCard shows the large page hero AND the object thumb (ADR 0018 visual-first)', () => {
    const src = read('EvidenceCard.jsx')
    // The finding's page rendered large is the HERO (Principle 2) — no longer a mere fallback:
    // rendered whenever scanId+file are present (self-hides if the backend can't rasterize).
    // (window sized to admit the pager + the vision-§17 page strip that now sit between them)
    expect(src).toMatch(/evcard-hero[\s\S]{0,1800}<Thumbnail scanId=\{card\.scanId\} file=\{card\.file\} page=\{card\.page \|\| 1\}/)
    // Slice 2: the hero passes the finding's locator so the page render carries the bounding box.
    // (heroLocator = the paged instance's locator, defaulting to card.locator — #122 pager.)
    expect(src).toMatch(/<Thumbnail[^>]*locator=\{heroLocator\}[^>]*maxHeight=\{360\}/)
    // #122 pager: with many flagged images the hero steps through each one.
    expect(src).toMatch(/instances\.length > 1 &&[\s\S]{0,500}Image \{heroIdx \+ 1\} of \{instances\.length\}/)
    // The offending image still renders as the object beside the text — following the pager, but
    // suppressed when the hero already IS that image (xlsx isolated-image lead), or
    // a disclosed crop has its own thumbnail and crop-specific transcription panel.
    expect(src).toMatch(/\{heroThumb && !heroIsImage && !heroCrop && \([\s\S]{0,80}<ProposalThumb/)
    expect(src).toMatch(/<CropReviewContext evidence=\{heroCrop\} thumb=\{heroThumb\} locator=\{heroLocator\}/)
  })

  it('both screens draw the thumb from the same helper', () => {
    expect(read('Remediate.jsx')).toMatch(/from '\.\/reviewCard\.js'/)
    expect(read('EvidenceCard.jsx')).toMatch(/from '\.\/reviewCard\.js'/)
  })
})
