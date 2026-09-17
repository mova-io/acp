/**
 * "Changes to confirm" at the DOM level: decisions are saved through the route bound to the
 * current artifact, a stale decision is flagged, nothing is shown as accepted without a record,
 * and demo mode says it does not save.
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
const { _resetSimReviews, changeDigest } = await import('./changeReview.js')

afterEach(unmountAll)

const FILE = 'guide.pdf'
const SHA = 'a'.repeat(64)
const DIFF = { rule_id: '1.1.1', seq: 0, before: '(no alt)', after: 'A red barn', note: 'Vision draft', verified: true }
const CID = `${FILE}::1.1.1::0`
const flush = async () => { await act(async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0)) }) }

async function mount(props = {}) {
  const { container, root } = createTestRoot()
  await act(async () => {
    root.render(createElement(ChangeReviewPanel, { scanId: 's1', file: FILE, diffs: [DIFF], ...props }))
  })
  await flush()
  return container
}
const button = (c, text) => [...c.querySelectorAll('button')].find((b) => b.textContent === text)

beforeEach(() => {
  api.sim = false
  _resetSimReviews()
  api.fetchChangeReviews = vi.fn(async () => ({ artifact: { currentSha256: SHA, correctedSha256: SHA, identityKind: 'corrected_sha256' }, reviews: {} }))
  api.putChangeReview = vi.fn(async (sid, file, id, body) => ({
    artifact: { currentSha256: SHA },
    review: { ...body, change_id: id, artifact_sha256: SHA, reviewer: 'r@example.com', at: '2026-09-17T10:00:00Z', stale: false },
  }))
})

describe('ChangeReviewPanel', () => {
  it('shows the change, its reason, technical status, and no decision when none is recorded', async () => {
    const c = await mount()
    const card = c.querySelector(`[data-change-id="${CID}"]`)
    expect(card).toBeTruthy()
    expect(card.textContent).toContain('(no alt)')
    expect(card.textContent).toContain('A red barn')
    expect(card.textContent).toContain('Vision draft')
    expect(card.textContent).toContain('Location not recorded')
    expect(card.textContent).toContain('Preview not available')
    expect(card.textContent).toMatch(/Verified — the re-scan/)
    expect(card.textContent).toContain('No decision recorded.')
    expect(card.textContent).not.toMatch(/Accepted/)
    for (const t of ['Accept', 'Edit', 'Reject', 'Unable to verify']) expect(button(c, t), t).toBeTruthy()
  })

  it('saves a decision bound to the current artifact and change, and announces it', async () => {
    const c = await mount()
    const note = c.querySelector('textarea')
    expect(c.querySelector(`label[for="${note.id}"]`).textContent).toMatch(/Note/)
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
      setter.call(note, 'Checked against page 3')
      note.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => { button(c, 'Accept').click() })
    await flush()
    expect(api.putChangeReview).toHaveBeenCalledTimes(1)
    const [sid, file, id, body] = api.putChangeReview.mock.calls[0]
    expect([sid, file, id]).toEqual(['s1', FILE, CID])
    expect(body.verdict).toBe('accepted')
    expect(body.note).toBe('Checked against page 3')
    expect(body.expected_sha256).toBe(SHA)
    expect(body.change_digest).toBe(await changeDigest(DIFF))
    expect(c.querySelector('[role="status"]').textContent).toMatch(/Accepted — decision recorded/)
    expect(c.textContent).toMatch(/Accepted by r@example.com/)
    expect(c.querySelector('.chgstale')).toBeNull()
  })

  it('Edit requires a corrected value and sends it', async () => {
    const c = await mount()
    await act(async () => { button(c, 'Edit').click() })
    const edit = [...c.querySelectorAll('textarea')].find((t) => /Corrected value/.test(c.querySelector(`label[for="${t.id}"]`)?.textContent || ''))
    expect(edit).toBeTruthy()
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
      setter.call(edit, 'A red barn at dusk')
      edit.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => { button(c, 'Save edit').click() })
    await flush()
    expect(api.putChangeReview.mock.calls[0][3]).toMatchObject({ verdict: 'edited', edited_value: 'A red barn at dusk' })
  })

  it('flags a decision recorded against an earlier version as needing recheck', async () => {
    api.fetchChangeReviews = vi.fn(async () => ({
      artifact: { currentSha256: 'b'.repeat(64) },
      reviews: { [CID]: { verdict: 'accepted', reviewer: 'r@example.com', at: '2026-09-01T00:00:00Z', artifact_sha256: SHA, change_digest: await changeDigest(DIFF), stale: true } },
    }))
    const c = await mount()
    const badge = c.querySelector('.chgstale')
    expect(badge).toBeTruthy()
    expect(badge.textContent).toBe('Needs recheck — the document changed since this decision')
  })

  it('flags a decision whose change content moved on, even on the same artifact', async () => {
    api.fetchChangeReviews = vi.fn(async () => ({
      artifact: { currentSha256: SHA },
      reviews: { [CID]: { verdict: 'accepted', reviewer: 'r', at: '2026-09-01T00:00:00Z', artifact_sha256: SHA, change_digest: 'c'.repeat(64) } },
    }))
    const c = await mount()
    expect(c.querySelector('.chgstale')).toBeTruthy()
  })

  it('refuses to record when the document has no recorded identity', async () => {
    api.fetchChangeReviews = vi.fn(async () => ({ artifact: { currentSha256: null }, reviews: {} }))
    const c = await mount()
    expect(button(c, 'Accept')).toBeUndefined()
    expect(c.textContent).toMatch(/Cannot record a decision: ACP has no recorded identity/)
  })

  it('refuses to record a change with no stable id', async () => {
    const c = await mount({ diffs: [{ rule_id: '1.1.1', before: 'a', after: 'b' }] })
    expect(button(c, 'Accept')).toBeUndefined()
    expect(c.textContent).toMatch(/no stable id/)
  })

  it('a failed save is shown and nothing is marked accepted', async () => {
    api.putChangeReview = vi.fn(async () => { throw new Error('the document changed since it was loaded') })
    const c = await mount()
    await act(async () => { button(c, 'Accept').click() })
    await flush()
    expect(c.querySelector('[role="status"]').textContent).toMatch(/Not saved: the document changed/)
    expect(c.textContent).toContain('No decision recorded.')
  })

  it('a failed load is an error, not an empty record', async () => {
    api.fetchChangeReviews = vi.fn(async () => { throw new Error('503 busy') })
    const c = await mount()
    expect(c.querySelector('[role="alert"]').textContent).toMatch(/Recorded decisions could not be loaded: 503 busy/)
  })

  it('demo mode says decisions are not saved and never calls the server', async () => {
    api.sim = true
    const c = await mount()
    expect(c.textContent).toMatch(/Demo mode — decisions are kept in this browser tab only and are not saved/)
    await act(async () => { button(c, 'Reject').click() })
    await flush()
    expect(api.putChangeReview).not.toHaveBeenCalled()
    expect(api.fetchChangeReviews).not.toHaveBeenCalled()
    expect(c.querySelector('[role="status"]').textContent).toMatch(/not saved/)
    expect(c.textContent).toMatch(/Rejected by Demo user.*\(demo — not saved\)/)
  })

  it('renders nothing when the file has no saved changes', async () => {
    const c = await mount({ diffs: [] })
    expect(c.textContent).toBe('')
  })
})
