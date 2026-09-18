import {
  Badge,
  Button,
  Checkbox,
  Codicon,
  EmptyState,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  ScrollArea,
  Separator,
  SIDEBAR_NAV_AREA,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const WISDOM_ROUTE = '/collective-wisdom'
const ROOT_KEY = ['collective-wisdom']

const text = value => (value == null ? '' : String(value))
const label = item => item?.slug || item?.name || item?.id || item?.skill_id || 'Unknown skill'
const versionOf = item =>
  item?.latest_version || item?.version || item?.latest_version_detail?.version?.version ||
  item?.latest_version_detail?.version || null
const requestBody = (viewId, fields = {}) => ({ view_id: viewId, ...fields })
const isObject = value => value != null && typeof value === 'object' && !Array.isArray(value)
const isPositiveInt = value => Number.isInteger(value) && value > 0
const isAuthError = error => error?.status === 401 || error?.statusCode === 401 || /\b401\b/.test(text(error?.message))

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

function CheckSummary({ check, title, authority = 'Check' }) {
  const value = isObject(check) ? check : null
  const decision = value?.upload_allowed ?? value?.allowed ?? value?.passed ?? null
  const status = decision === true ? 'passed' : decision === false ? 'blocked' : 'unavailable'
  const findings = Array.isArray(value?.findings) ? value.findings : []
  return jsxs('section', { className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
    jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: title }),
      jsx(Badge, { children: `${authority}: ${status}` })
    ] }),
    jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: value?.summary || value?.verdict || value?.state || (value ? 'No summary supplied.' : 'The service did not supply this check.') }),
    findings.length ? jsx(StringList, { items: findings }) : null
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
    jsx(KeyValue, { name: 'Satisfied', value: Array.isArray(value.satisfied) ? value.satisfied.join(', ') || 'None' : 'Unavailable' }),
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Limitations' }), jsx(StringList, { items: value.limitations, empty: 'No limitations reported.' })] }),
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Required setup (not authorized here)' }), jsx(StringList, { items: value.setup_actions, empty: 'No setup action reported.' })] }),
    jsxs('div', { children: [jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Blocking requirements' }), jsx(StringList, { items: value.blocked, empty: 'No blocked requirement reported.' })] })
  ] })
}

function FileManifest({ files, showContent }) {
  const values = Array.isArray(files) ? files : []
  return jsx('ul', { className: 'space-y-2', children: values.map((file, index) =>
    jsxs('li', { className: 'space-y-1 rounded-[4px] border border-(--ui-stroke-secondary) p-2 text-xs', children: [
      jsx('div', { className: 'break-all font-mono', children: file?.path || file?.name || `file ${index + 1}` }),
      jsx(KeyValue, { name: 'hash', value: file?.hash, mono: true }),
      jsx(KeyValue, { name: 'size', value: Number.isInteger(file?.size) ? `${file.size} bytes` : null }),
      showContent
        ? file?.content_utf8 != null
          ? jsx('pre', { className: 'max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-[4px] border border-(--ui-stroke-secondary) p-2 text-xs', children: file.content_utf8 })
          : jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: 'Full file content unavailable — confirmation is blocked.' })
        : null
    ] }, file?.path || file?.name || index)
  ) })
}

