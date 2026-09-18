// P1 approval binding in the FileDrawer's review-in-place card, mounted for real with the real api.js;
// only `fetch` is a synthetic server implementing the PUT /hitl/queue/{id} contract. Before this change
// the drawer sent the decision version only (audit gap 8).
import { act, createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import FileDrawer from './FileDrawer.jsx'

vi.mock('./sim.js', async actual => ({ ...(await actual()), SIM: false }))
afterEach(() => { unmountAll(); vi.unstubAllGlobals() })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }

const SCAN = 'scan-drawer'
const FILE = 'synthetic-drawer.docx'
const STALE = 'This suggestion or its document changed after you opened it. Review the current version, then approve again.'
const SHA_A = 'a'.repeat(64)   // the corrected copy on screen
const SHA_C = 'c'.repeat(64)   // the corrected copy after another approved write
const rowA = { id: 'drawer-a', scan_id: SCAN, file: FILE, rule_id: '2.4.4', rule_name: 'Link purpose', status: 'pending',
  decision_version: 2, source_revision: 'rev-A', proposal_snapshot_ids: ['snap-A1'], finding_count: 1, corrected_artifact: SHA_A,
  proposals: [{ locator: 'docx:link:1', before: 'click here', proposed_value: 'Read the synthetic summary', source: 'AI' }] }
const rowB = { ...rowA, source_revision: 'rev-B', proposal_snapshot_ids: ['snap-B1'],
  proposals: [{ locator: 'docx:link:1', before: 'click here', proposed_value: 'Open the synthetic quarterly summary', source: 'AI' }] }
const doc = { file: FILE, type: 'DOCX', status: 'analysed', score: 70, compliant: false,
  issues: [{ wcag: 'SC_2_4_4', rule_id: 'DOCX_LINK_PURPOSE', severity: 'SERIOUS', detail: 'Link text is not descriptive.', auto: false }] }

// `nested` answers the 409 the way FastAPI's HTTPException(detail={...}) serialises it.
function server(current, { nested = false } = {}) {
  const s = { current, puts: [], lists: 0 }
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  const refuse = (code, message) => json(nested ? { detail: { code, message } } : { code, message }, 409)
  s.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url)
    if (opts.method === 'PUT' && /\/hitl\/queue\/[^/?]+$/.test(u)) {
      const body = JSON.parse(opts.body)
      s.puts.push({ url: u, method: opts.method, body })
      if (body.status === 'approved' && (body.expected_source_revision == null || body.expected_proposal_snapshot_ids == null
          || body.expected_corrected_sha256 == null))
        return refuse('viewed_version_required', 'Refresh this suggestion before approving it.')
      if (body.expected_source_revision != null && (body.expected_source_revision !== s.current.source_revision
          || JSON.stringify(body.expected_proposal_snapshot_ids) !== JSON.stringify(s.current.proposal_snapshot_ids)
          || (body.expected_corrected_sha256 != null && body.expected_corrected_sha256 !== s.current.corrected_artifact)))
        return refuse('stale_viewed_version', STALE)
      return json({ ...s.current, status: body.status })
    }
    if (/\/hitl\/queue\?scan_id=/.test(u)) { s.lists += 1; return json([s.current]) }
    // Everything else the drawer reads is incidental here: an unavailable status model, empty lists.
    if (/\/status(\?|$)/.test(u)) return json({ available: false })
    return json([])
  })
  return s
}
const settle = (ms = 50) => act(async () => { await new Promise(r => setTimeout(r, ms)) })
const click = el => act(async () => { el.click() })

async function openCard(srv) {
  vi.stubGlobal('fetch', srv.fetch)
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(FileDrawer, { file: doc, scanId: SCAN, onClose: () => {} })))
  await settle()
  const reviewHere = [...container.querySelectorAll('button')].find(b => b.textContent.includes('Review here'))
  expect(reviewHere, container.textContent.slice(0, 800)).toBeTruthy()
  await click(reviewHere)
  await settle()
  return container
}
const approveButton = c => c.querySelector('.evcard .qbtn.approve')

