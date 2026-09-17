import { createElement, act, useEffect, useState } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createTestRoot } from './testRoots.js'
import CanonicalStageCard from './CanonicalStageCard.jsx'
import WorkflowStageStack from './WorkflowStageStack.jsx'
import { canonicalStageCardModel, canonicalWorkflowStages, currentCanonicalStage, priorCanonicalStages,
  stageNeedsAttention } from './canonicalStageCard.js'

const SNAPSHOT = {
  workflow_id: 'workflow-1', workflow_revision: 3, stage: 'release',
  execution_id: 'execution-1', revision: 12, state: 'processing',
  last_durable_update_at: '2026-09-07T01:02:03+00:00',
  counts: { work_items: { unit: 'work items', total: 10, queued: 2, processing: 1,
    completed: 5, failed: 1, cancelled: 0, skipped: 1 } },
  reconciliation: { unit: 'work items', total: 10, accounted: 10, unaccounted: 0, exact: true },
  integrity: { ok: true, affected: [], violations: [] },
  control: { cancel_requested: false },
  sealed_output: null,
}

const render = (snapshot) => renderToStaticMarkup(createElement(CanonicalStageCard, { snapshot }))
const here = dirname(fileURLToPath(import.meta.url))

describe('canonical stage card', () => {
  it('renders the server-authored equation with an explicit unit and revisions', () => {
    expect(canonicalStageCardModel(SNAPSHOT).workItems).toEqual([
      ['Completed', 5], ['Failed', 1], ['Skipped', 1], ['Processing', 1],
      ['Waiting', 2], ['Stopped manually', 0],
    ])
    const html = render(SNAPSHOT)
    expect(html).toContain('Integrity check: 10 of 10 work items accounted for')
    expect(html).toContain('Completed</dt><dd')
    expect(html).toContain('>5</dd>')
    expect(html).toContain('Failed</dt><dd')
    expect(html).toContain('Skipped</dt><dd')
    expect(html).toContain('Processing</dt><dd')
    expect(html).toContain('Waiting</dt><dd')
    expect(html).toContain('Workflow revision 3 · snapshot revision 12')
    expect(html).toContain('execution-1')
    expect(html).toContain('Not yet sealed')
    expect(html).toContain('<details>')
    expect(html).not.toContain('<details open=""')
    expect(html).toContain('View accounting')
    expect(html).toContain('canonical-stage-card__progress')
    expect(html).toContain('width:100%')
  })

  it('announces a live reconciliation delta without carrying it into another execution', async () => {
    const { container, root } = createTestRoot()
    await act(async () => { root.render(createElement(CanonicalStageCard, { snapshot: {
      ...SNAPSHOT, state: 'processing_complete', domain_reconciliation: {
        unit: 'inventory documents', total: 10, accounted: 4, exact: true, buckets: { Active: 4 },
      },
    } })) })
    await act(async () => { root.render(createElement(CanonicalStageCard, { snapshot: {
      ...SNAPSHOT, revision: 13, state: 'processing_complete', domain_reconciliation: {
        unit: 'inventory documents', total: 10, accounted: 7, exact: true, buckets: { Active: 7 },
      },
    } })) })
    expect(container.querySelector('.canonical-stage-card__delta').textContent).toBe('+3')
    expect(container.querySelector('.canonical-stage-card__delta').getAttribute('aria-label')).toBe('3 newly reconciled')

    await act(async () => { root.render(createElement(CanonicalStageCard, { snapshot: {
      ...SNAPSHOT, execution_id: 'execution-2', revision: 1, domain_reconciliation: {
        unit: 'inventory documents', total: 5, accounted: 1, exact: true, buckets: { Active: 1 },
      },
    } })) })
    expect(container.querySelector('.canonical-stage-card__delta')).toBeNull()
    await act(async () => { root.unmount() })
  })

  it('keeps embedded cards expanded because their parent disclosure owns the collapsed state', () => {
    const html = renderToStaticMarkup(createElement(CanonicalStageCard, {
      snapshot: SNAPSHOT, embedded: true, receivedAt: Date.now(),
    }))
    expect(html).not.toContain('canonical-stage-card__summary')
    expect(html).toContain('canonical-stage-card__embedded')
    expect(html).toContain('Release · Processing')
    expect(html).toContain('Workflow revision 3 · snapshot revision 12')
    expect(html).toContain('live-heartbeat-bars')
  })

  it.each(['discover', 'assess', 'remediate', 'release'])('keeps %s live history in collapsed and expanded views', (stage) => {
    const html = renderToStaticMarkup(createElement(CanonicalStageCard, {
      snapshot: { ...SNAPSHOT, stage }, receivedAt: Date.now(),
    }))
    expect(html.match(/live-heartbeat-bars/g)).toHaveLength(2)
    expect(html.match(new RegExp(`data-stage="${stage}"`, 'g'))).toHaveLength(2)
    expect(html).toContain('Live · refreshed now')
  })

  it('removes heartbeat history from terminal cards and leaves canonical totals authoritative', () => {
    const html = renderToStaticMarkup(createElement(CanonicalStageCard, {
      snapshot: { ...SNAPSHOT, state: 'succeeded' }, receivedAt: Date.now(),
    }))
    expect(html).not.toContain('live-heartbeat-bars')
    expect(html).toContain('Integrity check: 10 of 10 work items accounted for')
  })

  it.each(['succeeded', 'processing_complete'])('uses the rich completed cards without frozen heartbeat bars for %s stages', (state) => {
    const lineage = { workflow_id: 'provider-neutral', workflow_revision: 3, stages: [
      { ...SNAPSHOT, stage: 'discover', state, source: 'drive', domain_reconciliation: {
        unit: 'inventory documents', total: 147, accounted: 147, exact: true,
        buckets: { Active: 147 },
      } },
      { ...SNAPSHOT, stage: 'assess', execution_id: 'assess-complete', state, source: 'sharepoint',
        domain_reconciliation: { unit: 'eligible documents', total: 147, accounted: 147,
          exact: true, buckets: { assessed: 147 } } },
    ] }
    const html = renderToStaticMarkup(createElement(WorkflowStageStack, { lineage, receivedAt: Date.now() }))
    expect(html).toContain('Discovery complete')
    expect(html).toContain('Assessment complete')
    expect(html).not.toContain('workflow-sse-card')
    expect(html).not.toContain('live-heartbeat-bars')
  })

  it('rehydrates the bullet-based live Discovery card from canonical SSE data', () => {
    const html = renderToStaticMarkup(createElement(WorkflowStageStack, { receivedAt: Date.now(),
      lineage: { workflow_id: 'drive-live', workflow_revision: 3, stages: [{
        ...SNAPSHOT, workflow_id: 'drive-live', stage: 'discover', source: 'drive', state: 'processing',
        domain_reconciliation: { unit: 'inventory documents', total: 986, accounted: 147,
          exact: true, buckets: { active: 147, folders_visited: 12 } },
      }] },
    }))
    expect(html).toContain('Discovering documents')
    expect(html).toContain('Discovery steps')
    expect(html).toContain('Documents found')
    expect(html).toContain('live-heartbeat-bars')
    expect(html).toContain('livecounter')
    expect(html).not.toContain('workflow-sse-card')
  })

  it('keeps retained history mounted while an earlier-stage card collapses and expands', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-07T01:02:03Z'))
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: 'workflow-collapse', workflow_revision: 3,
      stages: [{ ...SNAPSHOT, stage: 'discover', state: 'succeeded' },
        { ...SNAPSHOT, stage: 'assess', execution_id: 'assess-1', state: 'processing' }] }
    await act(async () => { root.render(createElement(WorkflowStageStack, {
      lineage, activeStage: 'assess', receivedAt: Date.now(),
    })) })
    expect(container.querySelectorAll('.live-heartbeat-bars')).toHaveLength(1)
    const summary = container.querySelector('[data-stage="discover"] .workflow-stage-stack__summary')
    await act(async () => { summary.click() })
    expect(container.querySelectorAll('.live-heartbeat-bars')).toHaveLength(1)
    await act(async () => { summary.click() })
    expect(container.querySelectorAll('.live-heartbeat-bars')).toHaveLength(1)
    expect(container.querySelector('.workflow-sse-card')).not.toBeNull()
    expect(container.textContent).not.toContain('Final · refreshed now')
    await act(async () => { vi.advanceTimersByTime(10_000) })
    expect(container.textContent).not.toContain('Final · refreshed now')
    await act(async () => { root.unmount() })
    vi.useRealTimers()
  })

  it('renders stage state and current ownership as quiet metadata', () => {
    const html = renderToStaticMarkup(createElement(WorkflowStageStack, {
      lineage: { workflow_id: 'workflow-status', stages: [SNAPSHOT] },
    }))
    expect(html).toContain('workflow-stage-stack__state')
    expect(html).toContain('workflow-stage-stack__ownership')
    expect(html).toContain('Current')
  })

  it('omits future stages until the workflow creates them', () => {
    const html = renderToStaticMarkup(createElement(WorkflowStageStack, {
      lineage: { workflow_id: 'workflow-locked', stages: [{ ...SNAPSHOT, stage: 'discover' }] },
    }))
    expect(html).not.toContain('Locked')
    expect(html).not.toContain('data-stage="assess"')
    expect(html).not.toContain('data-stage="remediate"')
    expect(html).not.toContain('data-stage="release"')
  })

  it('never turns unknown totals into zero', () => {
    const html = render({ ...SNAPSHOT, counts: { work_items: { unit: 'work items', total: null } },
      reconciliation: { unit: 'work items', total: null, accounted: null, unaccounted: null } })
    expect(html).toContain('Integrity check: Not reported of Not reported work items')
    expect(html).not.toContain('0 of 0')
    expect(html).toContain('Completed</dt><dd')
    expect(html).toContain('>Not reported</dd>')
  })

  it('withholds the reconciled claim when integrity fails', () => {
    const html = render({ ...SNAPSHOT, integrity: { ok: false, affected: ['work_item_partition'] },
      reconciliation: { ...SNAPSHOT.reconciliation, exact: false, unaccounted: -1 } })
    expect(html).toContain('Accounting temporarily inconsistent.')
    expect(html).not.toContain('10 of 10 work items accounted for')
    expect(html).not.toContain('aria-label="Release work-item counts"')
  })

  it('distinguishes a requested stop, a completed stop, and failure', () => {
    expect(canonicalStageCardModel({ ...SNAPSHOT,
      control: { cancel_requested: true } }).stateLabel).toBe('Stopping safely')
    expect(canonicalStageCardModel({ ...SNAPSHOT, state: 'cancelled',
      control: { cancel_requested: true } }).stateLabel).toBe('Stopped manually')
    expect(canonicalStageCardModel({ ...SNAPSHOT, state: 'failed' }).stateLabel).toBe('Failed')
  })

  it('names finished processing without claiming verified fixes or publication', () => {
    expect(canonicalStageCardModel({ ...SNAPSHOT, stage: 'remediate', state: 'processing_complete' }).stateLabel).toBe('Processing finished')
    expect(canonicalStageCardModel({ ...SNAPSHOT, stage: 'remediate', state: 'succeeded' }).stateLabel).toBe('Processing finished')
  })

  it.each(['processing_complete', 'succeeded'])('does not claim publication from a terminal release state %s', (state) => {
    const html = render({ ...SNAPSHOT, stage: 'release', state })
    expect(html).toContain('Publication processing finished')
    expect(html).not.toContain('>Complete<')
    expect(html).not.toContain('>Delivery complete<')
  })

  it('keeps the canonical partition visible while leased work drains after a stop request', () => {
    const html = render({ ...SNAPSHOT, control: { cancel_requested: true } })
    expect(html).toContain('Stopping safely')
    expect(html).toContain('Processing</dt><dd')
    expect(html).toContain('>1</dd>')
    expect(html).toContain('Waiting</dt><dd')
    expect(html).toContain('>2</dd>')
  })

  it('does not equate completed work items with resolved findings', () => {
    const html = render({ ...SNAPSHOT, state: 'succeeded',
      sealed_output: { manifest_id: 'manifest-1' } })
    expect(html).toContain('canonical-stage-card__state is-complete">Publication processing finished')
    expect(html).toContain('manifest-1')
    expect(html).toContain('does not mean every accessibility finding was resolved')
    expect(html).not.toContain('all findings resolved')
  })

  it.each([
    ['discover', {
      unit: 'inventory documents', scope: 'discovered inventory',
      equation: 'inventory = sum(lifecycle status buckets)', total: 24, partitioned: 24,
      unaccounted: 0, exact: true, buckets: { Active: 20, 'Archive Candidate': 4 },
    }, ['24 of 24 inventory documents reconciled', 'Active</dt><dd', '>20</dd>', 'Archive Candidate</dt><dd']],
    ['assess', {
      unit: 'eligible documents', scope: 'immutable Assess input',
      equation: 'eligible = waiting + processing + assessed + failed + cancelled + skipped',
      total: 10, accounted: 10, unaccounted: 0, exact: true,
      buckets: { waiting: 1, processing: 2, assessed: 5, failed: 1, cancelled: 1, skipped: 0 },
    }, ['10 of 10 eligible documents reconciled', 'Assessed</dt><dd', 'Stopped manually</dt><dd']],
    ['remediate', {
      unit: 'assessed findings', scope: 'current Remediate execution',
      equation: 'assessed findings = sum(current disposition buckets)',
      total: 9, accounted: 9, unaccounted: 0, exact: true,
      buckets: { resolved_verified: 5, awaiting_review: 2, failed: 1, excluded: 1 },
    }, ['9 of 9 assessed findings reconciled', 'Resolved · verified</dt><dd', 'Awaiting review</dt><dd']],
    ['release', {
      unit: 'requested documents', scope: 'immutable Release request',
      equation: 'requested = waiting + processing + published + completed unverified + failed + cancelled + skipped',
      total: 8, accounted: 8, unaccounted: 0, exact: true,
      buckets: { waiting: 1, processing: 0, published: 5, completed_unverified: 1,
        failed: 0, cancelled: 1, skipped: 0 },
    }, ['8 of 8 requested documents reconciled', 'Published · verified</dt><dd',
      'Completed · not verified</dt><dd', 'Stopped manually</dt><dd']],
  ])('uses %s domain accounting as the primary stage story', (stage, domain, expected) => {
    const html = render({ ...SNAPSHOT, stage, domain_reconciliation: domain })
    expected.forEach((text) => expect(html).toContain(text))
    expect(html).toContain(`Integrity check: ${domain.equation}`)
    expect(html).toContain('Operational work-item progress')
    expect(html).toContain(`aria-label="${stage[0].toUpperCase()}${stage.slice(1)} work-item counts"`)
  })

  it('withholds operational substitutions when domain accounting is incomplete', () => {
    const html = render({ ...SNAPSHOT, domain_reconciliation: {
      unit: 'requested documents', scope: 'immutable Release request',
      equation: 'requested = sum(document outcomes)', total: null, accounted: null,
      unaccounted: null, exact: false, buckets: { published: null, failed: null },
    } })
    expect(html).toContain('Accounting temporarily inconsistent.')
    expect(html.match(/class="machine-value"/g)).toHaveLength(3)
    expect(html).not.toContain('10 of 10 requested documents')
    expect(html).not.toContain('Operational work-item progress')
  })
})

