import { afterEach, expect, it } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import ReviewQueueTabs from './ReviewQueueTabs.jsx'
import { automaticReviewResponsibility as responsibility } from './automaticReviewResponsibility.js'
afterEach(unmountAll)
const draft = { id: 'draft', file: 'chart.xlsx', rule_id: '1.4.5', hasProposal: true, after: 'Chart description', automaticDisposition: { responsibility: 'human', state: 'review_required' } }
const ready = { ...draft, _raw: { corrected_artifact: 'none', proposal_digest: 'digest-test', proposal_snapshot_ids: ['snapshot'], source_revision: 1, decision_version: 1 } }
it('separates incomplete drafts from actionable judgment without inventing automatic work', () => {
 expect(responsibility(draft)).toBe('check')
 expect(responsibility(ready)).toBe('human')
 for (const row of [{ ...ready, stale: true }, { ...ready, after: '' }, { ...ready, _raw: { corrected_artifact: 'none', proposal_digest: 'digest-test', ...ready._raw, finding_count: 2 } }, { ...ready, canApprove: false }]) expect(responsibility(row)).toBe('check')
 expect(responsibility({ ...draft, manual: true })).toBe('human')
 expect(responsibility(draft, { draft: { state: 'assigned' } })).toBe('human')
 expect(responsibility({ ...draft, rejectedFix: true })).toBe('human')
 expect(responsibility({ ...draft, automaticQueued: true })).toBe('acp')
})
it('counts only four actionable items from 129 and restores drafts when ready', async () => {
 const { root, container } = createTestRoot()
 const queue = [...Array.from({length:125}, (_,i)=>({...draft,id:`draft-${i}`})), ...Array.from({length:4}, (_,i)=>({id:`manual-${i}`,rule_id:'1.4.5',manual:true})), ...Array.from({length:140}, (_,i)=>({id:`check-${i}`,rule_id:'1.1.1'}))]
 const render = rows => act(async()=>root.render(createElement(ReviewQueueTabs,{queue:rows,scanId:'scan',decisions:{},automatic:true,value:'review',onChange:()=>{}})))
 const counts = ()=>[...container.querySelectorAll('button strong')].map(x=>Number(x.textContent))
 await render(queue)
 expect(counts()).toEqual([4,0,265,0])
 await render(queue.map(row=>row.id==='draft-0'?{...ready,id:row.id}:row))
 expect(counts()).toEqual([5,0,264,0])
})
