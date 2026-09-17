import { useState } from 'react'

// Navigation and authoritative status reads only. Publishing retries retain their
// existing admission checks and an accepted plan never changes destination here.
export default function ReleaseRecoveryActions({ error, disabled, destinationLocked,
  destinationPicker, onReconnect, onCheckStatus }) {
  const [checking, setChecking] = useState(false)
  const [checked, setChecked] = useState(false)
  const code = error?.diagnosticCode
  const reconnect = code === 'destination_authorization_required'
  const destination = ['destination_not_found', 'destination_write_access_required', 'release_resource_unavailable'].includes(code)
  if (!reconnect && !destination) return null
  const check = async () => {
    if (disabled || checking) return
    setChecking(true); setChecked(false)
    try { const refreshed = await onCheckStatus?.(); setChecked(refreshed === true ? 'success' : 'unavailable') }
    catch { setChecked('unavailable') }
    finally { setChecking(false) }
  }
  return <div className="release-recovery-next-step">
    {reconnect && <button type="button" className="ghost" disabled={disabled} onClick={onReconnect}>Reconnect source in Sources</button>}
    {destination && <>
      {destinationLocked ? <p>This release keeps its approved destination. Restore that folder or its write access, then check delivery status. Choosing a different folder requires a new release plan.</p>
        : <p>Choose a folder you can write to. Selection does not publish any files; review the destination before retrying.</p>}
      {!destinationLocked && !disabled && destinationPicker}
      <button type="button" className="ghost" disabled={disabled || checking} onClick={check}>{checking ? 'Checking delivery status…' : 'Check delivery status'}</button>
      {checked && <p role="status">{checked === 'success' ? 'Status refreshed. No publishing retry was sent.' : 'Status is still unavailable. No publishing retry was sent.'}</p>}
    </>}
  </div>
}
