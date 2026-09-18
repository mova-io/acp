import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import FileCoverage from './FileCoverage.jsx'
import Tiles, { outcomeTileModel } from './WorkflowOutcomeTiles.jsx'
let root, container
afterEach(async () => { if(root) await act(async () => root.unmount()); container?.remove(); vi.useRealTimers(); vi.unstubAllGlobals() })
async function mount(content) { container=document.createElement('div');document.body.append(container);root=createRoot(container);await act(async () => root.render(content)) }
const domain={total:10,buckets:{awaiting_recorded_outcome:2,approved_pending_verification:1,resolved_verified:3,awaiting_review:2,unexpected_result:1,excluded:1}}
it('orders verified progress before separate unresolved outcomes and counts unknown findings once',async()=>{
 await mount(<Tiles stage="remediate" domain={domain}/>);
 const groups=container.querySelectorAll('[role="group"]')
 expect([...groups[0].querySelectorAll('.workflow-outcome-tiles__label')].map(el=>el.textContent)).toEqual(['Awaiting outcome','Applying & checking','Verified fixes'])
 // Label changed deliberately (finding/review reconciliation): the gray tile holds policy exclusions AND
 // findings whose target was replaced by a verified change, so "Excluded" alone misnamed the second.
 expect([...groups[1].querySelectorAll('.workflow-outcome-tiles__label')].map(el=>el.textContent)).toEqual(['Unresolved findings','Excluded or replaced'])
 const gray=groups[1].querySelector('.tone-gray .workflow-outcome-tiles__definition')
 expect(gray.textContent).toBe('0 replaced or superseded · 1 excluded by policy')
 const model=outcomeTileModel('remediate',domain)
 expect(model.tiles.find(tile=>tile.key==='attention').value).toBe(3)
 expect(model.tiles.reduce((sum,tile)=>sum+tile.value,0)).toBe(10)
 expect(container.textContent).not.toContain('Before unavailable')
})
it('opens accessible definitions without filtering or nesting buttons',async()=>{
 const filter=vi.fn();await mount(<Tiles stage="remediate" domain={domain} onFilter={filter} queueMode/>);
 expect(container.querySelector('button button')).toBeNull()
 const info=container.querySelector('[aria-label="About Unresolved findings"]')
 await act(async()=>info.focus())
 expect(container.querySelector('[role="tooltip"]').textContent).toContain('does not mean new issues were introduced')
 expect(info.getAttribute('aria-describedby')).toBe(container.querySelector('[role="tooltip"]').id)
 await act(async()=>document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})))
 expect(container.querySelector('[role="tooltip"]')).toBeNull()
 await act(async()=>info.click())
 expect(container.querySelector('[role="tooltip"]')).not.toBeNull()
 expect(filter).not.toHaveBeenCalled()
 await act(async()=>container.querySelector('.tone-amber > button').click())
 expect(filter).toHaveBeenCalledWith('attention')
})
it('shows an attempt-only progress bar and omits fixed scope comparisons',async()=>{
 const filter=vi.fn();await mount(<FileCoverage evidence={{counts:{withFindings:19,processed:11,remaining:8},baseline:{withFindings:19,processed:0,remaining:19}}} onSelect={filter}/>);
 expect(container.querySelector('progress').max).toBe(19)
 expect(container.querySelector('progress').value).toBe(11)
 expect(container.querySelector('.coverage-withFindings .kpi-comparison')).toBeNull()
 expect(container.textContent).toContain('11 of 19 files with findings have finished an automatic processing attempt')
 expect(container.textContent).toContain('may still leave unresolved findings')
 expect(container.querySelector('button button')).toBeNull()
 await act(async()=>container.querySelector('.coverage-processed > button').click())
 expect(filter).toHaveBeenCalledWith('processed')
})
it('withholds misleading progress when attempts cannot reconcile',async()=>{
 await mount(<FileCoverage evidence={{counts:{withFindings:19,processed:11,remaining:12}}}/>);
 expect(container.querySelector('progress')).toBeNull()
 expect(container.querySelector('.coverage-processed strong').textContent).toBe('—')
 expect(container.textContent).toContain('Awaiting recorded processing outcomes for 19 files')
})
it('keeps unavailable release classification separate from failed delivery with saved-plan scope',async()=>{
 await mount(<Tiles stage="release" scopeLabel="4 authorized files · entire saved plan" domain={{total:4,buckets:{published:1,failed:1,unclassified:1,unknown_status:1}}}/>);
 expect(container.textContent).toContain('4 authorized files · entire saved plan')
 const model=outcomeTileModel('release',{total:4,buckets:{published:1,failed:1,unclassified:1,unknown_status:1}})
 expect(model.tiles.find(tile=>tile.key==='attention').value).toBe(1)
 expect(model.tiles.find(tile=>tile.key==='unclassified').value).toBe(2)
 expect(model.tiles.reduce((sum,tile)=>sum+tile.value,0)).toBe(4)
})
it('uses warning for unresolved gains and successful green deltas for verified gains with reduced motion',async()=>{
 vi.useFakeTimers();vi.stubGlobal('matchMedia',()=>({matches:true}));
 await mount(<Tiles stage="remediate" domain={{total:2,buckets:{awaiting_recorded_outcome:2}}} executionId="same"/>);
 await act(async()=>root.render(<Tiles stage="remediate" domain={{total:2,buckets:{failed:1,resolved_verified:1}}} executionId="same"/>))
 expect(container.querySelector('.tone-amber .kpi-update-delta--warning').textContent).toBe('+1')
 expect(container.querySelector('.tone-green .kpi-update-delta--positive').textContent).toBe('+1')
 expect(container.querySelector('.tone-amber strong').textContent).toBe('1+1')
 await act(async()=>vi.advanceTimersByTime(2001))
 expect(container.querySelector('.kpi-update-delta')).toBeNull()
})
