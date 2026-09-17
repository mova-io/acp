import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { unresolvedWorkSummary } from './unresolvedWorkSummary.js'
import RemainingWorkStatus from './RemainingWorkStatus.jsx'
const draft = (id, extra = {}) => ({id, file:`${id}.docx`, rule_id:'1.1.1',status:'pending',after:'An image description',hasProposal:true,
  _raw:{proposal_snapshot_ids:['p'],source_revision:1,decision_version:1},...extra})
const mixed = [
  draft(1, {_raw:{finding_count:40}}),
  draft(2, {status:'verification_failed'}),
  draft(3, {file:'source.pdf',rule_id:'1.3.1'}),
  draft(4, {manual:true}),
  draft(5, {automaticDisposition:{responsibility:'human',reason:'This change requires individual review'}}),
  draft(6),
  draft(7, {automaticQueued:true}),
  draft(8, {status:'verified'}),
  draft(9, {status:'approved'}),
]
it('partitions outstanding items once without counting findings, jobs, or saved decisions', () => {
  const result=unresolvedWorkSummary(mixed)
  expect(result.total).toBe(6)
  expect(Object.fromEntries(result.groups.map(group=>[group.key,group.count]))).toEqual({recovery:2,unsupported:1,human:2,unknown:1})
  expect(result.groups.reduce((sum,group)=>sum+group.count,0)).toBe(result.total)
})
it('does not call generic manual reasons unsupported or incomplete assigned work automatic', () => {
  const result=unresolvedWorkSummary([draft(1,{_raw:{}}),draft(2,{automaticDisposition:{responsibility:'human',reason:'Manual work or no supported proposal writer'}})],{1:{state:'assigned'}})
  expect(result.groups.find(group=>group.key==='human').count).toBe(2)
  expect(result.groups.find(group=>group.key==='unsupported').count).toBe(0)
})
it('renders specific next steps and explicit limits on recovery promises', () => {
  const html=renderToStaticMarkup(<RemainingWorkStatus automatic rows={mixed} />)
  expect(html).toContain('6 review items in this view')
  expect(html).toContain('not document or unresolved-finding totals')
  expect(html).toContain('Automatic repair unavailable · 1 review item')
  expect(html).toContain('Human decisions or manual edits · 2 review items')
  expect(html).toContain('Status needs investigation · 1 review item')
  expect(html).toContain('Potential recovery')
  expect(html).toContain('retry when eligible')
  expect(html).toContain('then reassess the corrected copy')
})
it('does not turn an empty or completed review list into a claim that findings are resolved', () => {
  expect(unresolvedWorkSummary([draft(1,{status:'verified'})]).total).toBe(0)
  expect(renderToStaticMarkup(<RemainingWorkStatus rows={[]} />)).toBe('')
})

it('uses next steps available with automatic approval either on or off', () => {
  for (const automatic of [true, false]) {
    const html=renderToStaticMarkup(<RemainingWorkStatus automatic={automatic} rows={mixed} />)
    expect(html).toContain('Inspect the saved evidence or Live Operations')
    expect(html).not.toContain('Open Status checks')
    expect(html).not.toContain('Open Needs your input')
  }
})
