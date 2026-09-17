import { act, createElement, useEffect } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import Remediate from './Remediate.jsx'
vi.mock('./sim.js', async actual => ({ ...(await actual()), SIM: false }))
vi.mock('./useReviewQueueRefresh.js', () => ({ default: ({ onRows }) => {
  useEffect(() => { onRows([{ id: 'approved-item', scan_id: 'scan', file: 'a.docx', rule_id: '1.4.5',
    status: 'approved', applied: null, validated: false, decision_version: 1,
    proposals: [{ locator: 'image 1', proposed_value: 'Visible text', approved_value: 'Visible text', source: 'OCR' }] }]) }, [])
  return vi.fn()
} }))
vi.mock('./api.js', async actual => ({ ...(await actual()),
  getAppliedFixes: vi.fn(async()=>[]), getScanRemediationDiffs: vi.fn(async()=>[]),
  getHitlAnalytics: vi.fn(async()=>({})), getScanAiCalls: vi.fn(async()=>[]),
  getRemediationExceptions: vi.fn(async()=>({})), getRemediationStatus: vi.fn(async()=>({})),
}))
afterEach(() => { unmountAll(); vi.unstubAllGlobals() })
globalThis.IS_REACT_ACT_ENVIRONMENT = true

it.each([true, false])('real page calls the real retry API and renders acceptance=%s honestly', async accepted => {
  history.replaceState({}, '', '/?tab=remediate&mode=review')
  const calls=[]
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    calls.push([String(url), options])
    return new Response(JSON.stringify({ accepted, in_flight: accepted, status: accepted ? 'queued' : 'done',
      reason: accepted ? null : 'The document changed. Review the current version.' }), {status:200, headers:{'Content-Type':'application/json'}})
  }))
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Remediate, { run:{id:'scan',status:'completed'},
    files:[{file:'a.docx', type:'DOCX', remediated_at:'now', issues:[]}]})))
  const filter = container.querySelector('select[aria-label="Filter by status"]')
  await act(async()=>{filter.value='all'; filter.dispatchEvent(new Event('change',{bubbles:true}))})
  const retry = [...container.querySelectorAll('button')].find(b=>b.textContent==='Retry writing the approved fix')
  expect(retry, container.textContent).toBeTruthy()
  await act(async()=>retry.click())
  const writes=calls.filter(([url])=>url.endsWith('/hitl/queue/approved-item/retry-write'))
  expect(writes).toHaveLength(1)
  expect(writes[0][1].method).toBe('POST')
  expect(container.textContent).toContain(accepted ? 'Saving is queued or running.' : 'The document changed. Review the current version.')
  expect(calls.some(([url, opts])=>url.endsWith('/hitl/queue/approved-item') && opts?.method==='PUT')).toBe(false)
})

it('does not offer a retry from a read-only page', async()=>{
  history.replaceState({}, '', '/?tab=remediate&mode=review')
  vi.stubGlobal('fetch', vi.fn(async()=>new Response('{}', {status:200})))
  const {root,container}=createTestRoot()
  await act(async()=>root.render(createElement(Remediate,{run:{id:'scan',status:'completed'},files:[],readOnly:true})))
  expect([...container.querySelectorAll('button')].some(b=>b.textContent==='Retry writing the approved fix')).toBe(false)
})
