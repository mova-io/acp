import { describe, expect, it } from 'vitest'
import { releaseErrorCopy } from './releaseErrorCopy.js'

const RAW_404 = "HTTPStatusError: Client error '404 Not Found' for url 'https://graph.microsoft.com/v1.0/drives/b!sensitive/items/01secret/content'"

describe('releaseErrorCopy', () => {
  it('sanitizes an unclassified provider 404 without guessing which resource is missing', () => {
    const copy = releaseErrorCopy({ status: 404, message: RAW_404 }, 'SharePoint')
    expect(copy.message).toMatch(/resource.*missing or inaccessible/i)
    expect(copy.retryable).toBe(false)
    expect(copy.diagnosticCode).toBe('release_resource_unavailable')
    expect(copy.message).not.toMatch(/graph\.microsoft\.com|b!sensitive|01secret|HTTPStatusError/)
  })

  it('only diagnoses the destination when the service supplies a destination code', () => {
    const copy = releaseErrorCopy({ status: 404, detail: { code: 'release_destination_not_found' } }, 'SharePoint')
    expect(copy.message).toMatch(/destination is missing or inaccessible/i)
    expect(copy.diagnosticCode).toBe('destination_not_found')
    expect(copy.retryable).toBe(false)
  })

  it('only recommends reconnecting for a 401 authorization failure', () => {
    expect(releaseErrorCopy({ status: 401 }, 'SharePoint').message).toMatch(/Reconnect/)
    const denied = releaseErrorCopy({ status: 403 }, 'SharePoint')
    expect(denied.message).toMatch(/permission|write access/i)
    expect(denied.message).not.toMatch(/Reconnect/)
    expect(denied.retryable).toBe(false)
    expect(releaseErrorCopy({ status: 403, message: 'Folder not found' }, 'SharePoint').diagnosticCode)
      .toBe('destination_write_access_required')
    expect(releaseErrorCopy({ status: 404 }, 'SharePoint').message).not.toMatch(/Reconnect/)
  })

  it('does not retry a structured destination permission block', () => {
    const copy = releaseErrorCopy({ status: 409, detail: { code: 'release_destination_not_ready', preflight: { message: 'Folder permission denied' } } }, 'SharePoint')
    expect(copy.diagnosticCode).toBe('destination_write_access_required')
    expect(copy.retryable).toBe(false)
    expect(copy.message).not.toMatch(/Folder permission denied/)
  })

  it('does not invent a missing-folder diagnosis for a generic 404', () => {
    const copy = releaseErrorCopy({ status: 404, message: 'API route not found' }, 'SharePoint')
    expect(copy.diagnosticCode).toBe('release_resource_unavailable')
    expect(copy.message).not.toMatch(/moved|deleted/i)
    expect(copy.retryable).toBe(false)
  })

  it('does not mistake a source content 404 for a missing saved destination', () => {
    const copy = releaseErrorCopy({ status: 404,
      message: 'https://graph.microsoft.com/v1.0/drives/source/items/file/content not found' }, 'SharePoint')
    expect(copy.diagnosticCode).toBe('release_resource_unavailable')
    expect(copy.message).not.toMatch(/saved SharePoint destination/i)
  })

  it('allows a retry for temporary transport failures and safe unknown failures', () => {
    expect(releaseErrorCopy(new Error('504 Gateway Timeout')).retryable).toBe(true)
    expect(releaseErrorCopy(new Error('unexpected provider response')).retryable).toBe(true)
  })
})