function reviewProblems(review, kind) {
  const problems = []
  if (!isObject(review)) return ['Review response is missing.']
  if (kind === 'uninstall') {
    if (!text(review.review_id)) problems.push('Uninstall review identity is missing.')
    if (!isObject(review.installation) || !text(review.installation.skill_id)) problems.push('Installed snapshot is missing.')
    return problems
  }
  if (!text(review.receipt)) problems.push('Hash-bound receipt is missing.')
  if (kind === 'publication') {
    if (!text(review.draft_id)) problems.push('Draft identity is missing.')
    if (!isObject(review.hashes) || !['content', 'author_description', 'package_manifest'].every(key => text(review.hashes[key]))) {
      problems.push('Exact publication hashes are incomplete.')
    }
    if (!text(review.drafted_description || review.description || review.author_description)) problems.push('Author copy is missing.')
    if (!Array.isArray(review.files) || !review.files.length || review.files.some(file => !text(file?.path) || typeof file?.content_utf8 !== 'string')) {
      problems.push('Full reviewed publication file contents are incomplete.')
    }
    if (!isObject(review.local_scan)) problems.push('Local package scan is missing.')
    if (!isObject(review.professionalism_check)) problems.push('Professionalism assessment is missing.')
    if (review.security_check?.upload_allowed !== true) problems.push('Local security check does not explicitly allow upload.')
    if (!['open', 'moderated', 'managed'].includes(review.publication_mode) || !text(review.policy_version)) problems.push('Gateway publication policy is incomplete.')
    return problems
  }
  if (!text(review.skill_id) || !isPositiveInt(review.version)) problems.push('Skill/version identity is incomplete.')
  if (!text(review.content_hash || review.hashes?.content) || !text(review.manifest_hash || review.hashes?.manifest)) problems.push('Exact package hashes are incomplete.')
  if (!Array.isArray(review.files) || !review.files.length || review.files.some(file => !text(file?.path) || !text(file?.hash))) problems.push('Exact file manifest is incomplete.')
  if (!isObject(review.compatibility) || !text(review.compatibility.outcome)) problems.push('Compatibility evaluation is unavailable.')
  if (!isObject(review.local_scan) || !Object.hasOwn(review.local_scan, 'allowed')) problems.push('Local scanner result is unavailable.')
  if (!isObject(review.server_checks)) problems.push('Server checks are unavailable.')
  if (!isObject(review.policy)) problems.push('Review policy is unavailable.')
  if (review.policy?.scan_requires_confirmation === true || review.policy?.compatibility_requires_confirmation === true) problems.push('This review requires an explicit partial-compatibility or unavailable-scan acknowledgement that this bounded Desktop UI does not collect.')
  if (review.allowed !== true) problems.push('The service did not authorize confirmation.')
  const action = kind === 'update' ? 'confirm_update' : 'confirm_install'
  if (!Array.isArray(review.permitted_actions) || !review.permitted_actions.includes(action)) problems.push(`The service did not permit ${action.replace('_', ' ')}.`)
  if (kind === 'update') {
    if (!isPositiveInt(review.from_version) || !isObject(review.previous_installation)) problems.push('Previous installation snapshot is incomplete.')
    if (!Array.isArray(review.sensitive_expansion) || !isObject(review.preservation) || !isObject(review.authorization)) problems.push('Sensitive-change or preservation policy is incomplete.')
  }
  return problems
}

