import { describe, it, expect } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import AssessRunProgress from './AssessRunProgress.jsx'

// The Assess RUNNING screen (board assess-03): a live stage checklist, NOT a mid-run scoreboard.
// The rule it enforces is that no verdict-shaped count renders while the run is in flight — those
// live on the Overview now, where they read as a result. This screen says which document is being
// worked, how far through the estate we are, and that the results arrive at the end.

const SNAP = {
  available: true, active: true, phase: 'assessing', run_id: 's1',
  _live: { mode: 'live', measuredAt: Date.now() },
  totals: { discovered: 22, eligible: 22 },
  kpis: { completed: 8, need_attention: 3, unable_to_assess: 1, processing: 1 },
  queue: {
    current: { file: 'Finance/Q3 Board Pack.pdf', criterion: '1.1.1',
               criterion_name: 'Non-Text Content', action: 'Checking Image Alternatives' },
    in_flight: 1, queued: 13, workers: { busy: 1, max: 4 },
  },
}
const render = (snapshot, throughput, onStop) =>
  renderToStaticMarkup(createElement(AssessRunProgress, { snapshot, throughput, onStop }))

describe('the assessment running screen focuses on the document in flight', () => {
  it('describes the no-write boundary without misnaming SharePoint as a drive', () => {
    const html = render({ ...SNAP, source: 'sharepoint' })
    expect(html).toContain('connected source during assessment')
    expect(html).not.toContain('your drive at any point')
  })

  it('keeps the SharePoint boundary visible while documents are assessed', () => {
    const html = render({ ...SNAP, source: 'sharepoint', scope: { kind: 'sharepoint', sites: [
      { id: 's1', name: 'Clinical', status: 'complete', libraries: [{ id: 'l1', name: 'Documents' }] },
    ] } })
    expect(html).toContain('Content source')
    expect(html).toContain('SharePoint')
    expect(html).toContain('Clinical')
    expect(html).toContain('1 document library')
  })

  it('restores the bounded document activity card for SharePoint reconnects', () => {
    const html = render({ ...SNAP, source: 'sharepoint', documents: {
      completed: 51, displayed: 2, truncated: true, items: [
        { file: 'Clinical/History.pdf', score: 100, criteria: [] },
        { file: 'Clinical/Procedure.pdf', score: 81, criteria: ['1.3.1', '1.4.3'] },
      ],
    } })
    expect(html).toContain('Document activity')
    expect(html).toContain('8 of 22 documents assessed')
    expect(html).toContain('Showing 2 most recent completed documents')
    expect(html).toContain('Clinical/History.pdf')
    expect(html).not.toContain('/100')
    expect(html).not.toContain('alscore')
    expect(html).toContain('1.3.1')
    expect(html).toContain('overflow-y:auto')
  })

  it('separates overall assessment progress from the capped recent-completions list', () => {
    const items = Array.from({ length: 50 }, (_, i) => ({
      file: `Synthetic/Document-${i + 1}.pdf`, score: 100, criteria: [],
    }))
    const html = render({ ...SNAP,
      totals: { discovered: 147, eligible: 147 },
      kpis: { ...SNAP.kpis, completed: 79, processing: 1 },
      documents: { completed: 79, displayed: 50, truncated: true, items },
    })
    expect(html).toContain('79 of 147 documents assessed')
    expect(html).toContain('Showing 50 most recent completed documents')
    expect(html).not.toContain('Latest 50 Of 79 Completed')
    expect((html.match(/class="done"/g) || [])).toHaveLength(50)
  })

  it('formats internal SC rule ids as readable monospace criterion tags', () => {
    const html = render({ ...SNAP, documents: {
      completed: 1, displayed: 1, truncated: false, items: [
        { file: 'Clinical/Procedure.pdf', score: 81,
          criteria: ['SC_2_4_2', 'SC_1_1_1', '1.4.5 Images of Text', '1.4.3 Contrast (Minimum)', '3.1.2 Language of Parts'] },
      ],
    } })
    expect(html).toContain('title="SC_2_4_2">2.4.2</b>')
    expect(html).toContain('title="SC_1_1_1">1.1.1</b>')
    expect(html).toContain('title="1.4.5 Images of Text">1.4.5</b>')
    expect(html).toContain('title="1.4.3 Contrast (Minimum)">1.4.3</b>')
    expect(html).toContain('title="3.1.2 Language of Parts">3.1.2</b>')
    expect(html).not.toContain('>1.4.5 Images of Text</b>')
    expect(html).not.toContain('>SC_2_4_2</b>')
  })

  it('renders nothing until the live snapshot is available', () => {
    expect(render(null)).toBe('')
    expect(render({ available: false })).toBe('')
  })

  it('shows the live assessment stages, estate progress, and current document', () => {
    const html = render(SNAP, { etaText: 'about 1 min 50s left', ratePerMin: 12, points: [1, 3, 5, 8] })
    expect(html).toContain('Assessing documents')
    expect(html).toContain('Live')
    expect(html).toContain('Validated assessment scope')
    expect(html).toContain('Started assessment workers')
    expect(html).toContain('1 active · 3 standing by · 4 total')
    expect(html).toContain('Opened and assessed documents')
    expect(html).toContain('8 of 22 complete · 1 processing')
    expect(html).toContain('Finalized conformance results')
    expect(html).toContain('Finance/Q3 Board Pack.pdf')
    expect(html).toContain('Checking Image Alternatives')
    expect(html).toContain('Checking Non-Text Content')
    expect(html).toContain('successful canonical snapshot refresh')
    expect(html).toContain('about 1 min 50s left')
    expect(html).toMatch(/Results appear when the run finishes/)
    expect(html).toContain('Live updates · refreshed 0s ago')
    expect(html.indexOf('successful live update')).toBeLessThan(html.indexOf('Live updates · refreshed'))
    expect(html).toContain('aria-label="Live assessment accounting"')
    expect(html).toMatch(/Assessed.*<\/dt><dd><span class="livecounter"/)
    expect(html).toMatch(/Waiting.*<\/dt><dd>13<\/dd>/)
    for (const term of ['Assessed', 'Processing', 'Waiting', 'Eligible']) {
      expect(html).toContain(`aria-label="What does &quot;${term}&quot; mean?"`)
    }
  })

  it('shows truthful cloud second-opinion use and remaining budgets', () => {
    const html = render({ ...SNAP, second_opinion: {
      status: 'used', reason: '2 of 2 provider attempts succeeded',
      scan: { used: 2, limit: 5, remaining: 3 }, day: { used: 7, limit: 20, remaining: 13 },
      cost: { estimated_remaining_usd: 8.4, measured_scan_usd: 0.024 },
    } })
    expect(html).toContain('Cloud second opinions · used')
    expect(html).toContain('2 of 5 requests this scan')
    expect(html).toContain('13 daily requests remaining')
    expect(html).toContain('$8.40 estimated budget remaining')
    expect(html).toContain('$0.0240 measured for this scan')
  })

  it('labels dedicated worker heartbeat capacity as per-replica, never unavailable or aggregate', () => {
    const split = { ...SNAP, queue: { ...SNAP.queue,
      workers: { busy: 1, max: 2, idle: 1, capacity_scope: 'per_replica' } } }
    const html = render(split)
    expect(html).toContain('Assessment service online · 2 slots per replica')
    expect(html).not.toContain('Worker status unavailable')
  })

  it('keeps current processing in the disclosure and shows one rolling heartbeat strip above status', () => {
    const html = render(SNAP, { ratePerMin: 12, points: [1, 3, 5, 8] })
    expect(html).toMatch(/<details class="assess-live-details"/)
    expect(html).not.toMatch(/<details open="" class="assess-live-details"/)
    expect(html).toContain('Live processing details')
    expect(html).toContain('Processing Now')
    expect(html).not.toContain('Assessment throughput')
    expect(html.match(/live-heartbeat-bars/g)).toHaveLength(1)
  })

  it('never renders a mid-run verdict scoreboard', () => {
    // The whole point of board 3: no partially-filled count of failures. The snapshot carries
    // need_attention / unable_to_assess KPIs; this screen must not surface them as counts.
    const html = render(SNAP)
    expect(html, 'a need-attention verdict leaked onto the running screen').not.toMatch(/[Nn]eed.attention/)
    expect(html, 'an unable-to-assess verdict leaked onto the running screen').not.toMatch(/[Uu]nable to assess/)
    // No standalone "3" / "1" verdict counts from the KPI block — the only counts are the document
    // position and the estate size.
    expect(html).not.toMatch(/<b[^>]*>3<\/b>/)
  })

  it('turns every stage complete on the final live frame', () => {
    const last = { ...SNAP, kpis: { ...SNAP.kpis, completed: 22 } }
    const html = render(last)
    expect(html).toContain('Assessment complete')
    expect(html).toContain('22 of 22 complete')
    expect(html).toContain('Updates complete')
    expect(html).toContain('Assessment finished for 22 documents')
    expect(html).not.toContain('Processing Now:')
  })

  it('adds measured result detail to the completed assessment card', () => {
    const last = {
      ...SNAP,
      kpis: { ...SNAP.kpis, completed: 22, findings_so_far: 47, unable_to_assess: 2 },
      outcomes: { passed: 15, review: 5, failed: 2, skipped: 0, processing: 0 },
    }
    const html = render(last)
    expect(html).toContain('Completed assessment results')
    expect(html).toContain('Assessment results')
    expect(html).toContain('22 of 22 eligible documents finalized')
    expect(html).toContain('Findings recorded')
    expect(html).toContain('47 accessibility findings recorded across the completed assessment')
    expect(html).toContain('Documents passed')
    expect(html).toContain('Need review')
    expect(html).toContain('Could not complete')
    expect(html).toContain('2 documents could not be assessed and require follow-up')
    expect(html).toContain('Remediation recommendations are ready for supported findings')
  })

  it('omits pending result counts instead of presenting them as zero', () => {
    const last = {
      ...SNAP,
      kpis: { ...SNAP.kpis, completed: 22 },
      kpis_pending: ['findings_so_far', 'unable_to_assess'],
    }
    const html = render(last)
    expect(html).toContain('Assessment results')
    expect(html).not.toContain('Findings recorded')
    expect(html).not.toContain('accessibility findings recorded')
    expect(html).not.toContain('could not be assessed and require')
  })

  it('falls back to the lane label when no document is currently reported', () => {
    const idle = { ...SNAP, queue: { ...SNAP.queue, current: null, in_flight: 0 } }
    const html = render(idle)
    expect(html).toContain('Assessing documents')
    expect(html).toContain('Idle')
  })

  it('shows a preparation checklist rather than a sweeping bar when nothing has completed yet', () => {
    // At 0% we replace the full-width shimmer with a localized step list so the screen
    // communicates what ACP is doing rather than just animating across the page.
    const starting = { ...SNAP, kpis: { ...SNAP.kpis, completed: 0 } }
    const html = render(starting)
    expect(html).toContain('Preparing assessment')
    expect(html).not.toContain('track indeterminate')
    expect(html).toContain('Validating scan inventory')
    expect(html).toContain('Starting assessment workers')
    expect(html).toContain('Building the document queue')
    expect(html).toContain('Assessment in progress')
    expect(html).toContain('0 of 22 completed · 1 processing')
    expect(html).not.toContain('Opening the first documents')
  })

  it('switches from preparation to the live stage checklist once the first file completes', () => {
    const html = render(SNAP)
    expect(html).toContain('Assessing documents')
    expect(html).not.toContain('Preparing assessment')
    expect(html).toContain('Validated assessment scope')
  })
})

