// P1 approval binding through the global Review queue (HitlBell → ReviewCenter → EvidenceCard), mounted
// for real with the real api.js; only `fetch` is a synthetic server implementing the PUT /hitl/queue/{id}
// contract. Before this change the bell sent the decision version only (audit gap 8), so a
// re-assessment after the queue was loaded bound an approval to a version the reviewer never saw.
import { act, createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import HitlBell from './HitlBell.jsx'

vi.mock('./sim.js', async actual => ({ ...(await actual()), SIM: false }))
afterEach(() => { unmountAll(); vi.unstubAllGlobals() })
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const FILE = 'synthetic-bell.docx'
const STALE = 'This suggestion or its document changed after you opened it. Review the current version, then approve again.'
const rowA = { id: 'bell-a', scan_id: 'scan-bell', file: FILE, rule_id: '2.4.4', rule_name: 'Link purpose', status: 'pending',
  decision_version: 5, source_revision: 'rev-A', proposal_snapshot_ids: ['snap-A1'], finding_count: 1,
  proposals: [{ locator: 'docx:link:1', before: 'click here', proposed_value: 'Read the synthetic summary', source: 'AI' }] }
const rowB = { ...rowA, source_revision: 'rev-B', proposal_snapshot_ids: ['snap-B1'],
  proposals: [{ locator: 'docx:link:1', before: 'click here', proposed_value: 'Open the synthetic quarterly summary', source: 'AI' }] }

function server(current) {
  const s = { current, puts: [], lists: 0 }
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  s.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url)
    if (opts.method === 'PUT' && /\/hitl\/queue\/[^/?]+$/.test(u)) {
      const body = JSON.parse(opts.body)
      s.puts.push({ url: u, method: opts.method, body })
      if (body.status === 'approved' && (body.expected_source_revision == null || body.expected_proposal_snapshot_ids == null))
        return json({ code: 'viewed_version_required', message: 'Refresh this suggestion before approving it.' }, 409)
      if (body.expected_source_revision != null && (body.expected_source_revision !== s.current.source_revision
          || JSON.stringify(body.expected_proposal_snapshot_ids) !== JSON.stringify(s.current.proposal_snapshot_ids)))
        return json({ code: 'stale_viewed_version', message: STALE }, 409)
      return json({ ...s.current, status: body.status })
    }
    if (/\/hitl\/queue(\?|$)/.test(u) && (!opts.method || opts.method === 'GET')) { s.lists += 1; return json([s.current]) }
    return json([])
  })
  return s
}
const settle = (ms = 50) => act(async () => { await new Promise(r => setTimeout(r, ms)) })

async function openCard(srv) {
  vi.stubGlobal('fetch', srv.fetch)
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(HitlBell)))
  await settle()
  await act(async () => { window.dispatchEvent(new Event('acp:open-inbox')) })
  await settle()
  const header = [...document.querySelectorAll('.rc-item button')].find(b => b.textContent.includes(FILE))
  expect(header, document.body.textContent.slice(0, 500)).toBeTruthy()
  await act(async () => { header.click() })
  await settle()
  return container
}
const approveButton = () => document.querySelector('.evcard .qbtn.approve')
const click = el => act(async () => { el.click() })

describe('HitlBell — approvals carry the VIEWED version', () => {
  it('an ordinary approval sends the viewed source revision and snapshot ids', async () => {
    const srv = server(rowA)
    await openCard(srv)
    await click(approveButton())
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].method).toBe('PUT')
    expect(srv.puts[0].url).toMatch(/\/hitl\/queue\/bell-a$/)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 5,
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
  })

  it('re-assessed after the queue loaded: sends the VIEWED revision, shows the 409 sentence, re-reads, never retries', async () => {
    const srv = server(rowA)
    await openCard(srv)
    srv.current = rowB
    const lists = srv.lists
    await click(approveButton())
    await settle(120)
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ approval_scope: 'single', expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: ['snap-A1'] })
    const alert = document.querySelector('.hitlbell-viewed-version[role="alert"]')
    expect(alert?.textContent).toContain(STALE)
    expect(alert.textContent).toContain(FILE)
    expect(document.body.textContent).not.toContain('[object Object]')
    expect(srv.lists).toBeGreaterThan(lists)
    // The open card is on the CURRENT version for a fresh decision.
    expect(document.querySelector('.evcard').textContent).toContain('Open the synthetic quarterly summary')
    await settle(300)
    expect(srv.puts).toHaveLength(1)
    // The reviewer's own second click binds to what is now shown.
    await click(approveButton())
    await settle()
    expect(srv.puts).toHaveLength(2)
    expect(srv.puts[1].body).toMatchObject({ approval_scope: 'single', expected_source_revision: 'rev-B', expected_proposal_snapshot_ids: ['snap-B1'] })
  })

  it('F1: a poll that brings version B to an OPEN card remounts it on B, and the approval carries B with B\'s text', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const srv = server(rowA)
      await openCard(srv)
      const card = () => document.querySelector('.evcard')
      const editorText = () => [...card().querySelectorAll('textarea, input')].map(el => el.value).join(' ')
      expect(card().textContent).toContain('Read the synthetic summary')
      // A re-assessment lands, and the bell's own 30s poll (not an approval) brings version B in.
      srv.current = rowB
      const lists = srv.lists
      await act(async () => { vi.advanceTimersByTime(30000) })
      await settle()
      expect(srv.lists).toBeGreaterThan(lists)
      // The open card shows B — including the text its editor was seeded with — not A's draft.
      expect(card().textContent).toContain('Open the synthetic quarterly summary')
      expect(card().textContent).not.toContain('Read the synthetic summary')
      expect(editorText()).not.toContain('Read the synthetic summary')
      await click(approveButton())
      await settle()
      expect(srv.puts).toHaveLength(1)
      expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single', expected_version: 5,
        expected_source_revision: 'rev-B', expected_proposal_snapshot_ids: ['snap-B1'],
        approved_value: 'Open the synthetic quarterly summary' })
      expect(JSON.stringify(srv.puts[0].body)).not.toContain('Read the synthetic summary')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a row served without snapshot ids binds to the empty list it was shown, not to nothing', async () => {
    const { proposal_snapshot_ids, ...legacy } = rowA // eslint-disable-line no-unused-vars
    // The bell loads the legacy row (no list at all)…
    const srv = server(legacy)
    await openCard(srv)
    // …and the server reads a missing list as [] when it compares (api/store.py).
    srv.current = { ...legacy, proposal_snapshot_ids: [] }
    await click(approveButton())
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'approved', approval_scope: 'single',
      expected_source_revision: 'rev-A', expected_proposal_snapshot_ids: [] })
  })

  it('a row without its source revision is refused before sending, says to refresh, and re-reads the queue', async () => {
    const { source_revision, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server(unbound)
    await openCard(srv)
    const lists = srv.lists
    await click(approveButton())
    await settle(120)
    expect(srv.puts).toHaveLength(0)
    expect(document.body.textContent).toMatch(/must be refreshed before it can be approved/)
    expect(srv.lists).toBeGreaterThan(lists)
  })

  it('a skip without a binding is still sent — only approvals require it', async () => {
    const { source_revision, ...unbound } = rowA // eslint-disable-line no-unused-vars
    const srv = server(unbound)
    await openCard(srv)
    const skip = [...document.querySelectorAll('.evcard .qbtn.self')][0]
    await click(skip)
    await settle()
    expect(srv.puts).toHaveLength(1)
    expect(srv.puts[0].body).toMatchObject({ status: 'skipped', approval_scope: 'single', expected_version: 5, expected_source_revision: null })
  })
})