function ReviewPanel({ busy, kind, onCancel, onConfirm, review }) {
  const problems = reviewProblems(review, kind)
  const hashes = isObject(review?.hashes) ? review.hashes : {}
  const warnings = [
    ...(Array.isArray(review?.compatibility?.limitations) ? review.compatibility.limitations : []),
    ...(Array.isArray(review?.compatibility?.blocked) ? review.compatibility.blocked : []),
    ...(Array.isArray(review?.compatibility?.setup_actions) ? review.compatibility.setup_actions : []),
    ...(Array.isArray(review?.sensitive_expansion) ? review.sensitive_expansion : []),
    ...(Array.isArray(review?.local_scan?.findings) ? review.local_scan.findings : [])
  ]
  const isPublication = kind === 'publication'
  return jsxs('aside', { 'aria-label': 'Exact action review', className: 'space-y-4 rounded-[4px] border border-(--ui-stroke-secondary) p-4', children: [
    jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
      jsx('h3', { className: 'text-sm font-semibold', children: `${kind} review` }),
      jsx(Badge, { children: problems.length ? 'confirmation blocked' : 'ready to confirm' })
    ] }),
    jsx(KeyValue, { name: 'skill', value: label(review) }),
    jsx(KeyValue, { name: 'version', value: review?.version }),
    jsx(KeyValue, { name: 'from version', value: review?.from_version }),
    jsx(KeyValue, { name: 'receipt', value: review?.receipt || review?.review_id, mono: true }),
    jsx(KeyValue, { name: 'content hash', value: review?.content_hash || hashes.content, mono: true }),
    jsx(KeyValue, { name: 'manifest hash', value: review?.manifest_hash || hashes.manifest, mono: true }),
    jsx(KeyValue, { name: 'description hash', value: hashes.author_description, mono: true }),
    jsx(KeyValue, { name: 'package manifest hash', value: hashes.package_manifest, mono: true }),
    isPublication ? jsx(KeyValue, { name: 'author copy', value: review?.drafted_description || review?.description || review?.author_description }) : null,
    isPublication ? jsx(KeyValue, { name: 'Gateway outcome', value: review?.publication_mode === 'moderated' ? 'Moderated: confirmation submits for team moderation' : review?.publication_mode === 'open' ? 'Open: confirmation may publish directly' : review?.publication_mode === 'managed' ? 'Managed: confirmation follows organization-managed Gateway handling' : 'Unavailable' }) : null,
    isPublication ? jsx(KeyValue, { name: 'policy version', value: review?.policy_version }) : null,
    warnings.length ? jsxs('section', { className: 'space-y-1 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Material warnings and required follow-up' }),
      jsx(StringList, { items: warnings })
    ] }) : null,
    kind === 'uninstall' ? jsxs('section', { className: 'space-y-1', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Installed snapshot to remove' }),
      jsx(KeyValue, { name: 'skill', value: label(review?.installation) }),
      jsx(KeyValue, { name: 'version', value: review?.installation?.version }),
      jsx(KeyValue, { name: 'content hash', value: review?.installation?.content_hash, mono: true }),
      jsx(KeyValue, { name: 'target', value: review?.installation?.target_path, mono: true })
    ] }) : null,
    kind === 'install' || kind === 'update' ? jsx(Compatibility, { value: review?.compatibility }) : null,
    kind === 'install' || kind === 'update' ? jsx(CheckSummary, { title: 'Local scanner', check: review?.local_scan, authority: 'Local scan' }) : null,
    kind === 'install' || kind === 'update' ? jsx(CheckSummary, { title: 'Server security', check: review?.server_checks?.security_check || review?.server_checks?.scan, authority: 'Server' }) : null,
    kind === 'install' || kind === 'update' ? jsx(CheckSummary, { title: 'Server professionalism', check: review?.server_checks?.professionalism_check, authority: 'Server' }) : null,
    isPublication ? jsx(CheckSummary, { title: 'Local package scanner', check: review?.local_scan, authority: 'Local scan' }) : null,
    isPublication ? jsx(CheckSummary, { title: 'Local security and upload gate', check: review?.security_check, authority: 'Local security' }) : null,
    isPublication ? jsx(CheckSummary, { title: 'Professionalism review', check: review?.professionalism_check, authority: 'Assessment' }) : null,
    kind === 'update' ? jsxs('section', { className: 'space-y-2', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Update policy and sensitive changes' }),
      jsx(KeyValue, { name: 'local modified', value: review?.modified }),
      jsx(KeyValue, { name: 'preservation', value: review?.preservation?.action }),
      jsx(KeyValue, { name: 'preserved copy', value: review?.preservation?.destination, mono: true }),
      jsx(KeyValue, { name: 'authorization', value: review?.authorization?.kind }),
      jsx(KeyValue, { name: 'update mode', value: review?.authorization?.mode }),
      jsx(KeyValue, { name: 'accepted sensitive', value: review?.policy?.accepted_sensitive }),
      jsx(KeyValue, { name: 'accepted partial', value: review?.policy?.accepted_partial }),
      jsx(StringList, { items: review?.sensitive_expansion, empty: 'No sensitive expansion reported.' })
    ] }) : null,
    jsxs('details', { open: warnings.length > 0 || isPublication, children: [
      jsx('summary', { className: 'cursor-pointer text-xs font-semibold', children: isPublication ? 'Full reviewed files and exact metadata' : 'Exact file manifest and policy details' }),
      jsxs('div', { className: 'mt-3 space-y-3', children: [
        jsx(FileManifest, { files: review?.files, showContent: isPublication }),
        jsx(KeyValue, { name: 'requested mode', value: review?.policy?.requested_update_mode }),
        jsx(KeyValue, { name: 'scan confirmation', value: review?.policy?.scan_requires_confirmation }),
        jsx(KeyValue, { name: 'compat confirmation', value: review?.policy?.compatibility_requires_confirmation }),
        jsx(KeyValue, { name: 'expires', value: review?.authorization_expires_at })
      ] })
    ] }),
    review?.notice ? jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: review.notice }) : null,
    problems.length ? jsxs('section', { className: 'space-y-1', children: [
      jsx('h4', { className: 'text-xs font-semibold', children: 'Cannot confirm this review' }),
      jsx(StringList, { items: problems })
    ] }) : null,
    jsxs('div', { className: 'flex justify-end gap-2', children: [
      jsx(Button, { disabled: busy, onClick: onCancel, size: 'sm', type: 'button', variant: 'secondary', children: 'Cancel' }),
      jsx(Button, { disabled: busy || problems.length > 0, loading: busy, onClick: onConfirm, size: 'sm', type: 'button', children: `Confirm ${kind}` })
    ] })
  ] })
}

