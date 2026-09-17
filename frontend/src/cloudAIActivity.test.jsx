import { act } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import CloudAIActivity, { AIActivityCharts, LocalAIRequestActivity, observedPaceScale } from './CloudAIActivity.jsx'
import { readFileSync } from 'node:fs'
vi.mock('./useWaterfallDrawerMetrics.js', () => ({ default: vi.fn(() => ({})) }))
import useMetrics from './useWaterfallDrawerMetrics.js'
afterEach(async () => { await unmountAll(); vi.clearAllMocks() })
it('uses an explicit observed scale rather than inventing a performance target', () => {
 expect(observedPaceScale({value:3},{points:[{value:2},{value:8},{value:null}]}).scaleMax).toBe(8)
 expect(observedPaceScale({value:3},{points:[]}).scaleMax).toBeUndefined()
})
it('shows individual provider usage, charts and honest missing values', async () => {
 const {root,container}=createTestRoot()
 await act(async () => root.render(<AIActivityCharts data={{models:{rows:[{id:'anthropic:sonnet',label:'anthropic · sonnet',value:2,active:1,input_tokens:null,output_tokens:50,average_seconds:40,timed_attempts:1}]},pace:{value:2,observedSeconds:90,unit:'attempts/min'},trend:{points:[{timestamp:'2026-09-13T12:00:00Z',value:2}]},spend:{rows:[]},contribution:{rows:[]}}}/>))
 expect(container.textContent).toContain('anthropic · sonnet')
 expect(container.textContent).toContain('Unavailable')
 expect(container.querySelector('.wd-gauge')).not.toBeNull()
 expect(container.querySelector('.wd-trend')).not.toBeNull()
 expect(container.querySelector('[role="region"]').tabIndex).toBe(0)
 expect(container.textContent).toContain('not a verified repair')
 const dashboard=container.querySelector('.wd-ai-dashboard')
 expect(dashboard.children).toHaveLength(6)
 expect([...dashboard.children].every(child=>child.matches('section.wd-chart'))).toBe(true)
 expect(dashboard.lastElementChild.getAttribute('aria-label')).toBe('Individual model usage')
 expect(dashboard.querySelectorAll('details summary')).toHaveLength(1)
})
it('balances chart rows and keeps Assess-size headings even inside the waterfall card', () => {
 const css=readFileSync('src/waterfall-drawer-charts.css','utf8')
 expect(css).toMatch(/\.wd-ai-dashboard\s*\{[^}]*display:flex;[^}]*flex-wrap:wrap/)
 expect(css).toMatch(/\.wd-ai-dashboard > \.wd-chart\s*\{[^}]*flex:1 1 calc\(33\.333% - 8px\);[^}]*min-width:min\(100%,260px\)/)
 expect(css).toMatch(/\.wd-ai-dashboard > \.wd-model-details\s*\{[^}]*flex-basis:100%/)
 expect(css).toMatch(/\.wf-card \.wd-ai-dashboard \.wd-chart h3\s*\{[^}]*font-family:var\(--font-ui\);[^}]*font-size:13px/)
 expect(css).not.toContain('.wd-ai-dashboard > .wd-charts { display:contents; }')
})
it('keeps disabled-run evidence visible without enabling metrics requests', async () => {
 const {root,container}=createTestRoot()
 await act(async () => root.render(<CloudAIActivity scanId="s" batchId="r" aiEnabled={false} live/>))
 expect(container.textContent).toContain('Saved mode unavailable')
 expect(container.textContent).not.toContain('AI usage · this run')
 expect(useMetrics).toHaveBeenLastCalledWith(expect.objectContaining({scanId:'s',batchId:'r',enabled:false}))
})

it('shows measured latency distribution and validation coverage without claiming verified fixes', async () => {
 const {root,container}=createTestRoot()
 await act(async () => root.render(<AIActivityCharts data={{models:{rows:[{id:'local:m',label:'local · m',value:3,median_seconds:3,p95_seconds:10,usable_percent:50,validated_attempts:2,validation_unavailable:1,provider_timing:{model_load_ms:{average:12,measured_attempts:1},inference_ms:{average:80,measured_attempts:1}}}]}}}/>))
 expect(container.textContent).toContain('Median / P95')
 expect(container.textContent).toContain('3 / 10 s')
 expect(container.textContent).toContain('50%')
 expect(container.textContent).toContain('12 / 80 ms')
 expect(container.textContent).toContain('1 load · 1 inference measurements')
 expect(container.textContent).toContain('2 validated · 1 unavailable')
 expect(container.textContent).toContain('not pure model latency')
})
it('shows actual admission wait with coverage and excludes unlinked calls honestly', async () => {
 useMetrics.mockReturnValue({data:{mode:'recorded',complete:true,models:{rows:[{id:'cloud:m',label:'cloud · m',provider_timing:{queue_wait_ms:{average:250,measured_attempts:2}}}]}}})
 const {root,container}=createTestRoot()
 await act(async () => root.render(<CloudAIActivity scanId="s" batchId="r"/>))
 expect(container.textContent).toContain('Admission wait')
 expect(container.textContent).toContain('250 ms')
 expect(container.textContent).toContain('2 measured waits')
 expect(container.textContent).toContain('Calls without a run link are excluded')
 expect(container.textContent).toContain('not the provider’s internal queue')
})
it('keeps absent admission measurements unavailable rather than zero', async () => {
 const {root,container}=createTestRoot()
 await act(async () => root.render(<AIActivityCharts data={{models:{rows:[{id:'legacy:m',label:'legacy · m'}]}}}/>))
 const headers=[...container.querySelectorAll('thead th')]
 const index=headers.findIndex(header=>header.textContent==='Admission wait')
 expect(index).toBeGreaterThan(0)
 expect(container.querySelector('tbody tr').children[index].textContent).toContain('Unavailable')
 expect(container.querySelector('tbody tr').children[index].textContent).not.toContain('0 ms')
})

it('shows explicitly bound local requests without claiming spending or verified output', async () => {
 const {root,container}=createTestRoot()
 await act(async () => root.render(<LocalAIRequestActivity data={{complete:true,rows:[{model:'local-vision',requests:2,successful_responses:1,unsuccessful_requests:1,average_request_ms:225,measured_requests:2,timings:{queue_wait_ms:{average:250,measured_requests:2}}}]}}/>))
 expect(container.textContent).toContain('local-vision')
 expect(container.textContent).toContain('250 ms')
 expect(container.textContent).toContain('2 measured waits')
 expect(container.textContent).toContain('A successful response is not a verified repair')
 expect(container.textContent).toContain('Unavailable')
 expect(container.textContent).not.toContain('Settled spend')
 expect(container.querySelector('[role="region"]').tabIndex).toBe(0)
})
it('does not show an empty local activity table or imply missing requests were zero', async () => {
 const {root,container}=createTestRoot()
 await act(async () => root.render(<LocalAIRequestActivity data={{complete:true,rows:[]}}/>))
 expect(container.textContent).toBe('')
})