describe('current canonical stage selection', () => {
  it('prefers active work over a newer terminal stage', () => {
    const stage = currentCanonicalStage({ stages: [
      { stage: 'assess', state: 'processing', revision: 4, last_durable_update_at: '2026-09-07T01:00:00Z' },
      { stage: 'discover', state: 'succeeded', revision: 8, last_durable_update_at: '2026-09-07T01:02:00Z' },
    ] })
    expect(stage.stage).toBe('assess')
  })

  it('shows the furthest pipeline stage when every stage is terminal', () => {
    const stage = currentCanonicalStage({ stages: [
      { stage: 'release', state: 'cancelled', revision: 2,
        last_durable_update_at: '2026-09-07T01:00:00Z' },
      { stage: 'discover', state: 'succeeded', revision: 8,
        last_durable_update_at: '2026-09-07T01:04:00Z' },
      { stage: 'remediate', state: 'succeeded', revision: 6,
        last_durable_update_at: '2026-09-07T01:03:00Z' },
    ] })
    expect(stage.stage).toBe('release')
  })
})

describe('cumulative workflow stage selection', () => {
  const lineage = { workflow_revision: 3, stages: [
    { stage: 'discover', state: 'succeeded', revision: 2, workflow_revision: 3 },
    { stage: 'assess', state: 'succeeded', revision: 4, workflow_revision: 3 },
    { stage: 'remediate', state: 'processing', revision: 5, workflow_revision: 3 },
    { stage: 'assess', state: 'succeeded', revision: 99, workflow_revision: 2 },
  ] }

  it('shows only predecessors from the same workflow revision', () => {
    expect(priorCanonicalStages(lineage, 'discover')).toEqual([])
    expect(priorCanonicalStages(lineage, 'assess').map((stage) => stage.stage)).toEqual(['discover'])
    expect(priorCanonicalStages(lineage, 'remediate').map((stage) => [stage.stage, stage.revision]))
      .toEqual([['discover', 2], ['assess', 4]])
    expect(priorCanonicalStages(lineage, 'publish').map((stage) => stage.stage))
      .toEqual(['discover', 'assess', 'remediate'])
    expect(priorCanonicalStages(lineage, 'monitor').map((stage) => stage.stage))
      .toEqual(['discover', 'assess', 'remediate'])
  })

  it('opens exceptional prior stages without treating a successful stage as exceptional', () => {
    expect(stageNeedsAttention(lineage.stages[0])).toBe(false)
    expect(stageNeedsAttention({ ...lineage.stages[0], state: 'failed' })).toBe(true)
    expect(stageNeedsAttention({ ...lineage.stages[0], integrity: { ok: false } })).toBe(true)
  })
})

