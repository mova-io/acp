import { createElement } from 'react'
import { act } from 'react'
import { describe, it, expect, afterEach, vi } from 'vitest'
import axe from 'axe-core'
import { createTestRoot, unmountAll } from './testRoots.js'
import BalancedSummary, { ScanActivityCalendar } from './BalancedSummary.jsx'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

const props = {
  showSupplemental: true, // Retained chart variants remain testable for explicit restoration.
  run: { id: 'chosen', files: 2, scope: { scan_scope: { '1.3.1': true, '1.1.1': true }, inventory: { discovered: 2, assessment_eligible: 2, by_format: { docx: 2 } } } },
  files: [{ file: 'finance.docx', type: 'DOCX', score: 45, status: 'analysed', department: 'Finance', issues: [{ wcag: '1.3.1', severity: 'SERIOUS' }, { wcag: '1.1.1', severity: 'MODERATE', has_proposal: true }] }],
  cap: { docx: { '1.3.1': 'auto', '1.1.1': 'assisted' } },
}
const render = async (p = props) => {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(BalancedSummary, p)))
  return { root, container }
}
afterEach(unmountAll)

describe('balanced summary approved dashboard', () => {
  it('renders the five selected charts with real Assess/Remediate tags, not severity columns', async () => {
    const { container: c } = await render()
    expect([...c.querySelectorAll('.balanced-grid h2')].map(h => h.textContent)).toEqual(['Estate coverage by file type', 'Document formats', 'Remediation opportunities', 'Finding categories', 'Human review age'])
    expect(c.querySelector('.balanced-heatmap').textContent).toContain('Auto')
    expect(c.querySelector('.balanced-heatmap').textContent).toContain('Approve')
    expect(c.querySelector('.balanced-heatmap').textContent).not.toContain('Serious')
    expect(c.querySelector('.balanced-review').textContent).toContain('Not recorded')
    expect(c.querySelectorAll('table')).toHaveLength(5)
  })
  it('opens the exact contributing findings from a department/tag cell and clears details on scan change', async () => {
    const { root, container: c } = await render()
    await act(async () => c.querySelector('[aria-label="Finance, Auto: 1 findings"]').click())
    expect(c.querySelector('.balanced-evidence').textContent).toContain('finance.docx')
    expect(c.querySelector('.balanced-evidence').textContent).toContain('1.3.1')
    expect(c.querySelector('.balanced-evidence').textContent).not.toContain('1.1.1')
    await act(async () => root.render(createElement(BalancedSummary, { ...props, run: { id: 'other' }, files: [] })))
    expect(c.querySelector('.balanced-evidence')).toBeNull()
    expect(c.querySelector('.balanced-findings').textContent).toContain('not available')
  })
  it('has keyboard-operable charts and accessible table semantics', async () => {
    const { container: c } = await render()
    expect(c.querySelector('.balanced-tile').tagName).toBe('BUTTON')
    const result = await axe.run(c, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } })
    expect(result.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.html) }))).toEqual([])
  })
  it('puts the activity calendar alongside all selected-scan charts and preserves UTC drill-down values', async () => {
    const onDay = vi.fn(), day = { date: '2026-09-14', attempts: 22, statuses: { done: 19, failed: 3 } }
    const { container: c } = await render({ ...props, calendar: createElement(ScanActivityCalendar, { activity: [day], onDay }) })
    expect(c.querySelectorAll('.balanced-grid h2')).toHaveLength(6)
    expect(c.textContent).toContain('selected scan chosen')
    expect(c.textContent).toContain('22 dated attempts')
    await act(async () => c.querySelector('[aria-label="2026-09-14: 22 attempts. Show matching scans"]').click())
    expect(onDay).toHaveBeenCalledWith(day)
    expect(c.querySelector('[aria-label="2026-09-13: no activity row recorded"]').textContent).toContain('—')
  })
  it('renders missing assessment results as unknown rather than measured zero findings', async () => {
    const { container: c } = await render({ run: { id: 'discovery', files: 1 }, files: [{ file: 'waiting.docx', type: 'DOCX', status: 'discovered', score: null, issues: [] }] })
    const tile = [...c.querySelectorAll('.balanced-metric')].find(t => t.textContent.includes('Total findings'))
    expect(tile.querySelector('strong').textContent).toBe('—')
    expect(c.querySelector('.balanced-remediation').textContent).toContain('not produced findings')
  })
})

