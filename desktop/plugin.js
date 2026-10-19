import {
  Badge,
  Button,
  Checkbox,
  Codicon,
  COMPOSER_AREAS,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DisclosureCaret,
  EmptyState,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Switch,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const WISDOM_ROUTE = '/collective-wisdom'
const ROOT_KEY = ['collective-wisdom']

const text = value => (value == null ? '' : String(value))
const label = item => {
  const givenName = text(item?.name).trim()
  if (givenName) return givenName
  const fallback = text(item?.skill_name || item?.slug).trim()
  return fallback && ![item?.id, item?.skill_id, item?.draft_id].includes(fallback) ? fallback : 'Name unavailable'
}
const versionOf = item =>
  item?.latest_version || item?.version || item?.latest_version_detail?.version?.version ||
  item?.latest_version_detail?.version || null
const requestBody = (viewId, fields = {}) => ({ view_id: viewId, ...fields })
const isObject = value => value != null && typeof value === 'object' && !Array.isArray(value)
const isPositiveInt = value => Number.isInteger(value) && value > 0
const isAuthError = error => error?.status === 401 || error?.statusCode === 401 || /\b401\b/.test(text(error?.message))
// Electron IPC may preserve only the HTTP error message, not statusCode.
const isUnavailable = error => error?.status === 503 || error?.statusCode === 503 || /\b503\b/.test(text(error?.message))
const UNAVAILABLE_MESSAGE = 'Collective Wisdom is temporarily unavailable.'

function ReadFailure({ error, onRetry, title }) {
  return jsxs('div', { className: 'space-y-3', children: [
    jsx(EmptyState, { title, description: isUnavailable(error) ? UNAVAILABLE_MESSAGE : text(error?.message || error) }),
    isUnavailable(error) ? jsx(Button, { onClick: onRetry, type: 'button', variant: 'secondary', children: 'Retry' }) : null
  ] })
}

const slashDialog = {
  hosts: [],
  listeners: new Set(),
  revision: 0,
  value: null,
  emit() {
    this.revision += 1
    for (const listener of this.listeners) listener()
  },
  open(value) { this.value = value; this.emit() },
  close(id) {
    if (id == null || this.value?.id === id) {
      this.value = null
      this.emit()
    }
  },
  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener) },
  snapshot() { return this.revision }
}

function parseWisdomDiscovery(value) {
  const match = text(value).trim().match(/^\/wisdom(?:\s+(.*))?$/i)
  if (!match) return null
  const args = text(match[1]).trim()
  if (!args) return { query: '', skillId: null }
  if (/^list$/i.test(args)) return { query: '', skillId: null }
  const browse = args.match(/^browse(?:\s+(.+))?$/i)
  if (browse && !text(browse[1]).trim().startsWith('--')) return { query: text(browse[1]).trim(), skillId: null }
  const show = args.match(/^show\s+(\S+)$/i)
  if (show) return { query: '', skillId: show[1] }
  return null
}

function captureDialogOrigin() {
  const owner = host.state.focusedSessionOwner?.get?.() || null
  const active = {
    connectionId: text(host.state.connectionId?.get?.()),
    profile: text(host.state.profile?.get?.() || 'default')
  }
  const origin = {
    connectionId: text(owner?.connectionId),
    profile: text(owner?.profile),
    runtimeSessionId: host.state.focusedSessionId?.get?.() ?? null,
    storedSessionId: host.state.focusedStoredSessionId?.get?.() ?? null
  }
  const routable = Boolean(origin.connectionId && origin.profile &&
    origin.connectionId === active.connectionId && origin.profile === active.profile)
  return { active, origin, routable }
}

function dialogOriginIsCurrent(origin) {
  const owner = host.state.focusedSessionOwner?.get?.()
  return host.state.gateway?.get?.() === 'open' &&
    text(host.state.connectionId?.get?.()) === origin.connectionId &&
    text(host.state.profile?.get?.() || 'default') === origin.profile &&
    text(owner?.connectionId) === origin.connectionId && text(owner?.profile) === origin.profile &&
    (host.state.focusedSessionId?.get?.() ?? null) === origin.runtimeSessionId &&
    (host.state.focusedStoredSessionId?.get?.() ?? null) === origin.storedSessionId
}

function Section({ children, description, title }) {
  return jsxs('section', { className: 'space-y-3', children: [
    jsxs('div', { className: 'space-y-1', children: [
      jsx('h2', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: title }),
      description ? jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: description }) : null
    ] }),
    children
  ] })
}

function KeyValue({ mono = false, name, value }) {
  if (value == null || value === '') return null
  return jsxs('div', { className: 'grid grid-cols-[9rem_minmax(0,1fr)] gap-2 text-xs', children: [
    jsx('span', { className: 'text-(--ui-text-quaternary)', children: name }),
    jsx('span', { className: `${mono ? 'break-all font-mono ' : ''}text-(--ui-text-secondary)`, children: text(value) })
  ] })
}

function StringList({ empty = 'None', items }) {
  const values = Array.isArray(items) ? items : []
  return values.length
    ? jsx('ul', { className: 'list-disc space-y-1 pl-5 text-xs text-(--ui-text-secondary)', children: values.map((item, index) =>
        jsx('li', { children: typeof item === 'string' ? item : text(item?.summary || item?.message || item?.path || 'Unspecified finding') }, `${index}-${text(item?.path || item)}`)
      ) })
    : jsx('p', { className: 'text-xs text-(--ui-text-quaternary)', children: empty })
}

function FindingList({ empty = 'No findings reported.', items }) {
  const values = Array.isArray(items) ? items : []
  return values.length
    ? jsx('ul', { className: 'list-disc space-y-1 pl-5 text-xs text-(--ui-text-secondary)', children: values.map((item, index) => {
        if (!isObject(item)) return jsx('li', { children: text(item) }, `${index}-${text(item)}`)
        const provenance = [item.severity, item.category, item.path && `${item.path}${item.line ? `:${item.line}` : ''}`].filter(Boolean).join(' · ')
        const message = text(item.message || item.summary || item.detail || 'Unspecified finding')
        return jsx('li', { children: provenance ? `${provenance} — ${message}` : message }, `${index}-${text(item.path || message)}`)
      }) })
    : jsx('p', { className: 'text-xs text-(--ui-text-quaternary)', children: empty })
}

function BoundedFindings({ items }) {
  const findings = Array.isArray(items) ? items : []
  const counts = findings.reduce((totals, finding) => {
    const level = String(finding?.severity || 'unspecified').toLowerCase()
    totals[level] = (totals[level] || 0) + 1
    return totals
  }, {})
  return jsxs('div', { className: 'space-y-1', children: [
    jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: `${findings.length} findings: ${Object.entries(counts).map(([level, count]) => `${count} ${level}`).join(', ')}` }),
    jsx(FindingList, { items: findings.slice(0, 3) }),
    findings.length > 3 ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Showing the first three findings.' }) : null
  ] })
}

function CheckSummary({ check, title, authority = 'Check', compact = false }) {
  const value = isObject(check) ? check : null
  const decision = value?.upload_allowed ?? value?.allowed ?? value?.passed ?? null
  const status = decision === true ? 'passed' : decision === false ? 'blocked' : text(value?.status || value?.verdict || value?.state || 'unavailable')
  const findings = Array.isArray(value?.findings) ? value.findings : []
  return jsxs('section', { className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
    jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: title }),
      jsx(Badge, { children: `${authority}: ${status}` })
    ] }),
    jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: value?.summary || value?.verdict || value?.state || (value ? 'No summary supplied.' : 'The service did not supply this check.') }),
    findings.length ? jsx(compact ? BoundedFindings : FindingList, { items: findings }) : null
  ] })
}

function Compatibility({ value }) {
  if (!isObject(value)) {
    return jsx(CheckSummary, { title: 'Compatibility', check: null, authority: 'Local evaluation' })
  }
  return jsxs('section', { className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
    jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Compatibility' }),
      jsx(Badge, { children: value.outcome || 'unavailable' })
    ] }),
    jsx(KeyValue, { name: 'Works with', value: Array.isArray(value.satisfied) ? value.satisfied.join(', ') || 'No checks reported' : 'Unavailable' }),
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Limitations' }), jsx(StringList, { items: value.limitations, empty: 'No limitations reported.' })] }),
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Setup needing your attention' }), jsx(StringList, { items: value.setup_actions, empty: 'No additional setup reported.' })] }),
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'What prevents installation' }), jsx(StringList, { items: value.blocked, empty: 'Nothing reported.' })] })
  ] })
}

function FileManifest({ files, showContent }) {
  const values = Array.isArray(files) ? files : []
  return jsx('ul', { className: 'space-y-2', children: values.map((file, index) =>
    jsxs('li', { className: 'space-y-1 rounded-[4px] border border-(--ui-stroke-secondary) p-2 text-xs', children: [
      jsx('div', { className: 'break-all font-mono', children: file?.path || file?.name || `file ${index + 1}` }),

      jsx(KeyValue, { name: 'size', value: Number.isInteger(file?.size) ? `${file.size} bytes` : null }),
      showContent
        ? file?.content_utf8 != null
          ? jsx('pre', { className: 'max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-[4px] border border-(--ui-stroke-secondary) p-2 text-xs', children: file.content_utf8 })
          : typeof file?.content_base64 === 'string'
            ? jsxs('div', { className: 'space-y-1', children: [
                jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: 'Opaque exact bytes are retained as inert base64; active preview is disabled.' }),
                jsx('a', { className: 'text-xs text-(--ui-accent)', download: text(file.path).split('/').at(-1) || 'opaque.bin', href: `data:application/octet-stream;base64,${file.content_base64}`, children: 'Export exact opaque bytes' })
              ] })
            : jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: 'Full file content unavailable — confirmation is blocked.' })
        : null
    ] }, file?.path || file?.name || index)
  ) })
}

function scanGuard(value) {
  const scan = isObject(value?.local_scan) ? value.local_scan : value
  return isObject(scan?.guard) ? scan.guard : scan
}

function safeWarningFindings(value) {
  const findings = scanGuard(value)?.findings
  return Array.isArray(findings) ? findings.filter(item => isObject(item) &&
    ['severity', 'category', 'path', 'line', 'message', 'summary', 'detail'].some(key => item[key] != null && item[key] !== '')) : []
}

function warningIdentity(value) {
  return JSON.stringify(safeWarningFindings(value).map(item => ({
    severity: text(item.severity), category: text(item.category), path: text(item.path),
    line: Number.isInteger(item.line) ? item.line : null,
    message: text(item.message), summary: text(item.summary), detail: text(item.detail)
  })))
}

