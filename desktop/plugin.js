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
  EmptyState,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  ScrollArea,
  Separator,
  SIDEBAR_NAV_AREA,
  Textarea,
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
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'What prevents installation' }), jsx(StringList, { items: value.blocked, empty: 'Nothing reported.' })] }),
    ...(Array.isArray(value.notes) && value.notes.length ? [jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Notes' }), jsx(StringList, { items: value.notes, empty: '' })] })] : [])
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

function saveReadyFile(file) {
  const value = { path: text(file?.path), ...(file?.mode === 'exec' ? { mode: 'exec' } : {}) }
  if (typeof file?.content_utf8 === 'string') return { ...value, content_utf8: file.content_utf8 }
  return { ...value, content_base64: text(file?.content_base64) }
}

function dataHref(value) {
  return `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(value, null, 2))}`
}

function PreparedEditor({ draftId, draftName, editor, onChange, onSave, saving }) {
  const updateFile = (index, changes) => onChange({
    ...editor,
    files: editor.files.map((file, position) => position === index ? { ...file, ...changes } : file)
  })
  const removeFile = index => onChange({ ...editor, files: editor.files.filter((_file, position) => position !== index) })
  const exportName = `wisdom-${text(draftName).replace(/[^a-zA-Z0-9._-]+/g, '-')}.json`
  const exportPayload = {
    draft_id: draftId,
    author_description: editor.author_description,
    files: editor.files.map(saveReadyFile),
    expected_hashes: editor.expected_hashes
  }
  return jsxs('section', { className: 'space-y-3', children: [
    jsx('h4', { className: 'text-xs font-semibold', children: 'Edit complete private draft' }),
    jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Saving replaces the complete draft file set. Rename paths directly, remove files deliberately, and add text files below. Opaque bytes remain inert base64 and can be exported exactly.' }),
    jsxs('label', { className: 'block space-y-1 text-xs', children: [
      jsx('span', { className: 'block text-(--ui-text-quaternary)', children: 'Author description' }),
      jsx(Textarea, { 'aria-label': 'Author description', className: 'min-h-20 w-full', onChange: event => onChange({ ...editor, author_description: event.target.value }), value: editor.author_description })
    ] }),
    jsx('a', { className: 'text-xs text-(--ui-accent)', download: exportName, href: dataHref(exportPayload), children: 'Export complete save-ready draft JSON' }),
    jsx('ul', { className: 'space-y-3', children: editor.files.map((file, index) => {
      const originalPath = file._label || file.path || 'new file'
      const opaque = typeof file.content_utf8 !== 'string'
      return jsxs('li', { className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
        jsx(Input, { 'aria-label': `Path for ${originalPath}`, onChange: event => updateFile(index, { path: event.target.value }), value: file.path }),
        opaque
          ? jsxs('div', { className: 'space-y-1', children: [
              jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: `Opaque bytes (${text(file.content_base64).length} base64 characters); active preview is disabled.` }),
              jsx('a', { className: 'text-xs text-(--ui-accent)', download: text(file.path).split('/').at(-1) || 'opaque.bin', href: `data:application/octet-stream;base64,${text(file.content_base64)}`, children: 'Export exact opaque bytes' })
            ] })
          : jsx(Textarea, { 'aria-label': `Content for ${originalPath}`, className: 'min-h-32 w-full font-mono text-xs', onChange: event => updateFile(index, { content_utf8: event.target.value }), value: file.content_utf8 }),
        jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
          jsxs('label', { className: 'flex items-center gap-2 text-xs', children: [
            jsx(Checkbox, { 'aria-label': `Executable ${originalPath}`, checked: file.mode === 'exec', onCheckedChange: value => updateFile(index, { mode: value === true ? 'exec' : 'file' }) }),
            'Executable intent'
          ] }),
          jsx(Button, { onClick: () => removeFile(index), size: 'sm', type: 'button', variant: 'secondary', children: `Remove ${originalPath}` })
        ] })
      ] }, `${index}-${originalPath}`)
    }) }),
    jsxs('div', { className: 'flex flex-wrap gap-2', children: [
      jsx(Button, { onClick: () => onChange({ ...editor, files: [...editor.files, { _label: 'new file', path: '', mode: 'file', content_utf8: '' }] }), size: 'sm', type: 'button', variant: 'secondary', children: 'Add text file' }),
      jsx(Button, { disabled: saving || !editor.author_description.trim() || editor.files.some(file => !text(file.path).trim()), loading: saving, onClick: onSave, size: 'sm', type: 'button', children: 'Save and re-review' })
    ] })
  ] })
}

function scanGuard(value) {
  const scan = isObject(value?.local_scan) ? value.local_scan : value
  return isObject(scan?.guard) ? scan.guard : scan
}

function hasReviewableWarnings(value) {
  return value?.security_check?.local_status === 'warning_required' || scanGuard(value)?.allowed === false
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
    isPublication ? jsx(KeyValue, { name: 'copy source', value: review?.copy_provenance_line }) : null,
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
  if (isObject(status) && ['installed', 'not_installed', 'unmanaged_local', 'removed', 'unavailable'].includes(status.state)) return status
  return { state: 'unavailable', text: 'Installation status unavailable', installed_version: null, latest_version: versionOf(item) }
}

