import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test, { after, afterEach } from 'node:test'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'

const PLUGIN_URL = new URL('../../desktop/plugin.js', import.meta.url)
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost' })
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const ReactModule = await import('react')
const React = ReactModule.default
const { act } = ReactModule
const { createRoot } = await import('react-dom/client')
const ReactQuery = await import('@tanstack/react-query')
const { QueryClient, QueryClientProvider, useMutation, useQuery, useQueryClient } = ReactQuery
const { jsx, jsxs } = await import('react/jsx-runtime')

after(() => dom.window.close())
afterEach(() => { document.body.replaceChildren() })

function syntheticModule(context, values) {
  return new vm.SyntheticModule(Object.keys(values), function initialize() {
    for (const [name, value] of Object.entries(values)) this.setExport(name, value)
  }, { context })
}

function atom(initial) {
  let value = initial
  const listeners = new Set()
  return {
    get: () => value,
    set(next) { value = next; listeners.forEach(listener => listener()) },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) }
  }
}

function Primitive({ as = 'div', children, loading: _loading, size: _size, variant: _variant, ...props }) {
  return React.createElement(as, props, children)
}

function Button(props) {
  return React.createElement(Primitive, { ...props, as: 'button' })
}

function Input(props) {
  return React.createElement(Primitive, { ...props, as: 'input' })
}

// Faithful to the Radix callback contract used by the real SDK component.
function Checkbox({ checked, onCheckedChange, ...props }) {
  assert.equal('onChange' in props, false, 'SDK Checkbox must not receive a native onChange handler')
  return React.createElement('button', {
    ...props,
    'aria-checked': checked ? 'true' : 'false',
    onClick: () => onCheckedChange?.(!checked),
    role: 'checkbox',
    type: 'button'
  })
}

async function loadPlugin(restHandler, initial = {}) {
  const calls = { navigate: [], notify: [], registrations: [], rest: [] }
  const events = []
  const profile = atom(initial.profile || 'A')
  const connectionId = atom(initial.connectionId || 'remote-a')
  const gateway = atom(initial.gateway || 'open')
  const host = {
    state: { connectionId, gateway, profile },
    onEvent(type, callback) { events.push([type, callback]); return () => events.splice(events.findIndex(row => row[1] === callback), 1) },
    navigate(path) { calls.navigate.push(path) },
    notify(payload) { calls.notify.push(payload) },
    notifyError(error, fallback) { calls.notify.push({ kind: 'error', message: fallback, detail: String(error?.message || error) }) }
  }
  const sdk = {
    Badge: props => React.createElement(Primitive, { ...props, as: 'span' }),
    Button,
    Checkbox,
    Codicon: props => React.createElement(Primitive, { ...props, as: 'span' }),
    EmptyState: ({ title, description }) => React.createElement('section', {}, React.createElement('h2', {}, title), React.createElement('p', {}, description)),
    Input,
    PALETTE_AREA: 'palette',
    ROUTES_AREA: 'routes',
    ScrollArea: props => React.createElement(Primitive, props),
    Separator: props => React.createElement(Primitive, { ...props, as: 'hr' }),
    SIDEBAR_NAV_AREA: 'sidebar.nav',
    host,
    useMutation,
    useQuery,
    useQueryClient,
    useValue(source) { return React.useSyncExternalStore(source.subscribe, source.get, source.get) }
  }
  const context = vm.createContext({ console, setTimeout, clearTimeout, URLSearchParams })
  const source = await readFile(PLUGIN_URL, 'utf8')
  const module = new vm.SourceTextModule(source, { context, identifier: fileURLToPath(PLUGIN_URL) })
  await module.link(specifier => {
    if (specifier === '@hermes/plugin-sdk') return syntheticModule(context, sdk)
    if (specifier === 'react') return syntheticModule(context, React)
    if (specifier === 'react/jsx-runtime') return syntheticModule(context, { jsx, jsxs })
    throw new Error(`unsupported plugin import: ${specifier}`)
  })
  await module.evaluate()

  let dispose = () => undefined
  const ctx = {
    onDispose(callback) { dispose = callback },
    registerMany(contributions) { calls.registrations.push(...contributions); return () => undefined },
    async rest(path, options = {}) {
      const owner = { connectionId: connectionId.get(), gateway: gateway.get(), profile: profile.get() }
      calls.rest.push([path, options, owner])
      return restHandler(path, options, owner)
    }
  }
  const plugin = module.namespace.default
  plugin.register(ctx)
  return { calls, connectionId, dispose, emit: async event => { for (const [type, callback] of [...events]) if (type === '*' || type === event.type) await callback(event) }, gateway, plugin, profile }
}