describe('Other finding criteria breakdown', () => {
  const otherProps = {
    run: { id: 'scan-a', files: 2, scope: { scan_scope: { '1.3.1': true, '2.4.2': true, '3.1.1': true, '1.4.4': true }, inventory: { discovered: 2, assessment_eligible: 2, by_format: { docx: 2 } } } },
    files: [
      { file: 'a.docx', type: 'DOCX', score: 40, status: 'analysed', issues: [{ wcag: '1.3.1' }, { wcag: '1.3.1' }, { wcag: '1.3.1' }, { wcag: '1.3.1' }, { wcag: '1.3.1' }, { wcag: '2.4.2' }, { wcag: '3.1.1' }, { wcag: '2.4.4' }] },
      { file: 'b.docx', type: 'DOCX', score: 40, status: 'analysed', issues: [{ wcag: '2.4.2' }, { wcag: '1.4.4', resolution: 'fixed' }] },
    ],
    cap: { docx: { '1.3.1': 'auto', '2.4.2': 'auto', '3.1.1': 'auto', '1.4.4': 'auto' } },
  }
  const parts = c => {
    const other = [...c.querySelectorAll('.balanced-findings .balanced-rank:not(.balanced-subrank)')].find(b => b.textContent.startsWith('▸Other') || b.textContent.startsWith('▾Other'))
    return { other, region: c.querySelector(`[id="${other.getAttribute('aria-controls')}"]`) }
  }
  const children = c => [...c.querySelectorAll('.balanced-subrank')].map(b => [b.querySelector('span').textContent, b.querySelector('strong').textContent, b.querySelector('small').textContent])
  const evidence = c => [...c.querySelectorAll('.balanced-evidence tbody tr')].map(tr => [...tr.cells].slice(0, 2).map(td => td.textContent))

  it('discloses in-scope criteria inline, directly below Other and before the next parent row', async () => {
    const { container: c } = await render(otherProps)
    const { other, region } = parts(c)
    expect(other.getAttribute('aria-expanded')).toBe('false')
    expect(region.hidden).toBe(true)
    expect(other.textContent).toContain('3')
    await act(async () => { other.focus(); other.click() })
    expect(other.getAttribute('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(other)
    expect(region.hidden).toBe(false)
    expect(region.getAttribute('role')).toBe('group')
    expect(other.nextElementSibling).toBe(region)
    // Parent order is by count (Structure 5, Other 3, then zero rows), so the region sits between parents.
    expect(region.nextElementSibling.classList.contains('balanced-rank')).toBe(true)
    expect(region.previousElementSibling.previousElementSibling.textContent).toContain('Structure')
    expect(children(c)).toEqual([['2.4.2 Page Titled', '2', '67% of Other'], ['3.1.1 Language of Page', '1', '33% of Other']])
    // Cumulative parent percentages are unchanged by the children: 5/8, then 8/8.
    expect(other.querySelector('small').textContent).toBe('100% cumulative')
    await act(async () => other.click())
    expect(other.getAttribute('aria-expanded')).toBe('false')
    expect(region.hidden).toBe(true)
    expect(c.querySelector('.balanced-subrank')).toBeNull()
  })

  it('opens exactly the matching findings from a child bar, and all Other findings from the region', async () => {
    const { container: c } = await render(otherProps)
    await act(async () => parts(c).other.click())
    await act(async () => c.querySelector('.balanced-subrank').click())
    expect(c.querySelector('.balanced-evidence h2').textContent).toBe('Other · 2.4.2 Page Titled')
    expect(evidence(c)).toEqual([['a.docx', '2.4.2'], ['b.docx', '2.4.2']])
    await act(async () => [...c.querySelectorAll('.balanced-subrank-list .balanced-link')][0].click())
    expect(c.querySelector('.balanced-evidence h2').textContent).toBe('Other')
    expect(evidence(c)).toEqual([['a.docx', '2.4.2'], ['a.docx', '3.1.1'], ['b.docx', '2.4.2']])
  })

  it('collapses on scan change, stays open across a same-scan refresh, and follows refreshed data', async () => {
    const { root, container: c } = await render(otherProps)
    await act(async () => parts(c).other.click())
    const refreshed = { ...otherProps, files: [...otherProps.files, { file: 'c.docx', type: 'DOCX', score: 40, status: 'analysed', issues: [{ wcag: '3.1.1' }, { wcag: '3.1.1' }] }] }
    await act(async () => root.render(createElement(BalancedSummary, refreshed)))
    expect(parts(c).other.getAttribute('aria-expanded')).toBe('true')
    expect(children(c)).toEqual([['3.1.1 Language of Page', '3', '60% of Other'], ['2.4.2 Page Titled', '2', '40% of Other']])
    await act(async () => root.render(createElement(BalancedSummary, { ...refreshed, run: { ...refreshed.run, id: 'scan-b' } })))
    expect(parts(c).other.getAttribute('aria-expanded')).toBe('false')
    expect(c.querySelector('.balanced-subrank')).toBeNull()
    // Returning to the first scan does not resurrect its old disclosure state.
    await act(async () => root.render(createElement(BalancedSummary, refreshed)))
    expect(parts(c).other.getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps an empty Other non-expandable and renders nothing without an assessment', async () => {
    const { root, container: c } = await render(props)
    const { other, region } = parts(c)
    expect(other.disabled).toBe(true)
    expect(other.getAttribute('aria-expanded')).toBe('false')
    expect(region.hidden).toBe(true)
    await act(async () => root.render(createElement(BalancedSummary, { run: { id: 'none' }, files: [] })))
    expect(c.querySelector('.balanced-findings .balanced-rank')).toBeNull()
    expect(c.querySelector('.balanced-subrank-list')).toBeNull()
  })

  it('passes axe with the breakdown expanded', async () => {
    const { container: c } = await render(otherProps)
    await act(async () => parts(c).other.click())
    const result = await axe.run(c, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } })
    expect(result.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.html) }))).toEqual([])
  })
})

// Owner retired these supplemental visuals from both tabs on 2026-09-14.
it.each([false, true])('omits retired charts and legend from the live dashboard (analytics=%s)', async analytics => {
 const {container:c}=await render({...props,showSupplemental:false,calendar:analytics ? createElement(ScanActivityCalendar,{activity:[]}) : null})
 expect(c.querySelector('.balanced-tags')).toBeNull()
 expect(c.querySelector('.balanced-formats')).toBeNull()
 expect(c.querySelector('.balanced-review')).toBeNull()
 expect(c.querySelectorAll('.balanced-grid>section')).toHaveLength(analytics ? 4 : 3)
 expect(c.querySelector('.balanced-heatmap')).toBeTruthy()
})