function reviewProblems(review, kind, acknowledgements = {}) {
  const problems = []
  if (!isObject(review)) return ['Review response is missing.']
  if (kind === 'uninstall') {
    if (!text(review.review_id)) problems.push('This removal review has expired. Start again.')
    if (!isObject(review.installation) || !text(review.installation.skill_id)) problems.push('Your installed copy could not be checked. Try again.')
    return problems
  }
  if (!text(review.receipt)) problems.push('This review has expired. Start again.')
  if (kind === 'publication') {
    if (!text(review.draft_id)) problems.push('This draft is unavailable. Open it again.')
    if (!isObject(review.hashes) || !['content', 'author_description', 'package_manifest'].every(key => text(review.hashes[key]))) {
      problems.push('The skill changed or could not be checked. Review it again.')
    }
    if (!text(review.drafted_description || review.description || review.author_description)) problems.push('Add a description before sharing.')
    if (!Array.isArray(review.files) || !review.files.length || review.files.some(file => !text(file?.path) || (typeof file?.content_utf8 !== 'string' && typeof file?.content_base64 !== 'string'))) {
      problems.push('Some files could not be reviewed. Open the draft again.')
    }
    if (!isObject(review.local_scan)) problems.push('Safety check is unavailable. Try again.')
    if (!isObject(review.professionalism_check)) problems.push('Sharing check is unavailable. Try again.')
    if (review.security_check?.upload_allowed !== true) problems.push('This skill is not cleared for sharing.')
    if (!['open', 'moderated', 'managed'].includes(review.publication_mode) || !text(review.policy_version)) problems.push('Sharing is unavailable right now. Try again later.')
    return problems
  }
  if (!text(review.skill_id) || !isPositiveInt(review.version)) problems.push('This skill version is unavailable. Open it again.')
  if (!text(review.content_hash || review.hashes?.content) || !text(review.manifest_hash || review.hashes?.manifest)) problems.push('The skill could not be checked. Review it again.')
  if (!Array.isArray(review.files) || !review.files.length || review.files.some(file => !text(file?.path) || !text(file?.hash))) problems.push('Some files could not be reviewed. Try again.')
  if (!isObject(review.compatibility) || !text(review.compatibility.outcome)) problems.push('We could not check whether this skill will work here.')
  if (!isObject(review.local_scan) || !Object.hasOwn(review.local_scan, 'allowed')) problems.push('Safety check is unavailable. Try again.')
  if (!isObject(review.server_checks)) problems.push('Team checks are unavailable. Try again.')
  if (!isObject(review.policy)) problems.push('This review is unavailable. Try again.')
  const warningAccepted = review.policy?.warning_acceptance?.accepted === true || review.policy?.accepted_warnings === true
  const partialAccepted = review.policy?.accepted_partial === true || acknowledgements.partial === true
  if (review.policy?.scan_requires_confirmation === true && !warningAccepted) problems.push('Review and acknowledge the warnings to continue.')
  if (review.policy?.compatibility_requires_confirmation === true && !partialAccepted) problems.push('Review what may not work or need setup to continue.')
  if (review.allowed !== true) problems.push('This action cannot continue right now.')
  const action = kind === 'update' ? 'confirm_update' : 'confirm_install'
  if (!Array.isArray(review.permitted_actions) || !review.permitted_actions.includes(action)) problems.push('This action is not available for this review.')
  if (kind === 'update') {
    const unknownLocal = review.from_version === null && review.local_baseline === 'unknown'
      && review.previous_installation?.version === null
      && review.previous_installation?.target_provenance?.kind === 'local_source'
      && review.authorization?.kind === 'manual_receipt'
      && review.preservation?.action === 'preserve_unmanaged_copy'
      && review.policy?.accepted_modified_preservation === true
    if ((!isPositiveInt(review.from_version) && !unknownLocal) || !isObject(review.previous_installation)) problems.push('Your installed copy could not be checked. Try again.')
    if (!Array.isArray(review.sensitive_expansion) || !isObject(review.preservation) || !isObject(review.authorization)) problems.push('We could not check how your local changes will be protected.')
    if (review.sensitive_expansion?.length && review.policy?.accepted_sensitive !== true) problems.push('Review and acknowledge the new access this update needs.')
  }
  return problems
}

function reviewContinuation(kind) {
  return ['publication', 'update', 'install'].includes(kind) ? 'the Wisdom review' : null
}

function ReviewAcknowledgements({ busy, kind, onChange, onReview, review, value }) {
  const scanRequired = review?.policy?.scan_requires_confirmation === true && review?.policy?.warning_acceptance?.accepted !== true && review?.policy?.accepted_warnings !== true
  const partialRequired = review?.policy?.compatibility_requires_confirmation === true && review?.policy?.accepted_partial !== true
  const sensitiveRequired = kind === 'update' && review?.sensitive_expansion?.length > 0 && review?.policy?.accepted_sensitive !== true
  if (!scanRequired && !partialRequired && !sensitiveRequired) return null
  const findings = safeWarningFindings(review)
  const requiresReReview = scanRequired || sensitiveRequired || (kind === 'update' && partialRequired)
  const ready = (!scanRequired || (findings.length > 0 && value.warnings)) && (!partialRequired || value.partial) && (!sensitiveRequired || value.sensitive)
  return jsxs('section', { className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
    jsx('h4', { className: 'text-xs font-semibold', children: 'Before you continue' }),
    scanRequired ? jsxs('div', { className: 'space-y-2', children: [
      findings.length ? jsx(FindingList, { items: findings }) : jsx('p', { children: 'Warnings are unavailable. Try again later.' }),
      jsxs('label', { className: 'flex items-start gap-2 text-xs', children: [
        jsx(Checkbox, { 'aria-label': `Acknowledge ${kind} scanner warnings`, checked: value.warnings, disabled: findings.length === 0, onCheckedChange: checked => onChange({ ...value, warnings: checked === true }) }),
        'I have read these warnings.'
      ] })
    ] }) : null,
    partialRequired ? jsxs('label', { className: 'flex items-start gap-2 text-xs', children: [
      jsx(Checkbox, { 'aria-label': `Accept ${kind} partial compatibility`, checked: value.partial, onCheckedChange: checked => onChange({ ...value, partial: checked === true }) }),
      review?.setup_commands?.length ? 'I understand what may not work; setup will ask separately.' : 'I understand what may not work or need more setup.'
    ] }) : null,
    sensitiveRequired ? jsxs('label', { className: 'flex items-start gap-2 text-xs', children: [
      jsx(Checkbox, { 'aria-label': 'Accept update sensitive capability expansion', checked: value.sensitive, onCheckedChange: checked => onChange({ ...value, sensitive: checked === true }) }),
      'I understand the new access this update needs.'
    ] }) : null,
    requiresReReview ? jsx(Button, { disabled: busy || !ready, loading: busy, onClick: onReview, size: 'sm', type: 'button', children: 'Continue review' }) : null
  ] })
}

function ReviewPanel({ acknowledgements, busy, cancelLabel, kind, onCancel, onConfirm, review }) {
  const problems = reviewProblems(review, kind, acknowledgements)
  const continuation = reviewContinuation(kind)

  const warnings = [
    ...(Array.isArray(review?.compatibility?.limitations) ? review.compatibility.limitations : []),
    ...(Array.isArray(review?.compatibility?.blocked) ? review.compatibility.blocked : []),
    ...(Array.isArray(review?.compatibility?.setup_actions) ? review.compatibility.setup_actions : []),
    ...(Array.isArray(review?.sensitive_expansion) ? review.sensitive_expansion : [])
  ]
  const isPublication = kind === 'publication'
  return jsxs('aside', { 'aria-label': 'Exact action review', className: 'space-y-4 rounded-[4px] border border-(--ui-stroke-secondary) p-4', children: [
    jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
      jsx('h3', { className: 'text-sm font-semibold', children: `${{ install: 'Install', update: 'Update', uninstall: 'Remove', publication: 'Share' }[kind]} review` }),
      jsx(Badge, { children: problems.length ? 'confirmation blocked' : 'ready to confirm' })
    ] }),
    jsx(KeyValue, { name: 'skill', value: label(review) }),
    jsx(KeyValue, { name: 'version', value: review?.version }),
    jsx(KeyValue, { name: 'from version', value: review?.from_version ?? (kind === 'update' && review?.local_baseline === 'unknown' ? 'Local copy (published version unknown)' : undefined) }),
    jsx(KeyValue, { name: 'files', value: Array.isArray(review?.files) ? review.files.length : null }),
    isPublication ? jsx(KeyValue, { name: 'author copy', value: review?.drafted_description || review?.description || review?.author_description }) : null,
    isPublication ? jsx(KeyValue, { name: 'Gateway outcome', value: review?.publication_mode === 'moderated' ? 'Moderated: confirmation submits for team moderation' : review?.publication_mode === 'open' ? 'Open: confirmation may publish directly' : review?.publication_mode === 'managed' ? 'Managed: confirmation follows organization-managed Gateway handling' : 'Unavailable' }) : null,

    warnings.length ? jsxs('section', { className: 'space-y-1 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Material warnings and required follow-up' }),
      jsx(StringList, { items: kind === 'install' ? warnings.slice(0, 3) : warnings }),
      kind === 'install' && warnings.length > 3 ? jsx('p', { className: 'text-xs', children: `Showing three of ${warnings.length} notes.` }) : null
    ] }) : null,
    ['install', 'update'].includes(kind) && review?.setup_commands?.length ? jsxs('section', { className: 'space-y-1', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Setup commands (run only after separate permission)' }),
      jsx('pre', { className: 'whitespace-pre-wrap break-all text-xs', children: review.setup_commands.map(command => JSON.stringify(command)).join('\n') })
    ] }) : null,
    kind === 'uninstall' ? jsxs('section', { className: 'space-y-1', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Installed copy to remove' }),
      jsx(KeyValue, { name: 'skill', value: label(review?.installation) }),
      jsx(KeyValue, { name: 'version', value: review?.installation?.version })
    ] }) : null,
    kind === 'install' || kind === 'update' ? jsx(Compatibility, { value: review?.compatibility }) : null,
    kind === 'install' || kind === 'update' ? jsx(CheckSummary, { title: 'Local scanner', check: review?.local_scan, authority: 'Local scan', compact: kind === 'install' }) : null,
    kind === 'install' || kind === 'update' ? jsx(CheckSummary, { title: 'Server security', check: review?.server_checks?.security_check || review?.server_checks?.scan, authority: 'Server', compact: kind === 'install' }) : null,
    kind === 'install' || kind === 'update' ? jsx(CheckSummary, { title: 'Server professionalism', check: review?.server_checks?.professionalism_check, authority: 'Server', compact: kind === 'install' }) : null,
    isPublication ? jsx(CheckSummary, { title: 'Local package scanner', check: review?.local_scan, authority: 'Local scan' }) : null,
    isPublication ? jsx(CheckSummary, { title: 'Local security and upload gate', check: review?.security_check, authority: 'Local security' }) : null,
    isPublication ? jsx(CheckSummary, { title: 'Professionalism review', check: review?.professionalism_check, authority: 'Assessment' }) : null,
    isPublication && isObject(review?.server_checks) ? jsx(CheckSummary, { title: 'Gateway security scan', check: review.server_checks.security_check || review.server_checks.scan, authority: 'Gateway' }) : null,
    kind === 'update' ? jsxs('section', { className: 'space-y-2', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Local changes' }),
      jsx('p', { className: 'text-xs', children: review?.modified ? review?.preservation?.action === 'preserve_unmanaged_copy' ? 'Your edited copy will be kept separately before updating.' : 'Local edits detected. Check how they will be protected before updating.' : 'No local edits reported.' }),
      review?.preservation?.destination ? jsx(KeyValue, { name: 'saved copy', value: review.preservation.destination }) : null,
      jsx(StringList, { items: review?.sensitive_expansion, empty: 'No sensitive expansion reported.' })
    ] }) : null,
    kind === 'install' || kind === 'uninstall' ? null : jsxs('details', { children: [
      jsx('summary', { className: 'cursor-pointer text-xs font-semibold', children: 'Reviewed files' }),
      jsxs('div', { className: 'mt-3 space-y-3', children: [
        jsx(FileManifest, { files: review?.files, showContent: isPublication })
      ] })
    ] }),
    review?.notice ? jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: review.notice }) : null,
    problems.length ? jsxs('section', { className: 'space-y-1', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Cannot confirm this review' }),
      jsx(StringList, { items: problems }),
      continuation ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: `Keep this review unconfirmed. Continue with ${continuation} to inspect what needs attention.` }) : null
    ] }) : null,
    jsxs('div', { className: 'flex justify-end gap-2', children: [
      jsx(Button, { disabled: busy, onClick: onCancel, size: 'sm', type: 'button', variant: 'secondary', children: cancelLabel || (isPublication ? 'Keep draft; do not share' : 'Cancel') }),
      jsx(Button, { disabled: busy || problems.length > 0, loading: busy, onClick: onConfirm, size: 'sm', type: 'button', children: { publication: 'Share', uninstall: 'Remove', install: 'Install', update: 'Update' }[kind] })
    ] })
  ] })
}

function completedFileChange(kind, result) {
  const key = { install: 'installed', update: 'updated' }[kind.toLowerCase()]
  return !!key && result?.[key] === true && result.pending !== true && result.rejected !== true && !result.error &&
    [undefined, null, 'completed', 'done', 'inactive'].includes(result.state)
}

function setupIncompleteResult(kind, result) {
  return completedFileChange(kind, result) &&
    ((result.setup && result.setup.state !== 'passed') || (result.setup_required === true && result.ready_to_use !== true))
}

function operationSucceeded(kind, result, name) {
  const key = { install: 'installed', update: 'updated', uninstall: 'uninstalled' }[kind]
  return typeof name === 'string' && name.trim().length > 0 && name !== 'Name unavailable' && result?.[key] === true &&
    !setupIncompleteResult(kind, result) &&
    result.pending !== true && result.rejected !== true && !result.error &&
    [undefined, null, 'completed', 'done', 'inactive'].includes(result.state)
}