test('completed turns request one best-effort notice and use native notifications', async () => {
  const loaded = await loadPlugin(async path => {
    if (path === '/notices/end-turn') return {
      supported: true, offered: 1, polled: true,
      notices: [{ category: 'new_skill', skill_name: 'Useful helper' }]
    }
    throw new Error(`unexpected ${path}`)
  })

  await loaded.emit({ type: 'session_end' })

  assert.ok(loaded.calls.rest.some(([path]) => path === '/notices/end-turn'))
  const notice = loaded.calls.notify.at(-1)
  assert.equal(notice.kind, 'info')
  assert.equal(notice.message, 'Collective Wisdom: Useful helper is available. Open Wisdom to review.')
})

async function renderPage(loaded) {
  const container = document.createElement('div')
  document.body.append(container)
  const route = loaded.calls.registrations.find(item => item.area === 'routes')
  assert.ok(route, 'route contribution should be registered')
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false }, mutations: { gcTime: 0, retry: false } } })
  const root = createRoot(container)
  await act(async () => { root.render(React.createElement(QueryClientProvider, { client }, route.render())) })
  await settle()
  return {
    container,
    async cleanup() {
      await act(async () => root.unmount())
      client.clear()
      container.remove()
    }
  }
}

async function settle(ms = 25) {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, ms)) })
}

async function click(element) {
  assert.ok(element, 'expected clickable element')
  await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
  await settle()
}

