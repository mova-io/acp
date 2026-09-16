/**
 * The universal scan gate, at the App level.
 *
 * `doScan` is the single dispatch funnel, but every entry point now calls `requestScan` (wired as
 * their `onScan` prop), which OPENS the app-level ScanReviewModal instead of scanning. The only
 * path that actually starts a scan is the modal's "Start scan" confirm. This mounts the REAL App,
 * signs in via the demo persona, and drives Discover's "Re-scan all sources" to prove:
 *   - clicking a scan entry opens the gate and does NOT start a scan;
 *   - confirming in the gate is what starts the scan (the scan API is called then, not before);
 *   - cancelling the gate starts nothing.
 *
 * Re-pointed 2026-09-02: scan entry moved OFF Discover (PRD "ACP Discover and Overview
 * Simplification"). Discover's "Re-scan all sources" and "Choose folder to scan…" buttons were
 * removed; Sources ("New scan", `onScan('all')`) is the entry point these flows now start from.
 * The gate itself is unchanged — it is app-level and shared by every entry point.
 *
 * Durable (queuedScan) is the default (2026-08-21) — see App.jsx's own comment on that useState —
 * so a confirm from this gate takes the QUEUED path. startScanQueued is stubbed to throw, so
 * `doScan` bails immediately after calling it — we assert only that it WAS or WAS NOT called,
 * never running the real poll loop.
 *
 * DOM-level, not browser-level: this repo's preview server runs vite rooted at the SHARED checkout
 * whatever worktree you are in (CLAUDE.md), so a browser check would exercise code without this.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import { gotoStep } from './wizardNav.testkit.js'

// vite replaces these `define` literals at build time; vitest does not, and App.jsx reads them
// unguarded in its header, so define them as globals before it renders.
globalThis.__BUILD_TIME__ = '2026-08-01T00:00:00.000Z'
globalThis.__BUILD_VERSION__ = '2026.8.1'

const mode = vi.hoisted(() => ({ sim: true }))
vi.mock('./sim.js', async (actual) => ({ ...(await actual()), get SIM() { return mode.sim } }))
const checkDiscoveryPreflight = vi.fn()

const startScanQueued = vi.fn(async () => { throw new Error('test stub — do not run the poll loop') })
const startScan = vi.fn(async () => { throw new Error('test stub — do not run the poll loop') })
const getScanLocations = vi.fn(async () => ({
  locations: { drive: [{ id: 'old-filter', name: 'Previous filtered folder' }] },
}))

// Spread the real api and override only what App's startup + a scan touch, so a new export never
// silently breaks this mount. getConfig → demo mode (persona picker); the scan API throws so the
// dispatch is observable without the durable poll loop.
vi.mock('./api.js', async (importActual) => ({
  ...(await importActual()),
  getConfig: vi.fn(async () => ({ auth: 'demo' })),
  getRubric: vi.fn(async () => ({ target: 'WCAG 2.1 AA', hash: 'abcdef0123' })),
  getSources: vi.fn(async () => []),
  listScans: vi.fn(async () => []),
  getActiveScan: vi.fn(async () => null),
  getSettings: vi.fn(async () => ({ scan_scope: '' })),
  updateSettings: vi.fn(async () => ({ scan_scope: '' })),
  getScan: vi.fn(async () => ({ run: { id: 's1', status: 'done' }, files: [] })),
  getDecisions: vi.fn(async () => ({})),
  getScanLocations,
  checkDiscoveryPreflight,
  startScanQueued,
  openDiscoverStream: vi.fn(() => ({ close: vi.fn() })),
  // The Durable toggle is no longer on the modal, so a default confirm takes the NON-queued path
  // and THIS is the call that has to be observable. Throws for the same reason startScanQueued
  // does: it stops doScan before the getJob poll loop.
  startScan,
}))

const { default: App } = await import('./App.jsx')

afterEach(unmountAll)
beforeEach(() => { mode.sim = true; checkDiscoveryPreflight.mockReset(); startScanQueued.mockClear(); startScan.mockClear(); getScanLocations.mockClear() })

const flush = async () => { for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() }) }
const click = async (el) => { await act(async () => { el.click() }); await flush() }
const dialog = (c) => c.querySelector('[role="dialog"]')
const byText = (c, sel, re) => [...c.querySelectorAll(sel)].find((e) => re.test(e.textContent))
const durableSwitch = (c) => [...dialog(c).querySelectorAll('[role="switch"]')]
  .find((s) => (s.getAttribute('aria-label') || '').includes('Durable scan'))

// `signIn` picks the demo persona, because RBAC decides which tabs exist (sim.js `allow`): the
// Compliance Officer the SSO button signs in as has `discover` but NOT `integrations`, and the
// Platform Admin is the reverse. A test that needs the Sources tab has to be the admin.
async function mountSignedInOn(tabLabel, { persona = null } = {}) {
  const { root, container } = createTestRoot()
  await act(async () => { root.render(createElement(App)) })
  await flush()
  const signIn = persona
    ? byText(container, 'button.personacard', persona)
    : byText(container, 'button', /Sign in with SSO/)
  expect(signIn, `no sign-in control for ${persona || 'SSO'}`).toBeTruthy()
  await click(signIn)
  const tab = byText(container, '[role="tab"]', tabLabel)
  expect(tab, `no ${tabLabel} tab after sign-in`).toBeTruthy()
  await click(tab)
  return container
}

const mountSignedInOnDiscover = () => mountSignedInOn(/Discover/)
// Sources owns the scan entry point since 2026-09-02: its page-level "New scan" is onScan('all'),
// which is what Discover's "Re-scan all sources" used to be.
const mountSignedInOnSources = () => mountSignedInOn(/Sources/, { persona: /Platform Admin/ })
const newScan = (c) => byText(c, 'button', /^New scan$/)

describe('the universal scan gate (App)', () => {
  it('implements one-stop workflow tabs with a resolvable active panel', async () => {
    const c = await mountSignedInOnDiscover()
    const tabs = [...c.querySelectorAll('nav[aria-label="Compliance workflow"] [role="tab"]')]
    const active = tabs.find((tab) => tab.getAttribute('aria-selected') === 'true')
    expect(active.textContent).toMatch(/Discover/)
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([active])
    expect(tabs.every((tab) => tab.getAttribute('aria-controls') === 'workflow-panel')).toBe(true)
    const panel = c.querySelector('#workflow-panel[role="tabpanel"]')
    expect(panel).toBeTruthy()
    expect(panel.getAttribute('aria-labelledby')).toBe(active.id)

    await act(async () => active.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'ArrowRight', bubbles: true,
    })))
    await flush()
    const next = tabs.find((tab) => tab.getAttribute('aria-selected') === 'true')
    expect(next.textContent).toMatch(/Assess/)
    expect(document.activeElement).toBe(next)
  })

  it('opens the review modal from a scan entry point and does NOT scan yet', async () => {
    const c = await mountSignedInOnSources()
    const rescan = newScan(c)
    expect(rescan, 'no "New scan" button on Sources').toBeTruthy()
    expect(dialog(c)).toBeNull()
    await click(rescan)
    // The gate is open…
    const d = dialog(c)
    expect(d).toBeTruthy()
    expect(d.getAttribute('aria-label')).toBe('New discovery')
    // The "Formats & WCAG criteria" heading is gone (it named step 2's subject while the
    // user was on step 1). This assertion was a proxy for "the review modal opened" — so
    // assert the estimate line, which is the modal's own content and is step-independent.
    expect(d.textContent).toMatch(/Document count is determined when the scan starts|documents in/)
    // …and nothing has been dispatched.
    expect(startScanQueued).not.toHaveBeenCalled()
  })

  it('does not put the engine switches in front of the user', async () => {
    // The four behaviour toggles were removed from this surface: they are platform behaviour, not
    // per-run decisions. Their DEFAULTS are still pinned — against App.jsx's source, in
    // scanBehaviourDefaults.test.js — so this assertion moved rather than disappeared, and what
    // it guards here is that they do not come back to the gate.
    const c = await mountSignedInOnSources()
    await click(newScan(c))
    expect(durableSwitch(c), 'the Durable toggle is back on the scan gate').toBeFalsy()
    expect([...dialog(c).querySelectorAll('[role="switch"]')].length).toBe(0)
  })

  it('starts the scan only when the gate is confirmed', async () => {
    const c = await mountSignedInOnSources()
    await click(newScan(c))
    expect(startScanQueued).not.toHaveBeenCalled()
    // Durable is ON by default and no longer togglable here, so a confirm takes the queued path
    // (App.jsx's `if (queuedScan) { startScanQueued(...) }`). Three steps, so walk to the run
    // control the way a user does.
    //
    // Both eras of this comment were sitting here at once — "Three steps now, so walk to Start"
    // immediately above "One screen now (PRD DISC-01) — no Continue to walk". Neither was deleted
    // when the other was added, so the file asserted one thing and explained two. The guarantee
    // itself never changed: nothing scans until the gate is confirmed.
    await gotoStep(dialog(c), act, 3)
    const start = dialog(c).querySelector('button[data-wizard-forward]')
    expect(start).toBeTruthy()
    expect(start.textContent).toMatch(/Run discovery/)
    await click(start)
    // Confirm dispatched the scan (source 'all' for the page-level "New scan") and closed the gate.
    expect(startScanQueued).toHaveBeenCalled()
    expect(startScanQueued.mock.calls[0][0]).toBe('all')
    expect(startScanQueued.mock.calls[0][6]).toEqual([])
    expect(startScanQueued.mock.calls[0][7]).toEqual([])
    expect(startScan).not.toHaveBeenCalled()
    expect(dialog(c)).toBeNull()
  })


  it('moves from Sources to Discover only after the backend accepts the start', async () => {
    startScanQueued.mockResolvedValueOnce({ scan_id: 'accepted-scan', job_id: 'accepted-job', workers: 1, worker_tier_alive: true })
    const c = await mountSignedInOn(/Sources/, { persona: /Compliance Officer/ })
    await click(newScan(c)); await gotoStep(dialog(c), act, 3)
    await click(dialog(c).querySelector('button[data-wizard-forward]'))
    const active = [...c.querySelectorAll('[role="tab"]')].find((tab) => tab.getAttribute('aria-selected') === 'true')
    expect(active.textContent).toMatch(/Discover/)
  })

  it('keeps Sources and its error visible when the backend rejects the start', async () => {
    startScanQueued.mockRejectedValueOnce(Object.assign(new Error('source unavailable'), { status: 400 }))
    const c = await mountSignedInOn(/Sources/, { persona: /Compliance Officer/ })
    await click(newScan(c)); await gotoStep(dialog(c), act, 3)
    await click(dialog(c).querySelector('button[data-wizard-forward]'))
    const active = [...c.querySelectorAll('[role="tab"]')].find((tab) => tab.getAttribute('aria-selected') === 'true')
    expect(active.textContent).toMatch(/Sources/)
    expect(c.textContent).toMatch(/source unavailable/)
  })

  it('cancelling the gate starts nothing', async () => {
    const c = await mountSignedInOnSources()
    await click(newScan(c))
    const cancel = [...dialog(c).querySelectorAll('button')].find((b) => b.textContent.trim() === 'Cancel')
    await click(cancel)
    expect(dialog(c)).toBeNull()
    expect(startScanQueued).not.toHaveBeenCalled()
    expect(startScan).not.toHaveBeenCalled()
  })
})

describe('Discover carries no scan entry point of its own', () => {
  // Found live 2026-08-28: "Choose folder to scan…" used to open a standalone FolderPicker modal
  // and then re-open the gate at its default "Entire connected source" step — re-asking the folder
  // question a moment after it was answered. It was unified onto the gate (`folderFirst`), and on
  // 2026-09-02 the PRD "ACP Discover and Overview Simplification" removed BOTH of Discover's scan
  // buttons: a scan is started from Sources, and Discover reports what a scan found.
  //
  // The unified folder path is not deleted, only unreferenced — App.requestScan still takes
  // `folderFirst` and still threads it to ScanReviewModal's `startInFolderMode` — so restoring an
  // entry point stays one commit (CLAUDE.md). Both halves are pinned below.
  const SAVED_DRIVE_FOLDERS = { locations: { drive: [{ id: 'old-filter', name: 'Previous filtered folder' }] } }
  afterEach(() => {
    sessionStorage.removeItem('gd_token')
    // mockClear (beforeEach) only forgets the CALLS; a per-test mockResolvedValue would otherwise
    // leak into the next test as a permanently empty saved-locations response.
    getScanLocations.mockImplementation(async () => SAVED_DRIVE_FOLDERS)
  })

  it('offers neither "Re-scan all sources" nor "Choose folder to scan…" on Discover', async () => {
    sessionStorage.setItem('gd_token', 'test-token')
    const c = await mountSignedInOnDiscover()
    expect(byText(c, 'button', /Re-scan all sources/)).toBeFalsy()
    expect(byText(c, 'button', /Choose folder to scan/)).toBeFalsy()
    // Discover DID render — otherwise the two absences above prove nothing about the buttons.
    expect(c.querySelector('#workflow-panel').textContent.length).toBeGreaterThan(0)
    expect(dialog(c)).toBeNull()
    expect(startScanQueued).not.toHaveBeenCalled()
    expect(startScan).not.toHaveBeenCalled()
  })

  it('"New scan" on Sources opens the gate on "Entire connected source" when nothing is saved', async () => {
    // Needs a connected source with a folder hierarchy — without one, the wizard shows its flat
    // "no folder hierarchy to narrow" case instead of the two-option radiogroup at all.
    sessionStorage.setItem('gd_token', 'test-token')
    getScanLocations.mockResolvedValue({ locations: {} })
    const c = await mountSignedInOnSources()
    await click(newScan(c))
    const d = dialog(c)
    expect(d, 'no gate opened from Sources').toBeTruthy()
    expect(d.getAttribute('aria-label')).toBe('New discovery')
    const radios = [...d.querySelectorAll('[role="radio"]')]
    const entire = radios.find((r) => /Entire connected source/.test(r.textContent))
    expect(entire, 'no "Entire connected source" option in the gate').toBeTruthy()
    expect(entire.getAttribute('aria-checked')).toBe('true')
    expect(radios.find((r) => /Specific folders/.test(r.textContent)).getAttribute('aria-checked')).toBe('false')
  })

  it('restores a saved folder narrowing rather than silently widening the scan', async () => {
    // The default mock has a saved drive folder. An empty folder list and "everything" look
    // identical on screen, and the reassuring reading of a blank list is the wrong one
    // (ScanScopeWizard's own comment) — so a source that was last scanned narrowly must reopen
    // narrow, not quietly become a whole-Drive crawl because nobody re-picked the folder.
    sessionStorage.setItem('gd_token', 'test-token')
    const c = await mountSignedInOnSources()
    await click(newScan(c))
    const radios = [...dialog(c).querySelectorAll('[role="radio"]')]
    expect(radios.find((r) => /Specific folders/.test(r.textContent)).getAttribute('aria-checked')).toBe('true')
    expect(radios.find((r) => /Entire connected source/.test(r.textContent)).getAttribute('aria-checked')).toBe('false')
  })

  it('keeps the folder-first path wired, so an entry point can be restored in one commit', () => {
    const app = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'App.jsx'), 'utf8')
    expect(app).toMatch(/const requestScan = \(source, folder = null,\s+\{ folderFirst = false/)
    expect(app).toMatch(/startInFolderMode=\{pendingScan\.folderFirst\}/)
    // …and nothing calls it, which is why the DOM test above can assert the button is gone.
    const callers = readdirSync(dirname(fileURLToPath(import.meta.url)))
      .filter((f) => f.endsWith('.jsx') && !f.includes('.test.'))
      .filter((f) => /folderFirst: true/.test(readFileSync(join(dirname(fileURLToPath(import.meta.url)), f), 'utf8')))
    expect(callers).toEqual([])
  })
})


describe('scan submission failures stay visible', () => {
  async function submit(c) {
    await click(newScan(c))
    await gotoStep(dialog(c), act, 3)
    mode.sim = false
    await click(dialog(c).querySelector('button[data-wizard-forward]'))
  }

  it('shows a blocked preflight reason without submitting a scan', async () => {
    const c = await mountSignedInOnSources()
    checkDiscoveryPreflight.mockResolvedValue({
      verdict: 'blocked', blocked_reasons: ['Selected folder is unreachable'],
    })
    await submit(c)
    expect(checkDiscoveryPreflight).toHaveBeenCalled()
    expect(startScanQueued).not.toHaveBeenCalled()
    expect(c.querySelector('.err[role="alert"]')?.textContent).toContain('Selected folder is unreachable')
    expect(c.textContent).not.toContain('This scan completed and found no files')
  })

  it('keeps a lost submission response uncertain rather than reporting failure', async () => {
    const c = await mountSignedInOnSources()
    checkDiscoveryPreflight.mockResolvedValue({ verdict: 'ready' })
    startScanQueued.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await submit(c)
    expect(startScanQueued).toHaveBeenCalled()
    expect(c.textContent).toContain('The server did not confirm your scan')
    expect(c.querySelector('.err[role="alert"]')).toBeNull()
  })
})