function OperationOutcome({ kind, result }) {
  if (!result) return null
  const subject = label(result)
  const setupIncomplete = setupIncompleteResult(kind, result)
  const setupRan = setupIncomplete && result.setup?.state === 'passed' && result.setup_execution_authorized === true
  const setupStopped = result.setup?.state && result.setup.state !== 'passed'
  let setupDetail = 'Setup is required; readiness has not been verified. Check setup status before use.'
  if (kind === 'Update') setupDetail = 'This version needs setup; readiness has not been verified. Check setup status before use.'
  if (setupStopped) setupDetail = `Setup ${text(result.setup.state)}${result.setup.step ? ` at step ${result.setup.step}` : ''}. Check its status before trying again.`
  const shareUnlinked = kind === 'Publication' && result.state === 'done' &&
    result.publication_state === 'published' && result.managed_enrollment?.state === 'not_linked'
  return jsxs('section', { 'aria-live': 'polite', className: 'mt-3 space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
    jsx('h3', { className: 'text-xs font-semibold', children: setupRan ? `✅ ${subject} ${kind === 'Update' ? 'updated' : 'installed'}` : setupIncomplete ? `${subject}: files ${kind === 'Update' ? 'updated' : 'installed'}; setup incomplete` : `${kind} requires attention for ${subject}` }),
    jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: setupRan ? 'Setup ran successfully. Have your agent confirm the skill is ready before using it.' : setupIncomplete ? setupDetail : shareUnlinked ? 'Shared successfully, but local updates are not linked yet. See details for recovery.' : 'The service did not report a completed result. Check current status before deciding what to do next.' }),
    setupRan ? null : jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Check its status before trying again. This action will not retry automatically.' }),
    jsxs('details', { children: [
      jsx('summary', { className: 'cursor-pointer text-xs text-(--ui-text-tertiary)', children: 'Technical details' }),
      jsxs('div', { className: 'mt-2 space-y-1', children: [
        jsx(KeyValue, { name: 'operation', value: result.operation_id, mono: true }),
        jsx(KeyValue, { name: 'state', value: result.state || (result.pending ? 'pending' : result.rejected ? 'rejected' : 'unknown') }),
        jsx(KeyValue, { name: 'phase', value: result.phase }),
        jsx(KeyValue, { name: 'detail', value: result.detail || result.error }),
        jsx(KeyValue, { name: 'retry safe', value: result.retry_safe })
      ] })
    ] })
  ] })
}

function SetupPermission({ pending, busy, onDefer, onRun }) {
  if (!pending) return null
  return jsxs('section', { 'aria-label': 'Setup permission', className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-4', children: [
    jsx('h3', { className: 'text-sm font-semibold', children: `Setup needed for ${pending.name}` }),
    jsx('p', { className: 'text-xs', children: `The files are ${pending.operation === 'update' ? 'updated' : 'installed'}, but this skill is not ready yet. Hermes will run these commands on this computer if you choose Run setup:` }),
    jsx('pre', { className: 'whitespace-pre-wrap break-all text-xs', children: pending.commands.join('\n') }),
    jsx('p', { className: 'text-xs', children: `Run setup now? Not now leaves the files ${pending.operation === 'update' ? 'updated' : 'installed'} without running the commands.` }),
    jsxs('div', { className: 'flex justify-end gap-2', children: [
      jsx(Button, { disabled: busy, onClick: onDefer, size: 'sm', type: 'button', variant: 'secondary', children: 'Not now' }),
      jsx(Button, { disabled: busy, loading: busy, onClick: onRun, size: 'sm', type: 'button', children: 'Run setup' })
    ] })
  ] })
}

function lifecycleFor(identity, gateway, manualGeneration, tracker) {
  const signature = `${identity.connectionId}\u0000${identity.profile}`
  if (tracker.signature !== signature || tracker.gateway !== gateway || tracker.manualGeneration !== manualGeneration) {
    tracker.signature = signature
    tracker.gateway = gateway
    tracker.manualGeneration = manualGeneration
    tracker.generation += 1
  }
  return {
    ...identity,
    generation: tracker.generation,
    key: `${identity.connectionId}:${identity.profile}:${tracker.generation}`
  }
}

function useCurrentMutation(client, lifecycle, operation, onSuccess, failureMessage, onFailure) {
  const currentRef = useRef(lifecycle)
  const activeRef = useRef(false)
  const mountedRef = useRef(true)
  currentRef.current = lifecycle
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  const isCurrent = token => mountedRef.current && client.active() && token.mounted() && currentRef.current.key === token.key &&
    host.state.connectionId.get() === token.connectionId && host.state.profile.get() === token.profile
  return useMutation({
    retry: false,
    mutationFn: async variables => {
      const token = lifecycle
      if (!isCurrent(token)) throw Object.assign(new Error('This review is no longer current.'), { stale: true })
      if (activeRef.current) throw Object.assign(new Error('This action is already in progress.'), { stale: true })
      activeRef.current = true
      try {
        const result = await operation(variables, token, isCurrent)
        if (!isCurrent(token)) throw Object.assign(new Error('This review is no longer current.'), { stale: true })
        return { result, token }
      } finally {
        activeRef.current = false
      }
    },
    onSuccess: async payload => {
      if (isCurrent(payload.token)) await onSuccess(payload.result, payload.token, isCurrent)
    },
    onError: (error, variables) => {
      onFailure?.(error, variables)
      if (!error?.stale && isCurrent(lifecycle)) host.notifyError(
        isUnavailable(error) ? new Error(UNAVAILABLE_MESSAGE) : error,
        isUnavailable(error) ? UNAVAILABLE_MESSAGE : failureMessage
      )
    }
  })
}

function queryOptions(key, queryFn, enabled = true) {
  return {
    enabled,
    gcTime: 0,
    queryFn,
    queryKey: [...ROOT_KEY, ...key],
    refetchOnMount: false,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: Infinity
  }
}

function catalogStatus(item) {
  const status = item?.installation_status
  if (isObject(status) && ['installed', 'not_installed', 'unmanaged_local', 'unavailable'].includes(status.state)) return status
  return { state: 'unavailable', text: 'Installation status unavailable', installed_version: null, latest_version: versionOf(item) }
}

function catalogAction(item) {
  const status = catalogStatus(item)
  if (status.state === 'not_installed') return 'install'
  if (status.state === 'unmanaged_local' && isPositiveInt(status.latest_version)) return 'update'
  if (status.state === 'installed' && isPositiveInt(status.latest_version) && isPositiveInt(status.installed_version) && status.latest_version > status.installed_version) return 'update'
  return null
}

function Catalog({ client, dialog = false, initialSearch = '', initialSelected = null, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState(initialSearch)
  const [selected, setSelected] = useState(initialSelected)
  const [page, setPage] = useState(0)
  const [review, setReview] = useState(null)
  const [setupPermission, setSetupPermission] = useState(null)
  const [acknowledgements, setAcknowledgements] = useState({ warnings: false, partial: false, sensitive: false })
  const [outcome, setOutcome] = useState(null)
  const catalog = useQuery(queryOptions(['catalog', lifecycle.key, search], () => client.rest(`/catalog?q=${encodeURIComponent(search.trim())}`)))
  const detail = useQuery(queryOptions(['detail', lifecycle.key, selected], () => client.rest(`/catalog/${encodeURIComponent(selected)}`), Boolean(selected)))
  const versions = useQuery(queryOptions(['versions', lifecycle.key, selected], () => client.rest(`/catalog/${encodeURIComponent(selected)}/versions`), Boolean(selected)))
  const prepare = useCurrentMutation(client, lifecycle, async (selection, token, isCurrent) => {
    const item = selection.item || selection
    const version = selection.version || versionOf(item)
    const action = selection.action || catalogAction(item)
    const prepared = await (action === 'update'
      ? client.rest('/update/review', { method: 'POST', body: requestBody(viewId, { skill_id: item.id, preserve_modified: true, accept_sensitive: false, accept_partial: false, accept_warnings: false }) })
      : client.rest('/install/review', { method: 'POST', body: requestBody(viewId, { reference: version ? `${item.id}@v${version}` : item.id, accept_warnings: false }) }))
    const review = { ...prepared, name: label(item) !== 'Name unavailable' ? label(item) : label(prepared), operation: action }
    if (!isCurrent(token)) throw Object.assign(new Error('This review is no longer current.'), { stale: true })
    if (reviewProblems(review, action).length ||
        (action === 'install' && Array.isArray(review.setup_commands) && review.setup_commands.length)) {
      return { review }
    }
    const result = await client.rest(action === 'update' ? '/update/confirm' : '/install/confirm', {
      method: 'POST', body: action === 'update'
        ? requestBody(viewId, { receipt: review.receipt })
        : requestBody(viewId, { receipt: review.receipt, accept_partial: false })
    })
    return { operation: action, result: { ...result, name: label(review) }, reviewed: review }
  }, async (payload, token, isCurrent) => {
    setOutcome(null)
    setAcknowledgements({ warnings: false, partial: false, sensitive: false })
    if (payload.review) {
      setReview(payload.review)
      return
    }
    const { operation, result, reviewed } = payload
    setReview(null)
    if (operation === 'update' && Array.isArray(reviewed.setup_commands) && reviewed.setup_commands.length &&
        completedFileChange(operation, result)) {
      setSetupPermission({ receipt: reviewed.receipt, commands: reviewed.setup_commands,
        name: result.name, operation })
      setOutcome({ operation, result: { ...result, setup_required: true, ready_to_use: null } })
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'catalog', lifecycle.key] })
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'detail', lifecycle.key] })
      return
    }
    if (operationSucceeded(operation, result, result?.name)) {
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'catalog', lifecycle.key] })
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'detail', lifecycle.key] })
      await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
      if (isCurrent(token)) host.notify({ kind: 'success', message: operation === 'update' ? `✅ ${result.name} updated` : `✅ ${result.name} installed` })
    } else {
      setOutcome({ operation, result: result || { state: 'unknown' } })
    }
  }, 'Could not prepare the package review')
  const reReview = useCurrentMutation(client, lifecycle, async current => {
    const updating = current.operation === 'update'
    const next = await client.rest(updating ? '/update/review' : '/install/review', {
      method: 'POST', body: updating
        ? requestBody(viewId, { skill_id: current.skill_id, preserve_modified: true, accept_sensitive: acknowledgements.sensitive, accept_partial: acknowledgements.partial, accept_warnings: acknowledgements.warnings })
        : requestBody(viewId, { reference: `${current.skill_id}@v${current.version}`, accept_warnings: acknowledgements.warnings })
    })
    if (current.content_hash !== next.content_hash || current.manifest_hash !== next.manifest_hash || warningIdentity(current) !== warningIdentity(next)) {
      throw new Error('The exact package or scanner findings changed. Start a fresh review and acknowledge the new facts.')
    }
    return { ...next, name: label(current), operation: updating ? 'update' : 'install' }
  }, setReview, 'Could not bind the package acknowledgements')
  const confirm = useCurrentMutation(client, lifecycle, async current => ({
    operation: current.operation,
    reviewed: current,
    skillName: label(current),
    result: await client.rest(current.operation === 'update' ? '/update/confirm' : '/install/confirm', {
      method: 'POST', body: current.operation === 'update'
        ? requestBody(viewId, { receipt: current.receipt })
        : requestBody(viewId, { receipt: current.receipt, accept_partial: acknowledgements.partial })
    })
  }), async (payload, token, isCurrent) => {
    const { operation, result, skillName, reviewed } = payload
    const name = skillName !== 'Name unavailable' ? skillName : label(result)
    setReview(null)
    if (['install', 'update'].includes(operation) && Array.isArray(reviewed.setup_commands) && reviewed.setup_commands.length &&
        completedFileChange(operation, result)) {
      setSetupPermission({ receipt: reviewed.receipt, commands: reviewed.setup_commands, name, operation })
      setOutcome({ operation, result: { ...result, name, setup_required: true, ready_to_use: null } })
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'catalog', lifecycle.key] })
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'detail', lifecycle.key] })
      return
    }
    if (operationSucceeded(operation, result, name)) {
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'catalog', lifecycle.key] })
      await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'detail', lifecycle.key] })
      await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
      if (isCurrent(token)) {
        host.notify({ kind: 'success', message: operation === 'update' ? `✅ ${name} updated` : `✅ ${name} installed` })
      }
    } else {
      setOutcome({ operation, result: { ...(result || { state: 'unknown' }), name } })
    }
  }, 'The reviewed package action was not applied')
  const runSetup = useCurrentMutation(client, lifecycle, async pending => ({
    pending,
    result: await client.rest(pending.operation === 'update' ? '/update/setup' : '/install/setup', {
      method: 'POST', body: requestBody(viewId, { receipt: pending.receipt })
    })
  }), ({ pending, result }) => {
    setSetupPermission(null)
    setOutcome({ operation: pending.operation, result: { ...result, name: pending.name } })
  }, 'Could not confirm setup. Check its status before trying again.', () => {
    setSetupPermission(null) // A lost acknowledgement cannot authorize another click.
  })
  const skills = Array.isArray(catalog.data?.skills) ? catalog.data.skills : []
  const chosen = skills.find(item => item.id === selected)
  const detailedSkill = detail.data?.skill || detail.data
  const detailItem = selected ? {
    ...(chosen || {}), ...(isObject(detailedSkill) ? detailedSkill : {}),
    installation_status: detail.data?.installation_status || detailedSkill?.installation_status || chosen?.installation_status
  } : null
  const detailAction = catalogAction(detailItem)
  const catalogFailure = catalog.error || (selected && (detail.error || versions.error))
  const pageCount = Math.max(1, Math.ceil(skills.length / 5))
  const visibleSkills = dialog ? skills.slice(page * 5, page * 5 + 5) : skills
  useEffect(() => { setPage(0) }, [search])
  useEffect(() => { if (page >= pageCount) setPage(pageCount - 1) }, [page, pageCount])

  const back = () => {
    if (review) setReview(null)
    else setSelected(null)
    setOutcome(null)
  }

  return jsxs('div', { className: 'space-y-5', children: [
    (!dialog || (!selected && !review)) ? jsx(Input, { 'aria-label': 'Search shared skills', onChange: event => setSearch(event.target.value), placeholder: 'Search shared skills', type: 'search', value: search }) : null,
    catalogFailure ? jsx(ReadFailure, { error: catalogFailure, title: 'Catalog unavailable', onRetry: () => {
      if (catalog.error) catalog.refetch()
      if (detail.error) detail.refetch()
      if (versions.error) versions.refetch()
    } }) : null,
    !catalog.isLoading && !catalogFailure && skills.length === 0 ? jsx(EmptyState, { title: 'No shared skills', description: 'No published skills match this search.' }) : null,
    !catalogFailure && (!dialog || (!selected && !review)) ? jsx('ul', { className: 'divide-y divide-(--ui-stroke-secondary)', children: visibleSkills.map(item => {
      const status = catalogStatus(item)
      const action = catalogAction(item)
      return (
      jsxs('li', { 'data-testid': `catalog-skill-${item.id}`, className: 'gap-3 py-3', style: { alignItems: 'center', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto' }, children: [
        jsxs('div', { className: 'min-w-0 flex-1', children: [
          jsx('div', { className: 'text-sm font-medium', children: label(item) }),
          jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', style: { display: '-webkit-box', maxWidth: '100%', overflow: 'hidden', overflowWrap: 'anywhere', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2 }, children: item.author_description || 'No publisher description.' }),
          jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: text(status.text || 'Installation status unavailable') }),
          jsx(OperationOutcome, { kind: outcome?.operation === 'update' ? 'Update' : 'Install', result: outcome?.result?.skill_id === item.id ? outcome.result : null })
        ] }),
        jsxs('div', { 'data-testid': `catalog-actions-${item.id}`, className: 'flex items-center justify-end gap-2', style: { flexWrap: 'wrap' }, children: [
          versionOf(item) ? jsx(Badge, { children: `v${versionOf(item)}` }) : null,
          jsx(Button, { onClick: () => setSelected(item.id), size: 'sm', type: 'button', variant: 'secondary', children: 'View' }),
          action ? jsx(Button, { disabled: prepare.isPending, onClick: () => prepare.mutate({ item, action }), size: 'sm', type: 'button', children: action === 'update' ? 'Update' : 'Install' }) : null
        ] })
      ] }, item.id)
      )
    }) }) : null,
    dialog && !catalogFailure && !selected && !review && skills.length > 5 ? jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
      jsx(Button, { disabled: page === 0, onClick: () => setPage(value => value - 1), size: 'sm', type: 'button', variant: 'secondary', children: 'Previous' }),
      jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: `${page + 1} / ${pageCount}` }),
      jsx(Button, { disabled: page + 1 >= pageCount, onClick: () => setPage(value => value + 1), size: 'sm', type: 'button', variant: 'secondary', children: 'Next' })
    ] }) : null,
    selected && !review && !catalogFailure ? jsxs(Section, { title: `${dialog ? '' : 'Details — '}${label(detailItem)}`, children: [
      jsx('p', { className: 'text-xs leading-5 text-(--ui-text-secondary)', children: detailItem?.author_description || detail.data?.latest_version_detail?.version?.author_description || 'No description supplied.' }),
      jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: text(catalogStatus(detailItem).text || 'Installation status unavailable') }),
      jsx('h3', { className: 'text-xs font-semibold', children: 'Version history' }),
      jsx('ul', { className: 'flex flex-wrap gap-2', children: (versions.data?.versions || []).map(item => jsx('li', { children: item.state && item.state !== 'published'
        ? jsx(Badge, { children: `v${item.version} · ${item.state}` })
        : detailAction === 'install'
          ? jsx(Button, { disabled: prepare.isPending, onClick: () => prepare.mutate({ item: detailItem, version: item.version, action: 'install' }), size: 'sm', type: 'button', variant: 'secondary', children: `Install v${item.version}` })
          : jsx(Badge, { children: `v${item.version}` })
      }, `${item.version}-${item.state}`)) }),
      jsxs('div', { className: 'flex justify-between gap-2', children: [
        dialog ? jsx(Button, { onClick: back, size: 'sm', type: 'button', variant: 'secondary', children: 'Back' }) : null,
        dialog && detailAction ? jsx(Button, { disabled: prepare.isPending, loading: prepare.isPending, onClick: () => prepare.mutate({ item: detailItem, action: detailAction }), size: 'sm', type: 'button', children: detailAction === 'update' ? 'Update' : 'Install' }) : null
      ] })
    ] }) : null,
    review ? jsx(ReviewAcknowledgements, { busy: reReview.isPending, kind: review.operation, onChange: setAcknowledgements, onReview: () => reReview.mutate(review), review, value: acknowledgements }) : null,
    review ? jsx(ReviewPanel, { acknowledgements, busy: confirm.isPending, cancelLabel: dialog ? 'Back' : undefined, kind: review.operation, onCancel: back, onConfirm: () => confirm.mutate(review), review }) : null,
    jsx(SetupPermission, { pending: setupPermission, busy: runSetup.isPending, onDefer: () => setSetupPermission(null), onRun: () => runSetup.mutate(setupPermission) }),
    jsx(OperationOutcome, { kind: outcome?.operation === 'update' ? 'Update' : 'Install', result: outcome && (dialog || !skills.some(item => item.id === outcome.result?.skill_id)) ? outcome.result : null })
  ] })
}

