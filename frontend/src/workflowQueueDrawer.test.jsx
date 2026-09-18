import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import Card from './WorkflowStageActivityCard.jsx'
import { getStageProgressQueue } from './api.js'
vi.mock('./api.js', () => ({ getStageProgressQueue: vi.fn() }))
let root, host
afterEach(async () => { if(root) await act(async()=>root.unmount()); root=null; host?.remove(); vi.clearAllMocks() })
const snapshot = (stage='remediate', id='run') => ({ stage, execution_id:id, generated_at:'now',
  domain_reconciliation:stage==='remediate' ? {total:3,accounted:3,exact:true,buckets:{resolved_verified:3}}
    : {total:1,accounted:1,exact:true,buckets:{completed_unverified:1}} })
async function render(data) {
  if(!root) {host=document.createElement('div');document.body.append(host);root=createRoot(host)}
  await act(async()=>root.render(<Card snapshot={data}/>))
}
it('opens the exact finding queue grouped by file and does not navigate', async()=> {
  getStageProgressQueue.mockResolvedValue({execution_id:'run',bucket:'verified',available:true,count:3,
    files:[{file:'a.docx',status:'verified',label:'Verified fixes',findingCount:3}]})
  await render(snapshot())
  const button=host.querySelector('.finding-outcome-kpis__verified > button')
  button.focus()
  await act(async()=>button.click())
  expect(getStageProgressQueue).toHaveBeenCalledWith('run','verified')
  const drawer=document.querySelector('[role="dialog"]')
  expect(drawer.textContent).toContain('a.docx')
  expect(drawer.textContent).toContain('3 findings')
  expect(drawer.textContent).toContain('1 file')
  await act(async()=>drawer.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(button)
})
it('opens the publication scope and discards a stale response when the run changes', async()=> {
  let resolve
  getStageProgressQueue.mockImplementation(()=>new Promise(r=>resolve=r))
  await render(snapshot('release'))
  await act(async()=>host.querySelector('.tone-green > .workflow-outcome-tiles__tile').click())
  expect(document.querySelector('[role="dialog"]').textContent).toContain('this release')
  await render(snapshot('release','next'))
  await act(async()=>resolve({execution_id:'run',bucket:'published',available:true,count:1,files:[{file:'old.pdf'}]}))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(host.textContent).not.toContain('old.pdf')
})
it('does not substitute a different population when membership disagrees with the tile', async()=> {
  getStageProgressQueue.mockResolvedValue({execution_id:'run',bucket:'verified',available:true,count:99,files:[{file:'other.docx'}]})
  await render(snapshot())
  await act(async()=>host.querySelector('.finding-outcome-kpis__verified > button').click())
  expect(document.querySelector('[role="dialog"]').textContent).toContain('queue changed')
  expect(document.querySelector('[role="dialog"]').textContent).not.toContain('other.docx')
})
it('offers "Open item" for an unresolved finding the server links to a review item, and asks for it', async()=> {
  getStageProgressQueue.mockResolvedValue({execution_id:'run',bucket:'attention',available:true,count:2,
    files:[{file:'synthetic.docx',status:'attention',label:'Needs attention',findingCount:2}]})
  const requests=[]
  const listener=event=>requests.push(event.detail)
  window.addEventListener('acp:open-review-item',listener)
  await render({ ...snapshot(), scan_id:'scan', domain_reconciliation:{total:4,accounted:4,exact:true,
    buckets:{resolved_verified:2,awaiting_review:2},
    unresolved_findings:[{finding_id:'f-111',file:'synthetic.docx',rule_id:'1.1.1',rule_name:'Images have alt text',disposition:'awaiting_review',review_item_id:'item-111'},
      {finding_id:'f-999',file:'synthetic.docx',rule_id:'2.4.2',rule_name:'Title',disposition:'awaiting_review',review_item_id:null}]} })
  const tile=[...host.querySelectorAll('button')].find(b=>b.textContent.includes('Unresolved findings'))
  await act(async()=>tile.click())
  const drawer=document.querySelector('[role="dialog"]')
  const open=[...drawer.querySelectorAll('button')].filter(b=>b.textContent.startsWith('Open '))
  // Only the finding that names its review item can be opened; the other has nothing to open.
  expect(open.map(b=>b.textContent)).toEqual(['Open 1.1.1 item — Images have alt text'])
  await act(async()=>open[0].click())
  window.removeEventListener('acp:open-review-item',listener)
  expect(requests).toEqual([{itemId:'item-111',scanId:'scan',tab:null}])
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})
