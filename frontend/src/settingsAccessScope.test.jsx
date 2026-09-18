/**
 * Settings is scoped to access management (Owners + Users) plus the one self-service action every
 * signed-in user needs (My Data — reset only your own scans; see resetMyData.test.jsx), plus
 * Worker Configuration (2026-08-28) — administrator-controlled worker CAPACITY only (the Azure
 * replica floor). "Settings lets an administrator change how workers operate" — the live queue/
 * job view lives in Monitor → Workers & Queue instead (monitorWorkersQueue.test.jsx), not here;
 * mixing observation into a configuration surface was the first draft of this and was steered
 * away from directly.
 *
 * Review Memory (2026-08-30, ADR 0021) joins on the same terms Worker Configuration did, and the
 * distinction is worth stating because the tab bar is pinned precisely so additions are argued
 * rather than assumed. It is administrator-controlled CONFIGURATION — authoring house-style rules
 * and accepting or dismissing rules the derivation job proposes — not observation. The evidence
 * BEHIND a proposal is shown inline because a decision cannot be made without it, but the
 * reviewer-analytics rollups it derives from stay in Scan Analytics; this tab does not become a
 * second place to go and look at numbers.
 *
 * Its backend (`GET/POST /org-memory`, `POST /org-memory/derive`, `PUT /org-memory/{id}/status`)
 * shipped admin-gated and tested with NO client at all — a 2026-08-30 audit found it, along with
 * the panel ADR 0021 specified and never got. Note that ADR 0021 modelled the routes on
 * `/ai/providers`, whose panel is one of the six removed below; the routes being similar did not
 * make the surfaces the same decision, and this one was asked for explicitly.
 *
 * The six OTHER admin panels (Scoring rules, Estate, File types, Remediated storage, Disposition,
 * and the global admin Data reset) remain removed. AI-provider governance was deliberately restored
 * when assessment-time cloud second opinions shipped: without this tab, administrators had no UI
 * control over the off-box document path. Worker
 * Configuration is not one of the six — it was added, not restored, and is covered separately
 * below.
 *
 * It also covers the Users-tab onboarding: two equal paths, Microsoft (SharePoint/OneDrive) and
 * Google (Drive), each ending at the same allowlist.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

afterEach(unmountAll)

const { default: Settings, ResetData, DriveMirror, AIProvidersPanel } = await import('./Settings.jsx')

const settle = async (ms = 340) => {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)) })
  for (let k = 0; k < 3; k++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}
const render = async () => {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(createElement(Settings, { onClose: () => {} })) })
  await settle()
  return container
}
const tabTexts = (c) => [...c.querySelectorAll('button[role="tab"]')].map((b) => b.textContent.trim())
const setValue = (el, v) => {
  const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
  set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('the settings panel includes access, worker and AI governance', () => {
  it('shows the approved settings tabs in their exact order', async () => {
    // `Roles` was added here by workspace RBAC slice 3 (PRD §8: "a dedicated Roles tab beside
    // People"). This assertion is deliberately exact, which is why adding a tab has to be a
    // decision recorded in a diff rather than something that quietly appears — the tab list is
    // the whole navigation of the admin panel.
    // `Scheduling` joined on 2026-09-06, immediately after Worker Configuration, per the
    // Settings -> Scheduling PRD §4. The two are one job: Worker Configuration sets warm
    // capacity now, Scheduling says when ACP should hold more of it. It is READ-ONLY — the
    // writable capacity control stays where it is (queuePanelCapacity.test.jsx).
    // `Rule explanations` joined on 2026-09-18, last: READ-ONLY product metadata (what each rule
    // checks per criterion and format, its thresholds and limits) with no write control at all —
    // pinned in ruleExplanations.test.jsx.
    expect(tabTexts(await render())).toEqual(
      ['Owners', 'Users', 'Roles', 'My Data', 'My Scope', 'Scheduling', 'Release', 'AI Governance', 'Review Memory', 'Rule explanations'])
  })

  it('no longer offers any of the removed ADMIN-ONLY tabs', async () => {
    const texts = tabTexts(await render())
    for (const gone of ['Scoring rules', 'Estate', 'File types', 'Remediated storage', 'Disposition', 'Data']) {
      expect(texts).not.toContain(gone)
    }
  })

  it('opens on the Users tab', async () => {
    const c = await render()
    expect(c.querySelector('button[role="tab"][aria-selected="true"]').textContent.trim()).toBe('Users')
  })
})

describe('the AI Governance tab', () => {
  it('mounts the provider controls and explains assessment-time off-box processing', async () => {
    const c = await render()
    const aiTab = [...c.querySelectorAll('button[role="tab"]')]
      .find((b) => b.textContent.trim() === 'AI Governance')
    expect(aiTab, 'no AI Governance tab').toBeTruthy()
    await act(async () => { aiTab.click() })
    await settle()
    expect(c.textContent).toMatch(/AI providers/)
    expect(c.textContent).toMatch(/LOW-confidence assessment findings/)
    expect(c.textContent).toMatch(/first rendered page only/)
    expect(c.textContent).toMatch(/Assessment second opinions/)
    expect(c.textContent).toMatch(/1\.3\.5 Identify Input Purpose/)
    expect(c.textContent).toMatch(/copied into each new scan/)
  })
})

// WorkerReplicaControl is already fully self-contained (its own polling, its own state) — this
// only proves Settings mounts it on the new tab, not its own internal behavior (see
// workerReplicaControl.test.jsx for that). Deliberately asserts QueuePanel does NOT render here —
// the live queue view belongs in Monitor → Workers & Queue, not in a configuration surface (see
// monitorWorkersQueue.test.jsx for that mount).
describe('retired Worker Configuration tab', () => {
  it('offers only Scheduling for capacity management', async () => {
    const c = await render()
    const names = [...c.querySelectorAll('[role="tab"]')].map((tab) => tab.textContent.trim())
    expect(names).not.toContain('Worker Configuration')
    expect(names).toContain('Scheduling')
  })
})

describe('the Scheduling tab', () => {
  it('separates capacity scheduling from scan scheduling and says Azure is not yet changed', async () => {
    const { container, root } = createTestRoot()
    await act(async () => { root.render(createElement(Settings, { onClose: () => {}, me: { is_admin: true } })) })
    await settle()
    const tab = [...container.querySelectorAll('button[role="tab"]')]
      .find((b) => b.textContent.trim() === 'Scheduling')
    await act(async () => { tab.click() })
    await settle()
    expect(container.textContent).toMatch(/Capacity scheduling/)
    expect(container.textContent).toMatch(/scheduled re-scans remain in Monitor/i)
    expect(container.textContent).toMatch(/do not change live Azure replicas yet/i)
    expect(container.querySelector('button')?.disabled).toBe(false)
  })
})

// ReviewMemory is fully self-contained (its own fetch, its own state) — this only proves Settings
// mounts it on the new tab and threads `me` through; the panel's own behaviour is covered in
// reviewMemory.test.jsx. The second assertion is the one worth having: SIM's /org-memory reports
// `enabled: false`, and the panel must say the rules are inert rather than listing them under a
// status that reads as "in effect". A mount that rendered the rules but dropped that line would
// still pass a naive "does the tab work" check.
describe('the Review Memory tab', () => {
  it('mounts the review-memory panel, and it reports the feature flag rather than status alone', async () => {
    const c = await render()
    const tabs = [...c.querySelectorAll('button[role="tab"]')]
    const memoryTab = tabs.find((b) => b.textContent.trim() === 'Review Memory')
    expect(memoryTab, 'no Review Memory tab').toBeTruthy()
    await act(async () => { memoryTab.click() })
    await settle()
    expect(c.querySelector('.rm-panel'), 'Review Memory tab rendered no panel').toBeTruthy()
    expect(c.textContent).toMatch(/Review memory is switched off/)
    expect(c.querySelector('.rm-state.rm-disabled')).toBeTruthy()
    // Still a configuration surface: the capacity control belongs to the tab next door.
    expect(c.textContent).not.toMatch(/Warm capacity/)
  })
})

describe('the retained admin panels are not deleted', () => {
  it('still exports the three local admin panels so their features and guards survive', () => {
    expect(typeof ResetData).toBe('function')
    expect(typeof DriveMirror).toBe('function')
    expect(typeof AIProvidersPanel).toBe('function')
  })
})

describe('the Users tab onboards Microsoft and Google identities in one flow', () => {
  it('offers both providers in a unified Add people dialog', async () => {
    const c = await render()
    const add = [...c.querySelectorAll('button')].find((b) => b.textContent.includes('Add people'))
    expect(add).toBeTruthy()
    await act(async () => add.click())
    // The dialog is PORTALLED to document.body — see peopleDialogPortal.test.jsx — so its copy
    // is no longer inside the Settings subtree this test mounted. `document.body` is the honest
    // scope for the dialog's own content; the container remains the scope for the panel.
    await act(async () => {})
    // BY ITS OWN LABEL, not by [role="dialog"][aria-modal="true"] — the Settings overlay carries
    // both of those attributes itself, so the generic selector matches the panel rather than the
    // dialog inside it and every assertion below silently tests the wrong element.
    const dialog = document.querySelector('[aria-labelledby="add-person-title"]')
    expect(dialog).toBeTruthy()
    expect(dialog.textContent).toContain('Microsoft')
    expect(dialog.textContent).toContain('Google')
    expect(dialog.textContent).toContain('Google test user')
  })

  it('adding a Google user persists it to the people roster', async () => {
    const c = await render()
    await act(async () => [...c.querySelectorAll('button')].find((b) => b.textContent.includes('Add people')).click())
    const input = document.querySelector('input[type="email"]')
    expect(input).toBeTruthy()
    setValue(input, 'newtester@gmail.com')
    const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Add person')
    await act(async () => { btn.click() })
    await settle()
    expect(c.textContent).toContain('newtester@gmail.com is ready to join ACP')
    expect(c.textContent).toContain('newtester@gmail.com')
  })
})