async function enter(input, value) {
  assert.ok(input, 'expected input')
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
  await act(async () => {
    setter.call(input, value)
    input.dispatchEvent(new window.Event('input', { bubbles: true }))
    input.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  await settle()
}

const hash = character => `sha256:${character.repeat(64)}`
const bootstrap = {
  status: { configured: true, state: 'active', pending_operations: [] },
  settings: { notifications: { delivery_mode: 'agent' } },
  activity: { mode: 'agent', assessments: [], interactions: [], installed_setup: [] },
  installations: []
}

function installReview(overrides = {}) {
  return {
    operation: 'install', receipt: 'install-receipt', skill_id: 'skill-1', slug: 'one', version: 2,
    content_hash: hash('a'), manifest_hash: hash('b'), hashes: { content: hash('a'), manifest: hash('b') },
    files: [{ path: 'SKILL.md', mode: 'file', hash: hash('c'), size: 42 }, { path: 'skill.manifest.json', mode: 'file', hash: hash('d'), size: 64 }],
    compatibility: { outcome: 'compatible', satisfied: ['Hermes >= 0.16'], setup_actions: [], limitations: [], blocked: [] },
    server_checks: {
      security_check: { allowed: true, summary: 'Server security checks passed.', findings: [] },
      professionalism_check: { state: 'unavailable', summary: 'Model review was not requested.', findings: [] }
    },
    local_scan: { allowed: true, verdict: 'allow', summary: 'Local scanner passed.', findings: [] },
    policy: { requested_update_mode: null, requires_explicit_confirmation: true, scan_requires_confirmation: false, compatibility_requires_confirmation: false },
    permitted_actions: ['confirm_install', 'recheck'], allowed: true,
    ...overrides
  }
}

function updateReview(overrides = {}) {
  return {
    ...installReview(), operation: 'update', receipt: 'update-receipt', from_version: 1,
    previous_content_hash: hash('0'), previous_installation: { skill_id: 'skill-1', slug: 'one', version: 1, content_hash: hash('0'), target_path: '/tmp/managed/one' },
    modified: true, sensitive_expansion: ['new network destinations: api.example.test'],
    preservation: { action: 'preserve_unmanaged_copy', destination: '/tmp/one-local-before-v2' },
    authorization: { kind: 'manual_receipt', mode: 'MANUAL' },
    policy: { requested_update_mode: 'MANUAL', requires_explicit_confirmation: true, scan_requires_confirmation: false, compatibility_requires_confirmation: false, manual_only: true, accepted_sensitive: true, accepted_partial: false, accepted_modified_preservation: true },
    permitted_actions: ['confirm_update', 'recheck'], allowed: true,
    ...overrides
  }
}

function baseHandler(path, options, owner) {
  if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}`, profile: owner.profile }
  if (path === '/bootstrap') return bootstrap
  if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', slug: 'one', author_description: 'Useful skill', latest_version: 2 }] }
  if (path === '/catalog/skill-1') return { id: 'skill-1', slug: 'one', author_description: 'Useful skill', latest_version_detail: { version: { version: 2 } } }
  if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }, { version: 1, state: 'withdrawn' }] }
  throw new Error(`unexpected REST ${options.method || 'GET'} ${path}`)
}

const button = (mounted, name) => [...mounted.container.querySelectorAll('button')].find(item => item.textContent === name)

test('registers the native page and performs catalog search, details, exact install review, and confirmation', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', reference: 'skill-1@v2' })
      return installReview()
    }
    if (path === '/install/confirm') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', receipt: 'install-receipt', accept_partial: false })
      return { installed: true, skill_id: 'skill-1', version: 2 }
    }
    return baseHandler(path, options, owner)
  })
  assert.equal(loaded.plugin.defaultEnabled, false)
  assert.deepEqual(loaded.calls.registrations.map(item => item.area), ['routes', 'sidebar.nav', 'palette'])
  const mounted = await renderPage(loaded)

  assert.match(mounted.container.textContent, /connection remote-a/)
  assert.match(mounted.container.textContent, /profile A/)
  await enter(mounted.container.querySelector('input[aria-label="Search shared skills"]'), 'useful')
  assert.ok(loaded.calls.rest.some(([path]) => path === '/catalog?q=useful'))
  await click(button(mounted, 'Details'))
  assert.match(mounted.container.textContent, /Version history/)
  assert.match(mounted.container.textContent, /withdrawn/)

  await click(button(mounted, 'Install'))
  assert.match(mounted.container.textContent, new RegExp(hash('a')))
  assert.match(mounted.container.textContent, /Hermes >= 0.16/)
  assert.match(mounted.container.textContent, /Server security checks passed/)
  assert.match(mounted.container.textContent, /Model review was not requested/)
  assert.match(mounted.container.textContent, /Local scanner passed/)
  assert.match(mounted.container.textContent, /SKILL\.md/)
  await click(button(mounted, 'Confirm install'))
  assert.equal(loaded.calls.notify.at(-1)?.kind, 'success')
  assert.equal(loaded.calls.rest.some(([path]) => path.startsWith('/views/') && path !== '/views'), false, 'must not DELETE an old view through a newly routed ctx.rest')
  await mounted.cleanup()
})

test('unknown required install review fields fail closed', async () => {
  const loaded = await loadPlugin((path, options, owner) => path === '/install/review'
    ? { receipt: 'looks-real', skill_id: 'skill-1', version: 2, allowed: true }
    : baseHandler(path, options, owner))
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.match(mounted.container.textContent, /confirmation blocked/)
  assert.match(mounted.container.textContent, /Compatibility evaluation is unavailable/)
  assert.equal(button(mounted, 'Confirm install').disabled, true)
  await mounted.cleanup()
})

test('partial compatibility cannot be confirmed without a dedicated acknowledgement control', async () => {
  const loaded = await loadPlugin((path, options, owner) => path === '/install/review'
    ? installReview({
        compatibility: { outcome: 'compatible_after_setup', satisfied: [], setup_actions: ['Run a separate setup review'], limitations: ['Requires setup before use'], blocked: [] },
        policy: { requested_update_mode: null, requires_explicit_confirmation: true, scan_requires_confirmation: false, compatibility_requires_confirmation: true }
      })
    : baseHandler(path, options, owner))
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.match(mounted.container.textContent, /Requires setup before use/)
  assert.match(mounted.container.textContent, /bounded Desktop UI does not collect/)
  assert.equal(button(mounted, 'Confirm install').disabled, true)
  await mounted.cleanup()
})

test('installed update renders nested policy/security data and Radix checkbox controls the request', async () => {
  const managed = { ...bootstrap, installations: [{ skill_id: 'skill-1', slug: 'one', version: 1, modified: true, content_hash: hash('0') }] }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return managed
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/update/review') {
      assert.equal(options.body.preserve_modified, false)
      return updateReview()
    }
    if (path === '/update/confirm') return { updated: true, skill_id: 'skill-1', version: 2 }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Installed'))
  const preserve = mounted.container.querySelector('[role="checkbox"][aria-label="Preserve locally modified files"]')
  assert.equal(preserve.getAttribute('aria-checked'), 'true')
  await click(preserve)
  assert.equal(preserve.getAttribute('aria-checked'), 'false')
  await click(button(mounted, 'Update'))
  assert.match(mounted.container.textContent, /new network destinations/)
  assert.match(mounted.container.textContent, /preserve_unmanaged_copy/)
  assert.match(mounted.container.textContent, /accepted sensitive/)
  assert.match(mounted.container.textContent, /Server professionalism/)
  assert.match(mounted.container.textContent, /Server: unavailable/)
  await click(button(mounted, 'Confirm update'))
  assert.equal(loaded.calls.notify.at(-1)?.message, 'Skill updated')
  await mounted.cleanup()
})

test('uninstall requires the exact installed snapshot and confirms its review id', async () => {
  const installation = { skill_id: 'skill-1', slug: 'one', version: 1, content_hash: hash('0'), target_path: '/tmp/managed/one' }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return { ...bootstrap, installations: [installation] }
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/uninstall/review') return { review_id: 'uninstall-review', installation }
    if (path === '/uninstall/confirm') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', review_id: 'uninstall-review' })
      return { uninstalled: true, skill_id: 'skill-1' }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Installed'))
  await click(button(mounted, 'Uninstall'))
  assert.match(mounted.container.textContent, /Installed snapshot to remove/)
  assert.match(mounted.container.textContent, new RegExp(hash('0')))
  await click(button(mounted, 'Confirm uninstall'))
  assert.equal(loaded.calls.notify.at(-1)?.message, 'Skill removed')
  await mounted.cleanup()
})

test('pending install outcome keeps operation identity and is never announced as installed', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview()
    if (path === '/install/confirm') return { pending: true, operation_id: 'op-install-1', skill_id: 'skill-1', phase: 'local_ledger_committed', error: 'Gateway reconciliation is temporarily unavailable' }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  await click(button(mounted, 'Confirm install'))
  assert.match(mounted.container.textContent, /op-install-1/)
  assert.match(mounted.container.textContent, /Do not repeat the mutation automatically/)
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  await mounted.cleanup()
})

test('enrollment uses the exact disclosure and real Checkbox onCheckedChange semantics', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return { ...bootstrap, status: { configured: false, state: 'not_configured' } }
    if (path === '/enrollment/disclosure') return { version: 'disclosure-v7', text: 'Exact complete enrollment disclosure.' }
    if (path === '/enrollment/confirm') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', accepted: true, disclosure_version: 'disclosure-v7' })
      return { configured: true, state: 'active' }
    }
    if (path.startsWith('/catalog?')) return { skills: [] }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  assert.match(mounted.container.textContent, /Exact complete enrollment disclosure/)
  const enroll = button(mounted, 'Enroll profile')
  assert.equal(enroll.disabled, true)
  const checkbox = mounted.container.querySelector('[role="checkbox"][aria-label="Accept enrollment disclosure"]')
  await click(checkbox)
  assert.equal(enroll.disabled, false)
  await click(enroll)
  assert.ok(loaded.calls.rest.some(([path]) => path === '/enrollment/confirm'))
  await mounted.cleanup()
})

test('publication inspects full bytes, renders checks/hashes/author copy, and reports moderated outcome accurately', async () => {
  const files = [
    { path: 'SKILL.md', content_utf8: '# Exact reviewed bytes' },
    { path: 'skill.manifest.json', content_utf8: '{"name":"local-skill"}' }
  ]
  const hashes = { content: hash('3'), author_description: hash('4'), package_manifest: hash('5') }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:1', skill_id: 'local-skill', files: [{ path: 'SKILL.md' }] }
    if (path === '/drafts/local%3A1') return { draft_id: 'local:1', author_description: 'Exact author copy for teammates.', files, expected_hashes: hashes }
    if (path === '/publication/review') return {
      receipt: 'publication-receipt', draft_id: 'local:1', skill_id: 'local-skill', hashes,
      description: 'Exact author copy for teammates.',
      security_check: { upload_allowed: true, summary: 'Exact local security gate allows upload.', findings: [] },
      professionalism_check: { state: 'unavailable', summary: 'Optional model review unavailable.', findings: [] },
      local_scan: { allowed: true, summary: 'Host scan passed.', findings: [] },
      publication_mode: 'moderated', policy_version: 'policy-9', notice: 'Nothing has been uploaded.'
    }
    if (path === '/publication/confirm') return { operation_id: 'publication-op', state: 'done', publication_state: 'pending_moderation', remote_draft_id: 'remote-1' }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  assert.match(mounted.container.textContent, /No upload or publication has occurred/)
  await click(button(mounted, 'Review publication'))
  assert.match(mounted.container.textContent, /Exact author copy for teammates/)
  assert.match(mounted.container.textContent, /# Exact reviewed bytes/)
  assert.match(mounted.container.textContent, new RegExp(hash('3')))
  assert.match(mounted.container.textContent, /Local security: passed/)
  assert.match(mounted.container.textContent, /Assessment: unavailable/)
  assert.match(mounted.container.textContent, /Moderated: confirmation submits for team moderation/)
  await click(button(mounted, 'Confirm publication'))
  assert.equal(loaded.calls.notify.at(-1)?.message, 'Submitted for team moderation')
  await mounted.cleanup()
})

test('publication hash comparison ignores object key insertion order', async () => {
  const expectedHashes = {
    content: hash('3'),
    author_description: hash('4'),
    package_manifest: hash('5')
  }
  const reviewedHashes = {
    package_manifest: hash('5'),
    content: hash('3'),
    author_description: hash('4')
  }
  const files = [{ path: 'SKILL.md', content_utf8: '# Exact reviewed bytes' }]
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:ordered', skill_id: 'local-skill' }
    if (path === '/drafts/local%3Aordered') return {
      author_description: 'Exact author copy.', files, expected_hashes: expectedHashes
    }
    if (path === '/publication/review') return {
      receipt: 'publication-receipt', draft_id: 'local:ordered', skill_id: 'local-skill',
      hashes: reviewedHashes, description: 'Exact author copy.', files,
      security_check: { upload_allowed: true, summary: 'Allowed.', findings: [] },
      professionalism_check: { state: 'unavailable', summary: 'Unavailable.', findings: [] },
      local_scan: { allowed: true, summary: 'Passed.', findings: [] },
      publication_mode: 'open', policy_version: 'policy-1'
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  await click(button(mounted, 'Review publication'))

  assert.match(mounted.container.textContent, /ready to confirm/)
  assert.equal(button(mounted, 'Confirm publication').disabled, false)
  await mounted.cleanup()
})

test('publication without an explicit security upload gate fails closed', async () => {
  const hashes = { content: hash('3'), author_description: hash('4'), package_manifest: hash('5') }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:1', skill_id: 'local-skill' }
    if (path === '/drafts/local%3A1') return { author_description: 'Copy', files: [{ path: 'SKILL.md', content_utf8: '# bytes' }], expected_hashes: hashes }
    if (path === '/publication/review') return { receipt: 'publication-receipt', draft_id: 'local:1', skill_id: 'local-skill', hashes, description: 'Copy', files: [], security_check: { summary: 'Scanner unavailable.' }, professionalism_check: { state: 'unavailable' }, publication_mode: 'open', policy_version: 'p1' }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  await click(button(mounted, 'Review publication'))
  assert.match(mounted.container.textContent, /does not explicitly allow upload/)
  assert.equal(button(mounted, 'Confirm publication').disabled, true)
  await mounted.cleanup()
})

test('A-B-A profile changes suppress a late review and open a fresh generation', async () => {
  let resolveOld
  const oldReview = new Promise(resolve => { resolveOld = resolve })
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review' && owner.profile === 'A') return oldReview
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  const firstGeneration = mounted.container.textContent.match(/generation (\d+)/)[1]
  await click(button(mounted, 'Install'))
  await act(async () => loaded.profile.set('B'))
  await settle()
  await act(async () => loaded.profile.set('A'))
  await settle()
  resolveOld(installReview({ receipt: 'stale-A-review' }))
  await settle()
  assert.doesNotMatch(mounted.container.textContent, /stale-A-review/)
  const currentGeneration = mounted.container.textContent.match(/generation (\d+)/)[1]
  assert.ok(Number(currentGeneration) > Number(firstGeneration))
  assert.ok(loaded.calls.rest.filter(([path]) => path === '/views').length >= 3)
  await mounted.cleanup()
})

test('connection replacement and reconnect suppress old results and require a new view', async () => {
  let resolveOld
  const oldReview = new Promise(resolve => { resolveOld = resolve })
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review' && owner.connectionId === 'remote-a') return oldReview
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  await act(async () => loaded.connectionId.set('remote-b'))
  await settle()
  await act(async () => loaded.gateway.set('connecting'))
  await settle()
  assert.match(mounted.container.textContent, /Reconnect before opening a new review/)
  await act(async () => loaded.gateway.set('open'))
  await settle()
  resolveOld(installReview({ receipt: 'old-connection-review' }))
  await settle()
  assert.doesNotMatch(mounted.container.textContent, /old-connection-review/)
  assert.ok(loaded.calls.rest.some(([path, , owner]) => path === '/views' && owner.connectionId === 'remote-b'))
  await mounted.cleanup()
})

test('unmount and plugin disable suppress late errors and success notifications', async () => {
  let rejectReview
  const delayed = new Promise((_resolve, reject) => { rejectReview = reject })
  const loaded = await loadPlugin((path, options, owner) => path === '/install/review' ? delayed : baseHandler(path, options, owner))
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  await mounted.cleanup()
  loaded.dispose()
  rejectReview(new Error('late network failure'))
  await settle()
  assert.deepEqual(loaded.calls.notify, [])
})

test('401 drops the old view and offers a fresh ownership recheck', async () => {
  let rejected = true
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap' && rejected) throw Object.assign(new Error('401 owner changed'), { status: 401 })
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  assert.match(mounted.container.textContent, /Ownership must be rechecked/)
  assert.match(mounted.container.textContent, /Cached data was dropped/)
  rejected = false
  await click(button(mounted, 'Reload with a fresh review'))
  assert.match(mounted.container.textContent, /No shared skills/)
  assert.ok(loaded.calls.rest.filter(([path]) => path === '/views').length >= 2)
  await mounted.cleanup()
})