function catalogAction(item) {
  const status = catalogStatus(item)
  // Removed outside Wisdom: the same reviewed install replaces the stale record.
  if (status.state === 'not_installed' || status.state === 'removed') return 'install'
  if (status.state === 'unmanaged_local' && isPositiveInt(status.latest_version)) return 'update'
  if (status.state === 'installed' && isPositiveInt(status.latest_version) && isPositiveInt(status.installed_version) && status.latest_version > status.installed_version) return 'update'
  return null
}

function describeCandidate(item) {
  const reasons = isObject(item?.local_reasons) ? item.local_reasons : {}
  if (item?.qualification === 'high_usage' && Number.isInteger(reasons.consecutive_business_days)) {
    return `used on ${reasons.consecutive_business_days} consecutive business days`
  }
  const refinements = reasons.meaningful_refinements ?? reasons.edits
  if (item?.qualification === 'refinement' && Number.isInteger(refinements) && Number.isInteger(reasons.stable_days)) {
    return `refined ${refinements} times and stable for ${reasons.stable_days} days`
  }
  return 'qualified from local use and refinement signals'
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
          action ? jsx(Button, { disabled: prepare.isPending, onClick: () => prepare.mutate({ item, action }), size: 'sm', type: 'button', children: action === 'update' ? 'Update' : status.state === 'removed' ? 'Reinstall' : 'Install' }) : null
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

function Installed({ bootstrap, client, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const [preserve, setPreserve] = useState(true)
  const [review, setReview] = useState(null)
  const [setupPermission, setSetupPermission] = useState(null)
  const [acknowledgements, setAcknowledgements] = useState({ warnings: false, partial: false, sensitive: false })
  const [outcome, setOutcome] = useState(null)
  const [checks, setChecks] = useState({})
  const check = useCurrentMutation(
    client,
    lifecycle,
    item => client.rest(`/updates/${encodeURIComponent(item.skill_id)}?view_id=${encodeURIComponent(viewId)}`),
    result => setChecks(current => ({ ...current, [result.skill_id]: { result } })),
    'Could not check for updates',
    (error, item) => setChecks(current => ({ ...current, [item.skill_id]: { error: isUnavailable(error) ? UNAVAILABLE_MESSAGE : text(error?.message || error), transient: isUnavailable(error) } }))
  )
  const update = useCurrentMutation(client, lifecycle, item => client.rest('/update/review', {
    method: 'POST', body: requestBody(viewId, { skill_id: item.skill_id, preserve_modified: preserve, accept_sensitive: false, accept_partial: false, accept_warnings: false })
  }), value => { setOutcome(null); setAcknowledgements({ warnings: false, partial: false, sensitive: false }); setReview({ ...value, name: label(installations.find(item => item.skill_id === value.skill_id)), operation: 'update' }) }, 'Could not prepare the update review')
  const reReview = useCurrentMutation(client, lifecycle, async current => {
    const next = await client.rest('/update/review', {
      method: 'POST', body: requestBody(viewId, {
        skill_id: current.skill_id,
        preserve_modified: preserve,
        accept_sensitive: acknowledgements.sensitive,
        accept_partial: acknowledgements.partial,
        accept_warnings: acknowledgements.warnings
      })
    })
    if (current.content_hash !== next.content_hash || current.manifest_hash !== next.manifest_hash || warningIdentity(current) !== warningIdentity(next)) {
      throw new Error('The exact update package or scanner findings changed. Start a fresh review and acknowledge the new facts.')
    }
    return { ...next, name: label(current), operation: 'update' }
  }, setReview, 'Could not bind the update acknowledgements')
  const uninstall = useCurrentMutation(client, lifecycle, item => client.rest('/uninstall/review', {
    method: 'POST', body: requestBody(viewId, { skill_id: item.skill_id })
  }), value => { setOutcome(null); setReview({ ...value, name: label(value.installation), operation: 'uninstall' }) }, 'Could not prepare the uninstall review')
  const confirm = useCurrentMutation(client, lifecycle, async current => ({
    operation: current.operation,
    reviewed: current,
    skillName: label(current),
    result: await client.rest(current.operation === 'uninstall' ? '/uninstall/confirm' : '/update/confirm', {
      method: 'POST', body: current.operation === 'uninstall'
        ? requestBody(viewId, { review_id: current.review_id })
        : requestBody(viewId, { receipt: current.receipt })
    })
  }), async (payload, token, isCurrent) => {
    const { operation, result, skillName, reviewed } = payload
    setReview(null)
    const name = skillName !== 'Name unavailable' ? skillName : label(result)
    if (operation === 'update' && Array.isArray(reviewed.setup_commands) && reviewed.setup_commands.length &&
        completedFileChange(operation, result)) {
      setSetupPermission({ receipt: reviewed.receipt, commands: reviewed.setup_commands, name, operation })
      setOutcome({ ...result, name, kind: operation, setup_required: true, ready_to_use: null })
      await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
      return
    }
    if (operationSucceeded(operation, result, name)) {
      await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
      if (isCurrent(token)) host.notify({
        kind: 'success',
        message: operation === 'uninstall' ? `✅ ${name} removed` : `✅ ${name} updated`
      })
    } else {
      setOutcome({ ...(result || { state: 'unknown' }), name, kind: operation })
    }
  }, 'The reviewed action was not completed')
  const runSetup = useCurrentMutation(client, lifecycle, async pending => ({
    pending,
    result: await client.rest('/update/setup', {
      method: 'POST', body: requestBody(viewId, { receipt: pending.receipt })
    })
  }), ({ pending, result }) => {
    setSetupPermission(null)
    setOutcome({ ...result, name: pending.name, kind: 'update' })
  }, 'Could not confirm setup. Check its status before trying again.', () => {
    setSetupPermission(null)
  })
  const installations = Array.isArray(bootstrap?.installations) ? bootstrap.installations : []
  const decisions = installations.filter(item => checks[item.skill_id]?.result?.state === 'update_available')
  return jsxs('div', { className: 'space-y-4', children: [
    jsxs('label', { className: 'flex items-center gap-2 text-xs', children: [
      jsx(Checkbox, { 'aria-label': 'Preserve locally modified files', checked: preserve, onCheckedChange: value => setPreserve(value === true) }),
      'Preserve locally modified files when preparing updates'
    ] }),
    decisions.length ? jsx(Section, { title: 'Updates needing your decision', description: 'Review the exact new version before choosing to update. Local edits are preserved when enabled above.', children:
      jsx('ul', { className: 'space-y-2', children: decisions.map(item => {
        const result = checks[item.skill_id].result
        return jsxs('li', { className: 'flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
          jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: `${label(item)} v${item.version || '?'} → v${result.latest_version || '?'}` }),
          jsx(Button, { disabled: update.isPending || (item.modified && !preserve), onClick: () => update.mutate(item), size: 'sm', type: 'button', children: 'Review update' })
        ] }, item.skill_id)
      }) })
    }) : null,
    installations.length === 0 ? jsx(EmptyState, { title: 'Nothing installed', description: 'Install a shared skill from Catalog.' }) : null,
    jsx('ul', { className: 'divide-y divide-(--ui-stroke-secondary)', children: installations.map(item => {
      const checked = checks[item.skill_id]
      const updateAvailable = checked?.result?.state === 'update_available'
      const checkStatus = checked?.error
        ? `Update check unavailable: ${checked.error}`
        : updateAvailable
          ? `v${checked.result.latest_version || '?'} is available. Review is a separate exact-version step.`
          : checked?.result?.state === 'current'
            ? 'This installation is current.'
            : checked?.result
              ? `Update unavailable: ${checked.result.reason || checked.result.state || 'the service did not return a reviewable update.'}`
              : 'Update review unavailable until you check for updates.'
      const preservationBlocked = item.modified && !preserve
      const removed = item.state === 'removed'
      return jsxs('li', { className: 'gap-3 py-3', style: { alignItems: 'center', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto' }, children: [
        jsxs('div', { className: 'min-w-0 flex-1', children: [
          jsx('div', { className: 'text-sm font-medium', children: label(item) }),
          jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: removed
            ? `${text(item.status_text || 'Removed from this computer outside Wisdom')} · v${item.version || '?'} was installed`
            : `Installed v${item.version || '?'}${item.modified ? ' · local edits detected · preservation required' : ''}` }),
          jsx('p', { 'aria-live': 'polite', className: 'text-xs text-(--ui-text-tertiary)', style: { overflowWrap: 'anywhere' }, children: removed ? 'Install it again from Catalog.' : preservationBlocked ? 'Update review unavailable: enable preservation to protect local edits.' : checkStatus }),
          jsx(OperationOutcome, { kind: outcome?.kind === 'uninstall' ? 'Uninstall' : 'Update', result: outcome?.skill_id === item.skill_id ? outcome : null })
        ] }),
        jsxs('div', { className: 'flex items-center justify-end gap-2', style: { flexWrap: 'wrap' }, children: [
          removed ? null : jsx(Button, { disabled: check.isPending, loading: check.isPending, onClick: () => check.mutate(item), size: 'sm', type: 'button', variant: 'secondary', children: checked?.transient ? 'Retry' : 'Check for updates' }),
          jsx(Button, { disabled: uninstall.isPending, onClick: () => uninstall.mutate(item), size: 'sm', type: 'button', variant: 'secondary', children: 'Remove' })
        ] })
      ] }, item.skill_id)
    }) }),
    review?.operation === 'update' ? jsx(ReviewAcknowledgements, { busy: reReview.isPending, kind: 'update', onChange: setAcknowledgements, onReview: () => reReview.mutate(review), review, value: acknowledgements }) : null,
    review ? jsx(ReviewPanel, { acknowledgements, busy: confirm.isPending, kind: review.operation, onCancel: () => setReview(null), onConfirm: () => confirm.mutate(review), review }) : null,
    jsx(SetupPermission, { pending: setupPermission, busy: runSetup.isPending, onDefer: () => setSetupPermission(null), onRun: () => runSetup.mutate(setupPermission) }),
    jsx(OperationOutcome, { kind: outcome?.kind === 'uninstall' ? 'Uninstall' : 'Update', result: outcome && !installations.some(item => item.skill_id === outcome.skill_id) ? outcome : null })
  ] })
}

