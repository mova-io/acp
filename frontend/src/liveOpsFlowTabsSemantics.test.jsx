/**
 * Live Operations "flow views" tablist (AdminLiveTraffic.jsx) — ARIA tabs semantics, DOM-level.
 *
 * Its own file because AdminLiveTraffic needs a specific api.js mock (the activity stream must
 * return an object with close()), unlike the pending-promise mock in subTabSemantics.test.jsx.
 * Same contract: tab ↔ tabpanel linked by ids that exist, aria-selected, roving tabindex,
 * Left/Right/Home/End. No screen-reader claims.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import ErrorBoundary from './ErrorBoundary.jsx'

vi.mock('./api.js', () => ({
  getCapacitySchedule: vi.fn(async () => ({ applied: false })),
  getAdminActivity: vi.fn(async () => ({
    generated_at: '2026-09-06T14:00:00Z',
    runs: [],
    summary: {
      active_runs: 0, active_workflows: 0, recent_runs: 0, running: 0, queued: 0,
      waiting_users: 0, available_slots: 7, worker_slots: 7, utilization_pct: 0,
      worker_tier_alive: true, by_stage: {}, worker_roles: {},
    },
  })),
  getWorkerCapacity: vi.fn(async () => ({ configured: false })),
  getLiveOpsCosts: vi.fn(async () => ({ configured: false, services: [],
    billing: { freshness_label: 'Azure billing feed not configured' } })),
  openAdminActivityStream: vi.fn(() => ({ close: vi.fn() })),
}))

const { default: AdminLiveTraffic } = await import('./AdminLiveTraffic.jsx')

globalThis.IS_REACT_ACT_ENVIRONMENT = true
afterEach(unmountAll)

async function open() {
  const { root, container } = createTestRoot()
  await act(async () => { root.render(createElement(ErrorBoundary, null, createElement(AdminLiveTraffic))) })
  await act(async () => { await Promise.resolve() })
  return container
}
const key = async (el, k) => act(async () => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }))
})
const list = (c) => c.querySelector('[role="tablist"][aria-label="Live Operations flow views"]')
const tabs = (c) => [...list(c).querySelectorAll('[role="tab"]')]
const selected = (c) => tabs(c).filter((t) => t.getAttribute('aria-selected') === 'true')
const stops = (c) => tabs(c).filter((t) => t.tabIndex === 0)

function expectLinked(c) {
  expect(selected(c)).toHaveLength(1)
  expect(stops(c)).toEqual(selected(c))
  for (const t of tabs(c)) {
    expect(t.id).not.toBe('')
    expect(t.hasAttribute('aria-expanded')).toBe(false)
    expect(document.getElementById(t.getAttribute('aria-controls'))?.getAttribute('role')).toBe('tabpanel')
  }
  const panel = document.getElementById(selected(c)[0].getAttribute('aria-controls'))
  expect(document.getElementById(panel.getAttribute('aria-labelledby'))).toBe(selected(c)[0])
  return panel
}

describe('Live Operations flow-view tabs', () => {
  it('renders two tabs, Infrastructure map selected', async () => {
    const c = await open()
    expect(tabs(c).map((t) => t.textContent)).toEqual(['Infrastructure map', 'Running jobs (0)'])
    expect(selected(c).map((t) => t.textContent)).toEqual(['Infrastructure map'])
  })

  it('links both tabs to an existing tabpanel labelled by the selected tab, with a roving tabindex', async () => {
    const c = await open()
    const panel = expectLinked(c)
    expect(panel.querySelector('.react-flow')).not.toBeNull()
  })

  it('moves focus and selection with ArrowRight/ArrowLeft/Home/End', async () => {
    const c = await open()
    const [infra, jobs] = tabs(c)
    infra.focus()
    await key(infra, 'ArrowRight')
    expect(document.activeElement).toBe(tabs(c)[1])
    expect(selected(c)).toEqual([jobs])
    expectLinked(c)
    await key(tabs(c)[1], 'Home')
    expect(document.activeElement).toBe(tabs(c)[0])
    expect(selected(c)).toEqual([tabs(c)[0]])
    await key(tabs(c)[0], 'End')
    expect(document.activeElement).toBe(tabs(c)[1])
    await key(tabs(c)[1], 'ArrowRight')
    expect(document.activeElement).toBe(tabs(c)[0])
    await key(tabs(c)[0], 'ArrowLeft')
    expect(document.activeElement).toBe(tabs(c)[1])
    expectLinked(c)
  })
})
