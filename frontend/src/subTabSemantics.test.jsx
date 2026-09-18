/**
 * Secondary role=tablist surfaces — what their DOM actually is.
 *
 * Companion to workflowNavigationSemantics.test.jsx (the primary nav) and
 * liveOpsFlowTabsSemantics.test.jsx (Live Operations flow views). Each test states a DOM fact
 * about the ARIA tabs pattern: tab ↔ tabpanel linkage by ids that exist, aria-selected (never
 * aria-expanded), a roving tabindex, and Left/Right/Home/End. No screen-reader claims.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, createElement } from 'react'
import { createTestRoot, unmountAll } from './testRoots.js'

// Every API call stays pending: these tests read tab markup only, and a never-settling promise
// keeps panel bodies from firing real fetches or setting state after teardown.
vi.mock('./api.js', async (importActual) => {
  const actual = await importActual()
  return Object.fromEntries(Object.entries(actual).map(([k, v]) =>
    [k, typeof v === 'function' ? vi.fn(() => new Promise(() => {})) : v]))
})

const { default: Settings } = await import('./Settings.jsx')
const { default: RemediationWorkspaceTabs } = await import('./RemediationWorkspaceTabs.jsx')
const { default: WaterfallVisualDrawer } = await import('./WaterfallVisualDrawer.jsx')
const { default: SourceDrawer } = await import('./SourceDrawer.jsx')

globalThis.IS_REACT_ACT_ENVIRONMENT = true
beforeEach(() => {
  history.replaceState({}, '', '/?tab=remediate')
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  HTMLDialogElement.prototype.close = function () { this.open = false }
})
afterEach(unmountAll)

const mount = async (el) => {
  const { root, container } = createTestRoot()
  await act(async () => { root.render(el) })
  return container
}
const key = async (el, k) => act(async () => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }))
})
const tabsIn = (c) => [...c.querySelectorAll('[role="tab"]')]
const selectedIn = (c) => tabsIn(c).filter((t) => t.getAttribute('aria-selected') === 'true')
const stopsIn = (c) => tabsIn(c).filter((t) => t.tabIndex === 0)

/** The full static contract for a tablist whose tabs all control ONE shared panel. */
function expectLinkedToSharedPanel(list) {
  const tabs = tabsIn(list)
  const sel = selectedIn(list)
  expect(sel).toHaveLength(1)
  expect(stopsIn(list)).toEqual(sel)
  const ids = tabs.map((t) => t.id)
  expect(ids.every(Boolean)).toBe(true)
  expect(new Set(ids).size).toBe(ids.length)
  for (const t of tabs) {
    expect(t.hasAttribute('aria-expanded')).toBe(false)
    const panel = document.getElementById(t.getAttribute('aria-controls'))
    expect(panel?.getAttribute('role')).toBe('tabpanel')
  }
  const panel = document.getElementById(sel[0].getAttribute('aria-controls'))
  expect(document.getElementById(panel.getAttribute('aria-labelledby'))).toBe(sel[0])
  return panel
}

/** Left/Right/Home/End move focus AND selection, with wrap-around. */
async function expectArrowKeys(getList) {
  const tabs = tabsIn(getList())
  const n = tabs.length
  const start = selectedIn(getList())[0]
  const i = tabs.indexOf(start)
  start.focus()
  await key(start, 'ArrowRight')
  const next = tabsIn(getList())[(i + 1) % n]
  expect(document.activeElement).toBe(next)
  expect(selectedIn(getList())).toEqual([next])
  expect(stopsIn(getList())).toEqual([next])
  await key(next, 'End')
  expect(document.activeElement).toBe(tabsIn(getList())[n - 1])
  expect(selectedIn(getList())).toEqual([tabsIn(getList())[n - 1]])
  await key(document.activeElement, 'Home')
  expect(document.activeElement).toBe(tabsIn(getList())[0])
  expect(selectedIn(getList())).toEqual([tabsIn(getList())[0]])
  await key(document.activeElement, 'ArrowLeft')
  expect(document.activeElement).toBe(tabsIn(getList())[n - 1])
  expectLinkedToSharedPanel(getList())
}

describe('Settings modal section tabs (Settings.jsx)', () => {
  const open = () => mount(createElement(Settings, { onClose: () => {}, me: { is_admin: false } }))
  const list = (c) => c.querySelector('[role="tablist"][aria-label="Settings sections"]')

  it('is a role=tablist of 10 buttons with aria-selected on exactly one, no aria-expanded', async () => {
    const c = await open()
    expect(list(c)).toBeTruthy()
    expect(tabsIn(list(c))).toHaveLength(10)
    for (const t of tabsIn(list(c))) expect(t.tagName).toBe('BUTTON')
    expect(selectedIn(list(c)).map((t) => t.textContent)).toEqual(['Users'])
  })

  it('links every tab to one tabpanel that wraps the section body and is labelled by the selected tab', async () => {
    const c = await open()
    const panel = expectLinkedToSharedPanel(list(c))
    expect(panel.classList.contains('setbody')).toBe(true)
  })

  it('uses a roving tabindex: only the selected tab is a Tab stop', async () => {
    const c = await open()
    expect(stopsIn(list(c)).map((t) => t.textContent)).toEqual(['Users'])
    expect(tabsIn(list(c)).filter((t) => t.getAttribute('tabindex') === '-1')).toHaveLength(9)
  })

  it('selects a section on click and relabels the panel', async () => {
    const c = await open()
    const roles = tabsIn(c).find((t) => t.textContent === 'Roles')
    await act(async () => { roles.click() })
    expect(selectedIn(c)).toEqual([roles])
    expectLinkedToSharedPanel(list(c))
  })

  it('moves focus and selection with ArrowRight/ArrowLeft/Home/End', async () => {
    const c = await open()
    await expectArrowKeys(() => list(c))
  })
})

