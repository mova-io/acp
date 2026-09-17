import { Fragment, useEffect, useId, useMemo, useRef, useState } from 'react'
import RemediationCategoryPill, { CATEGORY_SHORT_LABELS, categoryExplanation } from './RemediationCategoryPill.jsx'
import { REMEDIATION_CATEGORIES } from './remediationCategories.js'
import { balancedSummaryModel, treemapRects, activityMonths, COVERAGE_STATES, FINDING_GROUPS } from './balancedSummaryModel.js'
import './balanced-summary.css'

const number = value => value == null ? '—' : value.toLocaleString()
function Card({ title, note, kind, children }) {
  const id = useId()
  return <section className={`balanced-card balanced-${kind}`} aria-labelledby={id}>
    <h2 id={id}>{title}</h2><p className="balanced-note">{note}</p>{children}
  </section>
}
function DataTable({ caption, columns, rows }) {
  return <details className="balanced-data"><summary>View data: {caption}</summary><div className="balanced-scroll"><table>
    <caption>{caption}</caption><thead><tr>{columns.map(c => <th key={c} scope="col">{c}</th>)}</tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody>
  </table></div></details>
}

export function ScanActivityCalendar({ activity = [], onDay, disabled = false, stale = false }) {
  const months = useMemo(() => activityMonths(activity), [activity])
  const [choice, setChoice] = useState('')
  const selected = months.find(m => m.month === choice) || months.at(-1)
  const total = activity.reduce((sum, day) => sum + (Number.isSafeInteger(day.attempts) && day.attempts >= 0 ? day.attempts : 0), 0)
  const first = selected ? new Date(`${selected.month}-01T00:00:00Z`) : null
  const offset = first ? (first.getUTCDay() + 6) % 7 : 0
  const length = first ? new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate() : 0
  const byDate = new Map((selected?.days || []).map(day => [day.date, day]))
  const max = Math.max(1, ...(selected?.days || []).map(day => day.attempts))
  return <Card title="Scan activity calendar" kind="calendar" note={`${number(total)} dated attempts in the reporting interval · UTC start time${stale ? ' · stale snapshot' : ''}`}>
    {months.length > 1 && <label className="balanced-month">Month <select value={selected.month} onChange={e => setChoice(e.target.value)}>{months.map(({ month }) => <option key={month}>{month}</option>)}</select></label>}
    {!selected ? <p className="balanced-empty">No dated attempts in this interval.</p> : <>
      <h3 className="balanced-month-title">{first.toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' })}</h3>
      <div className="balanced-weekdays" aria-hidden="true">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(day => <span key={day}>{day}</span>)}</div>
      <div className="balanced-calendar-grid" role="group" aria-label="Daily scan attempts">
        {Array.from({ length: offset }, (_, i) => <span key={`blank-${i}`} />)}
        {Array.from({ length }, (_, i) => {
          const date = `${selected.month}-${String(i + 1).padStart(2, '0')}`, day = byDate.get(date)
          return day ? <button key={date} type="button" className={`balanced-day balanced-day-${day.attempts / max > .6 ? 'high' : 'low'}`}
            disabled={disabled || !onDay} aria-label={`${date}: ${number(day.attempts)} attempts. Show matching scans`}
            title={Object.entries(day.statuses || {}).map(([status, count]) => `${status}: ${count}`).join(' · ')} onClick={() => onDay?.(day)}>
            <span>{i + 1}</span><strong>{number(day.attempts)}</strong>
          </button> : <span key={date} className="balanced-day balanced-day-missing" role="img" aria-label={`${date}: no activity row recorded`}><span>{i + 1}</span><strong>—</strong></span>
        })}
      </div><p className="balanced-note">Select a day to inspect its runs. A dash means no activity row is recorded in this interval.</p>
    </>}
    <DataTable caption="Calendar activity" columns={['UTC date', 'Attempts', 'Recorded statuses']} rows={activity.map(day => [day.date, number(day.attempts), Object.entries(day.statuses || {}).map(([s, n]) => `${s}: ${n}`).join(' · ')])} />
  </Card>
}

