import { expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import QualityEvidenceSummary, {qualityEvidenceSummary} from './QualityEvidenceSummary.jsx'
import CloudAIActivity from './CloudAIActivity.jsx'
vi.mock('./useWaterfallDrawerMetrics.js',()=>({default:()=>({})}))
const view = {available:true,saved_ai_policy:{level:1,zone:'any',quality_first:true},spending:{currency:'USD',spent_units:12345,held_units:100000}}
it('separates selected mode, observed attempts, and verified findings',()=>{
 const html=renderToStaticMarkup(<QualityEvidenceSummary view={view} verifiedFindings={7} metrics={{complete:false,models:{rows:[{label:'Recorded provider · observed model',value:2}]}}} />)
 expect(html).toContain('Quality-first cloud')
 expect(html).toContain('observed model (2)')
 expect(html).toContain('7 findings')
 expect(html).toContain('$0.012345 settled')
 expect(html).toContain('$0.10 reserved')
 expect(html).toContain('Coverage is partial')
 expect(html).toContain('includes all repair methods')
})
it('never substitutes configured or proposal-linked models for actual run attempts',()=>{
 const data={...view,models:[{model:'old proposal model'}],run_graph:{steps:[{model:'configured unused model',state:'outcome_unknown'}]}}
 const html=renderToStaticMarkup(<QualityEvidenceSummary view={data} verifiedFindings={null} />)
 expect(html).not.toContain('old proposal model')
 expect(html).not.toContain('configured unused model')
 expect(html).toContain('does not establish that cloud AI was skipped')
 expect(html).toContain('Unavailable findings')
})
it('reports only explicit skip decisions and saved policy blockers',()=>{
 expect(qualityEvidenceSummary({view:{...view,run_graph:{steps:[{state:'not_needed'}]}}}).notices.join()).toContain('explicitly recorded the skip')
 expect(qualityEvidenceSummary({view:{...view,spending:{cap_units:0}}}).notices.join()).toContain('allowance is zero')
 expect(qualityEvidenceSummary({view:{...view,saved_ai_policy:{level:1,zone:'local'}}}).notices.join()).toContain('excluded by the saved local-only plan')
 expect(qualityEvidenceSummary({view:{...view,spending:{blocked:true}}}).notices.join()).toContain('charges are reconciled')
})
it('preserves missing evidence and currency rather than manufacturing zero or dollar charges',()=>{
 expect(qualityEvidenceSummary({}).mode).toBe('Saved mode unavailable')
 expect(qualityEvidenceSummary({view:{...view,saved_ai_policy:{level:1,zone:'any'}}}).mode).toBe('Saved mode unavailable')
 expect(qualityEvidenceSummary({view:{...view,spending:{currency:'EUR',spent_units:10}}}).settled).toBe('Unavailable')
 expect(qualityEvidenceSummary({view:{...view,available:false}}).settled).toBe('Unavailable')
})
it('keeps the saved disabled reason visible when AI charts are disabled',()=>{
 const html=renderToStaticMarkup(<CloudAIActivity aiEnabled={false} view={{...view,saved_ai_policy:{level:0,zone:'any'}}} />)
 expect(html).toContain('Rules only')
 expect(html).toContain('AI is disabled in the saved plan')
})
it('reservation-only activity shows held cost without claiming a model ran',()=>{
 const html=renderToStaticMarkup(<QualityEvidenceSummary view={{...view,stages:[{reserved:1,models:[]}],models:[{model:'configured'}]}} metrics={{complete:true,models:{rows:[]}}} verifiedFindings={0} />)
 expect(html).toContain('$0.10 reserved')
 expect(html).toContain('No linked model attempts available')
 expect(html).not.toContain('configured')
 expect(html).toContain('0 findings')
})

it.each([undefined, 'local'])('does not infer a cloud route from Quality-first alone (%s)',zone=>{
 const result=qualityEvidenceSummary({view:{...view,saved_ai_policy:{level:1,quality_first:true,zone}}})
 expect(result.mode).toBe('Quality-first requested · cloud route unconfirmed')
})
