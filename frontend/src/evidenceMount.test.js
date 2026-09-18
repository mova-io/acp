import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { VALUE_FIX, isValueFix, reviewTelemetry, locationLabel, buildEvidenceCard } from './reviewCard.js'

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')

describe('VALUE_FIX is shared, not duplicated', () => {
  it('covers every criterion whose fix is CONTENT the document must carry', () => {
    // Not only text a screen reader speaks: a `lang` marking is content too, and has to be
    // written in. 3.1.2 was missing, so its card promised "Fail → Pass after approval" and
    // "Approve all judgement items" swept it up — certifying files whose foreign passages
    // were still unmarked.
    expect([...VALUE_FIX].sort()).toEqual(
      ['1.1.1', '1.3.3', '2.4.2', '2.4.4', '2.4.9', '3.1.1', '3.1.2', '3.3.2'])
    expect(isValueFix('1.1.1')).toBe(true)
    expect(isValueFix('3.1.2')).toBe(true)    // a lang attribute is written, not merely agreed to
    expect(isValueFix('1.4.3')).toBe(false)   // contrast is a judgement call, nothing to type
    expect(isValueFix('1.4.6')).toBe(false)
  })

  it('ReviewCenter no longer keeps its own copy', () => {
    // Two screens disagreeing about which items get an editor is how a reviewer approves an
    // empty alt text on one and not the other.
    expect(read('ReviewCenter.jsx')).not.toMatch(/const VALUE_FIX = new Set/)
    expect(read('ReviewCenter.jsx')).toMatch(/from '\.\/reviewCard\.js'/)
  })
})

describe('reviewTelemetry — what the review actually was', () => {
  const base = { editable: true, status: 'approved', aiDraft: 'A cat', elapsedMs: 4200 }

  it('records the elapsed reviewer time', () => {
    expect(reviewTelemetry(base).reviewMs).toBe(4200)
  })

  it('flags an edit when the approved text differs from the AI draft', () => {
    expect(reviewTelemetry({ ...base, value: 'A tabby cat' }).edited).toBe(true)
    expect(reviewTelemetry({ ...base, value: 'A cat' }).edited).toBe(false)
  })

  it('counts authoring a value the AI could not draft as an edit', () => {
    expect(reviewTelemetry({ ...base, aiDraft: null, value: 'A tabby cat' }).edited).toBe(true)
  })

  it('keeps the AI draft alongside the final value, so both are auditable', () => {
    const t = reviewTelemetry({ ...base, value: 'A tabby cat' })
    expect(t.aiValue).toBe('A cat')
    expect(t.finalValue).toBe('A tabby cat')
  })

  it('sends no value on reject or skip — only an approval sets one', () => {
    expect(reviewTelemetry({ ...base, status: 'rejected', value: 'x' }).finalValue).toBeNull()
    expect(reviewTelemetry({ ...base, status: 'skipped', value: 'x' }).finalValue).toBeNull()
    expect(reviewTelemetry({ ...base, status: 'rejected', value: 'x' }).edited).toBe(false)
  })

  it('sends no value for a judgement item, which has nothing to type', () => {
    expect(reviewTelemetry({ ...base, editable: false, value: 'x' }).finalValue).toBeNull()
  })
})

describe('the evidence card is actually mounted, and owns the write', () => {
  const rc = () => read('ReviewCenter.jsx')

  it('ReviewCenter renders EvidenceCard for the open item', () => {
    expect(rc()).toMatch(/<EvidenceCard/)
    expect(rc()).toMatch(/import EvidenceCard from '\.\/EvidenceCard\.jsx'/)
  })

  it('the card decides through the parent, preserving the optimistic update', () => {
    // Calling updateHitlItem directly would bypass HitlBell's optimistic state + drain event.
    expect(read('EvidenceCard.jsx')).not.toMatch(/updateHitlItem/)
    // noteOut/finalValue collapse to note/value for a normal decision, and self-describe a
    // WCAG-exception resolution (decorative / essential logo) — either way it flows through onAct.
    expect(read('EvidenceCard.jsx')).toMatch(/onAct\(card\.id, status, noteOut, finalValue/)
  })

  it('telemetry reaches the API — hitl_events.review_ms was previously always null', () => {
    // `bound` is the viewed version (expected_version + source revision + snapshot ids) — see
    // viewedApprovalBell.test.jsx for the request body it produces.
    expect(read('HitlBell.jsx')).toMatch(/updateHitlItem\(itemId, status, note, approvedValue, \{ \.\.\.telemetry, \.\.\.bound \}\)/)
  })

  it('an editor appears for value fixes even when the AI drafted nothing', () => {
    // The old gate was `aiDraft != null`, which hid the box exactly when a human was needed.
    expect(read('EvidenceCard.jsx')).toMatch(/isValueFix\(card\.sc\)/)
    expect(read('EvidenceCard.jsx')).not.toMatch(/aiDraft\.current != null/)
  })
})

describe('locationLabel — where the criterion fails', () => {
  it('names slides for a deck and pages for a PDF', () => {
    expect(locationLabel({ file: 'deck.pptx', pages: '3' })).toBe('Slide 3')
    expect(locationLabel({ file: 'policy.pdf', pages: '7' })).toBe('Page 7')
    expect(locationLabel({ file: 'memo.docx', pages: '2' })).toBe('Page 2')
  })

  it('lists every page — 11 failing slides must not read as one', () => {
    expect(locationLabel({ file: 'deck.pptx', pages: '1,3,7' })).toBe('Slides 1, 3, 7')
  })

  it('caps a long list rather than flooding the header', () => {
    expect(locationLabel({ file: 'deck.pptx', pages: '1,2,3,4,5,6,7,8' })).toBe('Slides 1, 2, 3, 4, 5, 6 +2')
  })

  it('renders nothing when the analyser attributed no page', () => {
    // xlsx locates by cell; some rules are file-level. Show no location, never a wrong one.
    expect(locationLabel({ file: 'book.xlsx', pages: null })).toBeNull()
    expect(locationLabel({ file: 'deck.pptx', pages: '' })).toBeNull()
    expect(locationLabel({})).toBeNull()
    expect(locationLabel(null)).toBeNull()
  })

  it('survives a malformed pages string', () => {
    expect(locationLabel({ file: 'deck.pptx', pages: 'x,,3' })).toBe('Slide 3')
  })
})

describe('the card surfaces the location', () => {
  it('buildEvidenceCard carries location + page through', () => {
    const card = buildEvidenceCard({ id: 'i1', file: 'deck.pptx', rule_id: '1.1.1', page: 3, pages: '3,5' })
    expect(card.location).toBe('Slides 3, 5')
    expect(card.page).toBe(3)
  })

  it('EvidenceCard renders it only when present', () => {
    const src = read('EvidenceCard.jsx')
    expect(src).toMatch(/card\.location &&/)
  })
})