function WisdomDialogCatalog({ client, request }) {
  const activeConnectionId = useValue(host.state.connectionId) || ''
  const activeProfile = useValue(host.state.profile) || 'default'
  const focusedOwner = useValue(host.state.focusedSessionOwner)
  const focusedSessionId = useValue(host.state.focusedSessionId)
  const focusedStoredSessionId = useValue(host.state.focusedStoredSessionId)
  const gateway = useValue(host.state.gateway) || 'unknown'
  const tracker = useRef({ generation: request.id, mounted: true }).current
  tracker.mounted = true
  const current = client.active() && slashDialog.value?.id === request.id && gateway === 'open' &&
    activeConnectionId === request.origin.connectionId && activeProfile === request.origin.profile &&
    focusedOwner?.connectionId === request.origin.connectionId && focusedOwner?.profile === request.origin.profile &&
    focusedSessionId === request.origin.runtimeSessionId && focusedStoredSessionId === request.origin.storedSessionId
  const lifecycle = {
    connectionId: request.origin.connectionId,
    profile: request.origin.profile,
    generation: request.id,
    key: `dialog:${request.id}`,
    mounted: () => tracker.mounted && slashDialog.value?.id === request.id
  }
  const boundClient = {
    active: () => client.active() && slashDialog.value?.id === request.id && dialogOriginIsCurrent(request.origin),
    rest: (...args) => client.rest(...args)
  }
  const view = useQuery(queryOptions(['dialog-view', lifecycle.key], () => boundClient.rest('/views', { method: 'POST' }), current))

  useEffect(() => () => { tracker.mounted = false }, [tracker])
  useEffect(() => {
    if (!current) {
      tracker.mounted = false
      slashDialog.close(request.id)
    }
  }, [current, request.id, tracker])

  if (!current) return null
  if (view.error) return jsx(ReadFailure, { error: view.error, onRetry: () => { if (current) view.refetch() }, title: 'Shared skills unavailable' })
  if (!view.data) return jsx('p', { className: 'text-sm text-(--ui-text-tertiary)', children: 'Loading Shared skills…' })
  return jsx(Catalog, {
    client: boundClient,
    dialog: true,
    initialSearch: request.query,
    initialSelected: request.skillId,
    lifecycle,
    viewId: view.data.view_id
  })
}

function WisdomDialogHost({ client }) {
  useSyncExternalStore(listener => slashDialog.subscribe(listener), () => slashDialog.snapshot(), () => slashDialog.snapshot())
  const token = useRef(Symbol('wisdom-dialog-host')).current
  useEffect(() => {
    slashDialog.hosts.push(token)
    slashDialog.emit()
    return () => {
      slashDialog.hosts = slashDialog.hosts.filter(item => item !== token)
      slashDialog.emit()
    }
  }, [token])
  const request = slashDialog.value
  if (slashDialog.hosts[0] !== token || !request) return null
  return jsx(Dialog, {
    open: true,
    onOpenChange: open => { if (!open) slashDialog.close(request.id) },
    children: jsxs(DialogContent, { className: 'max-w-[48rem] text-(--ui-text-primary)', children: [
      jsxs(DialogHeader, { children: [
        jsx(DialogTitle, { children: 'Shared skills' }),
        jsx(DialogDescription, { className: 'text-(--ui-text-tertiary)', children: 'Browse shared skills and check what Install or Update will do. Declared setup commands run with Install; other setup may need your attention.' })
      ] }),
      jsx('div', { className: 'max-h-[min(70vh,44rem)] overflow-y-auto border-t border-(--ui-stroke-secondary) pt-4', children: request.error
        ? jsx(EmptyState, { title: 'Shared skills unavailable', description: request.error })
        : jsx(WisdomDialogCatalog, { client, request }) })
    ] })
  })
}


// ---------------------------------------------------------------------------
// cw/v1 Collective Wisdom page: master-detail, Installed | Browse.
// Every list and status comes from GET /library (backend aggregates the CORE
// team_status / folders modules); actions are one-click REST calls.
// ---------------------------------------------------------------------------

const DAY_MS = 24 * 60 * 60 * 1000
const libraryKey = lifecycle => [...ROOT_KEY, 'library', lifecycle.key]

// Palette → page handoff ("Share a skill…" opens the picker on arrival).
const pageIntent = {
  listeners: new Set(),
  revision: 0,
  value: null,
  set(value) { this.value = value; this.revision += 1; for (const listener of this.listeners) listener() },
  take() { const value = this.value; this.value = null; return value },
  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener) },
  snapshot() { return this.revision }
}

function daysSince(iso) {
  const stamp = Date.parse(text(iso))
  if (Number.isNaN(stamp)) return null
  return Math.max(0, Math.floor((Date.now() - stamp) / DAY_MS))
}

function updatedLabel(iso) {
  const days = daysSince(iso)
  if (days == null) return null
  if (days === 0) return 'Updated today'
  return `Updated ${days === 1 ? '1 day' : `${days} days`} ago`
}

function shortAgo(iso) {
  const days = daysSince(iso)
  if (days == null) return null
  return days === 0 ? 'updated today' : `updated ${days}d ago`
}

