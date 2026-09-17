import { useEffect, useState } from 'react'
import { remediationProgressFacts } from './remediationProgressFacts.js'

export default function RemediationProgressFacts({ snapshot }) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    setNow(Date.now())
    if (snapshot?.terminal) return undefined
    const timer = setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(timer)
  }, [snapshot?.run_id, snapshot?.batch_id, snapshot?.terminal])
  const facts = remediationProgressFacts(snapshot, now)
  return <section aria-label="Recorded file progress">
    <dl style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 16px', margin: '8px 0', fontSize: 12 }}>
      {[
        ['Files finished processing', facts.finished], ['Files processing', facts.processing],
        ['Files waiting', facts.waiting], ['Files routed for review or skipped', facts.routed],
      ].map(([label, value]) => <div key={label} style={{ display: 'flex', gap: 5 }}><dt className="muted">{label}</dt><dd style={{ margin: 0, fontWeight: 650 }}>{value === null ? 'Unavailable' : value.toLocaleString()}</dd></div>)}
    </dl>
    <p className="muted" style={{ fontSize: 12, margin: '4px 0' }}>Finished includes unsuccessful processing. These file counts do not mean fixes were verified or copies published.</p>
    <p className="muted" style={{ fontSize: 12, margin: '4px 0' }}>
      {snapshot?.terminal ? <>Started: {facts.started ? <time dateTime={facts.started}>{new Date(facts.started).toLocaleString()}</time> : 'Unavailable'}</>
        : <>Elapsed: {facts.elapsed ?? 'Unavailable'}</>}
      {' · '}{snapshot?.terminal ? 'Last recorded progress: ' : 'Last meaningful progress: '}
      {facts.material ? snapshot?.terminal ? <time dateTime={facts.material}>{new Date(facts.material).toLocaleString()}</time> : `${facts.progressAge} ago` : 'Unavailable'}
    </p>
  </section>
}