describe('Remediation workspace tabs (RemediationWorkspaceTabs.jsx:127-149)', () => {
  const open = () => mount(createElement(RemediationWorkspaceTabs, {
    runId: 'one', live: 'live content', review: 'review content', waterfall: 'ai content',
  }))

  it('links each tab to its own existing tabpanel and back', async () => {
    const c = await open()
    const tabs = tabsIn(c)
    expect(tabs).toHaveLength(3)
    for (const t of tabs) {
      const panel = document.getElementById(t.getAttribute('aria-controls'))
      expect(panel?.getAttribute('role')).toBe('tabpanel')
      expect(panel.getAttribute('aria-labelledby')).toBe(t.id)
      expect(t.hasAttribute('aria-expanded')).toBe(false)
    }
  })

  it('uses aria-selected with a roving tabindex, and hides unselected panels', async () => {
    const c = await open()
    expect(selectedIn(c).map((t) => t.id)).toEqual(['rem-mode-live'])
    expect(stopsIn(c).map((t) => t.id)).toEqual(['rem-mode-live'])
    expect(document.getElementById('rem-panel-live').hidden).toBe(false)
    expect(document.getElementById('rem-panel-review').hidden).toBe(true)
  })

  it('moves focus and selection with ArrowRight/ArrowLeft/Home/End', async () => {
    const c = await open()
    const [live, review, ai] = tabsIn(c)
    live.focus()
    await key(live, 'ArrowRight')
    expect(document.activeElement).toBe(review)
    expect(review.getAttribute('aria-selected')).toBe('true')
    expect(document.getElementById('rem-panel-review').hidden).toBe(false)
    await key(review, 'End')
    expect(document.activeElement).toBe(ai)
    await key(ai, 'Home')
    expect(document.activeElement).toBe(live)
    await key(live, 'ArrowLeft')
    expect(document.activeElement).toBe(ai)
  })
})

describe('Waterfall stage drawer tabs (WaterfallVisualDrawer.jsx)', () => {
  const open = () => mount(createElement(WaterfallVisualDrawer, { identity: 'run', onClose: () => {} }))

  it('links the selected tab to its rendered panel', async () => {
    const c = await open()
    const sel = selectedIn(c)
    expect(sel.map((t) => t.textContent)).toEqual(['Overview'])
    const panel = document.getElementById(sel[0].getAttribute('aria-controls'))
    expect(panel?.getAttribute('role')).toBe('tabpanel')
    expect(panel.getAttribute('aria-labelledby')).toBe(sel[0].id)
  })

  it('never points aria-controls at an id that is not in the document, before or after switching', async () => {
    // Only the selected panel is mounted, so only the selected tab may name one.
    const c = await open()
    const check = () => {
      for (const t of tabsIn(c)) {
        const ref = t.getAttribute('aria-controls')
        if (ref !== null) expect(document.getElementById(ref)).not.toBeNull()
      }
      expect(tabsIn(c).filter((t) => t.hasAttribute('aria-controls'))).toEqual(selectedIn(c))
    }
    check()
    const attempts = tabsIn(c).find((t) => t.textContent === 'Attempts')
    await act(async () => { attempts.click() })
    expect(selectedIn(c)).toEqual([attempts])
    check()
  })
})

describe('Source operations drawer tabs (SourceDrawer.jsx)', () => {
  const SOURCE = { id: 'sp-root', type: 'onedrive', name: 'OneDrive' }
  const open = () => mount(createElement(SourceDrawer, { source: SOURCE, files: [], scans: [], onClose: () => {} }))
  const list = () => document.querySelector('[role="tablist"][aria-label="Source operations"]')

  it('is a role=tablist of 4 buttons with aria-selected on Overview', async () => {
    await open()
    expect(tabsIn(list()).map((t) => t.textContent)).toEqual(['Overview', 'Scope', 'Rules', 'Activity'])
    expect(selectedIn(list()).map((t) => t.textContent)).toEqual(['Overview'])
  })

  it('links every tab to one tabpanel labelled by the selected tab, with a roving tabindex', async () => {
    await open()
    const panel = expectLinkedToSharedPanel(list())
    expect(panel.textContent).toMatch(/Discovery summary/)
  })

  it('moves focus and selection with ArrowRight/ArrowLeft/Home/End', async () => {
    await open()
    await expectArrowKeys(list)
  })
})