describe('app-level canonical ownership', () => {
  it('keeps one lineage hook and workflow stack alive outside the tab panel', () => {
    const app = readFileSync(join(here, 'App.jsx'), 'utf8')
    const hook = app.indexOf('useCanonicalStageLineage(')
    const signIn = app.search(/^ {2}if \(!me\) return <SignIn/m)
    const card = app.indexOf('<WorkflowStageStack')
    const panel = app.indexOf('id="workflow-panel"')
    expect(hook).toBeGreaterThan(-1)
    expect(hook).toBeLessThan(signIn)
    expect(card).toBeGreaterThan(-1)
    expect(card).toBeLessThan(panel)
    expect(app.indexOf('<WorkflowStageStack')).toBeLessThan(panel)
  })

  it('mounts every live detail only through the canonical stack', () => {
    const app = readFileSync(join(here, 'App.jsx'), 'utf8')
    expect(app).toContain("canonicalAvailable={canonicalStage?.stage === 'release'}")
    expect(app.match(/<WorkflowStageStack/g)).toHaveLength(1)
    expect(app.match(/<LiveAssessmentLive/g)).toHaveLength(1)
    expect(app.match(/<RemediationRunCard/g)).toHaveLength(1)
    expect(app).toContain('showRunProgress={false}')
  })
})

