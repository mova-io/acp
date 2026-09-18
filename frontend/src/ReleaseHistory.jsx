import { useEffect, useState } from 'react'
import { getReleaseManifest, listReleaseHistory } from './api.js'

function downloadJson(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

const when = (value) => value ? new Date(value).toLocaleString() : 'Time unavailable'
const outcome = (release) => [
  `${release.published || 0} released`,
  release.failed ? `${release.failed} failed` : null,
  release.remaining ? `${release.remaining} remaining` : null,
].filter(Boolean).join(' · ')

// Prefer the server's publication verdict over the raw execution status when it is present: a
// release whose copy was corrected after publication is not "completed" from the reader's view.
const PUBLICATION_STATUS = { current: 'Published (current copy)', out_of_date: 'Published copy out of date',
  identity_unknown: 'Published version unconfirmed', publishing: 'Publishing', attention: 'Needs attention', not_published: 'Not published' }
const DOCUMENT_PUBLICATION = { out_of_date: 'Published copy out of date', identity_unknown: 'Published version unconfirmed', publishing: 'Publishing updated copy' }
const releaseStatusLabel = (release) => PUBLICATION_STATUS[release.publication?.state] || release.status || 'unknown'

export default function ReleaseHistory({ refreshKey, loadHistory = listReleaseHistory,
  loadManifest = getReleaseManifest }) {
  const [state, setState] = useState({ loading: true, releases: [], error: '' })
  const [query, setQuery] = useState('')
  const load = () => {
    setState((old) => ({ ...old, loading: true, error: '' }))
    loadHistory(50).then((result) => setState({ loading: false, releases: result?.releases || [], error: '' }))
      .catch((error) => setState((old) => ({ ...old, loading: false,
        error: error?.message || 'Release history could not be loaded.' })))
  }
  useEffect(load, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const manifest = async (release) => {
    try {
      const value = await loadManifest(release.scan_id)
      downloadJson(value, `release-${release.release_id}-manifest.json`)
    } catch (error) {
      setState((old) => ({ ...old, error: error?.message || 'The manifest could not be downloaded.' }))
    }
  }
  const needle = query.trim().toLocaleLowerCase()
  const visible = needle ? state.releases.filter((release) => [
    release.release_id, release.scan_id, release.actor, release.source, release.folder_name,
    ...(release.destinations || []).flatMap((item) => [item.location, item.folder_name, item.provider]),
    ...(release.documents || []).flatMap((item) => [item.file, item.destination_path,
      item.status, item.verification, item.failure_category, item.explanation]),
  ].some((value) => String(value || '').toLocaleLowerCase().includes(needle))) : state.releases

  return (
    <details className="panel release-history">
      <summary className="release-record__summary">
        <span><b>Release history</b><small>Executions across all scans</small></span>
        <span>{state.loading ? 'Loading…' : `${state.releases.length} release${state.releases.length === 1 ? '' : 's'}`}</span>
      </summary>
      <div className="release-record__body">
        {state.releases.length > 0 && <label className="release-history__search">
          <span>Search release history</span>
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)}
                 placeholder="Folder, actor, file, destination, or execution ID" />
        </label>}
        {state.error && <div className="release-recovery" role="alert"><div><b>History needs attention</b><p>{state.error}</p></div><button className="ghost small" onClick={load}>Retry</button></div>}
        {!state.loading && !state.error && state.releases.length === 0 && <p className="muted">No releases have been created yet.</p>}
        {!state.loading && !state.error && state.releases.length > 0 && visible.length === 0 && <p className="muted">No releases match “{query}”.</p>}
        {visible.map((release) => (
          <details key={release.release_id} className="release-history__execution">
            <summary>
              <span><b>{release.folder_name || 'Release'}</b><small>{when(release.created_at)} · {release.actor || 'Actor unavailable'}</small></span>
              <span>{outcome(release)}</span>
            </summary>
            <dl className="release-history__facts">
              <dt>Execution</dt><dd><code>{release.release_id}</code></dd>
              <dt>Source</dt><dd>{release.source || 'Connected source'}</dd>
              <dt>Status</dt><dd>{releaseStatusLabel(release)}</dd>
              <dt>Updated</dt><dd>{when(release.updated_at)}</dd>
              <dt>Destination</dt><dd>{(release.destinations || []).length ? (release.destinations || []).map((item, index) => (
                <span key={`${item.location}-${index}`}>{index > 0 && ' · '}{item.folder_url
                  ? <a href={item.folder_url} target="_blank" rel="noopener noreferrer">{item.location || item.folder_name || item.provider} ↗</a>
                  : item.location || item.folder_name || item.provider}</span>
              )) : 'Destination unavailable'}</dd>
            </dl>
            <button className="ghost small" onClick={() => manifest(release)}>Download manifest</button>
            <div className="release-history__documents">
              {(release.documents || []).map((document, index) => (
                <div key={`${document.file}-${index}`} className={document.status === 'failed' ? 'release-history__document release-history__document--failed' : 'release-history__document'}>
                  <b>{document.file}</b>
                  <span>{document.status === 'failed' ? `Failed · ${document.explanation || document.failure_category || 'Retry this copy'}`
                    : `${DOCUMENT_PUBLICATION[document.publication_state] ? `${DOCUMENT_PUBLICATION[document.publication_state]} · ` : ''}${document.created ? 'Created' : 'Reused'} · ${document.verification || 'verification unavailable'}${document.checksum ? ` · SHA-256 ${document.checksum}` : ''}`}</span>
                  {document.destination_path && <code>{document.destination_path}</code>}
                  {document.released_url && <a href={document.released_url} target="_blank" rel="noopener noreferrer">Open released copy ↗</a>}
                </div>
              ))}
            </div>
          </details>
        ))}
      </div>
    </details>
  )
}
