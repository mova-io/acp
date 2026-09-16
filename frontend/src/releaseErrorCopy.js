const AUTH = /\b401\b|unauthori[sz]ed|invalid[_ -]?grant|token[_ -]?expired/i
const ACCESS = /\b403\b|forbidden|permission denied|access denied|insufficient privileges/i
const MISSING = /\b404\b|not found/i
const DESTINATION = /^(release_destination_not_found|destination_not_found|release_destination_unavailable)$/i
const NETWORK = /network|timeout|timed out|fetch failed|econnreset|502|503|504|gateway/i

const rawText = (error) => [error?.detail?.message, error?.detail?.preflight?.message, error?.message]
  .filter(Boolean).join(' ')

export function releaseErrorCopy(error, providerName = 'the destination') {
  const text = rawText(error)
  const status = Number(error?.status || error?.detail?.status || 0)
  const code = String(error?.detail?.code || error?.code || '')
  if (status === 401) return {
    message: `The ${providerName} connection needs attention. Reconnect it in Sources, then return here to continue the saved release.`,
    retryable: false,
    diagnosticCode: 'destination_authorization_required',
  }
  if (status === 403) return {
    message: `ACP does not have permission to publish to the saved ${providerName} destination. Choose a destination with write access or ask its owner for access.`,
    retryable: false,
    diagnosticCode: 'destination_write_access_required',
  }
  if (AUTH.test(text)) return {
    message: `The ${providerName} connection needs attention. Reconnect it in Sources, then return here to continue the saved release.`,
    retryable: false,
    diagnosticCode: 'destination_authorization_required',
  }
  if (ACCESS.test(text)) return {
    message: `ACP does not have permission to publish to the saved ${providerName} destination. Choose a destination with write access or ask its owner for access.`,
    retryable: false,
    diagnosticCode: 'destination_write_access_required',
  }
  if (status === 404 || MISSING.test(text)) {
    const destinationMissing = DESTINATION.test(code)
    return {
      message: destinationMissing
        ? `The saved ${providerName} destination is missing or inaccessible. Choose a destination you can open and write to before publishing again.`
        : 'A resource required for this release is missing or inaccessible. Check the saved destination and release status before trying again.',
      retryable: false,
      diagnosticCode: destinationMissing ? 'destination_not_found' : 'release_resource_unavailable',
    }
  }
  if (NETWORK.test(text)) return {
    message: `ACP could not reach ${providerName}. This is usually temporary.`,
    retryable: true,
    diagnosticCode: 'destination_temporarily_unreachable',
  }
  return {
    message: 'The release service did not complete the request. Completed copies remain safe.',
    retryable: true,
    diagnosticCode: 'release_request_failed',
  }
}
