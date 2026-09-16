const AUTH = /\b(401|403)\b|unauthori[sz]ed|forbidden|invalid[_ -]?grant|token[_ -]?expired/i
const MISSING = /\b404\b|not found/i
const NETWORK = /network|timeout|timed out|fetch failed|econnreset|502|503|504|gateway/i

const rawText = (error) => [error?.detail?.message, error?.detail?.preflight?.message, error?.message]
  .filter(Boolean).join(' ')

export function releaseErrorCopy(error, providerName = 'the destination') {
  const text = rawText(error)
  const status = Number(error?.status || error?.detail?.status || 0)
  if (status === 404 || MISSING.test(text)) return {
    message: `The saved ${providerName} destination could not be found. It may have been moved or deleted. Choose or restore the destination before publishing again.`,
    retryable: false,
    diagnosticCode: 'destination_not_found',
  }
  if ([401, 403].includes(status) || AUTH.test(text)) return {
    message: `The ${providerName} connection needs attention. Reconnect it in Sources, then return here to continue the saved release.`,
    retryable: false,
    diagnosticCode: 'destination_authorization_required',
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
