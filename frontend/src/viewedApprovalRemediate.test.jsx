// P1 approval binding, on the REAL Remediate page: an approval is bound to the version of the review
// row the reviewer was LOOKING AT — its source revision and its proposal snapshot ids — and never to
// whatever the server happens to hold at click time.
//
// Audit gap 8: a single approval sent only `expected_version`, so a re-assessment between opening the
// suggestion and clicking "Apply this fix" bound the approval to bytes the reviewer never saw (the
// server stamped its own current revision). Only a frozen batch selection sent a source revision.
//
// Mounted for real — the page, the review pane, the real api.js and the real queue-refresh hook — with
// only `fetch` replaced by a small synthetic server that implements the PUT /hitl/queue/{id} contract
// (409 stale_viewed_version / viewed_version_required). DOM-level on purpose: the preview server runs
// against the shared checkout, not this worktree (CLAUDE.md).
import { act, createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import Remediate from './Remediate.jsx'

vi.mock('./sim.js', async actual => ({ ...(await actual()), SIM: false }))
afterEach(() => { unmountAll(); vi.unstubAllGlobals() })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }

const SCAN = 'scan-viewed'
const FILE = 'synthetic-viewed.docx'
const STALE = 'This suggestion or its document changed after you opened it. Review the current version, then approve again.'
const SHA_A = 'a'.repeat(64)   // the corrected copy on screen
const SHA_C = 'c'.repeat(64)   // the corrected copy after another approved write

// One pending link-text row. Version A is what the page loads; version B is what a re-assessment
// produces while the reviewer is looking at A.
const rowA = { id: 'item-a', scan_id: SCAN, file: FILE, rule_id: '2.4.4', rule_name: 'Link purpose', status: 'pending',
  decision_version: 3, source_revision: 'rev-A', proposal_snapshot_ids: ['snap-A1'], finding_count: 1, corrected_artifact: SHA_A,
  proposals: [{ locator: 'docx:link:1', before: 'click here', proposed_value: 'Read the synthetic summary', source: 'AI' }] }
const rowB = { ...rowA, source_revision: 'rev-B', proposal_snapshot_ids: ['snap-B1'],
  proposals: [{ locator: 'docx:link:1', before: 'click here', proposed_value: 'Open the synthetic quarterly summary', source: 'AI' }] }

// A synthetic server. `current` is the row the server holds now; `nested` answers the 409 the way a
// FastAPI HTTPException(detail={...}) does, instead of a top-level JSON body.
function server({ current, nested = false }) {
  const s = { current, puts: [], lists: 0 }
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  s.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url)
    if (opts.method === 'PUT' && /\/hitl\/queue\/[^/?]+$/.test(u)) {
      const body = JSON.parse(opts.body)
      s.puts.push({ url: u, method: opts.method, body })
      const refuse = (code, message) => json(nested ? { detail: { code, message } } : { code, message, detail: code }, 409)
      if (body.status === 'approved' && (body.expected_source_revision == null || body.expected_proposal_snapshot_ids == null
          || body.expected_corrected_sha256 == null))
        return refuse('viewed_version_required', 'Refresh this suggestion before approving it.')
      if (body.expected_source_revision != null && (body.expected_source_revision !== s.current.source_revision
          || JSON.stringify(body.expected_proposal_snapshot_ids) !== JSON.stringify(s.current.proposal_snapshot_ids)
          || (body.expected_corrected_sha256 != null && body.expected_corrected_sha256 !== s.current.corrected_artifact))) {
        // D's contract: a frozen batch (no approval_scope) keeps the legacy HTTPException detail string.
        if (body.approval_scope !== 'single') return json({ detail: 'stale source revision' }, 409)
        return refuse('stale_viewed_version', STALE)
      }
      // Recorded: the row moves on (a held approval is re-bound, so it is no longer flagged).
      s.current = { ...s.current, status: body.status, approval_recheck_required: false,
        decision_version: s.current.decision_version + 1 }
      return json(s.current)
    }
    if (opts.method === 'POST' && /\/retry-write$/.test(u)) {
      s.retries = (s.retries || 0) + 1
      return json(s.retryAnswer || { accepted: true, in_flight: true, status: 'queued', reason: null })
    }
    if (/\/hitl\/queue\?scan_id=/.test(u)) { s.lists += 1; return json([s.current]) }
    return json({})
  })
  return s
}

async function mount(srv) {
  history.replaceState({}, '', '/?tab=remediate&mode=review')
  vi.stubGlobal('fetch', srv.fetch)
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Remediate, { run: { id: SCAN, status: 'completed' },
    files: [{ file: FILE, type: 'DOCX', issues: [] }] })))
  await settle()
  const filter = container.querySelector('select[aria-label="Filter by status"]')
  if (filter) await act(async () => { filter.value = 'all'; filter.dispatchEvent(new Event('change', { bubbles: true })) })
  return container
}
const settle = (ms = 50) => act(async () => { await new Promise(r => setTimeout(r, ms)) })
const pane = c => c.querySelector('.remediation-detail')
const button = (root, label) => [...root.querySelectorAll('button')].find(b => b.textContent.trim() === label)
async function open(c, id) {
  const row = c.querySelector(`#rinbox-row-${id}`)
  expect(row, c.textContent.slice(0, 600)).toBeTruthy()
  await act(async () => { row.click() })
}
const click = el => act(async () => { el.click() })

