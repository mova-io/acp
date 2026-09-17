/**
 * The PRIMARY workflow navigation's real DOM semantics, read off the mounted App.
 *
 * Written as evidence for an accessibility self-assessment that described this navigation as
 * "the ARIA Tabs pattern with correct roles, states (aria-expanded), and keyboard interaction".
 * These tests record what the DOM actually is, so the report can be written from facts:
 *
 *   - It IS a tab widget: <nav> > [role=tablist] > <button role=tab>, one [role=tabpanel].
 *   - Selection is conveyed by aria-selected (plus aria-current="step" on the same tab).
 *     aria-expanded is NOT used on these tabs at all.
 *   - Roving tabindex, and Left/Right/Home/End move focus AND selection (automatic activation).
 *   - A skip link targets <main id="main-content" tabindex="-1">, which wraps the tabpanel.
 *
 * The last block covers a view the role no longer permits. It was first recorded here as a known
 * defect with `it.fails` (no tab stop at all, and the panel labelled by the id of a tab that was no
 * longer rendered); App.jsx now falls back to the first enabled rendered tab for the tab stop and
 * names the panel with aria-label, and those are ordinary tests.
 *
 * DOM-level only (see CLAUDE.md: the preview server serves the shared checkout, not a worktree).
 * Nothing here is a claim about what a screen reader announces.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

globalThis.__BUILD_TIME__ = '2026-08-01T00:00:00.000Z'
globalThis.__BUILD_VERSION__ = '2026.8.1'

let ACCESS = null
const getMyAccess = vi.fn(async () => ACCESS)
// A job that is never done: App's reconnect poll keeps `busy` true, which locks (disables) the
// numbered workflow tabs other than Discover. Only reached when a test seeds active_job_id.
const getJob = vi.fn(async () => ({ done: false, phase: 'discovering', scan_id: 's1' }))

vi.mock('./api.js', async (importActual) => {
  const actual = await importActual()
  return {
    ...actual,
    getConfig: vi.fn(async () => ({ auth: 'demo' })),
    getRubric: vi.fn(async () => ({ target: 'WCAG 2.1 AA', hash: 'abcdef0123' })),
    getSources: vi.fn(async () => []),
    listScans: vi.fn(async () => []),
    getActiveScan: vi.fn(async () => null),
    getSettings: vi.fn(async () => ({ scan_scope: '' })),
    getDecisions: vi.fn(async () => ({})),
    getMyAccess,
    getJob,
    getScan: vi.fn(async () => ({ run: { id: 's1', status: 'done' }, files: [] })),
    getWorkspaceBootstrap: vi.fn(async () => ({
      me: { email: 'rev@hosp.org', is_admin: false, is_scope_owner: false, access: ACCESS },
      scan_id: null, scan_status: null, revision: 0, overview: null, scans: [], active_job: {},
    })),
  }
})

const { default: App } = await import('./App.jsx')

afterEach(async () => { await unmountAll(); sessionStorage.clear(); ACCESS = null })
beforeEach(() => { sessionStorage.clear(); history.replaceState({}, '', '/'); getMyAccess.mockClear() })

const flush = async () => { for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() }) }
const click = async (el) => { await act(async () => { el.click() }); await flush() }
const key = async (el, k) => {
  await act(async () => { el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })) })
  await flush()
}
const tablist = (c) => c.querySelector('nav[aria-label="Compliance workflow"] [role="tablist"]')
const tabs = (c) => [...tablist(c).querySelectorAll('[role="tab"]')]
const label = (t) => t.querySelector('.tablbl')?.textContent?.replace(/^completed: /, '').trim()
const tabNamed = (c, name) => tabs(c).find((t) => label(t) === name)
const selected = (c) => tabs(c).filter((t) => t.getAttribute('aria-selected') === 'true')
const tabStops = (c) => tabs(c).filter((t) => t.tabIndex === 0)

async function signIn() {
  const { container: c, root } = createTestRoot()
  await act(async () => { root.render(createElement(App)) })
  await flush()
  await click([...c.querySelectorAll('button')].find((b) => /Sign in with SSO/.test(b.textContent)))
  return c
}

describe('primary workflow navigation — structure', () => {
  it('is a <nav> landmark containing a role=tablist of <button role=tab>, not links', async () => {
    const c = await signIn()
    const nav = c.querySelector('nav[aria-label="Compliance workflow"]')
    expect(nav).toBeTruthy()
    expect(tablist(c)).toBeTruthy()
    expect(tablist(c).getAttribute('aria-label')).toBe('Compliance workflow')
    expect(tabs(c).length).toBeGreaterThan(4)
    for (const t of tabs(c)) expect(t.tagName).toBe('BUTTON')
    expect(nav.querySelectorAll('a[href]')).toHaveLength(0)
  })

  it('conveys selection with aria-selected on exactly one tab, and never uses aria-expanded', async () => {
    const c = await signIn()
    expect(selected(c)).toHaveLength(1)
    expect(label(selected(c)[0])).toBe('Overview')
    for (const t of tabs(c)) {
      expect(['true', 'false']).toContain(t.getAttribute('aria-selected'))
      expect(t.hasAttribute('aria-expanded')).toBe(false)
    }
  })

  it('also puts aria-current="step" on the selected tab only (redundant with aria-selected)', async () => {
    const c = await signIn()
    const current = tabs(c).filter((t) => t.hasAttribute('aria-current'))
    expect(current).toEqual(selected(c))
    expect(current[0].getAttribute('aria-current')).toBe('step')
  })

  it('uses a roving tabindex: one tab stop, the selected tab', async () => {
    const c = await signIn()
    expect(tabStops(c)).toEqual(selected(c))
    for (const t of tabs(c)) if (t !== selected(c)[0]) expect(t.getAttribute('tabindex')).toBe('-1')
  })

  it('points every tab at one tabpanel that exists and is labelled by the selected tab', async () => {
    const c = await signIn()
    for (const t of tabs(c)) expect(t.getAttribute('aria-controls')).toBe('workflow-panel')
    const panel = document.getElementById('workflow-panel')
    expect(panel?.getAttribute('role')).toBe('tabpanel')
    const labelledBy = panel.getAttribute('aria-labelledby')
    expect(document.getElementById(labelledBy)).toBe(selected(c)[0])
    // Tab ids are unique.
    const ids = tabs(c).map((t) => t.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

describe('skip link', () => {
  it('precedes the navigation and targets a focusable <main> that wraps the tabpanel', async () => {
    const c = await signIn()
    const skip = c.querySelector('a.skiplink')
    expect(skip?.getAttribute('href')).toBe('#main-content')
    expect(skip.textContent).toMatch(/Skip to main content/)
    const nav = c.querySelector('nav[aria-label="Compliance workflow"]')
    expect(skip.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const main = document.getElementById('main-content')
    expect(main?.tagName).toBe('MAIN')
    expect(main.getAttribute('tabindex')).toBe('-1')
    expect(main.contains(document.getElementById('workflow-panel'))).toBe(true)
    expect(nav.contains(main)).toBe(false)
    main.focus()
    expect(document.activeElement).toBe(main)
  })
})

describe('primary workflow navigation — keyboard', () => {
  it('ArrowRight moves focus AND selection to the next tab (automatic activation)', async () => {
    const c = await signIn()
    const first = selected(c)[0]
    const expectedNext = tabs(c)[tabs(c).indexOf(first) + 1]
    first.focus()
    await key(first, 'ArrowRight')
    expect(document.activeElement).toBe(expectedNext)
    expect(selected(c)).toEqual([expectedNext])
    expect(tabStops(c)).toEqual([expectedNext])
    expect(document.getElementById('workflow-panel').getAttribute('aria-labelledby')).toBe(expectedNext.id)
  })

  it('End goes to the last tab, Home to the first, and ArrowLeft wraps from first to last', async () => {
    const c = await signIn()
    const first = tabs(c)[0]
    first.focus()
    await key(first, 'End')
    const last = tabs(c)[tabs(c).length - 1]
    expect(document.activeElement).toBe(last)
    expect(last.getAttribute('aria-selected')).toBe('true')

    await key(last, 'Home')
    expect(document.activeElement).toBe(tabs(c)[0])
    expect(tabs(c)[0].getAttribute('aria-selected')).toBe('true')

    await key(tabs(c)[0], 'ArrowLeft')
    expect(document.activeElement).toBe(tabs(c)[tabs(c).length - 1])
  })

  it('ignores ArrowUp/ArrowDown (horizontal tablist; no aria-orientation set)', async () => {
    const c = await signIn()
    const first = selected(c)[0]
    first.focus()
    await key(first, 'ArrowDown')
    expect(document.activeElement).toBe(first)
    expect(selected(c)).toEqual([first])
    expect(tablist(c).hasAttribute('aria-orientation')).toBe(false)
  })

  it('keeps focus on the tab after a view change; it does not move focus into the panel', async () => {
    const c = await signIn()
    const first = selected(c)[0]
    first.focus()
    await key(first, 'ArrowRight')
    expect(document.activeElement.getAttribute('role')).toBe('tab')
    expect(document.getElementById('main-content').contains(document.activeElement)).toBe(false)
  })

  it('selects a tab on click', async () => {
    const c = await signIn()
    await click(tabNamed(c, 'Discover'))
    expect(selected(c).map(label)).toEqual(['Discover'])
    expect(tabStops(c).map(label)).toEqual(['Discover'])
  })
})

const ALL_TAB_KEYS = ['overview', 'integrations', 'discover', 'assess', 'remediate', 'publish',
  'monitor', 'liveops', 'analytics', 'graph', 'acr', 'settings']
const accessWith = (level) => ({ enforced: true, role: { id: 'm', name: 'Manager' }, capabilities: [],
  tabs: Object.fromEntries(ALL_TAB_KEYS.map((k) => [k, level(k)])) })
const panel = () => document.getElementById('workflow-panel')
// The panel's accessible name must come from something that exists: a rendered element named by
// aria-labelledby, or an aria-label. Never an id with nothing behind it.
const panelNameIsValid = () => {
  const ref = panel().getAttribute('aria-labelledby')
  if (ref) return document.getElementById(ref) !== null
  return Boolean(panel().getAttribute('aria-label'))
}
const hideAndRefocus = async (tabsLevel) => {
  ACCESS = { ...ACCESS, tabs: { ...ACCESS.tabs, ...tabsLevel } }
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  await flush()
}

describe('the current view is no longer a permitted tab (Access restricted)', () => {
  // Control: the setup below really reaches the Access restricted screen with Discover removed.
  // Without it the tests below could pass because the harness never got there.
  const reachRestricted = async () => {
    ACCESS = accessWith(() => 'operate')
    const c = await signIn()
    await click(tabNamed(c, 'Discover'))
    expect(selected(c).map(label)).toEqual(['Discover'])
    await hideAndRefocus({ discover: 'hidden' })
    expect(getMyAccess).toHaveBeenCalled()
    expect(tabNamed(c, 'Discover')).toBeUndefined()
    expect(panel().textContent).toMatch(/Access restricted/)
    return c
  }

  it('control: the harness reaches Access restricted with the Discover tab gone', async () => {
    await reachRestricted()
  })

  // Formerly `it.fails` (App.jsx derived tabIndex solely from `view === k`, so no tab got 0).
  it('keeps exactly one tab in the Tab order — the first rendered tab — without selecting it', async () => {
    const c = await reachRestricted()
    expect(tabStops(c)).toHaveLength(1)
    expect(tabStops(c)[0]).toBe(tabs(c)[0])
    expect(label(tabStops(c)[0])).toBe('Overview')
    expect(selected(c)).toHaveLength(0)
    for (const t of tabs(c)) expect(t.getAttribute('aria-selected')).toBe('false')
  })

  // Formerly `it.fails` (aria-labelledby named `workflow-tab-discover`, which was not rendered).
  it('names the tabpanel with aria-label instead of an id that is not in the document', async () => {
    await reachRestricted()
    expect(panel().hasAttribute('aria-labelledby')).toBe(false)
    expect(panel().getAttribute('aria-label')).toBe('Access restricted: Discover')
    expect(panelNameIsValid()).toBe(true)
  })

  it('puts aria-current on no tab when the current view is not rendered', async () => {
    const c = await reachRestricted()
    expect(tabs(c).filter((t) => t.hasAttribute('aria-current'))).toHaveLength(0)
    expect(c.querySelectorAll('[aria-current]')).toHaveLength(0)
  })

  it('does not navigate: the restriction screen stays, even after focusing the fallback tab stop', async () => {
    const c = await reachRestricted()
    const stop = tabStops(c)[0]
    stop.focus()
    expect(document.activeElement).toBe(stop)
    await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
    await flush()
    expect(panel().textContent).toMatch(/Access restricted/)
    expect(panel().textContent).toMatch(/Discover is not part of your Manager role/)
    expect(selected(c)).toHaveLength(0)
    expect(stop.getAttribute('aria-selected')).toBe('false')
    // Moving on is still the user's choice: an arrow key from the stop selects the next tab.
    await key(stop, 'ArrowRight')
    expect(selected(c).map(label)).toEqual(['Sources'])
    expect(panel().textContent).not.toMatch(/Access restricted/)
    expect(panel().getAttribute('aria-labelledby')).toBe(selected(c)[0].id)
    expect(panel().hasAttribute('aria-label')).toBe(false)
  })

  it('restores the normal tab stop and labelling when the view becomes permitted again', async () => {
    const c = await reachRestricted()
    await hideAndRefocus({ discover: 'operate' })
    expect(selected(c).map(label)).toEqual(['Discover'])
    expect(tabStops(c)).toEqual(selected(c))
    expect(panel().getAttribute('aria-labelledby')).toBe('workflow-tab-discover')
    expect(panel().hasAttribute('aria-label')).toBe(false)
  })
})

describe('restricted view while a scan locks (disables) the numbered steps', () => {
  // A scan in flight (App's reconnect path, as in discoverNavLiveIndicator.test.jsx) sets `busy`,
  // which disables every numbered tab except Discover and the current view. A disabled button
  // cannot take focus and the arrow-key handler skips it, so it must not be the tab stop.
  const reachBusyRestricted = async (levels) => {
    sessionStorage.setItem('active_job_id', 'j1')
    ACCESS = accessWith(() => 'operate')
    const c = await signIn()
    await click(tabNamed(c, 'Discover'))
    await act(async () => { await new Promise((r) => setTimeout(r, 500)) })
    await flush()
    expect(getJob, 'never reconnected — busy will never become true').toHaveBeenCalledWith('j1')
    await hideAndRefocus(levels)
    expect(panel().textContent).toMatch(/Access restricted/)
    return c
  }

  it('skips locked tabs and gives the tab stop to the first enabled rendered tab', async () => {
    // Rendered: Assess, Remediate (numbered, locked) then Live Operations (step 0, enabled).
    const keep = new Set(['assess', 'remediate', 'liveops'])
    const c = await reachBusyRestricted(Object.fromEntries(ALL_TAB_KEYS.map((k) => [k, keep.has(k) ? 'operate' : 'hidden'])))
    expect(tabs(c).map(label)).toEqual(['Assess', 'Remediate', 'Live Operations'])
    expect(tabs(c).map((t) => t.disabled)).toEqual([true, true, false])
    expect(tabStops(c).map(label)).toEqual(['Live Operations'])
    expect(selected(c)).toHaveLength(0)
    const stop = tabStops(c)[0]
    stop.focus()
    expect(document.activeElement).toBe(stop)
    // The key handler agrees: from the stop, ArrowRight wraps past the locked tabs back to itself.
    await key(stop, 'ArrowRight')
    expect(document.activeElement).toBe(stop)
  })

  it('gives no tab the stop when every rendered tab is locked, and the panel is still named', async () => {
    // Every tab still rendered is disabled; a tabIndex=0 on a disabled button would be a stop
    // that does not exist. The user reaches the Access restricted screen (and its own content)
    // through the skip link / normal Tab order instead.
    const keep = new Set(['assess', 'remediate'])
    const c = await reachBusyRestricted(Object.fromEntries(ALL_TAB_KEYS.map((k) => [k, keep.has(k) ? 'operate' : 'hidden'])))
    expect(tabs(c).map(label)).toEqual(['Assess', 'Remediate'])
    expect(tabs(c).every((t) => t.disabled)).toBe(true)
    expect(tabStops(c)).toHaveLength(0)
    expect(selected(c)).toHaveLength(0)
    expect(panel().getAttribute('aria-label')).toBe('Access restricted: Discover')
    expect(panelNameIsValid()).toBe(true)
  })
})

describe('restricted view with zero visible tabs', () => {
  it('renders an empty tablist with no tab stop, and a named panel explaining there is nowhere to go', async () => {
    ACCESS = accessWith(() => 'operate')
    const c = await signIn()
    await click(tabNamed(c, 'Discover'))
    await hideAndRefocus(Object.fromEntries(ALL_TAB_KEYS.map((k) => [k, 'hidden'])))
    expect(tablist(c)).toBeTruthy()
    expect(tabs(c)).toHaveLength(0)
    expect(c.querySelectorAll('[tabindex="0"][role="tab"]')).toHaveLength(0)
    expect(panel().textContent).toMatch(/Access restricted/)
    expect(panel().textContent).toMatch(/nothing in this workspace open to you/)
    expect(panel().hasAttribute('aria-labelledby')).toBe(false)
    expect(panel().getAttribute('aria-label')).toBe('Access restricted: Discover')
    expect(panelNameIsValid()).toBe(true)
  })

  it('names the panel "Access pending" when the server says a role has not been assigned yet', async () => {
    ACCESS = accessWith(() => 'operate')
    const c = await signIn()
    await click(tabNamed(c, 'Discover'))
    ACCESS = { ...accessWith(() => 'hidden'), role: null, pending: true }
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    await flush()
    expect(tabs(c)).toHaveLength(0)
    expect(panel().textContent).toMatch(/Access pending/)
    expect(panel().getAttribute('aria-label')).toBe('Access pending')
    expect(panelNameIsValid()).toBe(true)
  })
})