function Settings({ bootstrap, client, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const settings = useQuery(queryOptions(
    ['settings', lifecycle.key],
    () => client.rest(`/settings?view_id=${encodeURIComponent(viewId)}`)
  ))
  const current = settings.data || {}
  const mode = current.delivery_mode || bootstrap?.settings?.notifications?.delivery_mode || 'agent'
  const setMode = useCurrentMutation(client, lifecycle, next => client.rest('/settings/mode', {
    method: 'PATCH', body: requestBody(viewId, { mode: next, expected_mode: mode })
  }), async (_result, token, isCurrent) => {
    await Promise.all([
      queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] }),
      queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'settings', lifecycle.key] })
    ])
    if (isCurrent(token)) host.notify({ kind: 'success', message: 'Recommendation setting updated' })
  }, 'Recommendation setting was not changed')
  const mute = useCurrentMutation(client, lifecycle, duration => client.rest('/settings/mute', {
    method: 'PATCH',
    body: requestBody(viewId, { control_id: String(current.mute_control.revision), duration })
  }), async (_result, token, isCurrent) => {
    await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'settings', lifecycle.key] })
    if (isCurrent(token)) host.notify({ kind: 'success', message: 'Your notification mute was updated' })
  }, 'Your notification mute was not changed')
  const muteState = current.gateway_mute
  const muteControl = current.mute_control
  if (settings.error && isUnavailable(settings.error)) return jsx(ReadFailure, { error: settings.error, title: 'Settings unavailable', onRetry: () => settings.refetch() })
  return jsxs('div', { className: 'space-y-6', children: [
    jsx(Section, { title: 'Recommendations', description: mode === 'fixed'
      ? 'Off: proactive Wisdom recommendations are paused. You can still browse and review skills here.'
      : 'On: Wisdom may show useful skill and update recommendations.', children:
      jsxs('div', { className: 'flex gap-2', children: [
        jsx(Button, { disabled: current.mode_writable === false || mode === 'agent' || setMode.isPending, onClick: () => setMode.mutate('agent'), size: 'sm', type: 'button', variant: mode === 'agent' ? 'secondary' : 'ghost', children: 'On' }),
        jsx(Button, { disabled: current.mode_writable === false || mode === 'fixed' || setMode.isPending, onClick: () => setMode.mutate('fixed'), size: 'sm', type: 'button', variant: mode === 'fixed' ? 'secondary' : 'ghost', children: 'Off' })
      ] })
    }),
    jsx(Section, { title: 'Mute recommendations for you', description: 'Pause sharing suggestions and skill recommendations for you, not your teammates. You can still browse and install skills.', children: settings.error
      ? jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: `Mute unavailable: ${text(settings.error?.message || settings.error)}` })
      : !settings.data
        ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading current mute setting…' })
        : jsxs('div', { className: 'space-y-3', children: [
            jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: muteState?.muted
              ? `Recommendations are muted${muteState?.muted_until ? ` until ${muteState.muted_until}` : ''}.`
              : 'Recommendations are on for you.' }),
            muteControl ? jsxs('div', { className: 'flex flex-wrap gap-2', children: [
              jsx(Button, { disabled: mute.isPending, onClick: () => mute.mutate('1_day'), size: 'sm', type: 'button', variant: 'secondary', children: 'Mute for 1 day' }),
              jsx(Button, { disabled: mute.isPending, onClick: () => mute.mutate('1_week'), size: 'sm', type: 'button', variant: 'secondary', children: 'Mute for 1 week' }),
              jsx(Button, { disabled: mute.isPending, onClick: () => mute.mutate('30_days'), size: 'sm', type: 'button', variant: 'secondary', children: 'Mute for 30 days' }),
              jsx(Button, { disabled: mute.isPending, onClick: () => mute.mutate('forever'), size: 'sm', type: 'button', variant: 'secondary', children: 'Mute indefinitely' }),
              jsx(Button, { disabled: mute.isPending || muteState?.muted !== true, onClick: () => mute.mutate(null), size: 'sm', type: 'button', variant: 'secondary', children: 'Unmute' })
            ] }) : jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: 'Mute controls are unavailable here.' })
          ] })
    })
  ] })
}

