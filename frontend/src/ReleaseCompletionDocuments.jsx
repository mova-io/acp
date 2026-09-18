import { useId, useState } from 'react'
import './release-completion-documents.css'
import { savedCopyVerificationStatus } from './savedCopyVerificationStatus.js'

const PUBLICATION_LABELS = { ready: 'Ready to publish', released: 'Published', delivering: 'Delivering', applying: 'Applying fixes', failed: 'Failed', out_of_date: 'Published copy out of date', unconfirmed: 'Published version unconfirmed' }
// A copy delivered before a later correction: the link opens what was delivered, named as earlier.
const EARLIER_COPY = new Set(['out_of_date', 'unconfirmed'])

export default function ReleaseCompletionDocuments({ files = [], states = [], progressDocuments, results = {}, urls = {}, filter = 'all', onFilter, readOnly, publishing, onRetry, reportsByFile = {}, reportActions, receipt, coveredFiles = [], scopeId, revision }) {
  const id = useId()
  const [search, setSearch] = useState('')
  const [publication, setPublication] = useState('all')
  const [format, setFormat] = useState('all')
  const formats = [...new Set(files.map(file => file.file.split('.').pop().toUpperCase()))].sort()
  const [verification, setVerification] = useState('all')
  const rows = files.map((file, index) => ({ file, state: states[index] || { status: 'unknown', label: 'Status unavailable' }, result: results[file.file], verificationState: savedCopyVerificationStatus(file, results[file.file]) }))
  const queues = rows.filter(({ file, state }) => filter === 'all' || (progressDocuments
    ? progressDocuments.some(document => document.file === file.file && document.progressState === filter)
    : filter === 'attention' ? !['ready', 'released', 'delivering', 'applying'].includes(state.status) : state.status === filter))
  const visible = queues.filter(({ file, state, verificationState }) => file.file.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
    && (format === 'all' || file.file.split('.').pop().toUpperCase() === format)
    && (publication === 'all' || state.status === publication)
    && (verification === 'all' || verificationState.key === verification))
  const statuses = [...new Set(rows.map(({ state }) => state.status))]
  const rowHasAction = ({ file, state, result }) => Boolean(
    ((state.status === 'released' || EARLIER_COPY.has(state.status)) && (urls[file.file] || result?.published_url))
    || (!coveredFiles.includes(file.file) && result?.status === 'failed' && ['ready', 'failed'].includes(state.status))
    || reportsByFile[file.file])
  const showActions = visible.some(rowHasAction)
  const filtered = format !== 'all' || search || publication !== 'all' || verification !== 'all' || filter !== 'all'
  const clear = () => { setFormat('all'); setSearch(''); setPublication('all'); setVerification('all'); if (filter !== 'all') onFilter?.('all') }
  return <section className="panel release-completion-documents" aria-label="Publication outcomes" data-scope-id={scopeId} data-snapshot-revision={revision}>
    {receipt}
    {reportActions}
    <div className="release-documents-filters">
      <label htmlFor={`${id}-search`}>Search filenames<input id={`${id}-search`} type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search files…" /></label>
      <label htmlFor={`${id}-publication`}>Publication status<select id={`${id}-publication`} value={publication} onChange={event => setPublication(event.target.value)}><option value="all">All publication statuses</option>{statuses.map(status => <option key={status} value={status}>{PUBLICATION_LABELS[status] || rows.find(row => row.state.status === status).state.label || status}</option>)}</select></label>
      <label htmlFor={`${id}-format`}>File type<select id={`${id}-format`} value={format} onChange={event => setFormat(event.target.value)}><option value="all">All file types</option>{formats.map(value => <option key={value}>{value}</option>)}</select></label>
      <label htmlFor={`${id}-verification`}>Verification status<select id={`${id}-verification`} value={verification} onChange={event => setVerification(event.target.value)}><option value="all">All verification statuses</option><option value="passed">Selected checks passed</option><option value="unconfirmed">Awaiting verification</option><option value="remaining">Checked; findings remain</option><option value="failed">Verification failed</option></select></label>
      {filtered && <button className="ghost" onClick={clear}>Clear filters</button>}
    </div>
    <p className="release-documents-count" role="status">{visible.length} of {rows.length} documents shown{filter !== 'all' ? ' · Selected file queue' : ''}</p>
    <div className="release-documents-scroll" role="region" aria-label="Publication documents" tabIndex={0}><table><thead><tr><th scope="col">Document</th><th scope="col">Corrected copy</th><th scope="col">Saved-copy verification</th><th scope="col">Publication and remaining work</th>{showActions && <th scope="col" className="release-documents-action">Action</th>}</tr></thead>
      <tbody>{visible.map(({ file, state, result, verificationState }) => <tr key={file.file}>
        <th scope="row">{file.file}</th>
        <td>{file.remediated_at ? 'Saved in ACP' : 'Not saved yet'}</td>
        <td>{verificationState.label}<small style={{ display: 'block', marginTop: 6 }}>{verificationState.reason}</small></td>
        <td><strong>{state.label}</strong><p>{state.reason}</p>{result?.published_at && <small>Receipt recorded: {new Date(result.published_at).toLocaleString()}</small>}</td>
        {showActions && <td className="release-documents-action">{state.status === 'released' && (urls[file.file] || result?.published_url) ? <a href={urls[file.file] || result.published_url} target="_blank" rel="noopener noreferrer">Open published copy</a>
          : EARLIER_COPY.has(state.status) && (urls[file.file] || result?.published_url) ? <a href={urls[file.file] || result.published_url} target="_blank" rel="noopener noreferrer">Open earlier published copy</a>
          : !coveredFiles.includes(file.file) && result?.status === 'failed' && ['ready', 'failed'].includes(state.status) ? <button disabled={readOnly || publishing} onClick={() => onRetry([file.file])}>Retry delivery</button> : null}{reportsByFile[file.file]}</td>
        }
      </tr>)}</tbody>
    </table></div>
    {!visible.length && <p>{rows.length ? 'No documents match these filters.' : 'No documents to show yet.'}</p>}
  </section>
}