export default function BalancedSummary({ run, files = [], inventory = null, cap, assessment, calendar = null, onOpenFile, now, showSupplemental = false }) {
  const model = useMemo(() => balancedSummaryModel({ run, files, inventory, cap, assessment, now }), [run, files, inventory, cap, assessment, now])
  const [selected, setSelected] = useState(null)
  const evidenceRef = useRef(null), returnFocus = useRef(null)
  // A detail selection belongs to exactly one scan and is discarded when the scan changes.
  const detail = selected?.scanId === run?.id ? selected : null
  const open = (title, rows) => {
    returnFocus.current = document.activeElement
    setSelected({ title, rows, scanId: run?.id })
  }
  useEffect(() => {
    if (!detail) return
    evidenceRef.current?.focus()
    evidenceRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [detail])
  const close = () => {
    setSelected(null)
    if (returnFocus.current?.isConnected) returnFocus.current.focus()
  }
  const findingRows = rows => rows.map(row => ({ file: row.file, criterion: row.sc, category: row.category }))
  const metrics = [
    ['Documents discovered', model.discovered, model.truncated ? 'At least this many; listing was truncated' : 'Selected scan inventory'],
    ['Eligible coverage', model.rate == null ? null : `${Math.round(model.rate * 100)}%`, `${number(model.assessed)} assessed / ${number(model.eligible)} eligible documents`],
    ['Total findings', model.findingsKnown ? model.findings.length : null, 'Recorded findings in the selected assessment scope'],
    ['Auto-fix available', model.findingsKnown ? model.tags.automatic : null, 'Auto tag · supported rule-based fixes'],
    ['Human review', model.findingsKnown ? model.reviewCount : null, 'Approve + AI + Manual finding routes'],
  ]
  const columns = REMEDIATION_CATEGORIES.filter(([key]) => ['automatic', 'approval', 'suggestion', 'manual'].includes(key) || model.tags[key] > 0)
  const formats = model.formats
  const maxFormat = Math.max(1, ...formats.map(row => row.total))
  const groupRows = FINDING_GROUPS.map(label => ({ label, rows: model.groups[label] })).sort((a, b) => b.rows.length - a.rows.length)
  const maxGroup = Math.max(1, ...groupRows.map(row => row.rows.length))
  const ages = [['week', 'Under 7 days'], ['month', '7–30 days'], ['older', 'Over 30 days'], ['unknown', 'Not recorded']]
  const maxAge = Math.max(1, ...Object.values(model.age).map(rows => rows.length))
  let cumulative = 0
  const detailId = useId(), otherId = useId()
  // The Other breakdown belongs to one scan: it starts collapsed whenever the selected scan changes.
  const [otherFor, setOtherFor] = useState(null)
  if (otherFor != null && otherFor !== (run?.id ?? '')) setOtherFor(null)
  const otherTotal = model.groups.Other.length
  const otherOpen = otherFor != null && otherTotal > 0
  const toggleOther = () => setOtherFor(otherOpen ? null : run?.id ?? '')
  const percent = n => { const p = Math.round(n / otherTotal * 100); return p === 0 && n > 0 ? '<1%' : `${p}%` }
  return <div className={`balanced-summary${calendar ? ' balanced-summary-analytics' : ''}${showSupplemental ? '' : ' balanced-summary-focused'}`}>
    <p className="balanced-context">{run?.id ? <>Estate and finding charts: selected scan <strong>{run.id}</strong>.</> : 'Select an assessed scan to populate the estate and finding charts.'}
      {calendar && ' The activity calendar uses the reporting filters; these selected-scan charts do not aggregate repeated observations.'}</p>
    <div className="balanced-metrics">{metrics.map(([label, value, note]) => <div className="balanced-metric" key={label}><span>{label}</span><strong>{number(value)}</strong><small>{note}</small></div>)}</div>
    {run?.status === 'failed' && <p className="balanced-notice" role="status">The selected scan failed. Available results may be incomplete.</p>}
    {files.some(file => file.status === 'error') && <p className="balanced-notice">Some documents could not be assessed. They are shown separately from assessment results.</p>}
    {/* Supplemental charts are retained for restoration, but retired from both dashboard tabs. */}
    {showSupplemental && <div className="balanced-tags" role="group" aria-label="Remediation tags"><strong>Remediation tags</strong>{REMEDIATION_CATEGORIES.map(([key]) => <button type="button" key={key} disabled={!model.findingsKnown || !model.tags[key]}
      aria-label={`View ${CATEGORY_SHORT_LABELS[key]} findings: ${model.findingsKnown ? number(model.tags[key]) : 'not assessed'}`} title={categoryExplanation(key)}
      onClick={() => open(`${CATEGORY_SHORT_LABELS[key]} findings`, findingRows(model.findings.filter(row => row.category === key)))}>
      <RemediationCategoryPill category={key} count={model.findingsKnown ? model.tags[key] : null} />
    </button>)}</div>}
    <div className="balanced-grid">
      {calendar}
      <Card title="Estate coverage by file type" kind="coverage" note="Document counts on a common scale · awaiting assessment is separate from verification pending">
        {model.inconsistent ? <p className="balanced-notice">The available file records exceed the recorded format totals. Refresh this scan before comparing coverage.</p> : formats.length ? <>
          <div className="balanced-format-bars">{formats.map(row => <div key={row.key} className="balanced-format-row">
            <button type="button" className="balanced-link" onClick={() => open(`${row.label}: available document records`, row.files.map(file => ({ file })))}>{row.label}</button>
            <div className="balanced-stack" role="img" aria-label={`${row.label}: ${COVERAGE_STATES.map(([key, label]) => `${number(row[key])} ${label.toLowerCase()}`).join(', ')}`}>
              {COVERAGE_STATES.map(([key]) => <span key={key} className={`balanced-coverage-${key}`} style={{ width: `${row[key] / maxFormat * 100}%` }} />)}
            </div><strong>{number(row.total)}</strong>
          </div>)}</div>
          <div className="balanced-legend">{COVERAGE_STATES.filter(([key]) => formats.some(row => row[key])).map(([key, label]) => <span key={key}><i className={`balanced-coverage-${key}`} />{label}</span>)}</div>
        </> : <p className="balanced-empty">File-type inventory is not recorded yet.</p>}
        {formats.some(row => row.unknown) && <p className="balanced-note">Some per-file eligibility or assessment records are unavailable. Their coverage is not recorded.</p>}
        {model.discovered != null && model.formatTotal !== model.discovered && <p className="balanced-note">Format totals cover {number(model.formatTotal)} of {number(model.discovered)} discovered documents.</p>}
        <DataTable caption="Coverage by file type" columns={['Format', 'Total', ...COVERAGE_STATES.map(([, label]) => label)]} rows={formats.map(row => [row.label, number(row.total), ...COVERAGE_STATES.map(([key]) => number(row[key]))])} />
      </Card>
      {showSupplemental && <Card title="Document formats" kind="formats" note={`${number(model.formatTotal)} documents in recorded format totals · area = count`}>
        {formats.some(row => row.total > 0) ? <div className="balanced-treemap" role="group" aria-label="Document format treemap">{treemapRects(formats).map((row, i) => <button type="button" key={row.key} className={`balanced-tile balanced-tile-${i % 5}`}
          style={{ left: `${row.x}%`, top: `${row.y}%`, width: `${row.w}%`, height: `${row.h}%` }}
          aria-label={`${row.label}: ${number(row.total)} documents. View available records`} title={`${row.label}: ${number(row.total)} documents`}
          onClick={() => open(`${row.label}: available document records`, row.files.map(file => ({ file })))}>
          <span>{row.label}</span><strong>{number(row.total)}</strong>
        </button>)}</div> : <p className="balanced-empty">No format totals are available.</p>}
        <DataTable caption="Document formats" columns={['Format', 'Documents']} rows={formats.map(row => [row.label, number(row.total)])} />
      </Card>}
      <Card title="Remediation opportunities" kind="remediation" note="Finding instances by recorded department · the same tags as Assess and Remediate">
        {!model.findingsKnown ? <p className="balanced-empty">Assessment has not produced findings data yet.</p> : !model.findings.length ? <p className="balanced-empty">No findings recorded in this assessment scope.</p> : <div className="balanced-scroll"><table className="balanced-heatmap">
          <caption>Department and remediation tag counts</caption><thead><tr><th scope="col">Department</th>{columns.map(([key]) => <th key={key} scope="col"><RemediationCategoryPill category={key} /></th>)}</tr></thead>
          <tbody>{model.departments.map(([department, rows]) => <tr key={department}><th scope="row">{department}</th>{columns.map(([key]) => {
            const matches = rows.filter(row => row.category === key)
            return <td key={key}><button type="button" className={`remediation-category-pill--${key}`} disabled={!matches.length} aria-label={`${department}, ${CATEGORY_SHORT_LABELS[key]}: ${matches.length} findings`}
              onClick={() => open(`${department} · ${CATEGORY_SHORT_LABELS[key]}`, findingRows(matches))}>{number(matches.length)}</button></td>
          })}</tr>)}</tbody></table></div>}
        {model.departments.some(([name]) => name === 'Not recorded') && <p className="balanced-note">Department is not recorded for some documents; no department is inferred.</p>}
        <p className="balanced-note">AI applied is shown separately from Pending, without double counting. These tags describe findings, not whole-document conformance.</p>
      </Card>
      <Card title="Finding categories" kind="findings" note={model.findingsKnown ? `${number(model.findings.length)} findings · grouped by criterion` : 'Assessment data is not available yet'}>
        {model.findingsKnown && <div className="balanced-rank-list">{groupRows.map(({ label, rows }) => {
          cumulative += rows.length
          const other = label === 'Other'
          const parent = <button type="button" className="balanced-rank" key={label} disabled={!rows.length}
            {...other ? { 'aria-expanded': otherOpen, 'aria-controls': otherId, onClick: toggleOther } : { onClick: () => open(label, findingRows(rows)) }}>
            <span>{other && <span className="balanced-disclosure" aria-hidden="true">{otherOpen ? '▾' : '▸'}</span>}{label}</span><span className="balanced-rank-track"><i style={{ width: `${rows.length / maxGroup * 100}%` }} /></span><strong>{number(rows.length)}</strong>
            {model.findings.length > 0 && <small>{Math.round(cumulative / model.findings.length * 100)}% cumulative</small>}
          </button>
          if (!other) return parent
          return <Fragment key={label}>{parent}
            <div id={otherId} className="balanced-subrank-list" role="group" aria-label="Other findings by WCAG criterion" hidden={!otherOpen}>
              {otherOpen && <>
                <button type="button" className="balanced-link" onClick={() => open('Other', findingRows(rows))}>View all {number(otherTotal)} Other records</button>
                {model.otherCriteria.map(child => <button type="button" className="balanced-rank balanced-subrank" key={child.sc || '(none)'} onClick={() => open(`Other · ${child.label}`, findingRows(child.rows))}>
                  <span>{child.label}</span><span className="balanced-rank-track"><i style={{ width: `${child.rows.length / maxGroup * 100}%` }} /></span><strong>{number(child.rows.length)}</strong>
                  <small>{percent(child.rows.length)} of Other</small>
                </button>)}
              </>}
            </div>
          </Fragment>
        })}</div>}
        <DataTable caption="Finding categories" columns={['Category', 'Finding instances']} rows={groupRows.map(({ label, rows }) => [label, model.findingsKnown ? number(rows.length) : 'Not assessed'])} />
        <p className="balanced-note">Structure: 1.3.*; contrast: 1.4.3, 1.4.6, 1.4.11; text alternatives: 1.1.1. Other includes the remaining selected criteria; select it to break it down by criterion.</p>
      </Card>
      {showSupplemental && <Card title="Human review age" kind="review" note={model.findingsKnown ? `${number(model.reviewCount)} findings in Approve, AI, or Manual routes` : 'Review findings are not available yet'}>
        {model.findingsKnown && <div className="balanced-rank-list">{ages.map(([key, label]) => <button type="button" className="balanced-rank" key={key} disabled={!model.age[key].length} onClick={() => open(`${label} · review findings`, findingRows(model.age[key]))}>
          <span>{label}</span><span className={`balanced-rank-track balanced-age-${key}`}><i style={{ width: `${model.age[key].length / maxAge * 100}%` }} /></span><strong>{number(model.age[key].length)}</strong>
        </button>)}</div>}
        <DataTable caption="Human review age" columns={['Age from first seen', 'Finding instances']} rows={ages.map(([key, label]) => [label, model.findingsKnown ? number(model.age[key].length) : 'Not assessed'])} />
        <p className="balanced-note">Age requires a finding’s recorded first-seen date. Missing or invalid dates stay Not recorded. An AI route may be applied automatically depending on the remediation plan.</p>
      </Card>}
    </div>
    {detail && <section className="balanced-card balanced-evidence" aria-labelledby={detailId} ref={evidenceRef} tabIndex={-1}>
      <button type="button" className="balanced-close" onClick={close}>Close chart records</button><h2 id={detailId}>{detail.title}</h2>
      <p>{number(detail.rows.length)} available records{detail.rows.length > 200 && ' · showing the first 200'}. Inventory totals can include documents without available detail records.</p>
      <div className="balanced-scroll"><table><caption>Records supporting this chart selection</caption><thead><tr><th scope="col">Document</th><th scope="col">Criterion</th><th scope="col">Remediation tag</th></tr></thead><tbody>
        {detail.rows.slice(0, 200).map(({ file, criterion, category }, i) => <tr key={i}><td>{onOpenFile ? <button className="balanced-link" type="button" onClick={() => onOpenFile(file)}>{file.file || file.name}</button> : file.file || file.name}</td><td>{criterion || '—'}</td><td>{category ? <RemediationCategoryPill category={category} /> : 'Not an assessment finding'}</td></tr>)}
      </tbody></table></div>
    </section>}
  </div>
}