function Contributions({ client, lifecycle, viewId, shareSuggestions = false }) {
  const queryClient = useQueryClient()
  const [skillName, setSkillName] = useState('')
  const [draft, setDraft] = useState(null)
  const [preparedReview, setPreparedReview] = useState(null)
  const [editor, setEditor] = useState(null)
  const [savedEditor, setSavedEditor] = useState(null)
  const dirty = editor !== null && JSON.stringify(editor) !== JSON.stringify(savedEditor)
  const [savedNotice, setSavedNotice] = useState(false)
  const [warningAccepted, setWarningAccepted] = useState(false)
  const [keptDraft, setKeptDraft] = useState(null)
  const [review, setReview] = useState(null)
  const [outcome, setOutcome] = useState(null)
  // Sharing suggestions ship only in a release whose backend exposes them.
  const candidates = useQuery(queryOptions(
    ['candidates', lifecycle.key],
    () => client.rest(`/candidates?view_id=${encodeURIComponent(viewId)}`),
    shareSuggestions
  ))
  const savedDrafts = useQuery(queryOptions(
    ['drafts', lifecycle.key],
    () => client.rest(`/drafts?view_id=${encodeURIComponent(viewId)}`)
  ))
  const acceptDraft = value => {
    setDraft(value)
    setPreparedReview(null)
    setEditor(null)
    setSavedNotice(false)
    setWarningAccepted(false)
    setKeptDraft(null)
    setReview(null)
    setOutcome(null)
  }
  const prepareCandidate = useCurrentMutation(client, lifecycle, async item => ({
    ...await client.rest(`/candidates/${encodeURIComponent(item.event_id)}/prepare`, {
      method: 'POST', body: requestBody(viewId)
    }), name: label(item)
  }), acceptDraft, 'Could not prepare a private draft — nothing was uploaded or shared')
  const notNow = useCurrentMutation(client, lifecycle, item => client.rest(`/candidates/${encodeURIComponent(item.event_id)}/not-now`, {
    method: 'POST', body: requestBody(viewId)
  }), async () => {
    await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'candidates', lifecycle.key] })
  }, 'Could not save Not now')
  const prepare = useCurrentMutation(client, lifecycle, async () => ({
    ...await client.rest('/drafts/prepare', {
      method: 'POST', body: requestBody(viewId, { skill_name: skillName.trim() })
    }), name: skillName.trim()
  }), acceptDraft, 'Share preparation failed — nothing was uploaded or shared')
  const inspectDraft = async () => client.rest(`/drafts/${encodeURIComponent(draft.draft_id)}?view_id=${encodeURIComponent(viewId)}`)
  const reviewPrepared = (draftId, hashes) => client.rest(`/drafts/${encodeURIComponent(draftId)}/review`, {
    method: 'POST', body: requestBody(viewId, { reviewed_hashes: hashes })
  })
  const inspect = useCurrentMutation(client, lifecycle, async () => {
    const inspected = await inspectDraft()
    const reviewed = await reviewPrepared(draft.draft_id, inspected.expected_hashes)
    return { inspected, reviewed }
  }, ({ inspected, reviewed }) => {
    const nextEditor = {
      author_description: text(inspected.author_description),
      files: (Array.isArray(inspected.files) ? inspected.files : []).map(file => ({ ...saveReadyFile(file), _label: text(file.path) })),
      expected_hashes: inspected.expected_hashes
    }
    setEditor(nextEditor)
    setSavedEditor(nextEditor)
    setPreparedReview(reviewed)
    setSavedNotice(false)
    setWarningAccepted(false)
  }, 'Could not inspect the prepared draft')
  const save = useCurrentMutation(client, lifecycle, async () => {
    const payload = {
      author_description: editor.author_description,
      files: editor.files.map(saveReadyFile),
      expected_hashes: editor.expected_hashes
    }
    const saved = await client.rest(`/drafts/${encodeURIComponent(draft.draft_id)}`, {
      method: 'PUT', body: requestBody(viewId, payload)
    })
    const reviewed = await reviewPrepared(draft.draft_id, saved.hashes)
    return { payload, reviewed }
  }, ({ payload, reviewed }) => {
    const nextEditor = { ...payload, files: payload.files.map(file => ({ ...file, _label: text(file.path) })), expected_hashes: reviewed.hashes }
    setEditor(nextEditor)
    setSavedEditor(nextEditor)
    setPreparedReview(reviewed)
    setSavedNotice(true)
    setWarningAccepted(false)
    setReview(null)
  }, 'Could not save and re-review the prepared draft')
  const publicationReview = useCurrentMutation(client, lifecycle, async () => {
    if (dirty) throw new Error('Save and re-review or discard unsaved edits before continuing.')
    const inspected = await inspectDraft()
    const localReview = await reviewPrepared(draft.draft_id, inspected.expected_hashes)
    const warnings = hasReviewableWarnings(localReview)
    const details = safeWarningFindings(localReview)
    if (warnings && !details.length) {
      throw new Error('Draft kept: scanner warning details are unavailable; acknowledgement is blocked.')
    }
    if (warnings && !warningAccepted) {
      throw new Error('Acknowledge the displayed scanner warnings for this exact package before continuing.')
    }
    const reviewed = await client.rest('/publication/review', {
      method: 'POST',
      body: requestBody(viewId, { draft_id: draft.draft_id, accept_warnings: warnings && warningAccepted })
    })
    if (warnings && warningIdentity(localReview) !== warningIdentity(reviewed)) {
      throw new Error('The scanner findings changed during review. Inspect and acknowledge the updated exact warnings.')
    }
    const expected = inspected?.expected_hashes
    if (!isObject(expected) || !isObject(reviewed?.hashes) || !['content', 'author_description', 'package_manifest'].every(key => expected[key] === reviewed.hashes[key])) {
      throw new Error('Prepared bytes changed between inspection and publication review.')
    }
    return {
      ...reviewed,
      name: label(draft),
      author_description: inspected.author_description,
      drafted_description: inspected.author_description,
      files: inspected.files,
      operation: 'publication'
    }
  }, value => { setOutcome(null); setReview(value) }, 'Could not prepare the publication review')
  const publish = useCurrentMutation(client, lifecycle, async current => ({
    result: await client.rest('/publication/confirm', {
      method: 'POST', body: requestBody(viewId, { receipt: current.receipt })
    }),
    name: label(current)
  }), ({ result, name }) => {
    setReview(null)
    const named = { ...(result || { state: 'unknown' }), name }
    if (name !== 'Name unavailable' && result?.state === 'done' && ['published', 'pending_moderation'].includes(result?.publication_state) && result?.managed_enrollment?.state !== 'not_linked') {
      host.notify({ kind: 'success', message: result.publication_state === 'published' ? `✅ ${name} shared` : `${name} submitted for team review` })
    } else {
      setOutcome(named)
    }
  }, 'Publication was not completed')
  const warningRequired = hasReviewableWarnings(preparedReview)
  const warningFindings = safeWarningFindings(preparedReview)
  const busy = prepare.isPending || prepareCandidate.isPending || notNow.isPending || inspect.isPending || save.isPending || publicationReview.isPending || publish.isPending
  const shareCandidates = Array.isArray(candidates.data?.candidates) ? candidates.data.candidates : []
  const localDrafts = Array.isArray(savedDrafts.data?.drafts) ? savedDrafts.data.drafts : []
  const keepDraft = () => {
    if (dirty || busy) return
    setKeptDraft(label(draft))
    setReview(null)
    setDraft(null)
    setPreparedReview(null)
    setEditor(null)
    setSavedNotice(false)
    setWarningAccepted(false)
    queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'drafts', lifecycle.key] })
  }
  const transientRead = [candidates.error, savedDrafts.error].find(isUnavailable)
  if (transientRead) return jsx(ReadFailure, { error: transientRead, title: 'Sharing data unavailable', onRetry: () => {
    if (candidates.error) candidates.refetch()
    if (savedDrafts.error) savedDrafts.refetch()
  } })
  return jsxs('div', { className: 'space-y-5', children: [
    shareCandidates.length ? jsx(Section, { title: 'Worth sharing with your team', description: 'These suggestions come from local use and refinement signals. Share only prepares a private draft for your review.', children:
      jsx('ul', { className: 'divide-y divide-(--ui-stroke-secondary)', children: shareCandidates.map(item =>
        jsxs('li', { 'data-testid': `share-candidate-${item.event_id}`, className: 'gap-3 py-3', style: { alignItems: 'center', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto' }, children: [
          jsxs('div', { className: 'min-w-0', children: [
            jsx('div', { className: 'text-sm font-medium', children: label(item) }),
            jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: describeCandidate(item) })
          ] }),
          jsxs('div', { className: 'flex flex-wrap justify-end gap-2', children: [
            jsx(Button, { disabled: busy || dirty, onClick: () => { if (!dirty) prepareCandidate.mutate(item) }, size: 'sm', type: 'button', children: 'Share' }),
            jsx(Button, { disabled: busy, onClick: () => notNow.mutate(item), size: 'sm', type: 'button', variant: 'ghost', children: 'Not now' })
          ] })
        ] }, item.event_id)
      ) })
    }) : null,
    candidates.error ? jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: `Sharing suggestions unavailable: ${text(candidates.error?.message || candidates.error)}` }) : null,
    jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: 'Preparing a skill stays local and private. Review its files and warnings before choosing Share.' }),
    jsxs('div', { className: 'flex gap-2', children: [
      jsx(Input, { 'aria-label': 'Local skill name', onChange: event => setSkillName(event.target.value), placeholder: 'Local skill name', value: skillName }),
      jsx(Button, { disabled: !skillName.trim() || dirty || busy, loading: prepare.isPending, onClick: () => prepare.mutate(), type: 'button', children: 'Share' })
    ] }),
    localDrafts.length ? jsx(Section, { title: 'Saved local drafts', children:
      jsx('ul', { className: 'divide-y divide-(--ui-stroke-secondary)', children: localDrafts.map(item =>
        jsxs('li', { className: 'flex items-center justify-between gap-3 py-2', children: [
          jsxs('div', { className: 'min-w-0', children: [
            jsx('span', { className: 'text-sm font-medium', children: label(item) }),
            item.updated_at && !Number.isNaN(Date.parse(item.updated_at))
              ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: `Saved ${new Date(item.updated_at).toLocaleString()}` }) : null
          ] }),
          jsx(Button, { disabled: busy || dirty || !item.draft_id || label(item) === 'Name unavailable',
            onClick: () => acceptDraft({ draft_id: item.draft_id, name: label(item) }),
            size: 'sm', type: 'button', variant: 'secondary', children: 'Resume' })
        ] }, item.draft_id)
      ) }) }) : null,
    savedDrafts.error ? jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: 'Saved drafts are unavailable right now.' }) : null,
    keptDraft ? jsxs(Section, { title: 'Draft kept', children: [
      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: `${keptDraft} was kept locally. Nothing was shared. Select Resume to continue later.` })
    ] }) : null,
    draft ? jsxs(Section, { title: 'Private draft', children: [
      jsx(KeyValue, { name: 'skill', value: label(draft) }),
      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Nothing has been uploaded. The original skill is unchanged.' }),
      jsxs('div', { className: 'flex flex-wrap gap-2', children: [
        jsx(Button, { disabled: dirty || busy, onClick: () => inspect.mutate(), size: 'sm', type: 'button', variant: 'secondary', children: 'Review and edit exact files' }),
        jsx(Button, { disabled: dirty || busy || (warningRequired && (!warningFindings.length || !warningAccepted)), onClick: () => publicationReview.mutate(), size: 'sm', type: 'button', children: 'Continue to sharing review' }),
        jsx(Button, { disabled: dirty || busy, onClick: keepDraft, size: 'sm', type: 'button', variant: 'secondary', children: 'Keep draft and exit' })
      ] })
    ] }) : null,
    preparedReview ? jsxs(Section, { title: 'Exact local package review', children: [
      jsx(KeyValue, { name: 'author copy', value: preparedReview.author_description }),
      jsx(KeyValue, { name: 'copy source', value: preparedReview.copy_provenance_line }),

      jsx(CheckSummary, { title: 'Local package scanner', check: preparedReview.local_scan, authority: 'Local scan' }),
      jsx(CheckSummary, { title: 'Local security and upload gate', check: preparedReview.security_check, authority: 'Local security' }),
      warningRequired ? jsxs('section', { className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
        jsx('h4', { className: 'text-xs font-semibold', children: 'Material scanner warnings' }),
        jsx(FindingList, { items: warningFindings, empty: 'Warning details are unavailable; acknowledgement is blocked.' }),
        warningFindings.length ? jsxs('label', { className: 'flex items-start gap-2 text-xs', children: [
          jsx(Checkbox, { 'aria-label': 'Acknowledge displayed scanner warnings for this exact package', checked: warningAccepted, onCheckedChange: value => setWarningAccepted(value === true) }),
          'Acknowledge the displayed scanner warnings for this exact package.'
        ] }) : null
      ] }) : null,
      jsx(FileManifest, { files: preparedReview.files, showContent: true }),
      dirty ? jsxs('div', { className: 'space-y-2', children: [
        jsx('p', { 'aria-live': 'polite', className: 'text-xs text-(--ui-text-secondary)', children: 'Unsaved changes stay only in this page. Save and re-review or discard them before continuing or keeping the draft.' }),
        jsx(Button, { disabled: busy, onClick: () => setEditor(savedEditor), size: 'sm', type: 'button', variant: 'secondary', children: 'Discard unsaved edits' })
      ] }) : null,
      editor ? jsx('fieldset', { disabled: busy, className: 'min-w-0 border-0 p-0', children: jsx(PreparedEditor, { draftId: draft?.draft_id, draftName: label(draft), editor, onChange: value => { setEditor(value); setSavedNotice(false); setReview(null); setWarningAccepted(false) }, onSave: () => save.mutate(), saving: save.isPending }) }) : null,
      savedNotice ? jsx('p', { 'aria-live': 'polite', className: 'text-xs text-(--ui-text-secondary)', children: 'Draft saved and re-reviewed. Nothing was uploaded.' }) : null
    ] }) : null,
    review ? jsx(ReviewPanel, { busy: publish.isPending, kind: 'publication', onCancel: keepDraft, onConfirm: () => publish.mutate(review), review }) : null,
    jsx(OperationOutcome, { kind: 'Publication', result: outcome })
  ] })
}