function OperationOutcome({ kind, result }) {
  if (!result) return null
  return jsxs('section', { 'aria-live': 'polite', className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
    jsx('h3', { className: 'text-xs font-semibold', children: `${kind} requires attention` }),
    jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: result.detail || result.error || 'The service did not report a completed successful outcome. Reload authoritative status before deciding what to do next.' }),
    jsx(KeyValue, { name: 'operation', value: result.operation_id, mono: true }),
    jsx(KeyValue, { name: 'state', value: result.state || (result.pending ? 'pending' : result.rejected ? 'rejected' : 'unknown') }),
    jsx(KeyValue, { name: 'phase', value: result.phase }),
    jsx(KeyValue, { name: 'skill', value: result.skill_id }),
    jsx(KeyValue, { name: 'remote draft', value: result.remote_draft_id, mono: true }),
    jsx(KeyValue, { name: 'retry safe', value: result.retry_safe }),
    jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Do not repeat the mutation automatically. Use Activity and authoritative status to recover this exact operation.' })
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

function useCurrentMutation(client, lifecycle, operation, onSuccess, failureMessage) {
  const currentRef = useRef(lifecycle)
  currentRef.current = lifecycle
  const isCurrent = token => client.active() && token.mounted() && currentRef.current.key === token.key &&
    host.state.connectionId.get() === token.connectionId && host.state.profile.get() === token.profile
  return useMutation({
    retry: false,
    mutationFn: async variables => {
      const token = lifecycle
      if (!isCurrent(token)) throw Object.assign(new Error('This review is no longer current.'), { stale: true })
      const result = await operation(variables, token)
      if (!isCurrent(token)) throw Object.assign(new Error('This review is no longer current.'), { stale: true })
      return { result, token }
    },
    onSuccess: async payload => {
      if (isCurrent(payload.token)) await onSuccess(payload.result, payload.token, isCurrent)
    },
    onError: error => {
      if (!error?.stale && isCurrent(lifecycle)) host.notifyError(error, failureMessage)
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

function Catalog({ client, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState(null)
  const [review, setReview] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const catalog = useQuery(queryOptions(['catalog', lifecycle.key, search], () => client.rest(`/catalog?q=${encodeURIComponent(search.trim())}`)))
  const detail = useQuery(queryOptions(['detail', lifecycle.key, selected], () => client.rest(`/catalog/${encodeURIComponent(selected)}`), Boolean(selected)))
  const versions = useQuery(queryOptions(['versions', lifecycle.key, selected], () => client.rest(`/catalog/${encodeURIComponent(selected)}/versions`), Boolean(selected)))
  const prepare = useCurrentMutation(client, lifecycle, item => {
    const version = versionOf(item)
    return client.rest('/install/review', { method: 'POST', body: requestBody(viewId, { reference: version ? `${item.id}@v${version}` : item.id }) })
  }, result => { setOutcome(null); setReview(result) }, 'Could not prepare the install review')
  const confirm = useCurrentMutation(client, lifecycle, current => client.rest('/install/confirm', {
    method: 'POST', body: requestBody(viewId, { receipt: current.receipt, accept_partial: false })
  }), async (result, token, isCurrent) => {
    setReview(null)
    if (result?.installed === true) {
      await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
      if (isCurrent(token)) host.notify({ kind: 'success', message: `Installed ${label(result)}` })
    } else {
      setOutcome(result || { state: 'unknown' })
    }
  }, 'Install was not applied')
  const skills = Array.isArray(catalog.data?.skills) ? catalog.data.skills : []
  const chosen = skills.find(item => item.id === selected)
  const detailedSkill = detail.data?.skill || detail.data

  return jsxs('div', { className: 'space-y-5', children: [
    jsx(Input, { 'aria-label': 'Search shared skills', onChange: event => setSearch(event.target.value), placeholder: 'Search shared skills', type: 'search', value: search }),
    catalog.error ? jsx(EmptyState, { title: 'Catalog unavailable', description: text(catalog.error?.message || catalog.error) }) : null,
    !catalog.isLoading && !catalog.error && skills.length === 0 ? jsx(EmptyState, { title: 'No shared skills', description: 'No published skills match this search.' }) : null,
    jsx('ul', { className: 'divide-y divide-(--ui-stroke-secondary)', children: skills.map(item =>
      jsxs('li', { className: 'flex items-center gap-3 py-3', children: [
        jsxs('div', { className: 'min-w-0 flex-1', children: [
          jsx('div', { className: 'text-sm font-medium', children: label(item) }),
          jsx('p', { className: 'truncate text-xs text-(--ui-text-tertiary)', children: item.author_description || 'No publisher description.' })
        ] }),
        versionOf(item) ? jsx(Badge, { children: `v${versionOf(item)}` }) : null,
        jsx(Button, { onClick: () => setSelected(item.id), size: 'sm', type: 'button', variant: 'secondary', children: 'Details' }),
        jsx(Button, { disabled: prepare.isPending, onClick: () => prepare.mutate(item), size: 'sm', type: 'button', children: 'Install' })
      ] }, item.id)
    ) }),
    selected ? jsxs(Section, { title: `Details — ${label(detailedSkill || chosen)}`, children: [
      jsx('p', { className: 'text-xs leading-5 text-(--ui-text-secondary)', children: detailedSkill?.author_description || detail.data?.latest_version_detail?.version?.author_description || chosen?.author_description || 'No description supplied.' }),
      jsx('h3', { className: 'text-xs font-semibold', children: 'Version history' }),
      jsx('ul', { className: 'flex flex-wrap gap-2', children: (versions.data?.versions || []).map(item => jsx(Badge, { children: `v${item.version} · ${item.state || 'published'}` }, `${item.version}-${item.state}`)) })
    ] }) : null,
    review ? jsx(ReviewPanel, { busy: confirm.isPending, kind: 'install', onCancel: () => setReview(null), onConfirm: () => confirm.mutate(review), review }) : null,
    jsx(OperationOutcome, { kind: 'Install', result: outcome })
  ] })
}

function Installed({ bootstrap, client, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const [preserve, setPreserve] = useState(true)
  const [review, setReview] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const update = useCurrentMutation(client, lifecycle, item => client.rest('/update/review', {
    method: 'POST', body: requestBody(viewId, { skill_id: item.skill_id, preserve_modified: preserve, accept_sensitive: false, accept_partial: false })
  }), value => { setOutcome(null); setReview({ ...value, operation: 'update' }) }, 'Could not prepare the update review')
  const uninstall = useCurrentMutation(client, lifecycle, item => client.rest('/uninstall/review', {
    method: 'POST', body: requestBody(viewId, { skill_id: item.skill_id })
  }), value => { setOutcome(null); setReview({ ...value, operation: 'uninstall' }) }, 'Could not prepare the uninstall review')
  const confirm = useCurrentMutation(client, lifecycle, async current => ({ operation: current.operation, result: await client.rest(current.operation === 'uninstall' ? '/uninstall/confirm' : '/update/confirm', {
    method: 'POST', body: current.operation === 'uninstall'
      ? requestBody(viewId, { review_id: current.review_id })
      : requestBody(viewId, { receipt: current.receipt })
  }) }), async (payload, token, isCurrent) => {
    const { operation, result } = payload
    setReview(null)
    const succeeded = operation === 'uninstall' ? result?.uninstalled === true : result?.updated === true
    if (succeeded) {
      await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
      if (isCurrent(token)) host.notify({ kind: 'success', message: operation === 'uninstall' ? 'Skill removed' : 'Skill updated' })
    } else {
      setOutcome({ ...(result || { state: 'unknown' }), kind: operation })
    }
  }, 'The reviewed action was not completed')
  const installations = Array.isArray(bootstrap?.installations) ? bootstrap.installations : []
  return jsxs('div', { className: 'space-y-4', children: [
    jsxs('label', { className: 'flex items-center gap-2 text-xs', children: [
      jsx(Checkbox, { 'aria-label': 'Preserve locally modified files', checked: preserve, onCheckedChange: value => setPreserve(value === true) }),
      'Preserve locally modified files when preparing updates'
    ] }),
    installations.length === 0 ? jsx(EmptyState, { title: 'Nothing installed', description: 'Install a shared skill from Catalog.' }) : null,
    jsx('ul', { className: 'divide-y divide-(--ui-stroke-secondary)', children: installations.map(item =>
      jsxs('li', { className: 'flex items-center gap-3 py-3', children: [
        jsxs('div', { className: 'min-w-0 flex-1', children: [
          jsx('div', { className: 'text-sm font-medium', children: label(item) }),
          jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `v${item.version || '?'}${item.modified ? ' · local edits detected' : ''}` })
        ] }),
        jsx(Button, { disabled: update.isPending, onClick: () => update.mutate(item), size: 'sm', type: 'button', children: 'Update' }),
        jsx(Button, { disabled: uninstall.isPending, onClick: () => uninstall.mutate(item), size: 'sm', type: 'button', variant: 'secondary', children: 'Uninstall' })
      ] }, item.skill_id)
    ) }),
    review ? jsx(ReviewPanel, { busy: confirm.isPending, kind: review.operation, onCancel: () => setReview(null), onConfirm: () => confirm.mutate(review), review }) : null,
    jsx(OperationOutcome, { kind: outcome?.kind === 'uninstall' ? 'Uninstall' : 'Update', result: outcome })
  ] })
}

function ActivitySettings({ bootstrap, client, lifecycle, viewId }) {
  const queryClient = useQueryClient()
  const mode = bootstrap?.settings?.notifications?.delivery_mode || bootstrap?.activity?.mode || 'agent'
  const setMode = useCurrentMutation(client, lifecycle, next => client.rest('/settings/mode', {
    method: 'PATCH', body: requestBody(viewId, { mode: next, expected_mode: mode })
  }), async (_result, token, isCurrent) => {
    await queryClient.invalidateQueries({ exact: true, queryKey: [...ROOT_KEY, 'bootstrap', lifecycle.key] })
    if (isCurrent(token)) host.notify({ kind: 'success', message: 'Notification mode updated' })
  }, 'Notification mode was not changed')
  const pending = bootstrap?.status?.pending_operations || []
  return jsxs('div', { className: 'space-y-6', children: [
    jsx(Section, { title: 'Delivery mode', description: 'Agent mode injects bounded advice; fixed mode uses configured channels.', children:
      jsxs('div', { className: 'flex gap-2', children: ['agent', 'fixed'].map(value => jsx(Button, { disabled: value === mode || setMode.isPending, onClick: () => setMode.mutate(value), size: 'sm', type: 'button', variant: value === mode ? 'secondary' : 'ghost', children: value }, value)) })
    }),
    jsx(Section, { title: 'Unresolved operations', description: 'These identities are recovery handles, not successful outcomes.', children: pending.length
      ? jsx('ul', { className: 'space-y-2', children: pending.map(item => jsxs('li', { className: 'rounded-[4px] border border-(--ui-stroke-secondary) p-3', children: [
          jsx(KeyValue, { name: 'operation', value: item.operation_id, mono: true }), jsx(KeyValue, { name: 'kind', value: item.kind }), jsx(KeyValue, { name: 'phase', value: item.phase }), jsx(KeyValue, { name: 'skill', value: item.skill_id })
        ] }, item.operation_id)) })
      : jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No unresolved operation reported.' })
    }),
    jsx(Section, { title: 'Assessments', children: (bootstrap?.activity?.assessments || []).length
      ? jsx('ul', { className: 'space-y-2', children: bootstrap.activity.assessments.map((item, index) => jsx('li', { className: 'text-xs', children: item.available === false ? `Unavailable: ${item.unavailable_reason || 'No reason supplied'}` : (item.summary || item.reason || 'Assessment available') }, item.id || index)) })
      : jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No owner-bound assessment is currently available.' })
    })
  ] })
}

function Contributions({ client, lifecycle, viewId }) {
  const [skillName, setSkillName] = useState('')
  const [draft, setDraft] = useState(null)
  const [review, setReview] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const prepare = useCurrentMutation(client, lifecycle, () => client.rest('/drafts/prepare', {
    method: 'POST', body: requestBody(viewId, { skill_name: skillName.trim() })
  }), setDraft, 'Could not prepare the private draft')
  const publicationReview = useCurrentMutation(client, lifecycle, async () => {
    const inspected = await client.rest(`/drafts/${encodeURIComponent(draft.draft_id)}`)
    const reviewed = await client.rest('/publication/review', { method: 'POST', body: requestBody(viewId, { draft_id: draft.draft_id }) })
    const expected = inspected?.expected_hashes
    if (!isObject(expected) || !isObject(reviewed?.hashes) || !['content', 'author_description', 'package_manifest'].every(key => expected[key] === reviewed.hashes[key])) {
      throw new Error('Prepared bytes changed between inspection and publication review.')
    }
    return {
      ...reviewed,
      author_description: inspected.author_description,
      drafted_description: inspected.author_description,
      files: inspected.files,
      operation: 'publication'
    }
  }, value => { setOutcome(null); setReview(value) }, 'Could not prepare the publication review')
  const publish = useCurrentMutation(client, lifecycle, current => client.rest('/publication/confirm', {
    method: 'POST', body: requestBody(viewId, { receipt: current.receipt })
  }), result => {
    setReview(null)
    if (result?.state === 'done' && ['published', 'pending_moderation'].includes(result?.publication_state)) {
      const message = result.publication_state === 'published' ? 'Publication completed' : 'Submitted for team moderation'
      host.notify({ kind: 'success', message })
    } else {
      setOutcome(result || { state: 'unknown' })
    }
  }, 'Publication was not completed')
  return jsxs('div', { className: 'space-y-5', children: [
    jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: 'Preparing is local and private. Confirmation is enabled only after full exact bytes, author copy, hashes, checks, and Gateway policy are inspectable.' }),
    jsxs('div', { className: 'flex gap-2', children: [
      jsx(Input, { 'aria-label': 'Local skill name', onChange: event => setSkillName(event.target.value), placeholder: 'Local skill name', value: skillName }),
      jsx(Button, { disabled: !skillName.trim(), loading: prepare.isPending, onClick: () => prepare.mutate(), type: 'button', children: 'Prepare draft' })
    ] }),
    draft ? jsxs(Section, { title: 'Private draft', children: [
      jsx(KeyValue, { name: 'draft id', value: draft.draft_id, mono: true }),
      jsx(KeyValue, { name: 'skill', value: draft.skill_id || draft.skill_name }),
      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No upload or publication has occurred.' }),
      jsx(Button, { disabled: publicationReview.isPending, onClick: () => publicationReview.mutate(), size: 'sm', type: 'button', children: 'Review publication' })
    ] }) : null,
    review ? jsx(ReviewPanel, { busy: publish.isPending, kind: 'publication', onCancel: () => setReview(null), onConfirm: () => publish.mutate(review), review }) : null,
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
  useEffect(() => () => { tracker.mounted = false; tracker.generation += 1 }, [tracker])

  let content
  if (!available) {
    content = jsxs('div', { className: 'space-y-3', children: [
      jsx(EmptyState, { title: 'Collective Wisdom unavailable', description: connectionId ? `Gateway is ${gateway}. Reconnect before opening a new review.` : 'Desktop did not provide a connection identity. This SDK/backend combination cannot safely route Wisdom operations.' }),
      jsx(Button, { onClick: () => setManualGeneration(value => value + 1), type: 'button', variant: 'secondary', children: 'Recheck connection' })
    ] })
  } else if (bootstrap.error || view.error) {
    const error = bootstrap.error || view.error
    content = jsxs('div', { className: 'space-y-3', children: [
      jsx(EmptyState, { title: isAuthError(error) ? 'Ownership must be rechecked' : 'Collective Wisdom unavailable', description: isAuthError(error) ? 'The backend rejected this owner/view. Cached data was dropped; create a fresh view for the current connection and profile.' : text(error?.message || error) }),
      jsx(Button, { onClick: () => setManualGeneration(value => value + 1), type: 'button', variant: 'secondary', children: 'Reload with a fresh review' })
    ] })
  } else if (!bootstrap.data || !view.data) {
    content = jsx('p', { className: 'p-5 text-sm text-(--ui-text-tertiary)', children: 'Loading owner-bound Wisdom data…' })
  } else if (bootstrap.data.status?.configured !== true) {
    content = jsx(Enrollment, { client, lifecycle, viewId: view.data.view_id })
  } else if (tab === 'installed') {
    content = jsx(Installed, { bootstrap: bootstrap.data, client, lifecycle, viewId: view.data.view_id })
  } else if (tab === 'activity') {
    content = jsx(ActivitySettings, { bootstrap: bootstrap.data, client, lifecycle, viewId: view.data.view_id })
  } else if (tab === 'contribute') {
    content = jsx(Contributions, { client, lifecycle, viewId: view.data.view_id })
  } else {
    content = jsx(Catalog, { client, lifecycle, viewId: view.data.view_id })
  }

  const tabs = [['catalog', 'Catalog'], ['installed', 'Installed'], ['activity', 'Activity'], ['contribute', 'Contribute']]
  return jsxs('div', { className: 'flex h-full min-h-0 flex-col', children: [
    jsxs('header', { className: 'shrink-0 space-y-3 border-b border-(--ui-stroke-secondary) px-5 py-4', children: [
      jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
        jsx(Codicon, { name: 'library', size: '1rem' }), jsx('h1', { className: 'text-base font-semibold', children: 'Collective Wisdom' }),
        jsx(Badge, { children: `connection ${connectionId || 'unavailable'}` }), jsx(Badge, { children: `profile ${profile}` }), jsx(Badge, { children: `generation ${lifecycle.generation}` }), jsx(Badge, { children: `gateway ${gateway}` })
      ] }),
      jsx('nav', { 'aria-label': 'Collective Wisdom sections', className: 'flex flex-wrap gap-1', children: tabs.map(([value, title]) =>
        jsx(Button, { onClick: () => setTab(value), size: 'sm', type: 'button', variant: tab === value ? 'secondary' : 'ghost', children: title }, value)
      ) })
    ] }),
    jsx(ScrollArea, { className: 'min-h-0 flex-1', children: jsxs('main', { className: 'mx-auto w-full max-w-4xl space-y-5 p-5', children: [
      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Every review belongs to the displayed connection, profile, and generation. Reconnects and context changes require a new review. No mutation is retried automatically.' }),
      jsx(Separator, {}), content
    ] }) })
  ] })
}

export default {
  id: 'hermes-collective-wisdom',
  name: 'Collective Wisdom',
  description: 'Browse, install, update, and publish owner-bound team skills with exact review.',
  defaultEnabled: false,
  register(ctx) {
    let active = true
    const client = { active: () => active, rest: (...args) => {
      if (!active) return Promise.reject(Object.assign(new Error('Collective Wisdom plugin is disabled.'), { stale: true }))
      return ctx.rest(...args)
    } }
    const stopEvents = typeof host.onEvent === 'function' && typeof host.notify === 'function'
      ? host.onEvent('*', async event => {
      if (!active || !['session_end', 'session.ended'].includes(event?.type)) return
      try {
        const result = await ctx.rest('/notices/end-turn', { method: 'POST' })
        if (!active || !Array.isArray(result?.notices)) return
        for (const notice of result.notices) {
          const subject = notice.skill_name || notice.slug || notice.skill_id || 'A shared skill'
          host.notify({
            kind: 'info',
            message: `Collective Wisdom: ${subject} is available. Open Wisdom to review.`
          })
        }
      } catch {
        // Best-effort notices never disturb the completed conversation turn.
      }
      })
      : () => undefined
    ctx.onDispose(() => { active = false; stopEvents?.() })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: WISDOM_ROUTE }, render: () => jsx(WisdomPage, { client }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 60, data: { codicon: 'library', label: 'Wisdom', path: WISDOM_ROUTE } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'collective-wisdom.open', label: 'Collective Wisdom: Open', keywords: ['wisdom', 'skills', 'catalog', 'install', 'contribute'], run: () => host.navigate(WISDOM_ROUTE) } }
    ])
  }
}
