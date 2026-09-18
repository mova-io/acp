import { act } from 'react'
import { afterEach,expect,it } from 'vitest'
import { createTestRoot,unmountAll } from './testRoots.js'
import Tiles,{outcomeTileModel} from './WorkflowOutcomeTiles.jsx'
import Layout from './RemediationAutomationLayout.jsx'
afterEach(unmountAll)
const domain={available:true,total:10,buckets:{awaiting_recorded_outcome:2,approved_pending_verification:1,resolved_verified:3,awaiting_review:4}}
const batch={available:true,authorization_id:'plan',run_id:'run',scope_id:'plan',revision:2,total:3,delivered:1,remaining:2,status:'publishing',buckets:{waiting:1,processing:1,published:1,failed:0,skipped:0,unclassified:0},file_membership:{'a':'published','b':'processing','c':'waiting'}}
it('puts review next to unresolved and excluded without adding items to findings and combines settings below',async()=>{
 const {root,container}=createTestRoot()
 const mount=count=><><Tiles stage="remediate" domain={domain} executionId="run" reviewHostId="host-review" reviewScanId="scan"/><div id="host-automation" data-scan-id="scan" data-batch-id="run"/><Layout progressHostId="host" scanId="scan" batchId="run" policy={{enabled:true}} reviewCount={count} onOpenReview={()=>{}} authorization={{id:'plan',status:'publishing',batch_progress:batch,destination_label:'SharePoint / Saved'}}/></>
 await act(async()=>root.render(mount(2)))
 const rows=container.querySelectorAll('.workflow-outcome-tiles__grid')
 expect(rows.length).toBe(2)
 expect(rows[0].children.length).toBe(3)
 expect(rows[1].children.length).toBe(3)
 expect(rows[1].textContent).toContain('Unresolved findings')
 expect(rows[1].textContent).toContain('Excluded')
 expect(rows[1].textContent).toContain('Review workspace2')
 expect(outcomeTileModel('remediate',domain).total).toBe(10)
 expect(container.querySelectorAll('.remediation-automation-combined').length).toBe(1)
 const settings=container.querySelector('.remediation-automation-combined')
 expect(settings.open).toBe(false)
 expect(settings.textContent).toContain('1 of 3 authorized copies delivered')
 expect(settings.textContent).toContain('SharePoint / Saved')
 await act(async()=>settings.querySelector('summary').click())
 await act(async()=>root.render(mount(3)))
 expect(settings.open).toBe(true)
 expect(rows[1].textContent).toContain('Review workspace3')
})
it('refuses to portal current controls into a different scan or execution',async()=>{
 const {root,container}=createTestRoot()
 await act(async()=>root.render(<><div id="host-review" data-scan-id="other" data-batch-id="old"/><div id="host-automation" data-scan-id="other" data-batch-id="old"/><Layout progressHostId="host" scanId="scan" batchId="run" reviewCount={1}/></>))
 expect(container.querySelector('#host-review').children.length).toBe(0)
 expect(container.querySelector('#host-automation').children.length).toBe(0)
 expect(container.querySelector('.remediation-review-workspace-fallback')).not.toBeNull()
})
it('reattaches to replacement hosts with the same run identity after stage collapse and reopen',async()=>{
 const {root,container}=createTestRoot()
 const outside=document.createElement('div');document.body.append(outside)
 const hosts=()=>{outside.innerHTML='<div id="host-review" data-scan-id="scan" data-batch-id="run"></div><div id="host-automation" data-scan-id="scan" data-batch-id="run"></div>'}
 hosts()
 try {
  await act(async()=>root.render(<Layout progressHostId="host" scanId="scan" batchId="run" reviewCount={2}/>))
  expect(outside.querySelector('#host-review').textContent).toContain('Review workspace2')
  await act(async()=>{outside.innerHTML='';await Promise.resolve()})
  expect(container.querySelector('.remediation-review-workspace-fallback')).not.toBeNull()
  await act(async()=>{hosts();await Promise.resolve()})
  expect(outside.querySelector('#host-review').textContent).toContain('Review workspace2')
  expect(outside.querySelector('#host-automation details').open).toBe(false)
 } finally {await unmountAll();outside.remove()}
})
it('wires the six cells into the actual canonical remediation activity card',async()=>{
 const {default:Card}=await import('./WorkflowStageActivityCard.jsx')
 const {root,container}=createTestRoot()
 const snapshot={workflow_id:'workflow',workflow_revision:1,stage:'remediate',execution_id:'run',revision:1,state:'processing',counts:{work_items:{total:1,queued:0,processing:1,completed:0,failed:0,cancelled:0,skipped:0}},reconciliation:{unit:'work items',total:1,accounted:1,unaccounted:0,exact:true},integrity:{ok:true},control:{},domain_reconciliation:domain}
 await act(async()=>root.render(<><Card snapshot={snapshot} progressHostId="actual" progressScanId="scan"/><Layout progressHostId="actual" scanId="scan" batchId="run" policy={{enabled:true}} reviewCount={2}/></>))
 const rows=container.querySelectorAll('.workflow-outcome-tiles__grid')
 expect(rows.length).toBe(2)
 expect(rows[1].children.length).toBe(3)
 expect(rows[1].textContent).toContain('Review workspace2')
 expect(container.querySelector('#actual-automation details').open).toBe(false)
})
it('says status checks exist beside a zero review count, as tasks rather than findings',async()=>{
 const {root,container}=createTestRoot()
 await act(async()=>root.render(<Layout progressHostId="none" scanId="scan" batchId="run" policy={{enabled:true}} reviewCount={0} statusCheckCount={2}/>))
 const tile=container.querySelector('.remediation-review-workspace')
 expect(tile.textContent).toContain('Review workspace0')
 expect(tile.textContent).toContain('Review tasks needing your input')
 expect(tile.textContent).toContain('+ 2 status checks ACP is tracking')
 await act(async()=>root.render(<Layout progressHostId="none" scanId="scan" batchId="run" policy={{enabled:true}} reviewCount={0} statusCheckCount={0}/>))
 expect(container.querySelector('.remediation-review-workspace').textContent).not.toContain('status check')
})