function Enrollment({ client, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const [accepted, setAccepted] = useState(false)
  const disclosure = useQuery(queryOptions(['disclosure', lifecycle.key], () => client.rest('/enrollment/disclosure')))
  const enroll = useCurrentMutation(client, lifecycle, () => client.rest('/enrollment/confirm', {
    method: 'POST', body: requestBody(viewId, { accepted, disclosure_version: disclosure.data.version })
  }), async (_result, token, isCurrent) => {
    await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
    if (isCurrent(token)) host.notify({ kind: 'success', message: 'Collective Wisdom enrolled for this profile' })
  }, 'Enrollment was not completed')
  if (disclosure.error && isUnavailable(disclosure.error)) return jsx(ReadFailure, { error: disclosure.error, title: 'Disclosure unavailable', onRetry: () => disclosure.refetch() })
  return jsx(Section, { title: 'Enroll this profile', description: disclosure.data?.text || 'Loading disclosure…', children:
    jsxs('div', { className: 'space-y-3', children: [
      disclosure.error ? jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: `Disclosure unavailable: ${text(disclosure.error?.message || disclosure.error)}` }) : null,
      jsxs('label', { className: 'flex items-start gap-2 text-xs', children: [
        jsx(Checkbox, { 'aria-label': 'Accept enrollment disclosure', checked: accepted, onCheckedChange: value => setAccepted(value === true) }),
        'I reviewed and accept this disclosure for the currently connected profile.'
      ] }),
      jsx(Button, { disabled: !accepted || !disclosure.data, loading: enroll.isPending, onClick: () => enroll.mutate(), type: 'button', children: 'Enroll profile' })
    ] })
  })
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

