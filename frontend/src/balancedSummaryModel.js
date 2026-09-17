import { documentRow } from './assessMetrics.js'
import { remediationCategory, aiAppliedUnverified, REMEDIATION_CATEGORIES } from './remediationCategories.js'
import { assessmentEligible } from './estateFunnel.js'
import { runScopeCriteria } from './runScopeCriteria.js'
import { SC_NAME } from './wcagCatalog.js'

export const FORMAT_NAMES = { pdf: 'PDF', docx: 'Word', pptx: 'PowerPoint', xlsx: 'Excel', html: 'HTML', image: 'Images', av: 'Video / audio', other: 'Other' }
export const COVERAGE_STATES = [['assessed', 'Assessed'], ['pending', 'Awaiting assessment'], ['ineligible', 'Ineligible'], ['blocked', 'Could not assess'], ['unknown', 'Not recorded']]
export const FINDING_GROUPS = ['Structure', 'Contrast', 'Text alternatives', 'Other']
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null
export const measuredFile = file => file.status !== 'error' && (file.score != null || ['analysed', 'assessed', 'uncertain'].includes(file.status) || file.issues?.length > 0)
export function formatOf(file) {
  const type = String(file.format || file.type || file.file?.split('.').pop() || 'other').toLowerCase()
  if (type === 'htm') return 'html'
  if (FORMAT_NAMES[type]) return type
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'tif', 'tiff', 'bmp'].includes(type)) return 'image'
  if (['mp4', 'mov', 'avi', 'mp3', 'wav', 'm4a', 'webm'].includes(type)) return 'av'
  return 'other'
}
export function findingGroup(sc) {
  if (sc === '1.1.1') return 'Text alternatives'
  if (['1.4.3', '1.4.6', '1.4.11'].includes(sc)) return 'Contrast'
  if (sc?.startsWith('1.3.')) return 'Structure'
  return 'Other'
}
// One row per actual criterion, largest first. Every input finding lands in exactly one row, so the
// rows always sum to the parent. A missing criterion or an uncatalogued title is said so, not guessed.
export function criterionBreakdown(findings = []) {
  const by = new Map()
  for (const finding of findings) {
    const sc = typeof finding.sc === 'string' ? finding.sc.trim() : ''
    if (!by.has(sc)) by.set(sc, [])
    by.get(sc).push(finding)
  }
  return [...by].map(([sc, rows]) => ({ sc, rows,
    label: !sc ? 'Criterion not recorded' : `${sc} ${SC_NAME[sc] || '· title not in catalog'}` }))
    .sort((a, b) => b.rows.length - a.rows.length || !a.sc - !b.sc || a.sc.localeCompare(b.sc, 'en', { numeric: true }))
}
// Only a finding's recorded first-seen timestamp can establish its age. Never use the
// document creation/modification time or the scan completion time as a substitute.
export function reviewAge(finding, now) {
  const raw = finding.first_seen_at || finding.first_seen
  const at = typeof raw === 'string' && raw.trim() ? Date.parse(raw) : NaN
  if (!Number.isFinite(at) || at > now) return 'unknown'
  const days = (now - at) / 86400000
  return days < 7 ? 'week' : days <= 30 ? 'month' : 'older'
}

