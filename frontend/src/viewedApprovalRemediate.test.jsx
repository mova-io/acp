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

// One pending link-text row. Version A is what the page loads; version B is what a re-assessment
// produces while the reviewer is looking at A.
const rowA = { id: 'item-a', scan_id: SCAN, file: FILE, rule_id: '2.4.4', rule_name: 'Link purpose', status: 'pending',
  decision_version: 3, source_revision: 'rev-A', proposal_snapshot_ids: ['snap-A1'], finding_count: 1,
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
      if (body.status === 'approved' && (body.expected_source_revision == null || body.expected_proposal_snapshot_ids == null))
        return refuse('viewed_version_required', 'Refresh this suggestion before approving it.')
      if (body.expected_source_revision != null && (body.expected_source_revision !== s.current.source_revision
          || JSON.stringify(body.expected_proposal_snapshot_ids) !== JSON.stringify(s.current.proposal_snapshot_ids)))
        return refuse('stale_viewed_version', STALE)
      return json({ ...s.current, status: body.status, decision_version: s.current.decision_version + 1 })
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
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
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
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'],
      approved_values: ['Read the synthetic summary'] })
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
