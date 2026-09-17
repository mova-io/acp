import QualityEvidenceSummary from './QualityEvidenceSummary.jsx'
import useWaterfallDrawerMetrics from './useWaterfallDrawerMetrics.js'
import { ActivityTrend, ProcessingPace, ContributionBars, SettledSpend } from './WaterfallDrawerCharts.jsx'
import './waterfall-drawer-charts.css'

const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0
  ? new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value) : 'Unavailable'
export function observedPaceScale(pace, trend) {
  const values = (trend?.points || []).map(point => point.value).filter(value => typeof value === 'number' && Number.isFinite(value) && value >= 0)
  if (typeof pace?.value !== 'number' || !Number.isFinite(pace.value) || pace.value < 0 || !values.length) return pace
  return { ...pace, scaleMax: Math.max(1, pace.value, ...values), scaleLabel: 'Highest observed minute in this window; not a capacity target' }
}
export function AIActivityCharts({ data }) {
  const models = data.models?.rows || []
  return <div className="wd-charts wd-ai-dashboard">
    <ContributionBars data={data.models} />
    <ProcessingPace data={observedPaceScale(data.pace, data.trend)} />
    <ActivityTrend data={data.trend} />
    <SettledSpend data={data.spend} />
    <ContributionBars data={data.contribution} />
    <section className="wd-chart wd-model-details" aria-label="Individual model usage">
      <h3>Individual model usage</h3>
      <p>Tokens are provider-reported. Missing usage stays unavailable. Usable AI output is not a verified repair.</p>
      {!models.length ? <p>No model attempts linked to this run.</p> : <div className="wd-model-scroll" tabIndex={0} role="region" aria-label="Scrollable model usage table">
        <table><thead><tr><th>Provider and model</th><th>Attempts</th><th>Awaiting result</th><th>Input tokens</th><th>Output tokens</th><th>Average attempt time</th><th>Median / P95</th><th>Usable output</th><th>Admission wait</th><th>Model load / inference</th></tr></thead>
          <tbody>{models.map(model => <tr key={model.id}><th scope="row">{model.label}</th><td>{number(model.value)}</td><td>{number(model.active)}</td><td>{number(model.input_tokens)}</td><td>{number(model.output_tokens)}</td><td>{number(model.average_seconds)}{model.average_seconds != null ? ' s' : ''}<br /><small>{number(model.timed_attempts)} measured attempts</small></td><td>{number(model.median_seconds)} / {number(model.p95_seconds)} s</td><td>{number(model.usable_percent)}{model.usable_percent != null ? '%' : ''}<br /><small>{number(model.validated_attempts)} validated · {number(model.validation_unavailable)} unavailable</small></td><td>{number(model.provider_timing?.queue_wait_ms?.average)}{model.provider_timing?.queue_wait_ms?.average != null ? ' ms' : ''}<br /><small>{number(model.provider_timing?.queue_wait_ms?.measured_attempts)} measured waits</small></td><td>{number(model.provider_timing?.model_load_ms?.average)} / {number(model.provider_timing?.inference_ms?.average)} ms<br /><small>{number(model.provider_timing?.model_load_ms?.measured_attempts)} load · {number(model.provider_timing?.inference_ms?.measured_attempts)} inference measurements</small></td></tr>)}</tbody></table>
      </div>}
      <p>Usable output rate includes only settled attempts with known validation. Missing validation and partial coverage are not counted as failures. P95 uses the recorded attempt durations; it is not pure model latency. Model load and inference are provider-reported measurements; missing measurements stay unavailable. Admission wait measures ACP’s capacity gates, not the provider’s internal queue.</p>
      <p>{data.timing?.basis || 'Timing includes the attempt lifecycle, not just model response time.'}</p>
    </section>
  </div>
}
export function LocalAIRequestActivity({ data }) {
  if (!data?.rows?.length) return null
  return <section className="wd-chart wd-model-details" aria-label="Local AI requests linked to this run">
    <h3>Local AI requests</h3>
    <p>Requests linked to this run by the worker. A successful response is not a verified repair. Unlinked calls and requests still running are excluded.</p>
    {!data.complete && <p>Partial retained request coverage; these are not whole-run totals.</p>}
    <div className="wd-model-scroll" tabIndex={0} role="region" aria-label="Scrollable local AI requests table">
      <table><thead><tr><th>Model</th><th>Requests</th><th>Successful responses</th><th>Unsuccessful requests</th><th>Average request time</th><th>Admission wait</th><th>Model load / inference</th></tr></thead>
        <tbody>{data.rows.map(row => <tr key={row.model}>
          <th scope="row">{row.model}</th><td>{number(row.requests)}</td><td>{number(row.successful_responses)}</td><td>{number(row.unsuccessful_requests)}</td>
          <td>{number(row.average_request_ms)}{row.average_request_ms != null ? ' ms' : ''}<br /><small>{number(row.measured_requests)} measured requests</small></td>
          <td>{number(row.timings?.queue_wait_ms?.average)}{row.timings?.queue_wait_ms?.average != null ? ' ms' : ''}<br /><small>{number(row.timings?.queue_wait_ms?.measured_requests)} measured waits</small></td>
          <td>{number(row.timings?.model_load_ms?.average)} / {number(row.timings?.inference_ms?.average)} ms<br /><small>{number(row.timings?.model_load_ms?.measured_requests)} load · {number(row.timings?.inference_ms?.measured_requests)} inference measurements</small></td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>
}
export default function CloudAIActivity({ scanId, batchId, live, paused, aiEnabled, view, verifiedFindings }) {
  const metrics = useWaterfallDrawerMetrics({ scanId, batchId, scope: {}, live: live && !paused, enabled: aiEnabled !== false })
  if (aiEnabled === false) return <QualityEvidenceSummary view={view} verifiedFindings={verifiedFindings} />
  return <section className="wd-ai-usage" aria-label="AI usage for this remediation run">
    <QualityEvidenceSummary view={view} metrics={metrics.data} verifiedFindings={verifiedFindings} />
    <h3>AI usage · this run</h3>
    <p>Governed model attempts appear in the charts; linked local AI requests appear below. Calls without a run link are excluded, including some local AI activity. Select a waterfall model below for its individual evidence.</p>
    {metrics.loading && !metrics.data && <p role="status">Loading AI usage…</p>}
    {metrics.error && <p role="status">{metrics.data ? 'Refresh delayed; showing the last recorded usage.' : 'AI usage unavailable.'} <button type="button" onClick={metrics.refresh}>Retry usage</button></p>}
    {metrics.data && <><p>{metrics.data.mode === 'recorded' ? 'Saved activity' : 'Live activity'} · {metrics.data.complete ? 'Complete retained attempt coverage' : 'Partial retained coverage; charts are not whole-run totals'}</p><AIActivityCharts data={metrics.data} /><LocalAIRequestActivity data={metrics.data.local_activity} /></>}
  </section>
}