function formatSize(bytes) {
  if (!Number.isInteger(bytes)) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function newPowers(capabilities) {
  const values = (Array.isArray(capabilities) ? capabilities : []).map(text).filter(Boolean)
  if (!values.length) return null
  if (values.length === 1) return values[0]
  return `${values.slice(0, -1).join(', ')} and ${values.at(-1)}`
}

function usedByText(row) {
  return text(row?.used_by?.label) || null
}

function rowName(row) {
  return text(row?.name || row?.slug) || 'Name unavailable'
}

function statusBadge(row) {
  const status = row?.status || {}
  if (status.kind === 'needs_look') return { label: 'Needs a look', variant: 'warn' }
  if (status.kind === 'updated' && status.seen === false) return { label: 'Updated', variant: 'success' }
  if (status.kind === 'no_longer_shared') return { label: 'No longer shared', variant: 'muted' }
  if (row?.auto_update === false) return { label: 'Auto-update off', variant: 'outline' }
  return null
}

function WisdomBadge({ badge }) {
  if (!badge) return null
  return jsx(Badge, { size: 'xs', variant: badge.variant, children: badge.label })
}

// Same geometry as Capabilities' CapRow (h-11 two-line row, active fill).
function ListRow({ action, active, badge, meta, onSelect, subtitle, title }) {
  return jsxs('div', {
    className: `group/row flex h-11 w-full shrink-0 items-center rounded-md hover:text-(--ui-text-primary) ${active ? 'bg-(--ui-row-active-background) text-(--ui-text-primary)' : 'text-(--ui-text-secondary)'}`,
    children: [
      jsxs('button', {
        className: 'flex h-full min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-md pl-2 pr-1.5 text-left',
        onClick: onSelect,
        type: 'button',
        children: [
          jsxs('span', { className: 'min-w-0 flex-1', children: [
            jsxs('span', { className: 'flex min-w-0 items-center gap-1.5', children: [
              jsx('span', { className: 'truncate text-[0.78rem] font-medium', children: title }),
              jsx(WisdomBadge, { badge })
            ] }),
            subtitle ? jsx('span', { className: 'block truncate text-[0.62rem] text-(--ui-text-tertiary)', children: subtitle }) : null
          ] }),
          meta ? jsx('span', { className: 'shrink-0 rounded bg-(--ui-bg-quinary) px-1 py-px text-[0.6rem] tabular-nums leading-3.5 text-(--ui-text-tertiary)', children: meta }) : null
        ]
      }),
      action ? jsx('span', { className: 'mr-1.5 flex shrink-0 items-center', children: action }) : null
    ]
  })
}

function GroupHeader({ collapsible = false, count, label: title, onToggle, open = true }) {
  const content = [
    collapsible ? jsx(DisclosureCaret, { open }) : null,
    jsx('span', { className: 'truncate', children: title }),
    count != null ? jsx('span', { className: 'ml-auto tabular-nums text-(--ui-text-quaternary)', children: count }) : null
  ]
  const className = 'mt-2 flex h-6 w-full items-center gap-1 px-2 text-[0.65rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)'
  return collapsible
    ? jsx('button', { 'aria-expanded': open, className: `${className} cursor-pointer hover:text-(--ui-text-secondary)`, onClick: onToggle, type: 'button', children: content })
    : jsx('div', { className, children: content })
}

function ViewSwitch({ dot, onChange, value }) {
  const options = [['installed', 'Installed'], ['browse', 'Browse']]
  return jsx('div', { className: 'inline-grid w-fit auto-cols-fr grid-flow-col gap-0.5 rounded-[5px] bg-(--ui-bg-tertiary) p-0.5', role: 'tablist', children: options.map(([id, title]) =>
    jsxs('button', {
      'aria-selected': value === id,
      className: `flex min-w-0 items-center justify-center gap-1 rounded-[3px] px-2.5 py-0.5 text-[0.6875rem] font-medium whitespace-nowrap transition-colors ${value === id ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`,
      onClick: () => onChange(id),
      role: 'tab',
      type: 'button',
      children: [
        title,
        id === 'installed' && dot ? jsx('span', { 'aria-label': 'Something needs a look', className: 'size-1.5 rounded-full bg-(--ui-orange)' }) : null
      ]
    }, id)
  ) })
}

function KV({ name, value }) {
  if (value == null || value === '') return null
  return jsxs('div', { className: 'grid grid-cols-[7rem_minmax(0,1fr)] gap-2 py-1 text-xs', children: [
    jsx('span', { className: 'text-(--ui-text-quaternary)', children: name }),
    jsx('span', { className: 'min-w-0 break-words text-(--ui-text-secondary)', children: value })
  ] })
}

function Callout({ children, tone = 'neutral' }) {
  const border = tone === 'warn' ? 'border-(--ui-orange)/40' : tone === 'good' ? 'border-(--ui-green)/40' : 'border-(--ui-stroke-secondary)'
  return jsx('div', { className: `space-y-2 rounded-[6px] border ${border} bg-(--ui-bg-quinary) p-3 text-xs text-(--ui-text-secondary)`, children })
}

function SkillPreview({ client, lifecycle, skillId }) {
  const preview = useQuery(queryOptions(['preview', lifecycle.key, skillId], () => client.rest(`/skills/${encodeURIComponent(skillId)}/preview`), Boolean(skillId)))
  if (preview.isLoading) return jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading SKILL.md…' })
  if (preview.error) return jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: isUnavailable(preview.error) ? UNAVAILABLE_MESSAGE : 'Preview unavailable.' })
  const data = preview.data || {}
  if (data.skill_md) {
    return jsx('pre', { className: 'max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-[6px] border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-3 font-mono text-[0.7rem] leading-5 text-(--ui-text-secondary)', children: data.skill_md })
  }
  return jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: data.description || 'Install it to see the full SKILL.md.' })
}

function NeedsLookPanel({ busy, client, lifecycle, onCombine, onFlaggedUpdate, row, viewId }) {
  const [showFull, setShowFull] = useState(false)
  const reason = row.status?.reason
  const full = useQuery(queryOptions(
    ['combine', lifecycle.key, row.skill_id],
    () => client.rest(`/combine/${encodeURIComponent(row.skill_id)}?view_id=${encodeURIComponent(viewId)}`),
    showFull && reason === 'edited'
  ))
  if (reason === 'flagged') {
    const powers = newPowers(row.status?.new_capabilities)
    return jsxs(Callout, { tone: 'warn', children: [
      jsx('p', { className: 'font-medium text-(--ui-text-primary)', children: 'The new version needs more access, and the security check flagged it.' }),
      jsx('p', { children: powers ? `It now ${powers}. Until you update, you stay on your current version.` : 'Until you update, you stay on your current version.' }),
      jsx('div', { className: 'flex flex-wrap gap-2 pt-1', children:
        jsx(Button, { disabled: busy, loading: busy, onClick: onFlaggedUpdate, size: 'sm', type: 'button', children: 'Update' })
      })
    ] })
  }
  const canCombine = reason === 'edited'
  return jsxs(Callout, { tone: 'warn', children: [
    jsx('p', { className: 'font-medium text-(--ui-text-primary)', children: canCombine
      ? 'You edited this skill, and a new version is out.'
      : 'You edited this skill. Hermes can\'t combine automatically.' }),
    canCombine && row.status?.summary ? jsx('p', { children: row.status.summary }) : null,
    !canCombine ? jsx('p', { children: 'Keep your copy, or switch to the new version (your copy goes to the archive).' }) : null,
    canCombine ? jsx(Button, { onClick: () => setShowFull(value => !value), size: 'inline', type: 'button', variant: 'ghost', children: showFull ? 'Hide full skill' : 'Show full skill' }) : null,
    canCombine && showFull
      ? full.isLoading
        ? jsx('p', { className: 'text-(--ui-text-tertiary)', children: 'Preparing the combined version…' })
        : full.data?.available
          ? jsx('pre', { className: 'max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-[4px] border border-(--ui-stroke-tertiary) p-2 font-mono text-[0.7rem]', children: full.data.skill_md })
          : jsx('p', { className: 'text-(--ui-text-tertiary)', children: full.error ? 'The combined version is unavailable right now.' : 'Hermes can\'t combine automatically.' })
      : null,
    jsxs('div', { className: 'flex flex-wrap gap-2 pt-1', children: [
      canCombine ? jsx(Button, { disabled: busy, onClick: () => onCombine('combine'), size: 'sm', type: 'button', children: 'Combine' }) : null,
      jsx(Button, { disabled: busy, onClick: () => onCombine('keep_mine'), size: 'sm', type: 'button', variant: 'outline', children: 'Keep mine' }),
      jsx(Button, { disabled: busy, onClick: () => onCombine('use_new'), size: 'sm', type: 'button', variant: 'ghost', children: 'Use new version' })
    ] })
  ] })
}

function SkillDetail({ actions, client, lifecycle, selection, viewId, viewedUpdate }) {
  const { kind, row } = selection
  const installed = kind === 'installed'
  const shared = kind === 'shared' || (kind === 'browse' && row.shared_by_me)
  const status = installed ? (viewedUpdate || row.status || {}) : {}
  const needsLook = installed && status.kind === 'needs_look'
  const version = installed ? row.installed_version || row.latest_version : row.latest_version
  const when = shortAgo(installed ? row.applied_at || row.updated_at : row.updated_at)
  const updateAvailable = installed && !needsLook && row.auto_update === false &&
    isPositiveInt(row.latest_version) && isPositiveInt(row.installed_version) && row.latest_version > row.installed_version
  const browseInstalled = kind === 'browse' && row.installed
  const powers = status.kind === 'updated' ? newPowers(status.new_capabilities) : null

  const footer = []
  if (kind === 'browse' && !row.installed && !row.shared_by_me) {
    footer.push(jsx(Button, { disabled: actions.busy, loading: actions.installing === row.skill_id, onClick: () => actions.install(row), size: 'sm', type: 'button', children: 'Install' }, 'install'))
    footer.push(jsx('span', { className: 'text-[0.68rem] text-(--ui-text-tertiary)', children: 'Nothing runs until you use it.' }, 'hint'))
  }
  if (browseInstalled) footer.push(jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Installed' }, 'installed'))
  if (shared) {
    footer.push(row.edited
      ? jsx(Button, { disabled: actions.busy, onClick: () => actions.share(row.source_name || row.slug, { update: true, folder: row.folder, name: rowName(row) }), size: 'sm', type: 'button', children: 'Share update' }, 'share')
      : jsx(Button, { disabled: true, size: 'sm', type: 'button', variant: 'secondary', children: 'Up to date' }, 'share'))
  }
  footer.push(jsx('span', { className: 'flex-1' }, 'spacer'))
  if (row.portal_url) footer.push(jsx(Button, { onClick: () => actions.openPortal(row.portal_url), size: 'sm', type: 'button', variant: 'ghost', children: 'Open in Portal ↗' }, 'portal'))
  if (installed) footer.push(jsx(Button, { disabled: actions.busy, onClick: () => actions.uninstall(row), size: 'sm', type: 'button', variant: 'ghost', className: 'text-(--ui-danger)', children: 'Uninstall' }, 'uninstall'))

  return jsxs('main', { className: 'flex min-h-0 flex-col overflow-hidden', children: [
    jsx('div', { className: 'min-h-0 flex-1 overflow-y-auto overscroll-contain', children: jsxs('div', { className: 'mx-auto max-w-2xl space-y-5 px-5 py-4', children: [
      jsxs('header', { className: 'space-y-1.5', children: [
        jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
          jsx('h2', { className: 'text-base font-semibold text-(--ui-text-primary)', children: rowName(row) }),
          row.folder ? jsx(Badge, { variant: 'muted', children: row.folder }) : null,
          shared ? jsx(Badge, { variant: row.edited ? 'warn' : 'success', children: row.edited ? 'Edited since you shared' : 'Shared' }) : null,
          installed ? jsx(WisdomBadge, { badge: statusBadge({ ...row, status }) }) : null
        ] }),
        row.description ? jsx('p', { className: 'text-xs leading-5 text-(--ui-text-secondary)', children: row.description }) : null
      ] }),
      jsxs('div', { className: 'divide-y divide-(--ui-stroke-tertiary)', children: [
        jsx(KV, { name: 'Folder', value: row.folder || 'No folder' }),
        jsx(KV, { name: 'Owner', value: row.owner }),
        jsx(KV, { name: 'Version', value: version ? [`v${version}`, installed && updateAvailable ? `v${row.latest_version} available` : null, when].filter(Boolean).join(' · ') : when }),
        jsx(KV, { name: 'Used by', value: usedByText(row) })
      ] }),
      kind === 'browse' && row.local_name_collision ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'You also have a local skill with this name. The team copy installs alongside it.' }) : null,
      installed && status.kind === 'no_longer_shared' ? jsx(Callout, { children: jsx('p', { children: 'No longer shared. Your copy keeps working but won\'t receive updates.' }) }) : null,
      installed && status.kind === 'updated' ? jsx(Callout, { tone: 'good', children: jsx('p', { children: powers ? `Updated — now ${powers}.` : 'Updated to the latest version.' }) }) : null,
      updateAvailable ? jsx(Callout, { children: jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
        jsx('span', { className: 'flex-1', children: `v${row.latest_version} is available. Nothing changes until you update.` }),
        jsx(Button, { disabled: actions.busy, onClick: () => actions.manualUpdate(row), size: 'xs', type: 'button', children: 'Update' })
      ] }) }) : null,
      installed && status.kind !== 'no_longer_shared' ? jsxs('div', { className: 'flex items-start justify-between gap-4 rounded-[6px] border border-(--ui-stroke-tertiary) p-3', children: [
        jsxs('div', { className: 'space-y-0.5', children: [
          jsx('div', { className: 'text-xs font-medium text-(--ui-text-primary)', children: 'Auto-update' }),
          jsx('p', { className: 'text-[0.68rem] leading-4 text-(--ui-text-tertiary)', children: row.auto_update
            ? 'New versions install automatically. If you\'ve edited it, Hermes asks first.'
            : 'You\'ll see new versions here, but nothing changes until you update.' })
        ] }),
        jsx(Switch, { 'aria-label': 'Auto-update', checked: row.auto_update === true, disabled: actions.busy, onCheckedChange: value => actions.setAutoUpdate(row, value === true) })
      ] }) : null,
      needsLook
        ? jsx(NeedsLookPanel, {
            busy: actions.busy, client, lifecycle, row: { ...row, status }, viewId,
            onCombine: choice => actions.combine(row, choice),
            onFlaggedUpdate: () => actions.flaggedUpdate(row)
          })
        : jsxs('section', { className: 'space-y-2', children: [
            jsx('h3', { className: 'text-[0.65rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)', children: 'SKILL.md' }),
            jsx(SkillPreview, { client, lifecycle, skillId: row.skill_id })
          ] })
    ] }) }),
    jsx('footer', { className: 'shrink-0 border-t border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background) px-5 py-2.5', children:
      jsx('div', { className: 'mx-auto flex max-w-2xl flex-wrap items-center gap-2', children: footer })
    })
  ] })
}

