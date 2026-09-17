/**
 * Overview's supporting sections are accessible accordions.
 *
 * WHAT AN ACCORDION HAS TO BE, and why each of these is asserted rather than assumed:
 *   · a real <button> — a <div onClick> is not in the tab order and does not fire on Enter or
 *     Space, so the whole section becomes unreachable without a mouse. jsdom does not synthesize
 *     a click from a keydown, so "keyboard works" cannot be observed directly here; what CAN be
 *     observed is the thing that makes it work — a native button, type="button" (a bare <button>
 *     inside a form submits it), and no tabindex="-1" taking it back out of the tab order.
 *   · aria-expanded — the state, spoken. Without it a screen reader announces a button and no
 *     indication that anything opened.
 *   · aria-controls pointing at an element that EXISTS — a dangling id is worse than none, and it
 *     is exactly what happens when the panel is conditionally rendered away while collapsed. The
 *     panel element is therefore always in the DOM, `hidden`, with its children unmounted.
 *   · unique, stable ids — two sections sharing one id makes aria-controls ambiguous, and
 *     duplicate ids are invalid HTML that assistive tech resolves unpredictably.
 *
 * DOM-level, not browser-level: this repo's preview server runs vite rooted at the SHARED checkout
 * whatever worktree you are in (CLAUDE.md), so a browser check would exercise code without this.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import LiveOverview from './Overview.jsx'
import Overview from './RetiredOverviewWorkflow.jsx'

const here = dirname(fileURLToPath(import.meta.url))

const INVENTORY = {
  discovered: 12408, assessment_eligible: 9000,
  by_status: { assessable: 9000 }, by_format: { pdf: 7000, docx: 5408 },
}
const RUN = {
  id: 's1', status: 'complete', files: 12408, avg_score: 71, certifiable: 40, error: 0,
  completed_at: '2026-09-01T16:04:00Z', scope: { kind: 'drive', inventory: INVENTORY },
}
const FILES = [
  { file: 'a.docx', name: 'a.docx', type: 'DOCX', status: 'done', score: 60,
    issues: [{ sc: '1.1.1', wcag: 'SC_1_1_1', severity: 'CRITICAL' }] },
  { file: 'b.pdf', name: 'b.pdf', type: 'PDF', status: 'done', score: 90, issues: [] },
]

let container, root
afterEach(unmountAll)

const render = async (props = {}, Component = Overview) => {
  ;({ container, root } = createTestRoot())
  await act(async () => {
    root.render(createElement(Component, {
      run: RUN, files: FILES, trend: [], trendDates: [], onGo: () => {},
      scanList: [], onPickScan: () => {}, me: { email: 'auditor@example.com' }, ...props,
    }))
  })
  return container
}

const toggles = () => [...container.querySelectorAll('button.acc-toggle')]
// getElementById-by-hand: jsdom's CSS.escape is not available here, and the generated ids carry
// React's `useId` colons, which a raw `#id` selector cannot take.
const byId = (id) => [...container.querySelectorAll('[id]')].find((el) => el.id === id) || null
const sectionOf = (id) => container.querySelector(`[data-accordion="${id}"]`)
const toggleOf = (id) => sectionOf(id)?.querySelector('button.acc-toggle')

// The estate story and the one primary action start open. Supporting evidence starts collapsed.
const SECTIONS = [
  ['estate-progress',    'Estate progress',           true],
  ['assertion-scope',    'Scope of this Assertion',   false],
  ['estate-composition', 'Estate composition',        false],
  ['operational-details','Operational details',       false],
  ['assessment-summary', 'Assessment',                false],
  ['next-step',          'NEXT',                      true],
]

describe('Overview renders the simplified sections as accessible accordions', () => {
  it('has one accordion per section, and no others', async () => {
    await render()
    expect(toggles()).toHaveLength(SECTIONS.length)
    expect([...container.querySelectorAll('[data-accordion]')].map((el) => el.dataset.accordion).sort())
      .toEqual(SECTIONS.map(([id]) => id).sort())
  })

  it('titles each one', async () => {
    await render()
    for (const [id, title] of SECTIONS) {
      expect(toggleOf(id), `no accordion for ${id}`).toBeTruthy()
      expect(toggleOf(id).textContent, `wrong title on ${id}`).toContain(title)
    }
  })

  it('opens the ones a reader needs on load and collapses the detail', async () => {
    await render()
    for (const [id, , open] of SECTIONS) {
      expect(toggleOf(id).getAttribute('aria-expanded'), `wrong default state for ${id}`).toBe(String(open))
    }
    // At least one of each, or "sensible default state" would be satisfied by all-open/all-closed.
    expect(SECTIONS.some(([, , o]) => o)).toBe(true)
    expect(SECTIONS.some(([, , o]) => !o)).toBe(true)
  })
})

describe('each accordion header is a real, labelled, operable control', () => {
  it('is a native button that stays in the tab order', async () => {
    await render()
    for (const b of toggles()) {
      expect(b.tagName).toBe('BUTTON')
      // Not a submit button — a bare <button> inside a form submits it on Enter.
      expect(b.getAttribute('type')).toBe('button')
      expect(b.hasAttribute('disabled')).toBe(false)
      expect(b.getAttribute('tabindex')).toBeNull()
    }
  })

  it('names its own state, and the panel it controls', async () => {
    await render()
    for (const b of toggles()) {
      expect(['true', 'false']).toContain(b.getAttribute('aria-expanded'))
      const panelId = b.getAttribute('aria-controls')
      expect(panelId, 'no aria-controls').toBeTruthy()
      const panel = byId(panelId)
      expect(panel, `aria-controls points at a missing element: ${panelId}`).toBeTruthy()
      // …and it points back, so the panel is announced with the section's own name.
      expect(panel.getAttribute('aria-labelledby')).toBe(b.id)
      // Deliberately NOT role="region": the enclosing <section aria-label> already is one with
      // the same name, and nesting two identically-named landmarks is noise. See
      // AccordionSection.jsx's own comment; the APG omits it above ~6 panels, and there are 7.
      expect(panel.getAttribute('role')).toBeNull()
      expect(sectionOf(b.closest('[data-accordion]').dataset.accordion).getAttribute('aria-label'))
        .toBeTruthy()
    }
  })

  it('gives every header and panel an id nothing else on the screen shares', async () => {
    await render()
    const ids = [...container.querySelectorAll('[id]')].map((el) => el.id)
    expect(new Set(ids).size, 'duplicate id on the Overview').toBe(ids.length)
    for (const b of toggles()) {
      expect(b.id).toBeTruthy()
      expect(b.id).not.toBe(b.getAttribute('aria-controls'))
    }
  })

  it('keeps its ids stable across a re-render, so a reference cannot go stale', async () => {
    await render()
    const before = toggles().map((b) => [b.id, b.getAttribute('aria-controls')])
    await act(async () => {
      root.render(createElement(Overview, {
        run: RUN, files: FILES, trend: [], trendDates: [], onGo: () => {},
        scanList: [], onPickScan: () => {}, me: { email: 'auditor@example.com' },
      }))
    })
    expect(toggles().map((b) => [b.id, b.getAttribute('aria-controls')])).toEqual(before)
  })
})

describe('operating an accordion moves both the state and the content', () => {
  it('opens a collapsed section and unmounts its content again when closed', async () => {
    await render()
    const b = toggleOf('estate-composition')
    const panel = byId(b.getAttribute('aria-controls'))
    // Closed: announced closed, hidden from the a11y tree, and genuinely empty — not merely
    // invisible. Text still in the DOM would let a later test assert on content nobody can read.
    expect(b.getAttribute('aria-expanded')).toBe('false')
    expect(panel.hasAttribute('hidden')).toBe(true)
    expect(panel.textContent).toBe('')

    await act(async () => { b.click() })
    expect(b.getAttribute('aria-expanded')).toBe('true')
    expect(panel.hasAttribute('hidden')).toBe(false)
    expect(panel.textContent).toContain('Eligible')

    await act(async () => { b.click() })
    expect(b.getAttribute('aria-expanded')).toBe('false')
    expect(panel.textContent).toBe('')
  })

  it('opens a closed section without touching the primary action', async () => {
    await render()
    const assess = toggleOf('assessment-summary')
    const next = toggleOf('next-step')
    await act(async () => { assess.click() })
    expect(assess.getAttribute('aria-expanded')).toBe('true')
    expect(next.getAttribute('aria-expanded')).toBe('true')
  })
})

describe('report exports are consolidated', () => {
  it('uses one native Reports disclosure for every export', async () => {
    await render({}, LiveOverview)
    const menu = container.querySelector('details.reports-menu')
    expect(menu, 'Reports disclosure missing').toBeTruthy()
    expect(menu.querySelector(':scope > summary')?.textContent).toContain('Reports')
    const labels = [...menu.querySelectorAll('.reports-menu-items button')]
      .map((button) => button.textContent)
    expect(labels).toContain('Quarterly governance report')
    // The scan report is offered in the three contract modes (ReportModeMenu, inline).
    const named = [...menu.querySelectorAll('.reports-menu-items button')]
      .map((button) => button.getAttribute('aria-label'))
    expect(named).toEqual(expect.arrayContaining(['Summary — PDF', 'Reviewer packet — PDF', 'Full evidence — PDF']))
    expect(menu.querySelector('[role="group"][aria-label="Scan report"]')).toBeTruthy()
    expect(labels).toContain('Findings (CSV)')
    expect(container.querySelectorAll('.dashtoolbar > button')).toHaveLength(0)
  })
})

describe('the styles the keyboard depends on', () => {
  const css = readFileSync(join(here, 'styles.css'), 'utf8')

  it('draws a visible focus ring on the accordion header', () => {
    // The global :focus-visible rule already covers buttons; this one is stated again for these
    // headers specifically because they sit on a panel background, and a control that cannot be
    // seen to have focus cannot be operated by a keyboard user.
    expect(css).toMatch(/\.acc-toggle:focus-visible\s*\{[^}]*outline:[^}]*\}/)
    expect(css).toMatch(/:focus-visible\s*\{[^}]*outline: 2px solid/)
  })

  it('hides a collapsed panel in CSS as well as with the attribute', () => {
    expect(css).toMatch(/\.acc-panel\[hidden\]\s*\{\s*display:\s*none/)
  })
})

describe('the accordions are Overview-only — no scope drift onto Monitor', () => {
  it('Monitor renders no AccordionSection', () => {
    // The 2026-09-02 PRD asks for accordions on Overview. Monitor was given six in passing and
    // they were reverted; its own pre-existing "Monitoring settings" disclosure is untouched.
    const monitor = readFileSync(join(here, 'Monitor.jsx'), 'utf8')
    expect(monitor).not.toMatch(/AccordionSection/)
    expect(monitor).toMatch(/Monitoring settings/)
  })
})
