import { useCallback, useEffect, useMemo, useState } from 'react'
import { getRuleExplanations } from './api.js'
import './rule-explanations.css'

// Settings → Rule explanations. READ-ONLY for everyone who can open Settings: the payload is product
// metadata (what each rule checks, by what threshold, and what it cannot see), not configuration.
// Nothing here writes. The only controls are the search box, the two filters and the disclosure
// buttons — a threshold is shown as text with where it comes from, never as an input, because no
// endpoint exists that would change it and a field that cannot save is a claim the product can't keep.
//
// Every string is rendered as a React text node. The payload is derived from code comments and
// constants, which makes it trusted-looking rather than trusted; nothing here may become markup.

// Closed vocabularies from api/rule_explanations/schema.py. Status is always WORDS, never colour
// alone; an unknown value is shown verbatim and labelled as unrecognised rather than guessed at.
const STATUS_LABEL = {
  implemented: 'Implemented',
  partial: 'Partial',
  manual: 'Manual review only',
  not_implemented: 'Not implemented',
  not_applicable: 'Not applicable',
}
const STATUS_NOTE = {
  implemented: 'A detector runs and its findings reach assessment.',
  partial: 'A detector runs, but covers only part of this criterion (see what it does not check).',
  manual: 'ACP cannot judge this from the file; a person must.',
  not_implemented: 'This criterion applies to this format, but nothing checks it yet.',
}
const METHOD_LABEL = { deterministic: 'Deterministic', heuristic: 'Heuristic', ai: 'AI-assisted', manual: 'Manual' }
const FIX_LABEL = {
  auto: 'Auto-fix, verified by re-scan',
  assisted: 'Assisted fix: a person approves before it is written',
  review: 'Routed to a person; the file is left unchanged',
  none: 'Detection only; no fix',
}
const ASSESS_LANE = { auto: 'Automatic', review: 'Flagged for human review', human: 'Human judgement' }
const REMEDIATE_LANE = { auto: 'Automatic', assisted: 'Assisted (human approves)', human: 'Human only' }
const STATUS_ORDER = ['implemented', 'partial', 'manual', 'not_implemented', 'not_applicable']

const statusLabel = (s) => STATUS_LABEL[s] || `Unrecognised status (${String(s)})`
const fmtLabel = (f) => String(f || '').toUpperCase()
const arr = (v) => (Array.isArray(v) ? v : [])
const text = (v) => (v == null ? '' : typeof v === 'string' ? v : typeof v === 'object' ? JSON.stringify(v) : String(v))
const idFor = (sc) => `rx-${String(sc).replace(/[^A-Za-z0-9_-]/g, '-')}`
const shortSha = (s) => (s ? String(s).replace(/-dirty$/, '').slice(0, 7) + (String(s).endsWith('-dirty') ? '-dirty' : '') : '')

// The rubric field is described as "{disabled_rule_ids: [..]} or similar"; accept the shapes that
// plausibly arrive rather than silently dropping a disabled rule.
function rubricInfo(c) {
  const e = c?.enabled_in_rubric
  if (e === false) return { disabledAll: true, disabled: new Set() }
  const ids = arr(e?.disabled_rule_ids ?? e?.disabled_rules ?? e?.disabled ?? c?.disabled_rule_ids)
  return { disabledAll: e?.enabled === false, disabled: new Set(ids.map(String)) }
}

// Everything a search can hit for one format cell: rule IDs and every plain-language bullet.
function cellHaystack(fmt, cell) {
  return [
    fmt, statusLabel(cell?.status), cell?.evidence, cell?.fix,
    ...arr(cell?.checks), ...arr(cell?.does_not_check),
    ...arr(cell?.thresholds).flatMap((t) => [t?.label, t?.value, t?.setting, t?.source]),
    ...arr(cell?.rules).flatMap((r) => [r?.id, r?.source, r?.engine]),
  ].map(text).join('\n').toLowerCase()
}

function Bullets({ title, items }) {
  const list = arr(items).filter((x) => text(x).trim())
  if (!list.length) return null
  return (
    <div className="rx-block">
      <p className="rx-label">{title}</p>
      <ul>{list.map((x, i) => <li key={i}>{text(x)}</li>)}</ul>
    </div>
  )
}

