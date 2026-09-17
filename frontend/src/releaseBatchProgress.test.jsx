import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import WorkflowStageStack from './WorkflowStageStack.jsx'
import { releaseBatchProgress } from './releaseBatchProgress.js'

let root, container
afterEach(() => { if (root) act(() => root.unmount()); container?.remove() })
const snapshot = batch => ({stage:'release',state:'succeeded',execution_id:'incremental-9',workflow_id:'workflow',workflow_revision:1,revision:1,
  reconciliation:{total:1,accounted:1,exact:true},integrity:{ok:true},
  domain_reconciliation:{total:1,accounted:1,unit:'requested documents',exact:true,buckets:{published:1}},
  release_batch_progress:batch})
const batch = {available:true,authorization_id:'approved',run_id:'remediation',total:147,delivered:9,remaining:138,status:'waiting'}

it('shows cumulative authorized delivery without claiming the latest finished request completed the batch', () => {
  container=document.createElement('div');document.body.appendChild(container);root=createRoot(container)
  act(() => root.render(<WorkflowStageStack lineage={{scan_id:'scan',workflow_revision:1,stages:[snapshot(batch)]}}/>))
  const summary=container.querySelector('.workflow-stage-stack__summary')
  expect(summary.textContent).toContain('Publishing automatically')
  expect(summary.textContent).toContain('9 of 147 authorized files delivered')
  expect(summary.textContent).not.toContain('Complete')
  expect(container.textContent).toContain('Latest delivery request: 1 of 1 requested documents accounted for')
  expect(container.textContent).toContain('138 authorized files awaiting confirmed delivery')
})

it('never treats the overall scan population as approved scope for a manual or ambiguous request', () => {
  expect(releaseBatchProgress(snapshot({available:false,scope:'manual'}))).toBeNull()
  expect(releaseBatchProgress(snapshot({...batch,total:2}))).toMatchObject({available:false,state:'failed'})
  container=document.createElement('div');document.body.appendChild(container);root=createRoot(container)
  act(() => root.render(<WorkflowStageStack lineage={{scan_id:'scan',workflow_revision:1,stages:[snapshot(null)]}}/>))
  expect(container.querySelector('.workflow-stage-stack__summary').textContent).toContain('1 of 1 requested documents')
  expect(container.textContent).not.toContain('147')
})
it.each(['automatic','unknown'])('never shows Complete when %s batch delivery cannot be confirmed', scope => {
  container=document.createElement('div');document.body.appendChild(container);root=createRoot(container)
  act(() => root.render(<WorkflowStageStack lineage={{scan_id:'scan',workflow_revision:1,
    stages:[snapshot({available:false,scope})]}}/>))
  expect(container.querySelector('.workflow-stage-stack__summary').textContent).toContain('Delivery confirmation unavailable')
  expect(container.querySelector('.workflow-stage-stack__summary').textContent).not.toContain('Complete')
  expect(container.textContent).not.toContain('undefined')
})
it('names finished processing of the approved request when manual scope is positively established', () => {
  container=document.createElement('div');document.body.appendChild(container);root=createRoot(container)
  act(() => root.render(<WorkflowStageStack lineage={{scan_id:'scan',workflow_revision:1,
    stages:[snapshot({available:false,scope:'manual'})]}}/>))
  expect(container.querySelector('.workflow-stage-stack__summary').textContent).toContain('Publication processing finished')
  expect(container.querySelector('.workflow-stage-stack__summary').textContent).toContain('1 of 1 requested documents')
})

it('keeps stopped delivery visible instead of reviving automatic publication', () => {
  expect(releaseBatchProgress(snapshot({...batch,status:'stopped'}))).toMatchObject({state:'failed',label:'Automatic delivery stopped'})
})
it('shows blocked cumulative delivery as attention even after the latest request completes', () => {
  container=document.createElement('div');document.body.appendChild(container);root=createRoot(container)
  act(() => root.render(<WorkflowStageStack lineage={{scan_id:'scan',workflow_revision:1,
    stages:[snapshot({...batch,delivered:45,remaining:102,status:'blocked'})]}}/>))
  const summary=container.querySelector('.workflow-stage-stack__summary')
  expect(summary.textContent).toContain('Delivery needs attention')
  expect(summary.textContent).toContain('45 of 147 authorized files delivered')
  expect(summary.textContent).not.toContain('Complete')
})
it('does not declare release complete while the authorization is still finalizing reports or packaging', () => {
  expect(releaseBatchProgress(snapshot({...batch,delivered:147,remaining:0,status:'publishing'}))).toMatchObject({state:'processing',label:'Finalizing automatic release'})
})

it('keeps saved-plan tiles separate from the latest one-file request', () => {
  const full = {...batch,scope_id:'approved',revision:1,
    buckets:{waiting:138,processing:0,published:9,failed:0,skipped:0,unclassified:0},
    file_membership:Object.fromEntries(Array.from({length:147},(_,i)=>[`file-${i}`,i<9?'published':'waiting']))}
  container=document.createElement('div');document.body.appendChild(container);root=createRoot(container)
  act(()=>root.render(<WorkflowStageStack lineage={{scan_id:'scan',workflow_revision:1,stages:[snapshot(full)]}}/>))
  const tiles=container.querySelector('.workflow-outcome-tiles')
  expect(tiles.textContent).toContain('147')
  expect(tiles.querySelector('strong[aria-label="Published: 9"]')).not.toBeNull()
  expect(tiles.querySelector('strong[aria-label="Published: 1"]')).toBeNull()
  expect(tiles.querySelector('button.workflow-outcome-tiles__tile')).toBeNull() // Incremental queue endpoint cannot supply plan membership.
})
