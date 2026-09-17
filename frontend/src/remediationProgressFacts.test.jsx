import { act } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationRunCard from './RemediationRunCard.jsx'
import { remediationProgressFacts } from './remediationProgressFacts.js'

const now = Date.parse('2026-09-16T12:10:00Z')
const snapshot = { run_id: 'run', batch_id: 'batch', state: 'running', terminal: false, total_documents: 20,
  documents: { completed: 8, failed: 1, processing: 3, waiting: 5, review: 2, skipped: 1 },
  started_at: '2026-09-16T12:00:00Z', latest_progress_at: '2026-09-16T12:05:00Z',
  progress: { material_at: '2026-09-16T12:05:00Z', heartbeat_at: '2026-09-16T12:09:59Z' },
  generated_at: '2026-09-16T12:10:00Z', integrity: { ok: true, affected: [] } }
afterEach(() => { unmountAll(); vi.useRealTimers() })
async function mount(value = snapshot) {
  vi.useFakeTimers(); vi.setSystemTime(now)
  const test = createTestRoot()
  await act(async () => test.root.render(<RemediationRunCard snapshot={value} connected receivedAt={now} throughput={{calibrating:false,etaText:'3 minutes',ratePerMin:10}} />))
  return test
}
it('shows separate file dispositions and measured time in the mounted run card instead of a countdown', async () => {
  const { container } = await mount()
  const card = container.querySelector('[aria-label="Recorded file progress"]')
  expect(card.textContent).toContain('Files finished processing9')
  expect(card.textContent).toContain('Files processing3')
  expect(card.textContent).toContain('Files waiting5')
  expect(card.textContent).toContain('Files routed for review or skipped3')
  expect(card.textContent).toContain('Elapsed: 10m 0s')
  expect(card.textContent).toContain('Last meaningful progress: 5m 0s ago')
  expect(card.textContent).toContain('do not mean fixes were verified or copies published')
  expect(container.textContent).not.toContain('Estimated 3 minutes')
  await act(async () => vi.advanceTimersByTime(30000))
  expect(card.textContent).toContain('Elapsed: 10m 30s')
  expect(card.textContent).toContain('Last meaningful progress: 5m 30s ago')
})
it('does not refresh meaningful progress when only a heartbeat or poll arrives', async () => {
  const { container, root } = await mount()
  await act(async () => {
    vi.advanceTimersByTime(60000)
    root.render(<RemediationRunCard snapshot={{...snapshot, generated_at:'2026-09-16T12:11:00Z',
      progress:{...snapshot.progress,heartbeat_at:'2026-09-16T12:11:00Z'}}} receivedAt={now+60000} connected />)
  })
  expect(container.textContent).toContain('Last meaningful progress: 6m 0s ago')
})
it('withholds unknown, inconsistent, negative and invalidated file partitions', () => {
  for (const value of [ {...snapshot,documents:{completed:8}}, {...snapshot,total_documents:21},
    {...snapshot,documents:{...snapshot.documents,failed:-1}},
    {...snapshot,integrity:{ok:false,affected:['documents']}} ]) {
    expect(remediationProgressFacts(value, now)).toMatchObject({finished:null,processing:null,waiting:null,routed:null})
  }
  expect(remediationProgressFacts({...snapshot,active_attempts:Array(50).fill({})}, now).processing).toBe(3)
})
it('does not convert missing timestamps or heartbeat-only updates into fresh progress', async () => {
  const { container } = await mount({...snapshot,started_at:null,latest_progress_at:null,progress:{heartbeat_at:snapshot.generated_at}})
  expect(container.textContent).toContain('Elapsed: Unavailable')
  expect(container.textContent).toContain('Last meaningful progress: Unavailable')
  expect(remediationProgressFacts({...snapshot,started_at:'2027-01-01',progress:{material_at:'invalid'}},now).elapsed).toBeNull()
})
it('shows stable recorded times for terminal runs rather than an ever-growing elapsed timer', async () => {
  const { container } = await mount({...snapshot,terminal:true,state:'completed'})
  const card = container.querySelector('[aria-label="Recorded file progress"]')
  const before = card.textContent
  expect(before).not.toContain('Elapsed:')
  expect(card.querySelector('time').dateTime).toBe(snapshot.started_at)
  await act(async () => vi.advanceTimersByTime(120000))
  expect(card.textContent).toBe(before)
})
it('keeps the facts mounted in the live App remediation card', () => {
  expect(readFileSync('src/App.jsx','utf8')).toContain('<RemediationRunCard snapshot={remRun.snapshot}')
  expect(readFileSync('src/RemediationRunCard.jsx','utf8')).toContain('<RemediationProgressFacts snapshot={snapshot}')
})
