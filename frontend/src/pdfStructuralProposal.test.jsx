import { act, createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import { pdfStructuralSummary } from './pdfStructuralProposal.js'
import { laneOf, rowModel, isAiAssistedDraft } from './remediationInboxModel.js'
import { batchDecision, exclusionReason, snapshotFinding } from './batchReviewSelection.js'
import EvidenceCard from './EvidenceCard.jsx'
import Inbox from './RemediationInbox.jsx'
afterEach(unmountAll)
const heading={kind:'pdf-tag-heading',locator:'pdf:struct:source-bound-node',proposed_value:'{"op":"heading","role":"H1"}',before:'/P',subject_text:'Introduction',source:'tagged text match (heuristic)',explain_only:false}
const header={kind:'pdf-table-header-scope',locator:'pdf:struct:source-bound-header',proposed_value:'{"op":"header-scope","scope":"Column"}',subject_text:'Analyte',source:'existing tagged table (heuristic)',explain_only:false}
function finding(proposals=[heading],rule_id='2.4.6') {return {id:91,file:'report.pdf',rule_id,status:'pending',hasProposal:true,after:proposals[0].proposed_value,proposals,_raw:{finding_count:proposals.length,decision_version:2,source_revision:3,corrected_artifact:'none',proposal_digest:'digest-test',proposal_snapshot_ids:proposals.map((_,i)=>`snapshot-${i}`),proposals}}}
it('summarizes only recognized valid source-anchored plans without inventing text',()=>{
 expect(pdfStructuralSummary(heading)).toBe('Mark “Introduction” as Heading 1')
 expect(pdfStructuralSummary(header)).toBe('Associate “Analyte” with its column')
 expect(pdfStructuralSummary({...heading,subject_text:undefined})).toBe('Mark existing tagged text as Heading 1')
 for(const p of [{...heading,locator:'elsewhere'},{...heading,proposed_value:'{"op":"heading","role":"H7"}'},{...heading,proposed_value:'{"op":"heading","role":"H1","extra":true}'},{...header,proposed_value:'{"op":"heading","role":"H1"}'}])expect(pdfStructuralSummary(p)).toBeNull()
})
it('allows consented bulk application of both criteria with untouched JSON and lineage',()=>{
 for(const [p,rule] of [[heading,'2.4.6'],[header,'1.3.1']]){
  const f=finding([p],rule);expect(laneOf(f).key).toBe('apply');expect(isAiAssistedDraft(f)).toBe(false)
  expect(rowModel(f).laneShort).toBe('PDF tag change');expect(rowModel(f).lane.short).toBe('PDF tag change');expect(exclusionReason(f)).toBeNull()
  expect(batchDecision(snapshotFinding(f))).toMatchObject({approvedValues:[p.proposed_value],expectedProposalSnapshotIds:['snapshot-0'],expectedVersion:2,expectedSourceRevision:3})
 }
 expect(exclusionReason(finding([{...heading,proposed_value:'not JSON'}]))).toContain('Invalid structural proposal')
 expect(exclusionReason({...finding(),stale:true})).toContain('Stale')
})
it('shows friendly structural proposals with no prose editor in both live review surfaces',async()=>{
 const f=finding();const html=renderToStaticMarkup(<EvidenceCard item={f} onAct={()=>{}}/>)
 expect(html).toContain('Mark “Introduction” as Heading 1');expect(html).toContain('Technical plan');expect(html).toContain('Approve PDF tag change');expect(html).not.toContain('evcard-rec-input')
 const {root,container}=createTestRoot();await act(async()=>root.render(<Inbox queue={[f]} initialTab="needs-review"/>))
 expect(container.textContent).toContain('Mark “Introduction” as Heading 1');expect(container.textContent).toContain('Technical plan');expect(container.querySelector('#rem-draft')).toBeNull()
})
it('read-only structural approval still sends the exact executable server plan',async()=>{
 const onAct=vi.fn(async()=>{});const {root,container}=createTestRoot();await act(async()=>root.render(createElement(EvidenceCard,{item:finding(),onAct})))
 const approve=container.querySelector('.qbtn.approve');expect(approve.disabled).toBe(false)
 await act(async()=>approve.dispatchEvent(new MouseEvent('click',{bubbles:true})))
 expect(onAct).toHaveBeenCalledOnce();expect(onAct.mock.calls[0][4].approvedValues).toEqual([heading.proposed_value])
})
it('keeps Review layout and switch while removing publication/readiness actions, with a dismissible success notice',async()=>{
 const dismiss=vi.fn();const {root,container}=createTestRoot()
 await act(async()=>root.render(<Inbox queue={[{id:'manual',file:'manual.pdf'}]} autoApprove={true} onAutoApproveChange={()=>{}} onPublish={()=>{}} autoApproveNotice={{identity:'run'}} onDismissAutoApproveNotice={dismiss}/>))
 expect(container.textContent).not.toContain('Publish saved copies');expect(container.textContent).not.toContain('View run readiness')
 expect(container.querySelector('.run-auto-approve-switch.is-on')).not.toBeNull()
 const toast=container.querySelector('.run-auto-approve-toast');expect(toast.textContent).toContain('AI reviews will be automatically approved.');expect(toast.textContent).toContain('Items needing manual work stay in the review queue.')
 await act(async()=>toast.querySelector('button').click());expect(dismiss).toHaveBeenCalledOnce()
})

it.each([{queue:[]},{queue:[finding()]}])('live Review action row contains only the switch for empty and ready queues',async({queue})=>{
 const {root,container}=createTestRoot();await act(async()=>root.render(<Inbox queue={queue} autoApprove={false} onAutoApproveChange={()=>{}} onDecide={()=>{}} onPublish={()=>{}}/>))
 const actions=container.querySelector('.run-approval-actions');expect(actions.querySelectorAll('button')).toHaveLength(0);expect(actions.querySelector('[role="switch"]')).not.toBeNull()
})