function ShareDialog({ client, lifecycle, onClose, request, viewId }) {
  const queryClient = useQueryClient()
  const [prepared, setPrepared] = useState(null)
  const [folder, setFolder] = useState('')
  const [menu, setMenu] = useState(false)
  const [rename, setRename] = useState('')
  const [nameTaken, setNameTaken] = useState(false)
  const [checks, setChecks] = useState(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const start = useCurrentMutation(client, lifecycle, () => client.rest('/share/start', {
    method: 'POST', body: requestBody(viewId, { skill_name: request.skillName })
  }), value => {
    setPrepared(value)
    setFolder(text(request.folder || value?.suggestion?.folder))
    setNameTaken(value?.name_taken === true)
    setChecks(value?.checks || null)
  }, 'Could not prepare this skill for sharing — nothing was shared', () => onClose())
  useEffect(() => { start.mutate() }, [])
  const confirm = useCurrentMutation(client, lifecycle, () => client.rest('/share/confirm', {
    method: 'POST',
    body: requestBody(viewId, {
      draft_id: prepared.draft_id,
      skill_name: request.skillName,
      folder: folder.trim() || null,
      rename: nameTaken && rename.trim() ? rename.trim() : null,
      accept_warnings: acknowledged
    })
  }), async (result, token, isCurrent) => {
    if (result?.state === 'name_taken') { setNameTaken(true); return }
    if (result?.state === 'checks_failed') { setChecks(result.checks || { ok: false, blocked: true, messages: [] }); return }
    await queryClient.invalidateQueries({ queryKey: libraryKey(lifecycle) })
    if (!isCurrent(token)) return
    const name = text(result?.skill_name || request.skillName)
    if (result?.state === 'shared') {
      host.notify({ kind: 'success', message: request.update ? `Shared an update to ${name}.` : `Shared ${name}${result.folder ? ` in ${result.folder}` : ''}.` })
    } else if (result?.state === 'submitted') {
      host.notify({ kind: 'info', message: `${name} was sent to your team admins for review.` })
    } else {
      host.notify({ kind: 'warning', message: `Sharing ${name} didn't finish. Check again before retrying.` })
    }
    onClose()
  }, 'Sharing was not completed')
  const options = Array.isArray(prepared?.folder_options) ? prepared.folder_options : []
  const typed = folder.trim()
  const matches = options.filter(option => !typed || text(option.name).toLowerCase().includes(typed.toLowerCase()))
  const isNewFolder = typed && !options.some(option => text(option.name).toLowerCase() === typed.toLowerCase())
  const suggestion = prepared?.suggestion || {}
  const showReason = !request.folder && suggestion.reason && typed && typed === text(suggestion.folder)
  const files = Array.isArray(prepared?.files) ? prepared.files : []
  const totalSize = files.reduce((sum, file) => sum + (Number.isInteger(file.size) ? file.size : 0), 0)
  const team = text(prepared?.team_name) || 'your team'
  const checksFailed = checks && checks.ok === false
  const blocked = checksFailed && checks.blocked
  const shareDisabled = !prepared || confirm.isPending || blocked ||
    (checksFailed && checks.needs_acknowledgement && !acknowledged) || (nameTaken && !rename.trim())
  return jsx(Dialog, { open: true, onOpenChange: open => { if (!open && !confirm.isPending) onClose() }, children:
    jsxs(DialogContent, { className: 'max-w-[32rem] text-(--ui-text-primary)', children: [
      jsxs(DialogHeader, { children: [
        jsx(DialogTitle, { children: request.update ? `Share an update to ${request.name || request.skillName}` : `Share ${request.skillName}` }),
        jsx(DialogDescription, { className: 'text-(--ui-text-tertiary)', children: request.update
          ? 'Teammates with auto-update on get it automatically.'
          : `Everyone in ${team} can find and install it. You'll be listed as the owner.` })
      ] }),
      !prepared
        ? jsx('p', { className: 'py-6 text-center text-xs text-(--ui-text-tertiary)', children: 'Preparing your skill and choosing a folder…' })
        : jsxs('div', { className: 'space-y-4', children: [
            nameTaken ? jsxs('div', { className: 'space-y-1.5', children: [
              jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: `A teammate already shares a skill called ${request.skillName}. Rename yours to share it.` }),
              jsx(Input, { 'aria-label': 'New name', autoFocus: true, onChange: event => setRename(event.target.value), placeholder: 'New name', value: rename })
            ] }) : null,
            jsxs('div', { className: 'relative space-y-1', children: [
              jsxs('label', { className: 'flex items-center justify-between text-[0.68rem] font-medium text-(--ui-text-tertiary)', children: [
                'Folder',
                showReason ? jsx('span', { className: 'font-normal', children: 'Suggested' }) : null
              ] }),
              jsxs('div', { className: 'flex gap-1', children: [
                jsx(Input, {
                  'aria-label': 'Folder', 'aria-expanded': menu, className: 'flex-1', maxLength: 48, role: 'combobox',
                  onChange: event => { setFolder(event.target.value); setMenu(true) },
                  onFocus: () => setMenu(true),
                  onKeyDown: event => { if (event.key === 'Escape') setMenu(false); if (event.key === 'Enter') setMenu(false) },
                  placeholder: 'Pick or type a folder', value: folder
                }),
                jsx(Button, { 'aria-label': 'Show folders', onClick: () => setMenu(value => !value), size: 'sm', type: 'button', variant: 'ghost', children: jsx(Codicon, { name: 'chevron-down', size: '0.8rem' }) })
              ] }),
              menu ? jsxs('div', { className: 'absolute inset-x-0 top-full z-20 mt-1 max-h-52 overflow-y-auto rounded-[6px] border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) p-1 shadow-lg', role: 'listbox', children: [
                ...matches.map(option => jsxs('button', {
                  className: 'flex w-full items-center justify-between rounded-[4px] px-2 py-1 text-left text-xs hover:bg-(--ui-row-active-background)',
                  onClick: () => { setFolder(text(option.name)); setMenu(false) }, role: 'option', type: 'button',
                  children: [jsx('span', { children: option.name }), jsx('span', { className: 'tabular-nums text-(--ui-text-quaternary)', children: option.count || '' })]
                }, option.name)),
                isNewFolder ? jsx('button', { className: 'w-full rounded-[4px] px-2 py-1 text-left text-xs text-(--ui-text-secondary) hover:bg-(--ui-row-active-background)', onClick: () => setMenu(false), type: 'button', children: `+ New folder "${typed}"` }) : null,
                !matches.length && !isNewFolder ? jsx('p', { className: 'px-2 py-1 text-xs text-(--ui-text-tertiary)', children: 'Type a name to create a folder.' }) : null
              ] }) : null,
              showReason ? jsx('p', { className: 'text-[0.68rem] text-(--ui-text-tertiary)', children: suggestion.reason }) : null
            ] }),
            jsxs('details', { className: 'rounded-[6px] border border-(--ui-stroke-tertiary) text-xs', children: [
              jsxs('summary', { className: 'flex cursor-pointer items-center justify-between px-3 py-2 text-(--ui-text-secondary)', children: [
                jsx('span', { children: `${files.length} ${files.length === 1 ? 'file' : 'files'}${totalSize ? ` · ${formatSize(totalSize)}` : ''}` }),
                jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'Show' })
              ] }),
              jsx('ul', { className: 'space-y-0.5 border-t border-(--ui-stroke-tertiary) px-3 py-2', children: files.map(file =>
                jsxs('li', { className: 'flex justify-between gap-2 font-mono text-[0.68rem]', children: [
                  jsx('span', { className: 'truncate', children: file.path }),
                  jsx('span', { className: 'shrink-0 text-(--ui-text-quaternary)', children: formatSize(file.size) })
                ] }, file.path)
              ) })
            ] }),
            checksFailed ? jsxs(Callout, { tone: 'warn', children: [
              jsx('p', { className: 'font-medium text-(--ui-text-primary)', children: blocked ? 'A check failed, so this skill can\'t be shared yet.' : 'A check found something to look at before sharing.' }),
              Array.isArray(checks.messages) && checks.messages.length
                ? jsx('ul', { className: 'list-disc space-y-0.5 pl-4', children: checks.messages.map((message, index) => jsx('li', { children: message }, index)) })
                : null,
              !blocked && checks.needs_acknowledgement ? jsxs('label', { className: 'flex items-center gap-2', children: [
                jsx(Checkbox, { 'aria-label': 'I have read these warnings', checked: acknowledged, onCheckedChange: value => setAcknowledged(value === true) }),
                'I\'ve read these and still want to share.'
              ] }) : null
            ] }) : null
          ] }),
      jsxs('div', { className: 'flex items-center justify-end gap-2 pt-2', children: [
        jsx(Button, { disabled: confirm.isPending, onClick: onClose, size: 'sm', type: 'button', variant: 'ghost', children: 'Cancel' }),
        jsx(Button, { disabled: shareDisabled, loading: confirm.isPending, onClick: () => confirm.mutate(), size: 'sm', type: 'button', children: 'Share' })
      ] })
    ] })
  })
}

function SharePicker({ client, lifecycle, onClose, onPick }) {
  const [search, setSearch] = useState('')
  const skills = useQuery(queryOptions(['local-skills', lifecycle.key], () => client.rest('/local-skills')))
  const rows = (Array.isArray(skills.data?.skills) ? skills.data.skills : []).filter(row => {
    const needle = search.trim().toLowerCase()
    return !needle || `${row.name} ${row.description}`.toLowerCase().includes(needle)
  })
  return jsx(Dialog, { open: true, onOpenChange: open => { if (!open) onClose() }, children:
    jsxs(DialogContent, { className: 'max-w-[30rem] text-(--ui-text-primary)', children: [
      jsxs(DialogHeader, { children: [
        jsx(DialogTitle, { children: 'Share a skill' }),
        jsx(DialogDescription, { className: 'text-(--ui-text-tertiary)', children: 'Pick one of your skills. Only that skill is shared.' })
      ] }),
      jsx(Input, { 'aria-label': 'Search your skills', autoFocus: true, onChange: event => setSearch(event.target.value), placeholder: 'Search your skills', type: 'search', value: search }),
      jsx('div', { className: 'max-h-[min(55vh,26rem)] overflow-y-auto', children: skills.isLoading
        ? jsx('p', { className: 'py-6 text-center text-xs text-(--ui-text-tertiary)', children: 'Loading your skills…' })
        : skills.error
          ? jsx(ReadFailure, { error: skills.error, onRetry: () => skills.refetch(), title: 'Your skills are unavailable' })
          : rows.length
            ? rows.map(row => jsx(ListRow, { onSelect: () => onPick(row.name), subtitle: row.description, title: row.name }, row.name))
            : jsx(EmptyState, { title: search ? 'No matching skills' : 'Nothing left to share', description: search ? undefined : 'Every skill you made is already shared, or you haven\'t made one yet.' })
      })
    ] })
  })
}

function InstallConfirm({ busy, onCancel, onConfirm, pending }) {
  if (!pending) return null
  return jsx(Dialog, { open: true, onOpenChange: open => { if (!open) onCancel() }, children:
    jsxs(DialogContent, { className: 'max-w-[26rem] text-(--ui-text-primary)', children: [
      jsxs(DialogHeader, { children: [
        jsx(DialogTitle, { children: `Install ${pending.skill_name}?` }),
        jsx(DialogDescription, { className: 'text-(--ui-text-secondary)', children: pending.sentence })
      ] }),
      jsxs('div', { className: 'flex justify-end gap-2 pt-2', children: [
        jsx(Button, { disabled: busy, onClick: onCancel, size: 'sm', type: 'button', variant: 'ghost', children: 'Cancel' }),
        jsx(Button, { disabled: busy, loading: busy, onClick: onConfirm, size: 'sm', type: 'button', children: 'Install' })
      ] })
    ] })
  })
}