describe('FileDrawer — in-place approvals carry the VIEWED version', () => {
  it('an ordinary approval sends the viewed source revision and snapshot ids', async () => {
    const srv = server(rowA)
    const c = await openCard(srv)
    await click(approveButton(c))
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].method).toBe('PUT')
    expect(srv.puts[0].url).toMatch(/\/hitl\/queue\/drawer-a$/)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 2,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'], expected_corrected_sha256: SHA_A })
  })

  it('another approved write changed the corrected copy while open: the VIEWED sha is sent and the 409 stated', async () => {
    const srv = server(rowA)
    const c = await openCard(srv)
    srv.current = { ...rowA, corrected_artifact: SHA_C }
    await click(approveButton(c))
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ approval_scope: 'single', expected_corrected_sha256: SHA_A })
    expect(c.querySelector('.drawer-viewed-version[role="alert"]')?.textContent).toContain(STALE)
    await settle(300)
    expect(srv.puts).toHaveLength(1)
  })

  it('a HELD approval (approval_recheck_required) is offered in place as "Review and approve again", and re-approving sends the full current binding once', async () => {
    const held = { ...rowA, status: 'approved', applied: null, approval_recheck_required: true, approved_value: 'Read the synthetic summary' }
    const srv = server(held)
    vi.stubGlobal('fetch', srv.fetch)
    const { root, container: c } = createTestRoot()
    await act(async () => root.render(createElement(FileDrawer, { file: doc, scanId: SCAN, onClose: () => {} })))
    await settle()
    const toggle = [...c.querySelectorAll('button')].find(b => b.textContent.includes('Review and approve again'))
    expect(toggle, c.textContent.slice(0, 600)).toBeTruthy()
    await click(toggle)
    await settle()
    expect(c.querySelector('.drawer-reapprove')?.textContent).toMatch(/Approved earlier, then changed/)
    await click(approveButton(c))
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 2,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'], expected_corrected_sha256: SHA_A })
    await settle(300)
    expect(srv.puts).toHaveLength(1)
  })

  it('re-assessed while open: sends the VIEWED revision, states the 409 above the card, remounts it on the current version, never retries', async () => {
    const srv = server(rowA, { nested: true })
    const c = await openCard(srv)
    expect(c.querySelector('.evcard').textContent).toContain('Read the synthetic summary')
    srv.current = rowB
    const lists = srv.lists
    await click(approveButton(c))
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 2,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
    const alert = c.querySelector('.drawer-viewed-version[role="alert"]')
    expect(alert?.textContent).toContain(STALE)
    expect(c.textContent).not.toContain('[object Object]')
    expect(srv.lists).toBeGreaterThan(lists)
    // The card is on the CURRENT version, its editor seeded from it — not the text of version A.
    const card = c.querySelector('.evcard')
    expect(card.textContent).toContain('Open the synthetic quarterly summary')
    expect([...card.querySelectorAll('textarea, input')].map(el => el.value).join(' ')).not.toContain('Read the synthetic summary')
    await settle(300)
    expect(srv.puts).toHaveLength(1)
    await click(approveButton(c))
    await settle()
    expect(srv.puts).toHaveLength(2)
    expect(srv.puts[1].body).toMatchObject({ approval_scope: 'single', expected_source_revision: 'rev-B', expected_proposal_snapshot_ids: ['snap-B1'] })
    expect(c.querySelector('.drawer-viewed-version')).toBeNull()
  })

  it('a row without its binding is not approved: the drawer says to refresh and re-reads the queue', async () => {
    const { source_revision, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server(unbound)
    const c = await openCard(srv)
    const lists = srv.lists
    await click(approveButton(c))
    await settle(120)
    expect(srv.puts).toHaveLength(0)
    expect(c.querySelector('.drawer-viewed-version[role="alert"]')?.textContent).toMatch(/must be refreshed before it can be approved/)
    expect(srv.lists).toBeGreaterThan(lists)
  })

  // F3(a): the graph context (FileDrawer → GraphFindingsDrawer) used to hold the opened row as a
  // snapshot, while drawerAct binds to the CURRENT row by id — so after a refresh the card showed one
  // version and the approval named another.
  async function openGraphCard(srv) {
    vi.stubGlobal('fetch', srv.fetch)
    const { root, container } = createTestRoot()
    await act(async () => root.render(createElement(FileDrawer, { file: doc, scanId: SCAN, onClose: () => {}, context: 'graph' })))
    await settle()
    const open = [...container.querySelectorAll('button')].find(b => b.textContent === 'Review recorded suggestion')
    expect(open, container.textContent.slice(0, 600)).toBeTruthy()
    await click(open)
    await settle()
    return container
  }

  it('graph drawer: a refresh that brings version B shows B in the open card, and the approval carries B with B\'s text', async () => {
    const srv = server(rowA)
    const c = await openGraphCard(srv)
    expect(c.querySelector('.evcard').textContent).toContain('Read the synthetic summary')
    srv.current = rowB
    // Another surface announces a change; the drawer re-reads its rows (the existing refresh signal).
    await act(async () => { window.dispatchEvent(new Event('acp:hitl-changed')) })
    await settle()
    const card = c.querySelector('.evcard')
    expect(card.textContent).toContain('Open the synthetic quarterly summary')
    expect(card.textContent).not.toContain('Read the synthetic summary')
    await click(approveButton(c))
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 2,
      expected_source_revision: 'rev-B', expected_proposal_snapshot_ids: ['snap-B1'], expected_corrected_sha256: SHA_A,
      approved_value: 'Open the synthetic quarterly summary' })
    expect(JSON.stringify(srv.puts[0].body)).not.toContain('Read the synthetic summary')
  })

  it('graph drawer: re-assessed while open, the 409 is stated, the card remounts on B, and nothing retries', async () => {
    const srv = server(rowA)
    const c = await openGraphCard(srv)
    srv.current = rowB
    await click(approveButton(c))
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ approval_scope: 'single', expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
    expect(c.querySelector('.graph-viewed-version[role="alert"]')?.textContent).toContain(STALE)
    expect(c.querySelector('.evcard').textContent).toContain('Open the synthetic quarterly summary')
    await settle(300)
    expect(srv.puts).toHaveLength(1)
    await click(approveButton(c))
    await settle()
    expect(srv.puts).toHaveLength(2)
    expect(srv.puts[1].body).toMatchObject({ approval_scope: 'single', expected_source_revision: 'rev-B', expected_proposal_snapshot_ids: ['snap-B1'] })
  })

  it('a rejection without a binding is still sent', async () => {
    const { source_revision, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server(unbound)
    const c = await openCard(srv)
    await click(c.querySelector('.evcard .qbtn.reject'))
    await settle()
    const reason = [...c.querySelectorAll('.evcard button')].find(b => /inaccurate|wrong|other|unclear/i.test(b.textContent) && !b.classList.contains('qbtn'))
    expect(reason, c.querySelector('.evcard').textContent.slice(-600)).toBeTruthy()
    await click(reason)
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'rejected', approval_scope: 'single', expected_version: 2, expected_source_revision: null })
  })
})