describe('unified idempotent workflow integration', () => {
  const stage = (name, state, revision, extra = {}) => ({
    ...SNAPSHOT, stage: name, state, revision, workflow_revision: 7,
    execution_id: `${name}-${revision}`, ...extra,
  })

  it('restores exactly one current Assess card with completed Discover above and omits future stages', async () => {
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: 'restore', workflow_revision: 7, stages: [
      stage('discover', 'succeeded', 3), stage('assess', 'processing', 4),
    ] }
    await act(async () => { root.render(createElement(WorkflowStageStack, { lineage, activeStage: 'assess' })) })
    expect(container.querySelectorAll('[data-current="true"]')).toHaveLength(1)
    expect(container.querySelector('[data-current="true"]').dataset.stage).toBe('assess')
    expect(container.querySelector('[data-stage="discover"] .workflow-stage-stack__body').hidden).toBe(true)
    expect(container.querySelector('[data-stage="assess"] .workflow-stage-stack__body').hidden).toBe(false)
    expect(container.querySelector('[data-stage="remediate"]')).toBeNull()
    expect(container.querySelector('[data-stage="release"]')).toBeNull()
    expect(container.textContent).not.toContain('Discovering documents')
    expect([...container.querySelectorAll('button')].some((button) => /^Stop\b/.test(button.textContent))).toBe(false)
    await act(async () => { root.unmount() })
  })

  it.each(['discover', 'assess', 'remediate', 'release'])('keeps completed %s minimized even on its own tab', async (completedStage) => {
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: `completed-navigation-${completedStage}`, workflow_revision: 7,
      stages: [stage(completedStage, 'succeeded', 3)] }

    await act(async () => { root.render(createElement(WorkflowStageStack, {
      lineage, activeStage: completedStage,
    })) })
    expect(container.querySelector(`[data-stage="${completedStage}"] .workflow-stage-stack__body`).hidden).toBe(true)
    await act(async () => { root.unmount() })
  })

  it('opens the current completed Remediate stage when verified fixes do not cover all assessed findings', async () => {
    const { container, root } = createTestRoot()
    const remediation = stage('remediate', 'succeeded', 5, { domain_reconciliation: {
      unit: 'assessed findings', total: 4771, accounted: 4771, exact: true,
      buckets: { resolved_verified: 2002, awaiting_review: 0, failed: 0, excluded: 2769 },
    } })
    const lineage = { workflow_id: 'unresolved-remediation', workflow_revision: 7, stages: [remediation] }
    await act(async () => { root.render(createElement(WorkflowStageStack, { lineage, activeStage: 'remediate' })) })
    const body = container.querySelector('[data-stage="remediate"] .workflow-stage-stack__body')
    expect(body.hidden).toBe(false)
    expect(body.textContent).toContain('Processing finished; unresolved work remains')
    // A person's explicit collapse survives later snapshots for this execution.
    await act(async () => { container.querySelector('[data-stage="remediate"] .workflow-stage-stack__summary').click() })
    await act(async () => { root.render(createElement(WorkflowStageStack, { lineage: {
      ...lineage, stages: [{ ...remediation, revision: 6 }],
    }, activeStage: 'remediate' })) })
    expect(body.hidden).toBe(true)
    // The same completed stage stays collapsed outside its own workspace.
    await act(async () => { root.render(createElement(WorkflowStageStack, { lineage, activeStage: 'assess' })) })
    expect(container.querySelector('[data-stage="remediate"] .workflow-stage-stack__body').hidden).toBe(true)
    await act(async () => { root.unmount() })
  })

  it('allows a completed card to be reopened without reopening other completed cards', async () => {
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: 'completed-reopen', workflow_revision: 7, stages: [
      stage('discover', 'succeeded', 3), stage('remediate', 'succeeded', 5),
    ] }
    await act(async () => { root.render(createElement(WorkflowStageStack, { lineage, activeStage: 'remediate' })) })
    await act(async () => { container.querySelector('[data-stage="remediate"] .workflow-stage-stack__summary').click() })
    expect(container.querySelector('[data-stage="remediate"] .workflow-stage-stack__body').hidden).toBe(false)
    expect(container.querySelector('[data-stage="discover"] .workflow-stage-stack__body').hidden).toBe(true)
    await act(async () => { root.unmount() })
  })

  it('resets manual disclosure overrides when the active stage tab changes', async () => {
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: 'manual-navigation', workflow_revision: 7, stages: [
      stage('discover', 'succeeded', 3), stage('assess', 'succeeded', 4),
    ] }

    await act(async () => { root.render(createElement(WorkflowStageStack, {
      lineage, activeStage: 'assess',
    })) })
    const assessSummary = container.querySelector('[data-stage="assess"] .workflow-stage-stack__summary')
    await act(async () => { assessSummary.click() })
    expect(container.querySelector('[data-stage="assess"] .workflow-stage-stack__body').hidden).toBe(false)

    await act(async () => { root.render(createElement(WorkflowStageStack, {
      lineage, activeStage: 'discover',
    })) })
    expect(container.querySelector('[data-stage="discover"] .workflow-stage-stack__body').hidden).toBe(true)
    expect(container.querySelector('[data-stage="assess"] .workflow-stage-stack__body').hidden).toBe(true)
    await act(async () => { root.unmount() })
  })

  it.each(['discover', 'assess', 'remediate', 'release'])('collapses %s on completion, permits reopening, and opens a new execution', async (stageName) => {
    const { container, root } = createTestRoot()
    const render = async (state, execution = `${stageName}-run`) => {
      await act(async () => root.render(createElement(WorkflowStageStack, {
        activeStage: stageName, lineage: { workflow_id: `auto-collapse-${stageName}`, workflow_revision: 7,
          stages: [stage(stageName, state, 4, { execution_id: execution })] },
      })))
    }
    const body = () => container.querySelector(`[data-stage="${stageName}"] .workflow-stage-stack__body`)
    const toggle = () => container.querySelector(`[data-stage="${stageName}"] .workflow-stage-stack__summary`)
    await render('processing')
    expect(body().hidden).toBe(false)
    // Explicitly opening the live panel must not hold it open after completion.
    await act(async () => { toggle().click() })
    await act(async () => { toggle().click() })
    await render('succeeded')
    expect(body().hidden).toBe(true)
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    await act(async () => { toggle().click() })
    expect(body().hidden).toBe(false)
    await render('succeeded')
    expect(body().hidden).toBe(false)
    await act(async () => { toggle().click() })
    await render('processing', `next-${stageName}-run`)
    expect(body().hidden).toBe(false)
    await act(async () => { root.unmount() })
  })

  it('rejects stale revisions and never lets an upstream live flag reclaim downstream ownership', () => {
    const lineage = { workflow_revision: 7, stages: [
      stage('discover', 'processing', 99), stage('assess', 'processing', 4),
      stage('assess', 'succeeded', 3),
      stage('remediate', 'succeeded', 8, { workflow_revision: 6 }),
    ] }
    expect(canonicalWorkflowStages(lineage).map(({ stage: name, revision }) => [name, revision]))
      .toEqual([['discover', 99], ['assess', 4]])
    expect(currentCanonicalStage(lineage).stage).toBe('assess')
  })

  it('keeps the live detail instance and its state across collapse', async () => {
    let mounts = 0
    function Detail() {
      const [samples, setSamples] = useState(12)
      useEffect(() => { mounts += 1; return () => { mounts -= 1 } }, [])
      return <button type="button" onClick={() => setSamples((value) => value + 1)}>{samples} samples</button>
    }
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: 'retained', workflow_revision: 7,
      stages: [stage('discover', 'processing', 2)] }
    await act(async () => { root.render(createElement(WorkflowStageStack, {
      lineage, activeStage: 'discover', stageDetails: { discover: <Detail /> },
    })) })
    const summary = container.querySelector('[data-stage="discover"] .workflow-stage-stack__summary')
    const detailButton = container.querySelector('.workflow-stage-stack__live-detail button')
    await act(async () => { detailButton.click() })
    expect(detailButton.textContent).toBe('13 samples')
    await act(async () => { summary.click() })
    expect(container.querySelector('[data-stage="discover"] .workflow-stage-stack__body').hidden).toBe(true)
    expect(container.querySelector('.workflow-stage-stack__live-detail button').textContent).toBe('13 samples')
    expect(mounts).toBe(1)
    await act(async () => { summary.click() })
    expect(container.querySelector('.workflow-stage-stack__live-detail button').textContent).toBe('13 samples')
    expect(mounts).toBe(1)
    await act(async () => { root.unmount() })
  })
  it('lets the user minimize an attention card across refresh', async () => {
    const { container, root } = createTestRoot()
    const lineage = { workflow_id: 'attention-collapse', workflow_revision: 7, stages: [stage('remediate', 'failed', 4)] }
    const render = async () => act(async () => root.render(createElement(WorkflowStageStack, { lineage })))
    await render()
    const toggle = container.querySelector('[data-stage="remediate"] .workflow-stage-stack__summary')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await act(async () => toggle.click())
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    await act(async () => toggle.click())
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await render()
    expect(container.querySelector('[data-stage="remediate"] .workflow-stage-stack__body').hidden).toBe(true)
    expect(container.querySelector('[data-stage="remediate"]').classList.contains('needs-attention')).toBe(true)
    await act(async () => toggle.click())
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    await act(async () => root.unmount())
  })

  it('defaults every dashboard stage to collapsed, including attention cards, but allows opening', async () => {
    const {container,root}=createTestRoot()
    const lineage={workflow_id:'dashboard-collapsed',workflow_revision:7,stages:[stage('discover','succeeded',1),stage('assess','succeeded',2),stage('remediate','succeeded',3),stage('release','failed',4)]}
    await act(async()=>root.render(createElement(WorkflowStageStack,{lineage,defaultCollapsed:true})))
    const toggles=[...container.querySelectorAll('.workflow-stage-stack__summary')]
    expect(toggles).toHaveLength(4)
    expect(toggles.every(button=>button.getAttribute('aria-expanded')==='false')).toBe(true)
    expect(container.querySelector('[data-stage="release"]').classList.contains('needs-attention')).toBe(true)
    await act(async()=>toggles[3].click())
    expect(toggles[3].getAttribute('aria-expanded')).toBe('true')
    await act(async()=>root.unmount())
  })

})