describe('Remediate — single approvals carry the VIEWED version', () => {
  it('an ordinary approval sends the viewed source revision and proposal snapshot ids', async () => {
    const srv = server({ current: rowA })
    const c = await mount(srv)
    await open(c, 'item-a')
    await click(button(pane(c), 'Apply this fix'))
    await settle()
    expect(srv.puts).toHaveLength(1)
    const [put] = srv.puts
    expect(put.method).toBe('PUT')
    expect(put.url).toMatch(/\/hitl\/queue\/item-a$/)
    expect(put.body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 3,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'], expected_corrected_sha256: SHA_A })
  })

  it('another approved write changed the corrected copy: the body carries the VIEWED sha, the 409 is shown, and nothing retries', async () => {
    const srv = server({ current: rowA })
    const c = await mount(srv)
    await open(c, 'item-a')
    // A different approved fix was written to this document: same revision and proposals, new corrected copy.
    srv.current = { ...rowA, corrected_artifact: SHA_C }
    await click(button(pane(c), 'Apply this fix'))
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ approval_scope: 'single', expected_corrected_sha256: SHA_A })
    expect(c.textContent).toContain(STALE)
    await settle(300)
    expect(srv.puts).toHaveLength(1)
    await click(button(pane(c), 'Apply this fix'))
    await settle()
    expect(srv.puts).toHaveLength(2)
    expect(srv.puts[1].body).toMatchObject({ approval_scope: 'single', expected_corrected_sha256: SHA_C })
  })

  it('a row without a corrected-artifact binding is not approved unbound, and nothing is sent', async () => {
    const { corrected_artifact, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server({ current: unbound })
    const c = await mount(srv)
    await open(c, 'item-a')
    await click(button(pane(c), 'Apply this fix'))
    await settle(120)
    expect(srv.puts).toHaveLength(0)
    expect(pane(c).textContent).toMatch(/must be refreshed before it can be approved/)
  })

  it('the "none" sentinel (no corrected copy yet) is sent verbatim', async () => {
    const srv = server({ current: { ...rowA, corrected_artifact: 'none' } })
    const c = await mount(srv)
    await open(c, 'item-a')
    await click(button(pane(c), 'Apply this fix'))
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ approval_scope: 'single', expected_corrected_sha256: 'none' })
  })

  it('a re-assessment between view and click: the body carries the VIEWED revision, the 409 is shown, the row refreshes, and nothing retries', async () => {
    const srv = server({ current: rowA, nested: true })
    const c = await mount(srv)
    await open(c, 'item-a')
    expect(pane(c).textContent).toContain('Read the synthetic summary')
    // The re-assessment lands on the server; this page has not re-read the queue.
    srv.current = rowB
    const listsBefore = srv.lists
    await click(button(pane(c), 'Apply this fix'))
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 3,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
    expect(srv.puts[0].body.expected_source_revision).not.toBe('rev-B')
    // The server's own sentence, not "[object Object]" and not a generic failure.
    expect(c.textContent).toContain(STALE)
    expect(c.textContent).not.toContain('[object Object]')
    // That row was re-read, and the pane now shows the CURRENT version, still selected.
    expect(srv.lists).toBeGreaterThan(listsBefore)
    expect(c.querySelector('#rinbox-row-item-a')?.getAttribute('aria-current')).toBe('true')
    expect(pane(c).textContent).toContain('Open the synthetic quarterly summary')
    // Never an automatic second attempt.
    await settle(300)
    expect(srv.puts).toHaveLength(1)
    // A fresh decision on the current version is the reviewer's to make — and binds to B.
    await click(button(pane(c), 'Apply this fix'))
    await settle()
    expect(srv.puts).toHaveLength(2)
    expect(srv.puts[1].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 3,
      expected_source_revision: 'rev-B', expected_proposal_snapshot_ids: ['snap-B1'] })
  })

  it('a row without its version binding is not approved unbound: the pane says to refresh, and the queue is re-read', async () => {
    const { source_revision, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server({ current: unbound })
    const c = await mount(srv)
    await open(c, 'item-a')
    const listsBefore = srv.lists
    await click(button(pane(c), 'Apply this fix'))
    await settle(120)
    expect(srv.puts).toHaveLength(0)
    expect(pane(c).textContent).toMatch(/must be refreshed before it can be approved/)
    expect(srv.lists).toBeGreaterThan(listsBefore)
    expect(c.querySelector('#rinbox-row-item-a')?.getAttribute('aria-current')).toBe('true')
  })

  it('a frozen batch approval keeps its own frozen binding and the batch guard (no single scope)', async () => {
    const srv = server({ current: rowA })
    const c = await mount(srv)
    const all = [...c.querySelectorAll('button')].find(b => /^Approve all ready/.test(b.textContent.trim()))
    expect(all, 'batch control is mounted').toBeTruthy()
    await click(all)
    await settle()
    const confirm = [...c.querySelectorAll('button')].find(b => /^Confirm approval of/.test(b.textContent.trim()))
    if (confirm) { await click(confirm); await settle() }
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: null, expected_version: 3,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'], expected_corrected_sha256: SHA_A,
      approved_values: ['Read the synthetic summary'] })
  })

  it('a stale frozen batch (another approved write changed the corrected copy) is refused honestly: nothing recorded, the selection refreshes, no retry', async () => {
    const srv = server({ current: rowA })
    const c = await mount(srv)
    // The page (and so the frozen selection) holds corrected copy A; the server moved on to C.
    srv.current = { ...rowA, corrected_artifact: SHA_C }
    const lists = srv.lists
    const all = [...c.querySelectorAll('button')].find(b => /^Approve all ready/.test(b.textContent.trim()))
    await click(all)
    await settle()
    const confirm = [...c.querySelectorAll('button')].find(b => /^Confirm approval of/.test(b.textContent.trim()))
    if (confirm) { await click(confirm) }
    await settle(150)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: null, expected_corrected_sha256: SHA_A })
    // Nothing was recorded on the server, and the page says so in words (not the raw guard string).
    expect(srv.current.status).toBe('pending')
    expect(c.textContent).toContain('its document, corrected copy or suggestion moved on — so nothing was recorded')
    expect(c.textContent).not.toContain('stale source revision')
    expect(c.textContent).not.toMatch(/1 approved/)
    // The queue was re-read, so the frozen entry is now visibly out of date rather than re-sendable.
    expect(srv.lists).toBeGreaterThan(lists)
    await settle(300)
    expect(srv.puts).toHaveLength(1)
  })

  it('a batch row without a corrected-artifact binding is never sent in a batch', async () => {
    const { corrected_artifact, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server({ current: unbound })
    const c = await mount(srv)
    const all = [...c.querySelectorAll('button')].find(b => /^Approve all ready/.test(b.textContent.trim()))
    if (all && !all.disabled) { await click(all); await settle() }
    expect(srv.puts).toHaveLength(0)
  })

  it('a HELD approval (approval_recheck_required) offers "Review and approve again": one PUT with the full current binding, then the control is spent', async () => {
    const held = { ...rowA, status: 'approved', applied: null, validated: false, approval_recheck_required: true,
      approved_value: 'Read the synthetic summary', approved_source_revision: 'rev-0', decision_version: 4 }
    const srv = server({ current: held })
    const c = await mount(srv)
    await open(c, 'item-a')
    const again = button(pane(c), 'Review and approve again')
    expect(again, pane(c).textContent.slice(0, 400)).toBeTruthy()
    expect(pane(c).textContent).toMatch(/Approved earlier, then changed/)
    await click(again)
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 4,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'], expected_corrected_sha256: SHA_A,
      approved_value: 'Read the synthetic summary', resolution: null })
    // Re-bound on the server; the page does not offer (or send) it again.
    await settle(300)
    expect(srv.puts).toHaveLength(1)
    const still = button(pane(c) || c, 'Review and approve again')
    expect(!still || still.disabled).toBe(true)
  })

  it('the retry-write refusal for a moved corrected copy is shown verbatim', async () => {
    const ARTIFACT_MOVED = 'The saved corrected copy of this document has changed since this was approved, so the approval describes different bytes. Review this suggestion against the current copy, then approve it again.'
    const unwritten = { ...rowA, status: 'approved', applied: null, validated: false, approval_recheck_required: false,
      approved_value: 'Read the synthetic summary' }
    const srv = server({ current: unwritten })
    srv.retryAnswer = { accepted: false, in_flight: false, status: 'done', reason: ARTIFACT_MOVED }
    const c = await mount(srv)
    await open(c, 'item-a')
    const retry = button(pane(c), 'Retry writing the approved fix')
    expect(retry, pane(c).textContent.slice(0, 400)).toBeTruthy()
    await click(retry)
    await settle()
    expect(srv.retries).toBe(1)
    expect(pane(c).textContent).toContain(ARTIFACT_MOVED)
    expect(srv.puts).toHaveLength(0)
  })

  it('a rejection is never blocked by a missing binding, and carries the binding when the row has one', async () => {
    const { source_revision, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server({ current: unbound })
    const c = await mount(srv)
    await open(c, 'item-a')
    await click(button(pane(c), 'Needs manual work'))
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'rejected', approval_scope: 'single', expected_version: 3, expected_source_revision: null })

    unmountAll()
    const bound = server({ current: rowA })
    const c2 = await mount(bound)
    await open(c2, 'item-a')
    await click(button(pane(c2), 'Needs manual work'))
    await settle()
    expect(bound.puts).toHaveLength(1)
    expect(bound.puts[0].body).toMatchObject({ status: 'rejected', approval_scope: 'single', expected_version: 3,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
  })
})
