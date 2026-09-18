import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { firstBefore, pageOf, buildEvidenceCard } from './reviewCard.js'

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')

// A reviewer approving a change they cannot see is not a reviewer. Two facts make the review
// card honest: the PAGE the finding sits on (so the render shown is the right one), and the
// BEFORE value the fix replaces (so "what was added" has something to be added to).
//
// Both must fail closed. There is no bounding box anywhere in the pipeline and no page on many
// findings — when the analysers attributed nothing, the card shows nothing. It never guesses
// page 1, because page 1 is a real page and a picture of it is a specific false claim.

describe('pageOf — the analysers attribution, or nothing', () => {
  it('returns the page the finding was attributed to', () => {
    expect(pageOf({ page: 7 })).toBe(7)
    expect(pageOf({ page: 1 })).toBe(1)
  })

  it('never defaults to page 1 — an unattributed finding has no page', () => {
    expect(pageOf({})).toBeNull()
    expect(pageOf({ page: null })).toBeNull()
    expect(pageOf(null)).toBeNull()
    expect(pageOf(undefined)).toBeNull()
  })

  it('rejects values that are not a real page number', () => {
    expect(pageOf({ page: 0 })).toBeNull()        // 0-based leak, not a page
    expect(pageOf({ page: -1 })).toBeNull()
    expect(pageOf({ page: 2.5 })).toBeNull()
    expect(pageOf({ page: '3' })).toBeNull()      // getFilePage would build /page/3 by luck
    expect(pageOf({ page: NaN })).toBeNull()
  })
})

describe('firstBefore — the literal passage being replaced', () => {
  const item = (before) => ({ proposals: [{ proposed_value: 'es', ...(before !== undefined ? { before } : {}) }] })

  it('reads the offending text off the first proposal', () => {
    expect(firstBefore(item('El informe anual está disponible'))).toBe('El informe anual está disponible')
  })

  it('is null when the proposal named no before — the caller then describes the defect instead', () => {
    expect(firstBefore(item())).toBeNull()
    expect(firstBefore({ proposals: [] })).toBeNull()
    expect(firstBefore({})).toBeNull()
    expect(firstBefore(null)).toBeNull()
  })

  it('is distinct from the proposed value — a card showing one as the other says nothing', () => {
    const p = { before: 'click here', proposed_value: 'Download the 2026 annual report (PDF)' }
    expect(firstBefore({ proposals: [p] })).not.toBe(p.proposed_value)
  })
})

describe('Thumbnail fetches the page it claims to show', () => {
  const src = read('Thumbnail.jsx')

  it('routes a later page to /page/{n} and page 1 to the cheaper /thumbnail', () => {
    // renderPage = the geometry box's page when a locator resolves one, else the `page` prop.
    // Page N asks for `{ detail: true }` so the server can say which page it drew — the generic
    // route clamps, and a picture is only captioned page N when that is confirmed.
    expect(src).toMatch(/renderPage === 1\s*\?\s*getFileThumbnail\(scanId, file\)[\s\S]{0,80}:\s*getFilePage\(scanId, file, renderPage, \{ detail: true \}\)/)
  })

  it('re-fetches when the page changes — a stale render is the wrong page', () => {
    expect(src).toMatch(/\[scanId, file, renderPage, geomResolved\]/)
  })

  it('waits for geometry before rendering (no flash of the fallback page)', () => {
    expect(src).toMatch(/if \(!geomResolved\) return/)
  })

  it('names the page it actually rendered — hardcoded "page 1" alt is inaccurate alt text', () => {
    // ...and only when the page is CONFIRMED; otherwise the alt says it is not (thumbnailPages.test.jsx
    // checks the rendered DOM).
    expect(src).toMatch(/confirmed \? `Page \$\{renderPage\} of/)
    expect(src).not.toMatch(/`Page 1 of/)
  })
})

// These used to grep Remediate.jsx's WhyReview/ReviewItemCard, which were never rendered — the
// inbox has shipped EvidenceCard for a long time. The claims are still worth guarding, so they
// now point at the card that actually renders, plus the queue mapping that feeds it. One claim
// moved out entirely: "a deck has no page 3" is pageNoun via locationLabel, and
// evidenceMount.test.js asserts 'Slide 3' vs 'Page 7' on the function itself — a behavioural
// check, which beats grepping markup for it.
describe('the review card shows the reviewer the page and the passage', () => {
  const card = read('EvidenceCard.jsx')
  const src = read('Remediate.jsx')

  it('falls back to the rendered page when the proposal carried no image of its own', () => {
    // A 3.1.2 / 2.4.4 finding has no picture; without this the reviewer approved blind.
    expect(card).toMatch(/<ProposalThumb[\s\S]{0,400}\) : \([\s\S]{0,80}<Thumbnail/)
  })

  it('passes the finding page — not the cover — down to the render', () => {
    // heroPage: the pager's measured page, else the recorded page — never `|| 1`, which captioned
    // an unplaced finding "Page 1".
    expect(card).toMatch(/<Thumbnail[^>]*page=\{heroPage\}/)
    expect(card).not.toMatch(/page=\{card\.page \|\| 1\}/)
    expect(src).toMatch(/page: pageOf\(it\)/)
  })

  it('shows the current text only when the proposal supplied a literal one', () => {
    // `ba.before()` is a description of the defect ("image / chart — no alt text"). Labelling
    // that "Current text" would put a sentence the document does not contain on the card.
    expect(src).toMatch(/beforeLiteral: !!firstBefore\(it\)/)
    expect(src).toMatch(/before: firstBefore\(it\) \|\| ba\.before/)
  })
})

describe('buildEvidenceCard carries the validated page, not the raw row value', () => {
  it('surfaces the finding page so the inbox renders that page', () => {
    expect(buildEvidenceCard({ rule_id: '1.1.1', file: 'x.pdf', page: 7 }).page).toBe(7)
  })

  it('passes a bad page through as null rather than to the /page/{n} endpoint', () => {
    // Regression: the card assembled `page` twice — the second, unvalidated assignment won,
    // so a 0 or a "3" reached <Thumbnail page>. Only one `page` key may exist.
    expect(buildEvidenceCard({ rule_id: '1.1.1', file: 'x.pdf', page: 0 }).page).toBeNull()
    expect(buildEvidenceCard({ rule_id: '1.1.1', file: 'x.pdf', page: '3' }).page).toBeNull()
    expect(buildEvidenceCard({ rule_id: '1.1.1', file: 'x.pdf' }).page).toBeNull()
  })
})
