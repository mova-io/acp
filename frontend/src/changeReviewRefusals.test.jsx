/**
 * The hardened change-review route, at the DOM level.
 *
 * Five things the independent review found the panel getting wrong, each proved here:
 *   1. the PUT must carry BOTH `expected_sha256` and `change_digest`; a refusal for a missing one
 *      (422) or a moved one (409) has to reach the reviewer in words they can act on;
 *   2. verdict `edited` is a CORRECTION REQUESTED — not applied, not a confirmation;
 *   3. `stale: null` is "freshness unknown — recheck", never acceptance;
 *   4. saved changes the AI applied and nothing re-scanned must appear, marked not verified;
 *   5. a preview from the ambiguous page route is never captioned "after the edit".
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

const api = vi.hoisted(() => ({ sim: false, fetchChangeReviews: null, putChangeReview: null }))
vi.mock('./api.js', () => ({
  get SIM() { return api.sim },
  fetchChangeReviews: (...a) => api.fetchChangeReviews(...a),
  putChangeReview: (...a) => api.putChangeReview(...a),
}))

const { default: ChangeReviewPanel } = await import('./ChangeReviewPanel.jsx')
const { _resetSimReviews } = await import('./changeReview.js')

afterEach(unmountAll)

const FILE = 'guide.pdf'
const SHA = 'c'.repeat(64)

// Saved changes as api/report_facts.py builds them: a verified one and an applied-but-unverified
// one whose `u<16hex>` id the client could not construct for itself.
const VERIFIED = {
  id: `${FILE}::1.1.1::0`, rule_id: '1.1.1', seq: 0, before: '(no alt)', after: 'A red barn',
  note: 'Vision draft', verified: true, verification: 'verified',
  verificationDetail: 'The re-scan after this edit no longer reported the finding.',
  changeDigest: 'cd-verified', valueClipped: false,
}
const UNVERIFIED = {
  id: `${FILE}::1.3.1::u0123456789abcdef`, rule_id: '1.3.1', seq: null, locator: 'p#3',
  before: 'Heading text', after: 'Heading text (H2)', note: null,
  verified: false, verification: 'not_verified',
  verificationDetail: 'Applied to the saved copy; no re-scan has confirmed it.',
  changeDigest: 'cd-unverified', valueClipped: true,
}

const flush = async () => { await act(async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0)) }) }

async function mount(props = {}) {
  const { container, root } = createTestRoot()
  await act(async () => {
    root.render(createElement(ChangeReviewPanel, { scanId: 's1', file: FILE, diffs: [VERIFIED], ...props }))
  })
  await flush()
  return container
}
const button = (c, text) => [...c.querySelectorAll('button')].find((b) => b.textContent === text)
const cardFor = (c, id) => c.querySelector(`[data-change-id="${id}"]`)
const click = async (el) => { await act(async () => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })) }); await flush() }

const httpError = (status, detail) => { const e = new Error(detail); e.status = status; return e }

beforeEach(() => {
  api.sim = false
  _resetSimReviews()
  api.fetchChangeReviews = vi.fn(async () => ({
    artifact: { currentSha256: SHA, correctedSha256: SHA, identityKind: 'corrected_sha256' }, reviews: {},
  }))
  api.putChangeReview = vi.fn(async (sid, file, id, body) => ({
    artifact: { currentSha256: SHA },
    review: { ...body, change_id: id, artifact_sha256: SHA, reviewer: 'r@example.com', at: '2026-09-17T10:00:00Z', stale: false },
  }))
})

describe('the PUT is bound to what the reviewer actually looked at', () => {
  it('sends both binding tokens, using the server’s own change digest and id', async () => {
    const c = await mount()
    await click(button(c, 'Accept'))
    expect(api.putChangeReview).toHaveBeenCalledTimes(1)
    const [, , id, body] = api.putChangeReview.mock.calls[0]
    expect(id).toBe(VERIFIED.id)
    // The SAVED COPY's sha-256, which is what the route binds to.
    expect(body.expected_sha256).toBe(SHA)
    // The server's digest, not one recomputed from a before/after the store already clipped.
    expect(body.change_digest).toBe('cd-verified')
  })

  it("refuses locally, in words, when the SAVED COPY's identity is not recorded", async () => {
    // currentSha256 is present — it is the ORIGINAL's checksum. Binding a decision about a saved
    // change to that would record "this edit is correct" against bytes that do not contain the
    // edit, which is what the route's 409 refuses. The panel refuses it before sending.
    api.fetchChangeReviews = vi.fn(async () => ({
      artifact: { currentSha256: 'f'.repeat(64), correctedSha256: null, identityKind: 'source' }, reviews: {},
    }))
    const c = await mount()
    expect(button(c, 'Accept')).toBeUndefined()
    expect(c.querySelector('.chgunbindable').textContent).toMatch(/saved copy’s identity is not recorded/)
    expect(c.textContent).toMatch(/before these changes can be signed off/)
    expect(api.putChangeReview).not.toHaveBeenCalled()
  })

  it('every decision on such a file reads "freshness unknown", never accepted', async () => {
    api.fetchChangeReviews = vi.fn(async () => ({
      artifact: { currentSha256: 'f'.repeat(64), correctedSha256: null },
      reviews: {
        [VERIFIED.id]: {
          verdict: 'accepted', note: null, edited_value: null, reviewer: 'r@example.com',
          at: '2026-09-17T10:00:00Z', artifact_sha256: 'f'.repeat(64), change_digest: 'cd-verified',
        },
      },
    }))
    const c = await mount()
    expect(cardFor(c, VERIFIED.id).textContent).toMatch(/Freshness unknown — recheck/)
  })

  it('a 422 tells the reviewer the decision could not be tied to a version', async () => {
    api.putChangeReview = vi.fn(async () => { throw httpError(422, 'expected_sha256 and change_digest are required') })
    const c = await mount()
    await click(button(c, 'Accept'))
    const status = cardFor(c, VERIFIED.id).querySelector('[role="status"]')
    expect(status.textContent).toMatch(/Not saved/)
    expect(status.textContent).toMatch(/tied to the exact document version and the exact change/)
    expect(status.textContent).toMatch(/Reload the document/)
  })

  it('a 409 says the document moved while they were reviewing, and to look again', async () => {
    api.putChangeReview = vi.fn(async () => { throw httpError(409, 'artifact changed') })
    const c = await mount()
    await click(button(c, 'Accept'))
    const status = cardFor(c, VERIFIED.id).querySelector('[role="status"]')
    expect(status.textContent).toMatch(/moved on while you were reviewing it/)
    expect(status.textContent).toMatch(/record the decision again/)
    // Nothing on the card may now read as an accepted decision.
    expect(cardFor(c, VERIFIED.id).textContent).toMatch(/No decision recorded/)
  })

  it('a 409 about the saved copy’s identity says exactly that', async () => {
    api.putChangeReview = vi.fn(async () => { throw httpError(409, "the saved copy's identity is not recorded") })
    const c = await mount()
    await click(button(c, 'Accept'))
    expect(cardFor(c, VERIFIED.id).querySelector('[role="status"]').textContent)
      .toMatch(/no recorded checksum for the saved corrected copy/)
  })
})

describe('what the recorded verdicts mean', () => {
  it('"edited" reads as a correction REQUESTED, never as applied or confirmed', async () => {
    api.fetchChangeReviews = vi.fn(async () => ({
      artifact: { currentSha256: SHA, correctedSha256: SHA },
      reviews: {
        [VERIFIED.id]: {
          verdict: 'edited', note: null, edited_value: 'A red barn in a field',
          reviewer: 'r@example.com', at: '2026-09-17T10:00:00Z', artifact_sha256: SHA,
          change_digest: 'cd-verified', stale: false,
        },
      },
    }))
    const c = await mount()
    const card = cardFor(c, VERIFIED.id)
    expect(card.textContent).toMatch(/Correction requested/)
    expect(card.textContent).toMatch(/NOT been applied to the document/)
    expect(card.textContent).toMatch(/does not count as a confirmation/)
    expect(card.textContent).toMatch(/Requested wording \(not applied\): A red barn in a field/)
    // The old wording asserted the opposite: that the edit was accepted (and so written).
    expect(card.textContent).not.toMatch(/Accepted with edits/)
  })

  it('unknown freshness reads "recheck", not acceptance', async () => {
    api.fetchChangeReviews = vi.fn(async () => ({
      // No current artifact identity => the server cannot tell whether the decision still holds.
      artifact: { currentSha256: null, correctedSha256: null },
      reviews: {
        [VERIFIED.id]: {
          verdict: 'accepted', note: null, edited_value: null, reviewer: 'r@example.com',
          at: '2026-09-17T10:00:00Z', artifact_sha256: SHA, change_digest: 'cd-verified',
          stale: null, staleReason: 'No current artifact checksum is recorded.',
        },
      },
    }))
    const c = await mount()
    const card = cardFor(c, VERIFIED.id)
    expect(card.textContent).toMatch(/Freshness unknown — recheck/)
    expect(card.textContent).toMatch(/No current artifact checksum is recorded\./)
  })
})

describe('the changes that actually need a human are the ones shown', () => {
  it('lists applied-but-unverified changes, marked "AI applied · not verified"', async () => {
    const c = await mount({ diffs: [VERIFIED, UNVERIFIED] })
    const card = cardFor(c, UNVERIFIED.id)
    expect(card, 'the unverified saved change is missing from the panel').toBeTruthy()
    expect(card.querySelector('.chgunverified').textContent).toBe('AI applied · not verified')
    expect(card.textContent).toMatch(/no re-scan has confirmed it/i)
    // And the verified one is still distinguished, so the two are not levelled.
    expect(cardFor(c, VERIFIED.id).querySelector('.chgverified')).toBeTruthy()
  })

  it('says a clipped value was clipped WHEN RECORDED, not "see the full report"', async () => {
    const c = await mount({ diffs: [UNVERIFIED] })
    const clipped = cardFor(c, UNVERIFIED.id).querySelector('.chgclipped')
    expect(clipped.textContent).toMatch(/clipped by the store when it was recorded/)
    expect(clipped.textContent).toMatch(/not available in any report/)
    expect(clipped.textContent).not.toMatch(/Full evidence/)
  })

  it('a decision can be recorded against an unverified change, under its server id', async () => {
    const c = await mount({ diffs: [UNVERIFIED] })
    const accept = [...cardFor(c, UNVERIFIED.id).querySelectorAll('button')].find((b) => b.textContent === 'Accept')
    await click(accept)
    const [, , id, body] = api.putChangeReview.mock.calls[0]
    expect(id).toBe(`${FILE}::1.3.1::u0123456789abcdef`)
    expect(body.change_digest).toBe('cd-unverified')
  })
})

describe('a list that is missing the changes awaiting review says so', () => {
  it('shows the gap as an alert, and still shows what did arrive', async () => {
    // What is left when the unverified records cannot be read is the VERIFIED work — the work that
    // needs nothing from the reader. A page of that, with no notice, reads as "all done".
    const c = await mount({
      diffs: [VERIFIED],
      diffsError: 'The changes awaiting review could not be read, so this list is incomplete. The changes ACP applied and has not re-checked are exactly the ones that need a person, and none of them are shown here. Do not treat this as a complete list.',
    })
    const alert = c.querySelector('.chgincomplete')
    expect(alert).toBeTruthy()
    expect(alert.getAttribute('role')).toBe('alert')
    expect(alert.textContent).toMatch(/^The changes awaiting review could not be read/)
    expect(alert.textContent).toMatch(/Do not treat this as a complete list/)
    // It is not re-prefixed with a generic "could not be loaded", which buries the specific claim.
    expect(alert.textContent).not.toMatch(/Saved changes could not be loaded/)
    expect(cardFor(c, VERIFIED.id)).toBeTruthy()
  })

  it('other load failures keep the generic prefix', async () => {
    const c = await mount({ diffs: [VERIFIED], diffsError: '503 Service Unavailable' })
    expect(c.querySelector('.chgincomplete').textContent).toBe('Saved changes could not be loaded: 503 Service Unavailable')
  })
})

describe('preview provenance on screen', () => {
  it('captions an exact-bytes preview by version, and never says "after the edit"', async () => {
    const previews = {
      [VERIFIED.id]: {
        src: 'data:image/png;base64,AAAA', provenance: 'corrected', sha256: SHA, page: 2,
        caption: `Corrected copy, page 2 (sha ${SHA.slice(0, 12)})`,
        alt: 'Page 2 of the corrected copy', note: null, verified: true,
      },
    }
    const c = await mount({ previews })
    const fig = cardFor(c, VERIFIED.id).querySelector('figure')
    expect(fig.className).toMatch(/chgpreview-corrected/)
    expect(fig.querySelector('figcaption').textContent).toBe(`Corrected copy, page 2 (sha ${SHA.slice(0, 12)})`)
    expect(fig.querySelector('img').getAttribute('alt')).toBe('Page 2 of the corrected copy')
    expect(c.textContent).not.toMatch(/after the edit/i)
  })

  it('a bare data URL carries no provenance, so it is shown as version-not-verified', async () => {
    const c = await mount({ previews: { [VERIFIED.id]: 'data:image/png;base64,AAAA' } })
    const fig = cardFor(c, VERIFIED.id).querySelector('figure')
    expect(fig.className).toMatch(/chgpreview-unverified/)
    expect(fig.textContent).toMatch(/Document preview — version not verified/)
    expect(fig.textContent).toMatch(/not evidence of what changed/)
    expect(c.textContent).not.toMatch(/after the edit/i)
  })

  it('no preview at all says so explicitly', async () => {
    const c = await mount()
    expect(cardFor(c, VERIFIED.id).textContent).toMatch(/Visual preview not available for this change/)
  })
})