function useLibraryActions({ client, lifecycle, viewId, onShare }) {
  const queryClient = useQueryClient()
  const [confirmInstall, setConfirmInstall] = useState(null)
  const [setupPermission, setSetupPermission] = useState(null)
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: libraryKey(lifecycle) }),
    queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'preview', lifecycle.key] }),
    queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'combine', lifecycle.key] })
  ])
  const install = useCurrentMutation(client, lifecycle, ({ row, confirmed = false }) => client.rest('/install/now', {
    method: 'POST', body: requestBody(viewId, { skill_id: row.skill_id, auto_update: true, confirmed })
  }), async (result, token, isCurrent) => {
    if (result?.state === 'confirm') { setConfirmInstall(result); return }
    setConfirmInstall(null)
    if (result?.state === 'blocked') {
      host.notify({ kind: 'warning', message: text(result.sentence) })
      return
    }
    await refresh()
    if (!isCurrent(token)) return
    if (result?.state === 'installed') {
      host.notify({ kind: 'success', message: `Installed ${result.skill_name}. Ready in new sessions. Auto-update is ${result.auto_update === false ? 'off' : 'on'}.` })
      if (Array.isArray(result.setup_commands) && result.setup_commands.length && result.receipt) {
        setSetupPermission({ receipt: result.receipt, commands: result.setup_commands.map(command => typeof command === 'string' ? command : JSON.stringify(command)), name: result.skill_name, operation: 'install' })
      }
    } else {
      host.notify({ kind: 'warning', message: `Installing ${text(result?.skill_name) || 'this skill'} didn't finish. Check Installed before trying again.` })
    }
  }, 'Install did not complete')
  const runSetup = useCurrentMutation(client, lifecycle, pending => client.rest('/install/setup', {
    method: 'POST', body: requestBody(viewId, { receipt: pending.receipt })
  }), (_result, token, isCurrent) => {
    const name = setupPermission?.name
    setSetupPermission(null)
    if (isCurrent(token)) host.notify({ kind: 'success', message: `Setup finished for ${name}.` })
  }, 'Setup did not complete. Check its status before trying again.', () => setSetupPermission(null))
  const restore = useCurrentMutation(client, lifecycle, row => client.rest(`/uninstall/${encodeURIComponent(row.skill_id)}/undo`, {
    method: 'POST', body: requestBody(viewId)
  }).then(result => ({ result, row })), async ({ row }, token, isCurrent) => {
    await refresh()
    if (isCurrent(token)) host.notify({ kind: 'success', message: `Restored ${rowName(row)}.` })
  }, 'Could not restore the skill')
  const uninstall = useCurrentMutation(client, lifecycle, row => client.rest(`/uninstall/${encodeURIComponent(row.skill_id)}`, {
    method: 'POST', body: requestBody(viewId)
  }).then(result => ({ result, row })), async ({ result, row }, token, isCurrent) => {
    await refresh()
    if (!isCurrent(token)) return
    if (result?.uninstalled === true || result?.state === 'inactive' || result?.state === 'done') {
      host.notify({ kind: 'success', message: `Removed ${rowName(row)}`, detail: 'Moved to the archive.', action: { label: 'Undo', onClick: () => restore.mutate(row) } })
    } else {
      host.notify({ kind: 'warning', message: `Removing ${rowName(row)} didn't finish. Check Installed before trying again.` })
    }
  }, 'Uninstall did not complete')
  const setAutoUpdate = useCurrentMutation(client, lifecycle, ({ row, enabled }) => client.rest(`/installations/${encodeURIComponent(row.skill_id)}/auto-update`, {
    method: 'PATCH', body: requestBody(viewId, { enabled })
  }), refresh, 'Auto-update was not changed')
  const combine = useCurrentMutation(client, lifecycle, ({ row, choice }) => client.rest(`/combine/${encodeURIComponent(row.skill_id)}`, {
    method: 'POST', body: requestBody(viewId, { choice })
  }).then(result => ({ result, row, choice })), async ({ row, choice }, token, isCurrent) => {
    await refresh()
    if (!isCurrent(token)) return
    const name = rowName(row)
    host.notify({ kind: 'success', message: choice === 'keep_mine'
      ? `Kept your version of ${name}. The next version will ask again.`
      : choice === 'combine' ? `Combined ${name}. Your old copy is in the archive.` : `Updated ${name}. Your copy is in the archive.` })
  }, 'The update was not applied')
  const flaggedUpdate = useCurrentMutation(client, lifecycle, row => client.rest(`/update/${encodeURIComponent(row.skill_id)}/flagged`, {
    method: 'POST', body: requestBody(viewId)
  }).then(result => ({ result, row })), async ({ row }, token, isCurrent) => {
    await refresh()
    if (isCurrent(token)) host.notify({ kind: 'success', message: `Updated ${rowName(row)}.` })
  }, 'The update was not applied')
  const manualUpdate = useCurrentMutation(client, lifecycle, row => client.rest(`/update/${encodeURIComponent(row.skill_id)}/apply`, {
    method: 'POST', body: requestBody(viewId)
  }).then(result => ({ result, row })), async ({ result, row }, token, isCurrent) => {
    await refresh()
    if (!isCurrent(token)) return
    if (result?.updated === true) host.notify({ kind: 'success', message: `Updated ${rowName(row)}.` })
    else host.notify({ kind: 'warning', message: `Updating ${rowName(row)} didn't finish. Check again before retrying.` })
  }, 'The update was not applied')
  const busy = install.isPending || uninstall.isPending || restore.isPending || setAutoUpdate.isPending ||
    combine.isPending || flaggedUpdate.isPending || manualUpdate.isPending
  return {
    busy,
    installing: install.isPending ? install.variables?.row?.skill_id : null,
    install: row => install.mutate({ row }),
    uninstall: row => uninstall.mutate(row),
    setAutoUpdate: (row, enabled) => setAutoUpdate.mutate({ row, enabled }),
    combine: (row, choice) => combine.mutate({ row, choice }),
    flaggedUpdate: row => flaggedUpdate.mutate(row),
    manualUpdate: row => manualUpdate.mutate(row),
    share: (skillName, options = {}) => onShare({ skillName, ...options }),
    openPortal: url => { if (/^https:\/\//.test(text(url))) void client.openExternal?.(url) },
    dialogs: [
      jsx(InstallConfirm, {
        busy: install.isPending, pending: confirmInstall,
        onCancel: () => setConfirmInstall(null),
        onConfirm: () => install.mutate({ row: { skill_id: confirmInstall.skill_id }, confirmed: true })
      }, 'install-confirm'),
      setupPermission ? jsx(Dialog, { open: true, onOpenChange: open => { if (!open) setSetupPermission(null) }, children:
        jsx(DialogContent, { className: 'max-w-[32rem] text-(--ui-text-primary)', children:
          jsx(SetupPermission, { busy: runSetup.isPending, pending: setupPermission, onDefer: () => setSetupPermission(null), onRun: () => runSetup.mutate(setupPermission) })
        })
      }, 'setup') : null
    ]
  }
}

function Library({ client, data, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const [view, setView] = useState(data.needs_look > 0 || data.installed.length || data.shared.length ? 'installed' : 'browse')
  const [selectedKey, setSelectedKey] = useState(null)
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState(() => new Set())
  const [picker, setPicker] = useState(false)
  const [share, setShare] = useState(null)
  const [viewedUpdates, setViewedUpdates] = useState({})
  const seenRef = useRef(new Set())
  const actions = useLibraryActions({ client, lifecycle, viewId, onShare: setShare })
  useSyncExternalStore(listener => pageIntent.subscribe(listener), () => pageIntent.snapshot(), () => pageIntent.snapshot())
  useEffect(() => { if (pageIntent.value?.share) { pageIntent.take(); setPicker(true) } })

  const needle = search.trim().toLowerCase()
  const matches = row => !needle || [row.name, row.slug, row.description, row.owner, row.folder].some(value => text(value).toLowerCase().includes(needle))
  const installed = data.installed.filter(matches)
  const shared = data.shared.filter(matches)
  const browse = data.browse.filter(matches)
  const needsLook = installed.filter(row => row.status?.kind === 'needs_look')
  const otherInstalled = installed.filter(row => row.status?.kind !== 'needs_look')
  const groups = new Map()
  for (const row of browse) {
    const name = text(row.folder) || 'Other'
    if (!groups.has(name)) groups.set(name, [])
    groups.get(name).push(row)
  }
  const folders = [...groups.keys()].sort((a, b) => (a === 'Other') - (b === 'Other') || a.localeCompare(b))

  const entries = view === 'installed'
    ? [...needsLook.map(row => ['installed', row]), ...shared.map(row => ['shared', row]), ...otherInstalled.map(row => ['installed', row])]
    : folders.flatMap(name => groups.get(name).map(row => ['browse', row]))
  const keyOf = (kind, row) => `${kind}:${row.skill_id}`
  const selectedEntry = entries.find(([kind, row]) => keyOf(kind, row) === selectedKey) || entries[0] || null
  const selection = selectedEntry ? { kind: selectedEntry[0], row: selectedEntry[1] } : null

  // Viewing an Updated item clears its badge; keep showing what changed while it stays open.
  useEffect(() => {
    const row = selection?.kind === 'installed' ? selection.row : null
    if (!row || row.status?.kind !== 'updated' || row.status?.seen !== false || seenRef.current.has(row.skill_id)) return
    seenRef.current.add(row.skill_id)
    setViewedUpdates(current => ({ ...current, [row.skill_id]: row.status }))
    client.rest(`/status/${encodeURIComponent(row.skill_id)}/seen`, { method: 'POST', body: requestBody(viewId) })
      .then(() => queryClient.invalidateQueries({ queryKey: libraryKey(lifecycle) }))
      .catch(() => { seenRef.current.delete(row.skill_id) })
  }, [selection?.row?.skill_id, selection?.row?.status?.kind, selection?.row?.status?.seen])

  const teamEmpty = !data.browse.length && !data.installed.length && !data.shared.length
  const shareButton = jsx('button', { className: 'cursor-pointer text-[0.68rem] font-medium text-(--ui-text-tertiary) hover:text-(--ui-text-primary)', onClick: () => setPicker(true), type: 'button', children: 'Share a skill…' })
  const row = (kind, item) => {
    const key = keyOf(kind, item)
    const active = selection && keyOf(selection.kind, selection.row) === key
    if (kind === 'shared') {
      return jsx(ListRow, { active, badge: { label: item.edited ? 'Edited since you shared' : 'Shared', variant: item.edited ? 'warn' : 'success' }, meta: usedByText(item), onSelect: () => setSelectedKey(key), subtitle: [item.folder, item.no_longer_shared ? 'No longer shared' : null].filter(Boolean).join(' · ') || item.description, title: rowName(item) }, key)
    }
    if (kind === 'installed') {
      const status = viewedUpdates[item.skill_id] && item.status?.kind !== 'needs_look' ? { ...item.status, seen: true } : item.status
      return jsx(ListRow, { active, badge: statusBadge({ ...item, status }), onSelect: () => setSelectedKey(key), subtitle: [item.owner, updatedLabel(item.applied_at || item.updated_at)].filter(Boolean).join(' · '), title: rowName(item) }, key)
    }
    const action = item.installed
      ? jsx('span', { className: 'text-[0.62rem] text-(--ui-text-quaternary)', children: 'Installed' })
      : item.shared_by_me
        ? jsx('span', { className: 'text-[0.62rem] text-(--ui-text-quaternary)', children: 'Yours' })
        : jsx(Button, { disabled: actions.busy, loading: actions.installing === item.skill_id, onClick: () => actions.install(item), size: 'xs', type: 'button', variant: 'secondary', children: 'Install' })
    return jsx(ListRow, { action, active, meta: usedByText(item), onSelect: () => setSelectedKey(key), subtitle: [item.owner, item.description].filter(Boolean).join(' · '), title: rowName(item) }, key)
  }

  let list
  if (teamEmpty) {
    list = jsxs('div', { className: 'space-y-3 px-2 py-8 text-center', children: [
      jsx(EmptyState, { className: 'min-h-0', title: 'Nobody has shared a skill yet', description: 'Share one of yours and your team can install it.' }),
      jsx(Button, { onClick: () => setPicker(true), size: 'sm', type: 'button', children: 'Share a skill' })
    ] })
  } else if (view === 'installed') {
    list = !installed.length && !shared.length
      ? jsxs('div', { className: 'space-y-3 px-2 py-8 text-center', children: [
          jsx(EmptyState, { className: 'min-h-0', title: needle ? 'No matches' : 'Nothing installed yet', description: needle ? undefined : 'Browse what your team shared.' }),
          needle ? null : jsx(Button, { onClick: () => setView('browse'), size: 'sm', type: 'button', variant: 'secondary', children: 'Browse' })
        ] })
      : [
          needsLook.length ? jsx(GroupHeader, { count: needsLook.length, label: 'Needs a look' }, 'g-needs') : null,
          ...needsLook.map(item => row('installed', item)),
          shared.length ? jsx(GroupHeader, { count: shared.length, label: 'Shared by you' }, 'g-shared') : null,
          ...shared.map(item => row('shared', item)),
          otherInstalled.length ? jsx(GroupHeader, { count: otherInstalled.length, label: 'From your team' }, 'g-team') : null,
          ...otherInstalled.map(item => row('installed', item))
        ]
  } else {
    list = !browse.length
      ? jsx(EmptyState, { className: 'min-h-32', title: 'No matches', description: `No team skills match “${search.trim()}”.` })
      : folders.flatMap(name => {
          const open = !collapsed.has(name)
          return [
            jsx(GroupHeader, { collapsible: true, count: groups.get(name).length, label: name, open, onToggle: () => setCollapsed(current => {
              const next = new Set(current)
              if (next.has(name)) next.delete(name)
              else next.add(name)
              return next
            }) }, `g-${name}`),
            ...(open ? groups.get(name).map(item => row('browse', item)) : [])
          ]
        })
  }

  return jsxs('div', { className: 'flex h-full min-h-0 flex-col', children: [
    jsxs('header', { className: 'flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-2.5', children: [
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx(Codicon, { name: 'library', size: '1rem' }),
        jsx('h1', { className: 'text-sm font-semibold', children: 'Collective Wisdom' }),
        data.team_name ? jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: data.team_name }) : null
      ] }),
      jsx(ViewSwitch, { dot: data.needs_look > 0, onChange: next => { setView(next); setSelectedKey(null) }, value: view })
    ] }),
    jsxs('div', { className: 'grid min-h-0 flex-1 grid-cols-1 sm:grid-cols-[minmax(16rem,0.75fr)_minmax(0,1fr)]', children: [
      jsxs('aside', { className: 'flex min-h-0 flex-col border-r border-(--ui-stroke-tertiary) p-2', children: [
        jsxs('div', { className: 'mb-1 flex h-6 shrink-0 items-center justify-between gap-2 pl-2 pr-1', children: [
          jsx('span', { className: 'text-[0.68rem] font-medium text-(--ui-text-tertiary)', children: view === 'installed' ? 'Installed' : `${data.team_name || 'Team'} skills` }),
          shareButton
        ] }),
        teamEmpty ? null : jsx(Input, { 'aria-label': 'Search skills', className: 'mb-1 h-7 text-xs', onChange: event => setSearch(event.target.value), placeholder: 'Search names, owners, folders', type: 'search', value: search }),
        jsx('div', { className: 'min-h-0 flex-1 overflow-y-auto overscroll-contain', children: list })
      ] }),
      selection
        ? jsx(SkillDetail, { actions, client, lifecycle, selection, viewId, viewedUpdate: viewedUpdates[selection.row.skill_id] && selection.row.status?.kind !== 'needs_look' ? viewedUpdates[selection.row.skill_id] : null }, keyOf(selection.kind, selection.row))
        : jsx('main', { className: 'grid min-h-0 place-items-center p-6', children: jsx(EmptyState, { title: 'Select a skill', description: 'Its details show here.' }) })
    ] }),
    picker ? jsx(SharePicker, { client, lifecycle, onClose: () => setPicker(false), onPick: name => { setPicker(false); setShare({ skillName: name }) } }) : null,
    share ? jsx(ShareDialog, { client, lifecycle, onClose: () => setShare(null), request: share, viewId }, share.skillName) : null,
    ...actions.dialogs
  ] })
}