function WisdomPage({ client }) {
  const connectionId = useValue(host.state.connectionId) || ''
  const profile = useValue(host.state.profile) || 'default'
  const gateway = useValue(host.state.gateway) || 'unknown'
  const [manualGeneration, setManualGeneration] = useState(0)
  const tracker = useRef({ generation: 0, gateway: null, manualGeneration: -1, mounted: true, signature: null }).current
  tracker.mounted = true
  const lifecycle = { ...lifecycleFor({ connectionId, profile }, gateway, manualGeneration, tracker), mounted: () => tracker.mounted }
  const [tab, setTab] = useState('catalog')
  const available = client.active() && Boolean(connectionId) && gateway === 'open'
  const view = useQuery(queryOptions(['view', lifecycle.key], () => client.rest('/views', { method: 'POST' }), available))
  const bootstrap = useQuery(queryOptions(['bootstrap', lifecycle.key], () => client.rest('/bootstrap'), available))
  const surfaces = bootstrap.data?.surfaces || {}
  useEffect(() => () => { tracker.mounted = false; tracker.generation += 1 }, [tracker])

  let content
  if (!available) {
    content = jsxs('div', { className: 'space-y-3', children: [
      jsx(EmptyState, { title: 'Collective Wisdom unavailable', description: connectionId ? `Gateway is ${gateway}. Reconnect before opening a new review.` : 'Desktop did not provide a connection identity. This SDK/backend combination cannot safely route Wisdom operations.' }),
      jsx(Button, { onClick: () => setManualGeneration(value => value + 1), type: 'button', variant: 'secondary', children: 'Recheck connection' })
    ] })
  } else if (bootstrap.error || view.error) {
    const error = [bootstrap.error, view.error].find(isAuthError) || bootstrap.error || view.error
    content = jsxs('div', { className: 'space-y-3', children: [
      jsx(EmptyState, { title: isAuthError(error) ? 'Ownership must be rechecked' : 'Collective Wisdom unavailable', description: isAuthError(error) ? 'The backend rejected this owner/view. Cached data was dropped; create a fresh view for the current connection and profile.' : isUnavailable(error) ? UNAVAILABLE_MESSAGE : text(error?.message || error) }),
      jsx(Button, { onClick: isUnavailable(error) ? () => {
        if (bootstrap.error) bootstrap.refetch()
        if (view.error) view.refetch()
      } : () => setManualGeneration(value => value + 1), type: 'button', variant: 'secondary', children: isUnavailable(error) ? 'Retry' : 'Reload with a fresh review' })
    ] })
  } else if (!bootstrap.data || !view.data) {
    content = jsx('p', { className: 'p-5 text-sm text-(--ui-text-tertiary)', children: 'Loading owner-bound Wisdom data…' })
  } else if (bootstrap.data.status?.configured !== true) {
    content = jsx(Enrollment, { client, lifecycle, viewId: view.data.view_id })
  } else if (tab === 'installed') {
    content = jsx(Installed, { bootstrap: bootstrap.data, client, lifecycle, viewId: view.data.view_id })
  } else if (tab === 'settings' && surfaces.notification_settings === true) {
    content = jsx(Settings, { bootstrap: bootstrap.data, client, lifecycle, viewId: view.data.view_id })
  } else if (tab === 'contribute') {
    content = jsx(Contributions, { client, lifecycle, viewId: view.data.view_id, shareSuggestions: surfaces.share_suggestions === true })
  } else {
    content = jsx(Catalog, { client, lifecycle, viewId: view.data.view_id })
  }

  if (available && !bootstrap.error && !view.error && view.data && bootstrap.data?.status?.configured === true && ['installed', 'contribute'].includes(tab)) {
    const pending = (bootstrap.data.status.pending_operations || []).filter(row =>
      tab === 'contribute' ? row.kind === 'publication' : row.kind !== 'publication')
    content = jsxs('div', { className: 'space-y-4', children: [
      ...pending.map(row => jsx(OperationOutcome, {
        kind: ({ publication: 'Publication', install: 'Install', update: 'Update', wisdom_setup: 'Setup' })[row.kind] || 'Action',
        result: { ...row, phase: row.phase || row.stage,
          detail: 'An earlier action has not finished. Open a terminal and run hermes wisdom status --json to check its current state before trying again.' }
      }, row.operation_id)),
      content
    ] })
  }

  const tabs = [['catalog', 'Catalog'], ['installed', 'Installed'], ['contribute', 'Contribute']]
  return jsxs('div', { className: 'flex h-full min-h-0 flex-col', children: [
    jsxs('header', { className: 'shrink-0 space-y-3 border-b border-(--ui-stroke-secondary) px-5 py-4', children: [
      jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
        jsxs('div', { className: 'flex items-center gap-2', children: [jsx(Codicon, { name: 'library', size: '1rem' }), jsx('h1', { className: 'text-base font-semibold', children: 'Collective Wisdom' })] }),
        surfaces.notification_settings === true
          ? jsx(Button, { onClick: () => setTab('settings'), size: 'sm', type: 'button', variant: tab === 'settings' ? 'secondary' : 'ghost', children: 'Settings' })
          : null
      ] }),
      jsx('nav', { 'aria-label': 'Collective Wisdom sections', className: 'flex flex-wrap gap-1', children: tabs.map(([value, title]) =>
        jsx(Button, { onClick: () => setTab(value), size: 'sm', type: 'button', variant: tab === value ? 'secondary' : 'ghost', children: title }, value)
      ) })
    ] }),
    jsx(ScrollArea, { className: 'min-h-0 flex-1', children: jsxs('main', { className: 'mx-auto w-full max-w-4xl space-y-5 p-5', children: [
      jsxs('details', { children: [
        jsx('summary', { className: 'cursor-pointer text-xs text-(--ui-text-quaternary)', children: 'Connection details' }),
        jsx('p', { className: 'mt-2 text-xs text-(--ui-text-tertiary)', children: `connection ${connectionId || 'unavailable'} · profile ${profile} · generation ${lifecycle.generation} · gateway ${gateway}` })
      ] }),
      jsx(Separator, {}), content
    ] }) })
  ] })
}

export default {
  id: 'collective-wisdom',
  name: 'Collective Wisdom',
  description: 'Share, browse, install, and update team skills with exact review.',
  defaultEnabled: false,
  register(ctx) {
    let active = true
    const client = { active: () => active, rest: (...args) => {
      if (!active) return Promise.reject(Object.assign(new Error('Collective Wisdom plugin is disabled.'), { stale: true }))
      return ctx.rest(...args)
    } }
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
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 60, data: { codicon: 'library', label: 'Wisdom', path: WISDOM_ROUTE } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'collective-wisdom.open', label: 'Collective Wisdom: Open', keywords: ['wisdom', 'skills', 'catalog', 'install', 'contribute'], run: () => host.navigate(WISDOM_ROUTE) } },
      { id: 'slash-dialog', area: COMPOSER_AREAS.top, render: () => jsx(WisdomDialogHost, { client }) },
      { id: 'slash-middleware', area: COMPOSER_AREAS.middleware, order: 40, data: { handler: openDiscovery } }
    ])
  }
}
