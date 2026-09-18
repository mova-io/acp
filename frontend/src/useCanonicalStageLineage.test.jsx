// @vitest-environment jsdom
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import { isNewerLineage, useCanonicalStageLineage, STAGE_LINEAGE_REFRESH_EVENT } from './useCanonicalStageLineage.js'

afterEach(() => { unmountAll(); vi.useRealTimers() })

const response = (workflowRevision, stageRevision, stage = 'assess') => ({
  lineage: { workflow_revision: workflowRevision, scan_id: 'scan-1',
    stages: [{ stage, revision: stageRevision, state: 'processing' }] },
  content_digest: { algorithm: 'SHA-256', value: `${workflowRevision}-${stageRevision}` },
})

function Probe({ scanId, load }) {
  const value = useCanonicalStageLineage(scanId, load)
  return createElement('output', null,
    value.lineage ? `${value.lineage.scan_id}:${value.lineage.stages[0].revision}` : 'empty')
}

describe('canonical stage lineage continuity', () => {
  it('rejects an older stage or workflow revision', () => {
    expect(isNewerLineage(response(2, 8), response(2, 7))).toBe(false)
    expect(isNewerLineage(response(2, 8), response(1, 20))).toBe(false)
    expect(isNewerLineage(response(2, 8), response(3, 1))).toBe(true)
  })

  it('loads immediately and refreshes when the window regains focus', async () => {
    const load = vi.fn().mockResolvedValueOnce(response(1, 1)).mockResolvedValueOnce(response(1, 2))
    const { container, root } = createTestRoot()
    await act(async () => root.render(createElement(Probe, { scanId: 'scan-1', load })))
    expect(container.textContent).toBe('scan-1:1')
    await act(async () => window.dispatchEvent(new Event('focus')))
    expect(container.textContent).toBe('scan-1:2')
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('re-reads on an explicit refresh request for its own scan, and ignores one for another scan', async () => {
    const load = vi.fn().mockResolvedValueOnce(response(1, 1)).mockResolvedValueOnce(response(1, 2))
    const { container, root } = createTestRoot()
    await act(async () => root.render(createElement(Probe, { scanId: 'scan-1', load })))
    await act(async () => window.dispatchEvent(new CustomEvent(STAGE_LINEAGE_REFRESH_EVENT, { detail: { scanId: 'other-scan' } })))
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => window.dispatchEvent(new CustomEvent(STAGE_LINEAGE_REFRESH_EVENT, { detail: { scanId: 'scan-1' } })))
    expect(load).toHaveBeenCalledTimes(2)
    expect(container.textContent).toBe('scan-1:2')
  })

  it('clears the previous scan before loading the next', async () => {
    let resolveSecond
    const load = vi.fn((scanId) => scanId === 'scan-1'
      ? Promise.resolve(response(1, 4))
      : new Promise((resolve) => { resolveSecond = resolve }))
    const { container, root } = createTestRoot()
    await act(async () => root.render(createElement(Probe, { scanId: 'scan-1', load })))
    expect(container.textContent).toBe('scan-1:4')
    await act(async () => root.render(createElement(Probe, { scanId: 'scan-2', load })))
    expect(container.textContent).toBe('empty')
    await act(async () => resolveSecond({ ...response(1, 1), lineage: {
      ...response(1, 1).lineage, scan_id: 'scan-2' } }))
    expect(container.textContent).toBe('scan-2:1')
  })

  it('stops refreshing after session expiry', async () => {
    vi.useFakeTimers()
    const load = vi.fn().mockResolvedValue(response(1, 1))
    const { root } = createTestRoot()
    await act(async () => root.render(createElement(Probe, { scanId: 'scan-1', load })))
    window.dispatchEvent(new CustomEvent('acp:session-expired'))
    await act(async () => vi.advanceTimersByTimeAsync(30_000))
    expect(load).toHaveBeenCalledTimes(1)
  })
})