describe('Stop — board-exact placement, inline with what stopping does', () => {
  it('offers Stop only when both an active run and a handler are given', () => {
    expect(render(SNAP, undefined, () => {})).toContain('>Stop<')
    expect(render(SNAP, undefined, undefined), 'Stop rendered with no handler to call').not.toContain('>Stop<')
    const notActive = { ...SNAP, active: false }
    expect(render(notActive, undefined, () => {}), 'Stop offered on a run with nothing left to stop')
      .not.toContain('>Stop<')
  })

  it('sits beside the explanation of what stopping does, not alone', () => {
    const html = render(SNAP, undefined, () => {})
    const stopAt = html.indexOf('>Stop<')
    const explainAt = html.indexOf('Results appear when the run finishes')
    expect(stopAt).toBeGreaterThan(-1)
    expect(explainAt).toBeGreaterThan(-1)
    // Same flex row — within a couple hundred characters of markup, not on a separate row far away.
    expect(explainAt - stopAt).toBeLessThan(400)
  })
})

 describe('single document activity presentation', () => {
  it('uses the bottom-list layout with current processing and completed criteria, without scores', () => {
    const html = render({ ...SNAP, documents: { completed: 8, displayed: 1, truncated: false, items: [
      { file: 'Finished.pdf', score: 19, criteria: ['1.3.1'] },
    ] } })
    expect(html).toContain('Processing Now:')
    expect(html).toContain('Finance/Q3 Board Pack.pdf')
    expect(html).toContain('8 of 22 documents assessed')
    expect(html).toContain('max-height:420px')
    expect(html).toContain('Finished.pdf')
    expect(html).toContain('1.3.1')
    expect(html.match(/class="assesslist"/g)).toHaveLength(1)
    expect(html).not.toContain('/100')
  })
  it('shows the current-document banner before any file finishes', () => {
    const html = render({ ...SNAP, kpis: { completed: 0, processing: 1 }, documents: { completed: 0, displayed: 0, items: [] } })
    expect(html).toContain('Processing Now:')
    expect(html).toContain('0 of 22 documents assessed')
  })
 })