function Thresholds({ items }) {
  const list = arr(items)
  if (!list.length) return null
  return (
    <div className="rx-block">
      <p className="rx-label">Thresholds &amp; conditions</p>
      <ul>
        {list.map((t, i) => (
          <li key={i}>
            {text(t?.label)}{t?.value != null && t?.value !== '' ? <>: <b>{text(t.value)}</b></> : null}
            {' '}
            <span className="rx-tag">{t?.standard ? 'WCAG requirement' : 'ACP heuristic'}</span>
            {' '}
            <span className="rx-tag">
              {t?.configurable ? `Configurable: ${text(t?.setting) || 'setting not named'}` : 'Fixed in code'}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function FormatCell({ fmt, cell, rubric }) {
  const status = cell?.status
  const rules = arr(cell?.rules)
  const methods = [...new Set(rules.map((r) => r?.method).filter(Boolean))]
  const thresholdSources = arr(cell?.thresholds).filter((t) => t?.source)
  return (
    <section className="rx-format">
      <h5 className="rx-format-head">
        {fmtLabel(fmt)} <span className="rx-status">{statusLabel(status)}</span>
      </h5>
      {STATUS_NOTE[status] && <p className="muted rx-note">{STATUS_NOTE[status]}</p>}
      {(cell?.assessment_lane || cell?.remediation_lane) && (
        <p className="rx-lanes">
          {cell?.assessment_lane && <>Assessment: <b>{ASSESS_LANE[cell.assessment_lane] || text(cell.assessment_lane)}</b></>}
          {cell?.assessment_lane && cell?.remediation_lane && ' · '}
          {cell?.remediation_lane && <>Remediation: <b>{REMEDIATE_LANE[cell.remediation_lane] || text(cell.remediation_lane)}</b></>}
        </p>
      )}
      {methods.length > 0 && (
        <p className="rx-lanes">
          Method: {methods.map((m) => <span key={m} className="rx-tag">{METHOD_LABEL[m] || text(m)}</span>)}
        </p>
      )}
      <Bullets title="What it checks" items={cell?.checks} />
      <Thresholds items={cell?.thresholds} />
      <Bullets title="What it does not check" items={cell?.does_not_check} />
      {text(cell?.evidence).trim() && (
        <div className="rx-block"><p className="rx-label">Evidence</p><p>{text(cell.evidence)}</p></div>
      )}
      {text(cell?.fix).trim() && (
        <div className="rx-block"><p className="rx-label">Fix</p><p>{text(cell.fix)}</p></div>
      )}
      {(rules.length > 0 || thresholdSources.length > 0) && (
        <details className="rx-impl">
          <summary>Implementation details ({fmtLabel(fmt)})</summary>
          {rules.length > 0 && (
            <ul>
              {rules.map((r, i) => (
                <li key={`${text(r?.id)}-${i}`}>
                  <code>{text(r?.id)}</code>
                  {' — '}{METHOD_LABEL[r?.method] || text(r?.method) || 'Method not stated'}
                  {' · '}{FIX_LABEL[r?.fix] || text(r?.fix) || 'Fix not stated'}
                  {rubric.disabled.has(String(r?.id)) && <> · <b>Disabled in the active rubric</b></>}
                  <br />
                  <span className="muted">Engine: </span><code>{text(r?.engine) || 'acp'}</code>
                  <span className="muted"> · Source: </span><code>{text(r?.source) || 'not recorded'}</code>
                </li>
              ))}
            </ul>
          )}
          {thresholdSources.length > 0 && (
            <>
              <p className="rx-label">Threshold sources</p>
              <ul>
                {thresholdSources.map((t, i) => (
                  <li key={i}>{text(t?.label)}: <code>{text(t.source)}</code></li>
                ))}
              </ul>
            </>
          )}
        </details>
      )}
    </section>
  )
}

function Criterion({ c, cells, open, onToggle }) {
  const base = idFor(c.sc)
  const rubric = rubricInfo(c)
  const shown = cells.filter(([, cell]) => cell?.status !== 'not_applicable')
  const na = cells.filter(([, cell]) => cell?.status === 'not_applicable').map(([f]) => fmtLabel(f))
  return (
    <li className="rx-criterion">
      <h4 id={`${base}-h`} className="rx-criterion-head">
        {text(c.sc)} {text(c.name)} <span className="muted rx-level">Level {text(c.level) || 'not stated'}</span>
      </h4>
      {rubric.disabledAll && <p className="rx-note"><b>Disabled in the active rubric.</b></p>}
      <p className="rx-summary">
        {shown.map(([f, cell], i) => (
          <span key={f}>{i > 0 && ' · '}{fmtLabel(f)}: <b>{statusLabel(cell?.status)}</b></span>
        ))}
        {shown.length > 0 && na.length > 0 && ' · '}
        {na.length > 0 && <span>Not applicable to: {na.join(', ')}</span>}
      </p>
      <button type="button" className="ghost small rx-toggle" aria-expanded={open}
              aria-controls={`${base}-details`} onClick={onToggle}>
        {open ? 'Hide details' : 'Show details'}<span className="sr-only"> for {text(c.sc)} {text(c.name)}</span>
      </button>
      <div id={`${base}-details`} hidden={!open} className="rx-details">
        {open && (
          <>
            {rubric.disabled.size > 0 && (
              <p className="rx-note">
                The active rubric disables {rubric.disabled.size === 1 ? 'rule' : 'rules'}{' '}
                {[...rubric.disabled].map((id, i) => <span key={id}>{i > 0 && ', '}<code>{id}</code></span>)}.
              </p>
            )}
            {shown.length === 0 && <p className="muted">No format with a status other than not applicable.</p>}
            {shown.map(([f, cell]) => <FormatCell key={f} fmt={f} cell={cell} rubric={rubric} />)}
            {na.length > 0 && <p className="muted rx-note">Not applicable to: {na.join(', ')}.</p>}
          </>
        )}
      </div>
    </li>
  )
}

function ConfigurableToday({ settings }) {
  const list = arr(settings)
  return (
    <section className="rx-section" aria-labelledby="rx-configurable-h">
      <h4 id="rx-configurable-h">Configurable today</h4>
      <p className="rx-note">
        Thresholds are shown read-only. Changing them is not available in this version; the settings
        below are the only values that change rule behaviour today, and each is changed where noted.
      </p>
      {list.length === 0 ? <p className="muted">No configurable settings were reported.</p> : (
        <ul className="rx-settings">
          {list.map((s, i) => {
            const affects = Array.isArray(s?.affects) ? s.affects.map(text).join(', ') : text(s?.affects)
            return (
              <li key={text(s?.key) || i}>
                <b>{text(s?.label) || text(s?.key) || 'Unnamed setting'}</b>
                {s?.key && <> <code>{text(s.key)}</code></>}
                {s?.value !== undefined && <>: {text(s.value) || 'empty'}</>}
                <div className="muted rx-setting-meta">
                  {[
                    s?.scope && `Scope: ${text(s.scope)}`,
                    s?.editable_by && `Changed by: ${text(s.editable_by)}`,
                    s?.where && `Where: ${text(s.where)}`,
                    affects && `Affects: ${affects}`,
                  ].filter(Boolean).join(' · ')}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function Provenance({ p }) {
  const rubric = p?.rubric || {}
  const sources = arr(p?.sources)
  return (
    <footer className="rx-provenance muted" aria-label="Provenance">
      <p>
        Rule catalog version {text(p?.rule_catalog_version) || 'not recorded'}
        {' · '}Rubric {text(rubric.name) || 'not recorded'}{rubric.version ? ` v${text(rubric.version)}` : ''}
        {rubric.hash ? <> (hash <code>{text(rubric.hash).slice(0, 12)}</code>)</> : null}
        {' · '}Built from commit {p?.commit ? <code>{shortSha(text(p.commit))}</code> : 'not recorded'}
      </p>
      {sources.length > 0 && (
        <p>Derived from: {sources.map((s, i) => <span key={i}>{i > 0 && ', '}<code>{text(s)}</code></span>)}</p>
      )}
    </footer>
  )
}

export default function RuleExplanations({ me = null } = {}) {   // eslint-disable-line no-unused-vars
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [format, setFormat] = useState('')
  const [status, setStatus] = useState('')
  const [expanded, setExpanded] = useState({})

  const load = useCallback(() => {
    setLoading(true); setError('')
    getRuleExplanations()
      .then((d) => setData(d || {}))
      .catch((e) => setError(e?.message || 'request failed'))
      .finally(() => setLoading(false))
  }, [])
  useEffect(() => { load() }, [load])

  const criteria = arr(data?.criteria)
  const formats = useMemo(() => {
    const listed = arr(data?.formats).map(String)
    if (listed.length) return listed
    const seen = new Set()
    for (const c of arr(data?.criteria)) for (const f of Object.keys(c?.formats || {})) seen.add(f)
    return [...seen]
  }, [data])

  const q = query.trim().toLowerCase()
  const results = useMemo(() => criteria.map((c) => {
    let cells = formats.filter((f) => c?.formats?.[f]).map((f) => [f, c.formats[f]])
    // Formats the payload has for this criterion but did not list at the top still render.
    for (const f of Object.keys(c?.formats || {})) if (!formats.includes(f)) cells.push([f, c.formats[f]])
    if (format) cells = cells.filter(([f]) => f === format)
    if (status) cells = cells.filter(([, cell]) => cell?.status === status)
    if (!cells.length) return null
    if (q) {
      const head = [c?.sc, c?.name, c?.level && `level ${c.level}`].map(text).join('\n').toLowerCase()
      if (!head.includes(q)) {
        cells = cells.filter(([f, cell]) => cellHaystack(f, cell).includes(q))
        if (!cells.length) return null
      }
    }
    return { c, cells }
  }).filter(Boolean), [criteria, formats, format, status, q])

  if (loading && !data) return <p role="status" className="muted">Loading rule explanations…</p>
  if (error) {
    return (
      <div role="alert" className="rx-error">
        <p>Could not load rule explanations: {error}</p>
        <button type="button" className="ghost small" onClick={load}>Retry</button>
      </div>
    )
  }
  if (data?.available === false) {
    return (
      <div className="rx-panel">
        <h3 style={{ marginTop: 0 }}>Rule explanations</h3>
        <p role="status" className="rx-note">
          Rule explanations are not available in the demo build. They are generated by the API from
          the rule code it runs, and this build has no API; use a build served by the real API to read them.
        </p>
      </div>
    )
  }

  const counts = data?.counts || {}
  const byStatus = counts.by_status || {}
  const filtered = !!(q || format || status)
  const clear = () => { setQuery(''); setFormat(''); setStatus('') }

  return (
    <div className="rx-panel">
      <h3 style={{ marginTop: 0 }}>Rule explanations</h3>
      <p className="muted rx-intro">
        What ACP checks for each WCAG success criterion in each file format, the thresholds it decides
        by, what it cannot see, and what a fix can apply. This page is read-only for everyone.
      </p>
      {(counts.criteria != null || Object.keys(byStatus).length > 0) && (
        <p className="rx-note">
          {counts.criteria != null && <>{text(counts.criteria)} criteria</>}
          {counts.cells != null && <> · {text(counts.cells)} criterion–format cells</>}
          {Object.keys(byStatus).length > 0 && <> · {[...STATUS_ORDER.filter((s) => s in byStatus), ...Object.keys(byStatus).filter((s) => !STATUS_ORDER.includes(s))]
            .map((s) => `${statusLabel(s)}: ${text(byStatus[s])}`).join(' · ')}</>}
        </p>
      )}

      <div className="rx-filters" role="search" aria-label="Filter rule explanations">
        <label className="rx-field">
          <span>Search criteria</span>
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="SC number, name, rule ID or text" aria-describedby="rx-count" />
        </label>
        <label className="rx-field">
          <span>Format</span>
          <select value={format} onChange={(e) => setFormat(e.target.value)}>
            <option value="">All formats</option>
            {formats.map((f) => <option key={f} value={f}>{fmtLabel(f)}</option>)}
          </select>
        </label>
        <label className="rx-field">
          <span>Status</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {STATUS_ORDER.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
          </select>
        </label>
      </div>
      <p id="rx-count" className="rx-count" aria-live="polite" role="status">
        {filtered ? `Showing ${results.length} of ${criteria.length} criteria` : `${criteria.length} criteria`}
      </p>

      {results.length === 0 ? (
        <div className="rx-empty">
          <p>{criteria.length === 0 ? 'No criteria were reported.' : 'No criteria match these filters.'}</p>
          {filtered && <button type="button" className="ghost small" onClick={clear}>Clear search and filters</button>}
        </div>
      ) : (
        <ul className="rx-list">
          {results.map(({ c, cells }) => {
            const key = text(c.sc)
            const open = expanded[key] ?? !!q
            return (
              <Criterion key={key} c={c} cells={cells} open={open}
                         onToggle={() => setExpanded((m) => ({ ...m, [key]: !open }))} />
            )
          })}
        </ul>
      )}

      <ConfigurableToday settings={data?.settings} />
      <Provenance p={data?.provenance} />
    </div>
  )
}