function SignedOut({ onRetry }) {
  return jsx('div', { className: 'grid h-full place-items-center p-8', children: jsxs('div', { className: 'max-w-sm space-y-3 text-center', children: [
    jsx(Codicon, { name: 'organization', size: '1.75rem' }),
    jsx('h2', { className: 'text-sm font-semibold', children: 'Share skills with your team' }),
    jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: 'See the skills your teammates shared and share your own. Available on Nous team plans.' }),
    jsxs('div', { className: 'flex justify-center gap-2', children: [
      jsx(Button, { onClick: () => host.navigate('/settings?tab=providers'), size: 'sm', type: 'button', children: 'Sign in with Nous' }),
      jsx(Button, { onClick: onRetry, size: 'sm', type: 'button', variant: 'ghost', children: 'Check again' })
    ] })
  ] }) })
}

function ConsentGate({ client, consent, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const accept = useCurrentMutation(client, lifecycle, () => client.rest('/consent', {
    method: 'POST', body: requestBody(viewId)
  }), async () => {
    await queryClient.invalidateQueries({ queryKey: [...ROOT_KEY, 'consent', lifecycle.key] })
    await queryClient.invalidateQueries({ queryKey: libraryKey(lifecycle) })
  }, 'Collective Wisdom could not be turned on')
  return jsx('div', { className: 'grid h-full place-items-center p-8', children: jsxs('div', { className: 'max-w-md space-y-4', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [jsx(Codicon, { name: 'library', size: '1rem' }), jsx('h2', { className: 'text-sm font-semibold', children: 'Collective Wisdom' })] }),
    jsx('p', { className: 'text-xs leading-5 text-(--ui-text-secondary)', children: `Share skills with ${text(consent?.team_name) || 'your team'} and install the ones they share.` }),
    jsx('p', { className: 'whitespace-pre-line rounded-[6px] border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-3 text-xs leading-5 text-(--ui-text-secondary)', children: text(consent?.disclosure_text) }),
    jsx('div', { className: 'flex justify-end', children: jsx(Button, { disabled: !viewId, loading: accept.isPending, onClick: () => accept.mutate(), size: 'sm', type: 'button', children: 'Continue' }) })
  ] }) })
}

function WisdomPage({ client }) {
  const connectionId = useValue(host.state.connectionId) || ''
  const profile = useValue(host.state.profile) || 'default'
  const gateway = useValue(host.state.gateway) || 'unknown'
  const [manualGeneration, setManualGeneration] = useState(0)
  const tracker = useRef({ generation: 0, gateway: null, manualGeneration: -1, mounted: true, signature: null }).current
  tracker.mounted = true
  const lifecycle = { ...lifecycleFor({ connectionId, profile }, gateway, manualGeneration, tracker), mounted: () => tracker.mounted }
  const available = client.active() && Boolean(connectionId) && gateway === 'open'
  const consent = useQuery(queryOptions(['consent', lifecycle.key], () => client.rest('/consent'), available))
  const view = useQuery(queryOptions(['view', lifecycle.key], () => client.rest('/views', { method: 'POST' }), available && Boolean(consent.data)))
  const ready = consent.data?.accepted === true && consent.data?.configured === true
  const library = useQuery(queryOptions(['library', lifecycle.key], () => client.rest('/library'), available && ready))
  useEffect(() => () => { tracker.mounted = false; tracker.generation += 1 }, [tracker])
  const reload = () => setManualGeneration(value => value + 1)

  if (!available) {
    return jsx('div', { className: 'grid h-full place-items-center p-8', children: jsxs('div', { className: 'space-y-3 text-center', children: [
      jsx(EmptyState, { className: 'min-h-0', title: 'Collective Wisdom unavailable', description: connectionId ? `Hermes is ${gateway}. Reconnect to continue.` : 'Hermes Desktop did not provide a connection for this page.' }),
      jsx(Button, { onClick: reload, size: 'sm', type: 'button', variant: 'secondary', children: 'Check again' })
    ] }) })
  }
  const error = consent.error || view.error || library.error
  if (error) {
    const signedOut = isAuthError(error) || error?.status === 403 || error?.statusCode === 403 || /\b403\b/.test(text(error?.message))
    if (signedOut && (consent.error || !consent.data)) return jsx(SignedOut, { onRetry: reload })
    return jsx('div', { className: 'grid h-full place-items-center p-8', children: jsxs('div', { className: 'space-y-3 text-center', children: [
      jsx(EmptyState, { className: 'min-h-0', title: signedOut ? 'Your account changed' : 'Collective Wisdom unavailable', description: signedOut ? 'Reload to continue with the current account.' : isUnavailable(error) ? UNAVAILABLE_MESSAGE : text(error?.message || error) }),
      jsx(Button, { onClick: isUnavailable(error) ? () => { consent.refetch(); if (view.error) view.refetch(); if (library.error) library.refetch() } : reload, size: 'sm', type: 'button', variant: 'secondary', children: isUnavailable(error) ? 'Retry' : 'Reload' })
    ] }) })
  }
  if (!consent.data) return jsx('p', { className: 'p-5 text-sm text-(--ui-text-tertiary)', children: 'Loading Collective Wisdom…' })
  if (!ready) return jsx(ConsentGate, { client, consent: consent.data, lifecycle, viewId: view.data?.view_id })
  if (!library.data || !view.data) return jsx('p', { className: 'p-5 text-sm text-(--ui-text-tertiary)', children: 'Loading your team\'s skills…' })
  return jsx(Library, { client, data: {
    team_name: library.data.team_name,
    needs_look: library.data.needs_look || 0,
    installed: Array.isArray(library.data.installed) ? library.data.installed : [],
    shared: Array.isArray(library.data.shared) ? library.data.shared : [],
    browse: Array.isArray(library.data.browse) ? library.data.browse : []
  }, lifecycle, viewId: view.data.view_id })
}

export default {
  id: 'collective-wisdom',
  name: 'Collective Wisdom',
  description: 'Share skills with your team, install theirs, and keep them up to date.',
  defaultEnabled: false,
  register(ctx) {
    let active = true
    const client = { active: () => active, rest: (...args) => {
      if (!active) return Promise.reject(Object.assign(new Error('Collective Wisdom plugin is disabled.'), { stale: true }))
      return ctx.rest(...args)
    }, openExternal: url => (active && ctx.os?.openExternal ? ctx.os.openExternal(url) : Promise.resolve(false)) }
    // /notices/end-turn consumes a human offer without session/message binding.
    // Do not poll it from an unbound gateway event or from historical footer mounts.
    const openDiscovery = draft => {
      if (!active) return draft
      const parsed = parseWisdomDiscovery(draft?.text)
      if (!parsed) return draft
      const captured = captureDialogOrigin()
      const id = slashDialog.revision + 1
      const route = captured.origin.connectionId && captured.origin.profile
        ? `connection ${captured.origin.connectionId}, profile ${captured.origin.profile}`
        : 'the focused chat owner'
      slashDialog.open({
        ...parsed,
        id,
        origin: captured.origin,
        error: captured.routable
          ? null
          : `Hermes Desktop cannot safely route this dialog to ${route} because plugin REST currently follows connection ${captured.active.connectionId || 'unavailable'}, profile ${captured.active.profile}. No request was made. Switch the active profile to the focused chat or use the normal command outside this dialog.`
      })
      // Composer middleware cancellation preserves/restores this command in the
      // draft on the current Desktop host. Opening is idempotent and starts no turn.
      return null
    }
    ctx.onDispose(() => { active = false; slashDialog.close() })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: WISDOM_ROUTE }, render: () => jsx(WisdomPage, { client }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 60, data: { codicon: 'library', label: 'Collective Wisdom', path: WISDOM_ROUTE } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'collective-wisdom.open', label: 'Collective Wisdom: Open', keywords: ['wisdom', 'skills', 'team', 'browse', 'install'], run: () => host.navigate(WISDOM_ROUTE) } },
      { id: 'share', area: PALETTE_AREA, data: { id: 'collective-wisdom.share', label: 'Collective Wisdom: Share a skill…', keywords: ['wisdom', 'share', 'skill', 'team'], run: () => { pageIntent.set({ share: true }); host.navigate(WISDOM_ROUTE) } } },
      { id: 'slash-dialog', area: COMPOSER_AREAS.top, render: () => jsx(WisdomDialogHost, { client }) },
      { id: 'slash-middleware', area: COMPOSER_AREAS.middleware, order: 40, data: { handler: openDiscovery } }
    ])
  }
}