export function balancedSummaryModel({ run, files = [], inventory = null, cap, assessment, now = Date.now() } = {}) {
  const summary = run?.scope?.inventory
  const measured = files.filter(measuredFile)
  const criteria = runScopeCriteria(run)
  const options = { cap, assessment, ...(criteria ? { criteria } : {}) }
  const findings = measured.flatMap(file => (documentRow(file, options)?.findings || []).map(finding => {
    const accounting = remediationCategory(finding)
    // Split AI-applied out of Pending for presentation, so the visible columns stay disjoint.
    const category = accounting === 'applied' && aiAppliedUnverified(finding) ? 'ai_applied' : accounting
    return { ...finding, category, file, department: file.department?.trim() || 'Not recorded', group: findingGroup(finding.sc) }
  }))
  const tags = Object.fromEntries(REMEDIATION_CATEGORIES.map(([key]) => [key, 0]))
  const groups = Object.fromEntries(FINDING_GROUPS.map(key => [key, []]))
  const departments = new Map()
  const age = { week: [], month: [], older: [], unknown: [] }
  for (const finding of findings) {
    tags[finding.category]++
    groups[finding.group].push(finding)
    if (!departments.has(finding.department)) departments.set(finding.department, [])
    departments.get(finding.department).push(finding)
    if (['approval', 'suggestion', 'manual'].includes(finding.category)) age[reviewAge(finding, now)].push(finding)
  }

  // The inventory aggregate is authoritative for format totals. A missing per-file inventory
  // leaves residual coverage explicitly unknown, not automatically pending or ineligible.
  const formats = new Map()
  const ensure = key => {
    if (!formats.has(key)) formats.set(key, { key, label: FORMAT_NAMES[key], total: 0, assessed: 0, pending: 0, ineligible: 0, blocked: 0, unknown: 0, files: [] })
    return formats.get(key)
  }
  const hasFormatTotals = summary?.by_format && typeof summary.by_format === 'object'
  if (hasFormatTotals) for (const [key, value] of Object.entries(summary.by_format)) {
    if (count(value) != null) ensure(formatOf({ format: key })).total += value
  }
  const available = new Map()
  for (const file of inventory?.rows || []) if (file?.file) available.set(file.file, { inventory: file })
  for (const file of files) {
    const key = file.file || file.name
    if (key) available.set(key, { ...available.get(key), file })
  }
  for (const [name, record] of available) {
    const file = record.file
    const row = record.inventory
    const group = ensure(formatOf(row || file))
    if (!hasFormatTotals) group.total++
    group.files.push(file || { ...row, file: name, type: formatOf(row).toUpperCase(), _estateOnly: true })
    let state = 'unknown'
    if (row?.status === 'excluded') state = 'ineligible'
    else if (file && measuredFile(file)) state = 'assessed'
    else if (file?.status === 'error') state = 'blocked'
    else if (['metadata_only', 'unsupported'].includes(row?.status)) state = 'ineligible'
    else if (row?.status === 'assessable' || row?.assessment_eligible === true) state = 'pending'
    else if (row?.assessment_eligible === false) state = 'ineligible'
    group[state]++
  }
  const formatRows = [...formats.values()].sort((a, b) => b.total - a.total || a.label.localeCompare(b.label))
  let inconsistent = false
  for (const row of formatRows) {
    const known = COVERAGE_STATES.reduce((sum, [key]) => sum + row[key], 0)
    if (known > row.total) inconsistent = true
    else row.unknown += row.total - known
  }
  const discovered = count(summary?.discovered) ?? count(run?.files)
  const eligible = assessmentEligible(summary)
  const rate = eligible > 0 && measured.length <= eligible ? measured.length / eligible : null
  const findingsKnown = measured.length > 0
  return { discovered, eligible, assessed: measured.length, rate, formats: formatRows, inconsistent,
    formatTotal: formatRows.reduce((sum, row) => sum + row.total, 0), truncated: !!summary?.truncated,
    findingsKnown, findings, tags, groups, otherCriteria: criterionBreakdown(groups.Other), departments: [...departments].sort((a, b) => b[1].length - a[1].length), age,
    reviewCount: Object.values(age).reduce((sum, items) => sum + items.length, 0), now }
}

// Balanced recursive area partition. Clamp the split so even a dominant first item cannot
// recursively receive the entire input again (e.g. a 99:1 two-format estate).
export function treemapRects(items, x = 0, y = 0, w = 100, h = 100) {
  const positive = items.filter(item => item.total > 0)
  if (!positive.length) return []
  if (positive.length === 1) return [{ ...positive[0], x, y, w, h }]
  const total = positive.reduce((sum, item) => sum + item.total, 0)
  let subtotal = 0, split = 1
  for (let i = 0; i < positive.length - 1; i++) {
    subtotal += positive[i].total; split = i + 1
    if (subtotal >= total / 2) break
  }
  const a = positive.slice(0, split), b = positive.slice(split), fraction = subtotal / total
  return w >= h
    ? [...treemapRects(a, x, y, w * fraction, h), ...treemapRects(b, x + w * fraction, y, w * (1 - fraction), h)]
    : [...treemapRects(a, x, y, w, h * fraction), ...treemapRects(b, x, y + h * fraction, w, h * (1 - fraction))]
}

export function activityMonths(activity = []) {
  const months = new Map()
  for (const day of activity) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day.date || '') || count(day.attempts) == null) continue
    const date = new Date(`${day.date}T00:00:00Z`)
    if (!Number.isFinite(+date) || date.toISOString().slice(0, 10) !== day.date) continue
    const month = day.date.slice(0, 7)
    if (!months.has(month)) months.set(month, [])
    months.get(month).push(day)
  }
  return [...months].sort(([a], [b]) => a.localeCompare(b)).map(([month, days]) => ({ month, days }))
}
