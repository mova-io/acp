import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import { docProgress, remainingLabel } from './remediationInboxModel.js'

afterEach(unmountAll)
const { default: WorkspaceProgress } = await import('./WorkspaceProgress.jsx')

// Lanes drive effort: autoApplied → review (5s), hasProposal → apply (15s), bare → manual (120s).
const QUEUE = [
  { id: 1, file: 'a.docx', autoApplied: true },              // review · 5s
  { id: 2, file: 'a.docx', hasProposal: true, after: 'x' },  // apply · 15s
  { id: 3, file: 'a.docx' },                                 // manual · 120s
  { id: 4, file: 'b.pdf', autoApplied: true },               // review · 5s
]

describe('docProgress (pure)', () => {
  it('reports a single document’s resolved/total, percent, effort and ETA', () => {
    const p = docProgress(QUEUE, 'a.docx', {})
    expect(p).toMatchObject({ file: 'a.docx', resolved: 0, total: 3, pct: 0, remainingSec: 140, done: false })
    expect(p.remainingLabel).toBe('About 2 min remaining')   // 140s → 2 min
  })

  it('counts resolved decisions and recomputes remaining effort', () => {
    const p = docProgress(QUEUE, 'a.docx', { 1: { state: 'accepted' } })
    expect(p).toMatchObject({ resolved: 1, total: 3, pct: 33, remainingSec: 135 }) // 15 + 120
  })

  it('marks done and drops the ETA when a document is fully resolved', () => {
    const p = docProgress(QUEUE, 'a.docx', { 1: { state: 'accepted' }, 2: { state: 'accepted' }, 3: { state: 'rejected' } })
    expect(p).toMatchObject({ resolved: 3, total: 3, pct: 100, done: true, remainingLabel: '' })
  })

  it('falls back to the whole queue when no file is given', () => {
    expect(docProgress(QUEUE, null, {}).total).toBe(4)
  })

  it('remainingLabel switches from seconds to minutes at a minute', () => {
    expect(remainingLabel(5)).toBe('About 5 sec remaining')
    expect(remainingLabel(0)).toBe('')
    expect(remainingLabel(240)).toBe('About 4 min remaining')
  })
})

describe('WorkspaceProgress (component)', () => {
  let container, root
  beforeEach(() => { ;({ container, root } = createTestRoot()) })
  const render = async (props) => { await act(async () => { root.render(createElement(WorkspaceProgress, props)) }) }

  it('renders nothing for an empty queue', async () => {
    await render({ queue: [], decisions: {}, selected: null })
    expect(container.textContent.trim()).toBe('')
  })

  it('shows the selected document’s progress, percent and a progressbar role', async () => {
    await render({ queue: QUEUE, decisions: {}, selected: { file: 'a.docx' } })
    expect(container.textContent).toContain('a.docx')
    expect(container.textContent).toContain('0 of 3 actions complete')
    expect(container.textContent).toContain('0%')
    expect(container.textContent).toContain('1 applying / awaiting verification')
    const bar = container.querySelector('[role=progressbar]')
    expect(bar).toBeTruthy()
    expect(bar.getAttribute('aria-valuenow')).toBe('0')
  })

  it('keeps approval decisions in Processing until verification finishes', async () => {
    await render({ queue: QUEUE, decisions: { 1: { state: 'accepted' }, 2: { state: 'accepted' }, 3: { state: 'rejected' } }, selected: { file: 'a.docx' } })
    expect(container.textContent).not.toContain('100%')
    expect(container.textContent).toContain('awaiting verification')
    expect(container.textContent).not.toContain('Complete ✓')
  })

  it('falls back to a run-level label before anything is selected', async () => {
    await render({ queue: QUEUE, decisions: {}, selected: null })
    expect(container.textContent).toContain('This remediation run')
    expect(container.textContent).toContain('0 of 4 actions complete')
  })
})

it('shows pending action counts instead of an effort-based countdown when nothing is processing', async () => {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(WorkspaceProgress, {queue:[{id:1,file:'a.docx'},{id:2,file:'a.docx'}]})))
  expect(container.textContent).toContain('2 actions awaiting an outcome')
  expect(container.textContent).not.toMatch(/About .* remaining/)
})
