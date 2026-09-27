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
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    listen(listener) { listeners.add(listener); return () => listeners.delete(listener) }
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

function Textarea(props) {
  return React.createElement(Primitive, { ...props, as: 'textarea' })
}

function Dialog({ children, onOpenChange, open }) {
  if (!open) return null
  return React.createElement('div', {
    onKeyDown: event => { if (event.key === 'Escape') onOpenChange?.(false) },
    role: 'dialog'
  }, children)
}

function DialogPart({ children, ...props }) {
  return React.createElement('div', props, children)
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
  const focusedSessionId = atom(initial.focusedSessionId ?? 'runtime-1')
  const focusedStoredSessionId = atom(initial.focusedStoredSessionId ?? 'stored-1')
  const focusedSessionOwner = atom(initial.focusedSessionOwner === undefined
    ? { connectionId: connectionId.get(), profile: profile.get() }
    : initial.focusedSessionOwner)
  const host = {
    state: { connectionId, focusedSessionId, focusedSessionOwner, focusedStoredSessionId, gateway, profile },
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
    COMPOSER_AREAS: { middleware: 'composer.middleware', top: 'composer.top' },
    Dialog,
    DialogContent: DialogPart,
    DialogDescription: DialogPart,
    DialogFooter: DialogPart,
    DialogHeader: DialogPart,
    DialogTitle: ({ children, ...props }) => React.createElement('h2', props, children),
    EmptyState: ({ title, description }) => React.createElement('section', {}, React.createElement('h2', {}, title), React.createElement('p', {}, description)),
    Input,
    PALETTE_AREA: 'palette',
    ROUTES_AREA: 'routes',
    ScrollArea: props => React.createElement(Primitive, props),
    Separator: props => React.createElement(Primitive, { ...props, as: 'hr' }),
    SIDEBAR_NAV_AREA: 'sidebar.nav',
    TRANSCRIPT_MESSAGE_AREA: initial.inline ? 'chat.transcript-message' : undefined,
    Textarea,
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

  const disposers = []
  const ctx = {
    onDispose(callback) { disposers.push(callback) },
    registerMany(contributions) { calls.registrations.push(...contributions); return () => undefined },
    async rest(path, options = {}) {
      const owner = { connectionId: connectionId.get(), gateway: gateway.get(), profile: profile.get() }
      calls.rest.push([path, options, owner])
      return restHandler(path, options, owner)
    }
  }
  const plugin = module.namespace.default
  plugin.register(ctx)
  return {
    calls,
    connectionId,
    dispose() { for (const callback of disposers.splice(0)) callback() },
    emit: async event => { for (const [type, callback] of [...events]) if (type === '*' || type === event.type) await callback(event) },
    focusedSessionId,
    focusedSessionOwner,
    focusedStoredSessionId,
    gateway,
    plugin,
    profile
  }
}

test('unbound session_end cannot consume or toast a human notice', async () => {
  const loaded = await loadPlugin(async path => { throw new Error(`unexpected ${path}`) })
  await loaded.emit({ type: 'session_end' })
  assert.equal(loaded.calls.rest.length, 0)
  assert.equal(loaded.calls.notify.length, 0)
})

async function renderInline(loaded, props, { strict = false } = {}) {
  const entry = loaded.calls.registrations.find(item => item.area === 'chat.transcript-message')
  assert.ok(entry?.data?.match(props), 'a real slash-result must match the inline contribution')
  const container = document.createElement('div')
  document.body.append(container)
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false }, mutations: { gcTime: 0, retry: false } } })
  const root = createRoot(container)
  const draw = () => React.createElement(QueryClientProvider, { client }, entry.data.render(props))
  await act(async () => root.render(strict ? React.createElement(React.StrictMode, null, draw()) : draw()))
  await settle()
  return {
    container,
    async cleanup() { await act(async () => root.unmount()); client.clear(); container.remove() }
  }
}

const inlineProps = { kind: 'slash-result', sessionId: 'runtime-1', messageId: 'slash-1', isLast: true, command: '/wisdom browse', output: 'ordinary output' }

test('inline row uses SDK match and fences wrong row, A→B→A, disposal and footer notice', async () => {
  const loaded = await loadPlugin(baseHandler, { inline: true })
  const entry = loaded.calls.registrations.find(item => item.area === 'chat.transcript-message')
  assert.ok(entry)
  assert.equal(loaded.calls.registrations.some(item => item.area === 'composer.middleware'), false)
  assert.equal(entry.data.match({ ...inlineProps, command: '/other' }), false)
  assert.equal(entry.data.match({ ...inlineProps, kind: 'assistant-footer' }), false)
  const mounted = await renderInline(loaded, inlineProps, { strict: true })
  assert.match(mounted.container.textContent, /one.*Not installed/)
  assert.equal(mounted.container.querySelector('[role="dialog"]'), null)
  const staleButton = button(mounted, 'Install')
  assert.ok(staleButton)
  await act(async () => { loaded.profile.set('B'); loaded.profile.set('A') })
  await click(staleButton)
  assert.equal(loaded.calls.rest.some(([path]) => path === '/install/review'), false)
  await mounted.cleanup()
  loaded.dispose()
  assert.equal(loaded.calls.rest.some(([path]) => path === '/notices/end-turn'), false)
})

test('inline catalog reviews exact version before install and cannot act for another focused session', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview()
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', version: 2, ready_to_use: true }
    return baseHandler(path, options, owner)
  }, { inline: true })
  const mounted = await renderInline(loaded, inlineProps)
  try {
    const entry = loaded.calls.registrations.find(item => item.area === 'chat.transcript-message')
    assert.equal(entry.data.match({ ...inlineProps, kind: 'assistant-footer' }), false)
    await click(button(mounted, 'Install'))
    assert.deepEqual(loaded.calls.rest.filter(([path]) => path.startsWith('/install/')).map(([path]) => path),
      ['/install/review', '/install/confirm'])
    assert.equal(loaded.calls.navigate.length, 0)
  } finally { await mounted.cleanup(); loaded.dispose() }
  const wrong = await loadPlugin(baseHandler, { inline: true, focusedSessionId: 'runtime-other' })
  const denied = await renderInline(wrong, inlineProps)
  try {
    assert.equal(wrong.calls.rest.length, 0)
    assert.equal(denied.container.textContent, '')
  } finally { await denied.cleanup(); wrong.dispose() }
})

async function renderPage(loaded, { strict = false } = {}) {
  const container = document.createElement('div')
  document.body.append(container)
  const route = loaded.calls.registrations.find(item => item.area === 'routes')
  assert.ok(route, 'route contribution should be registered')
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false }, mutations: { gcTime: 0, retry: false } } })
  const root = createRoot(container)
  await act(async () => {
    const page = React.createElement(QueryClientProvider, { client }, route.render())
    root.render(strict ? React.createElement(React.StrictMode, null, page) : page)
  })
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

async function renderComposerContribution(loaded) {
  const container = document.createElement('div')
  document.body.append(container)
  const contribution = loaded.calls.registrations.find(item => item.area === 'composer.top')
  assert.ok(contribution, 'composer dialog host should be registered')
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false }, mutations: { gcTime: 0, retry: false } } })
  const root = createRoot(container)
  await act(async () => { root.render(React.createElement(QueryClientProvider, { client }, contribution.render())) })
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

const composerMiddleware = loaded => {
  const contribution = loaded.calls.registrations.find(item => item.area === 'composer.middleware')
  assert.ok(contribution?.data?.handler, 'composer middleware should be registered')
  return contribution.data.handler
}

async function submitComposer(loaded, draft) {
  let result
  await act(async () => { result = await composerMiddleware(loaded)(draft) })
  await settle()
  return result
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
  const prototype = input instanceof window.HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set
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
  if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', slug: 'one', author_description: 'Useful skill', latest_version: 2, installation_status: { state: 'not_installed', text: 'Not installed', installed_version: null, latest_version: 2 } }] }
  if (path === '/catalog/skill-1') return { id: 'skill-1', slug: 'one', author_description: 'Useful skill', latest_version_detail: { version: { version: 2 } }, installation_status: { state: 'not_installed', text: 'Not installed', installed_version: null, latest_version: 2 } }
  if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }, { version: 1, state: 'withdrawn' }] }
  throw new Error(`unexpected REST ${options.method || 'GET'} ${path}`)
}

const button = (mounted, name) => {
  const labels = { 'Prepare draft': 'Share', 'Share this exact reviewed package': 'Share', 'Uninstall': 'Remove', 'Acknowledge and update': 'Update' }
  const matches = [...mounted.container.querySelectorAll('button')].filter(item => item.textContent === (labels[name] || name))
  if (name === 'Prepare draft') return matches.find(item => item.previousElementSibling?.getAttribute('aria-label') === 'Local skill name')
  return name === 'Share this exact reviewed package' || name === 'Acknowledge and update' || (name === 'Remove' && mounted.container.querySelector('aside[aria-label="Exact action review"]')) ? matches.at(-1) : matches[0]
}
const reviewInstallButton = mounted => [...mounted.container.querySelectorAll('button')].filter(item => item.textContent === 'Install').at(-1)

test('catalog and in-chat discovery show names and status without identifier fallbacks', async () => {
  const skills = [
    { id: 'uuid-one', name: 'Friendly Helper', latest_version: 2, installation_status: { state: 'installed', installed_version: 1, latest_version: 2, text: 'Installed v1 · Update available v2' } },
    { id: 'uuid-two', latest_version: 1, installation_status: { state: 'not_installed', text: 'Not installed' } }
  ]
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills }
    if (path === '/catalog/uuid-one') return skills[0]
    if (path === '/catalog/uuid-one/versions') return { versions: [{ version: 2, state: 'published' }] }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  assert.match(page.container.textContent, /Friendly Helper.*Installed v1/)
  assert.match(page.container.textContent, /Name unavailable.*Not installed/)
  assert.doesNotMatch(page.container.textContent, /uuid-one|uuid-two/)
  await page.cleanup()
  const chat = await renderComposerContribution(loaded)
  await submitComposer(loaded, { text: '/wisdom show uuid-one' })
  assert.match(chat.container.textContent, /Friendly Helper.*Installed v1/)
  assert.doesNotMatch(chat.container.textContent, /uuid-one|requirements|Installing does not authorize setup/)
  await chat.cleanup()
})

test('reviewed update and removal use name-first terse controls without technical identity', async () => {
  const installation = { skill_id: 'uuid-one', name: 'Friendly Helper', version: 1, content_hash: hash('0'), target_path: '/managed/helper' }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/bootstrap') return { ...bootstrap, installations: [installation] }
    if (path === '/uninstall/review') return { review_id: 'remove-secret', installation }
    if (path === '/uninstall/confirm') return { uninstalled: true, skill_id: 'uuid-one' }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Installed'))
  assert.match(page.container.textContent, /Friendly Helper/)
  await click(button(page, 'Remove'))
  assert.match(page.container.textContent, /Friendly Helper/)
  assert.doesNotMatch(page.container.textContent, /uuid-one|remove-secret|sha256:/)
  await click([...page.container.querySelectorAll('button')].filter(item => item.textContent === 'Remove').at(-1))
  assert.equal(loaded.calls.rest.find(([path]) => path === '/uninstall/confirm')?.[1].body.review_id, 'remove-secret')
  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ Friendly Helper removed')
  await page.cleanup()
})

test('Desktop does not celebrate an opaque identifier as a skill name', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'uuid-one', latest_version: 2, installation_status: { state: 'not_installed', text: 'Not installed' } }] }
    if (path === '/install/review') return installReview({ skill_id: 'uuid-one', slug: 'uuid-one' })
    if (path === '/install/confirm') return { installed: true, skill_id: 'uuid-one' }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Install'))
  await click(reviewInstallButton(page))
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.doesNotMatch(page.container.textContent, /uuid-one/)
  await page.cleanup()
})

test('partial setup result names the installed skill without announcing full success', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', name: 'Friendly Helper', latest_version: 2, installation_status: { state: 'not_installed', text: 'Not installed' } }] }
    if (path === '/install/review') return installReview({ slug: 'Friendly Helper' })
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', setup: { state: 'failed', step: 2, detail: 'Command exited early' } }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Install'))
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.match(page.container.textContent, /Friendly Helper.*files installed|Friendly Helper.*Files installed/i)
  assert.match(page.container.textContent, /setup.*failed/i)
  assert.doesNotMatch(page.container.textContent, /skill-1/)
  await page.cleanup()
})

test('passed setup command with unknown readiness is not a Desktop install success', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', name: 'Friendly Helper', latest_version: 2, installation_status: { state: 'not_installed', text: 'Not installed' } }] }
    if (path === '/install/review') return installReview({ slug: 'Friendly Helper' })
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', setup_required: true, ready_to_use: null, setup: { state: 'passed', steps: 1 } }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Install'))
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.match(page.container.textContent, /Friendly Helper.*files installed; setup incomplete/i)
  await page.cleanup()
})

test('saved private draft resumes by name in a fresh Desktop view without preparing again', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/candidates?')) return { candidates: [] }
    if (path.startsWith('/drafts?')) return { drafts: [{ skill_name: 'Friendly Helper', draft_id: 'local:opaque-42', updated_at: 'now' }] }
    if (path.startsWith('/drafts/local%3Aopaque-42?view_id=')) return {
      draft_id: 'local:opaque-42', author_description: 'Helps the team',
      files: [{ path: 'SKILL.md', content_utf8: '# Friendly Helper' }],
      expected_hashes: { content: hash('a'), author_description: hash('b'), package_manifest: hash('c') }
    }
    if (path === '/drafts/local%3Aopaque-42/review') return {
      draft_id: 'local:opaque-42', files: [{ path: 'SKILL.md', content_utf8: '# Friendly Helper' }],
      hashes: { content: hash('a'), author_description: hash('b'), package_manifest: hash('c') }
    }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Contribute'))
  assert.match(page.container.textContent, /Friendly Helper/)
  assert.doesNotMatch(page.container.textContent, /local:opaque-42/)
  await click(button(page, 'Resume'))
  assert.match(page.container.textContent, /Private draft.*Friendly Helper/)
  await click(button(page, 'Review and edit exact files'))
  assert.ok(loaded.calls.rest.some(([path]) => path.startsWith('/drafts/local%3Aopaque-42?')))
  assert.equal(loaded.calls.rest.some(([path]) => path === '/drafts/prepare'), false)
  await page.cleanup()
})

test('contribution draft stays name-first while keeping its exact private transport identity', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/drafts/prepare') {
      assert.equal(options.body.skill_name, 'Friendly Helper')
      return { draft_id: 'local:opaque-42', skill_id: 'uuid-source' }
    }
    if (path.startsWith('/candidates?')) return { candidates: [] }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Contribute'))
  await enter(page.container.querySelector('input[aria-label="Local skill name"]'), 'Friendly Helper')
  await click(button(page, 'Prepare draft'))
  assert.match(page.container.textContent, /Private draft.*Friendly Helper/)
  assert.doesNotMatch(page.container.textContent, /uuid-source|local:opaque-42|sha256:/)
  await click(button(page, 'Keep draft and exit'))
  assert.match(page.container.textContent, /Friendly Helper was kept locally/)
  assert.doesNotMatch(page.container.textContent, /local:opaque-42/)
  await page.cleanup()
})

test('desktop slash dialog views read-only details and installs in one click', async () => {
  let catalogReads = 0
  let detailReads = 0
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) {
      catalogReads += 1
      return {
        skills: [{
          id: 'skill-1', slug: 'one', author_description: 'Useful skill', latest_version: 2,
          installation_status: { state: 'not_installed', text: 'Not installed', installed_version: null, latest_version: 2 }
        }]
      }
    }
    if (path === '/catalog/skill-1') {
      detailReads += 1
      return {
        id: 'skill-1', slug: 'one', author_description: 'Useful skill', latest_version: 2,
        installation_status: detailReads > 1
          ? { state: 'installed', text: 'Installed v2', installed_version: 2, latest_version: 2 }
          : { state: 'not_installed', text: 'Not installed', installed_version: null, latest_version: 2 }
      }
    }
    if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }] }
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/install/review') return installReview()
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', version: 2 }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderComposerContribution(loaded)
  const draft = { text: '/wisdom browse useful' }

  assert.equal(await submitComposer(loaded, draft), null)
  await settle()
  assert.match(mounted.container.textContent, /Shared skills/)
  assert.equal(loaded.calls.rest.some(([path]) => path === '/install/review'), false)
  assert.ok(button(mounted, 'View'))
  assert.ok(button(mounted, 'Install'), 'catalog rows expose the existing Install review action')

  await click(button(mounted, 'View'))
  assert.match(mounted.container.textContent, /Useful skill/)
  assert.ok(button(mounted, 'Back'))
  assert.equal(loaded.calls.rest.some(([path]) => path === '/install/review'), false)
  assert.equal(loaded.calls.rest.some(([path]) => path === '/install/confirm'), false)
  await click(button(mounted, 'Install'))

  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 1)
  assert.ok(catalogReads >= 2, 'successful install should refresh visible catalog status')
  assert.ok(detailReads >= 2, 'successful install should refresh the visible detail status')
  assert.match(mounted.container.textContent, /Installed v2/)
  await mounted.cleanup()
})

for (const strict of [false, true]) test(`desktop clean install has no second confirmation (strict=${strict})`, async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview({
      receipt: 'receipt-hidden-by-default',
      content_hash: hash('e'),
      manifest_hash: hash('f'),
      hashes: { content: hash('e'), manifest: hash('f') }
    })
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', version: 2 }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded, { strict })

  await click(button(mounted, 'Install'))

  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/review').length, 1)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 1)
  assert.doesNotMatch(mounted.container.textContent, /receipt-hidden-by-default/)
  assert.doesNotMatch(mounted.container.textContent, new RegExp(hash('e')))
  assert.equal(button(mounted, 'Confirm install'), undefined)
  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ one installed')
  await mounted.cleanup()
})

test('Desktop installs files, then asks permission before running declared setup', async () => {
  const command = 'printf done > ../setup-ran'
  const findings = Array.from({ length: 35 }, (_, index) => ({
    severity: 'warning', category: 'fixture', message: `warning-${index}`
  }))
  const files = Array.from({ length: 230 }, (_, index) => ({
    path: `references/file-${index}.md`, mode: 'file', hash: hash('a'), size: 8
  }))
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview({
      setup_commands: [command], files,
      local_scan: { allowed: true, summary: 'Reviewed warnings', findings }
    })
    if (path === '/install/confirm') return { installed: true, setup_required: true, ready_to_use: null }
    if (path === '/install/setup') return { installed: true, setup_required: true, ready_to_use: null,
      setup_execution_authorized: true, setup: { state: 'passed', steps: 1 } }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 0)
  assert.match(mounted.container.textContent, /files230/)
  assert.match(mounted.container.textContent, /printf done > \.\.\/setup-ran/)
  assert.match(mounted.container.textContent, /35 findings/)
  assert.doesNotMatch(mounted.container.textContent, /references\/file-229/)
  assert.doesNotMatch(mounted.container.textContent, /warning-34/)
  await click(reviewInstallButton(mounted))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 1)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/setup').length, 0)
  assert.match(mounted.container.textContent, /setup incomplete/i)
  assert.ok(button(mounted, 'Run setup'))
  await click(button(mounted, 'Run setup'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/setup').length, 1)
  assert.equal(loaded.calls.notify.some(item => item.message === '✅ one installed'), false)
  assert.match(mounted.container.textContent, /✅ one installedSetup ran successfully/)
  assert.match(mounted.container.textContent, /Have your agent confirm the skill is ready/i)
  assert.doesNotMatch(mounted.container.textContent, /setup incomplete|Check its status before trying again/i)
  await mounted.cleanup()
})

test('Desktop Not now leaves installed files visible as setup incomplete', async t => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview({ setup_commands: ['printf safe'] })
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', setup_required: true, ready_to_use: null }
    if (path === '/install/setup') throw new Error('setup must not run')
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  t.after(() => mounted.cleanup())
  await click(button(mounted, 'Install'))
  await click(reviewInstallButton(mounted))
  await click(button(mounted, 'Not now'))
  assert.equal(button(mounted, 'Run setup'), undefined)
  assert.match(mounted.container.textContent, /files installed; setup incomplete/i)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/setup').length, 0)
})

for (const contradiction of [{ pending: true }, { rejected: true }, { state: 'uncertain' }]) test(`desktop refuses contradictory install success ${JSON.stringify(contradiction)}`, { timeout: 5000 }, async t => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview({ setup_commands: ['printf safe'] })
    if (path === '/install/confirm') return { installed: true, skill_id: 'skill-1', ...contradiction }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  t.after(() => mounted.cleanup())
  await click(button(mounted, 'Install'))
  await click(reviewInstallButton(mounted))
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.match(mounted.container.textContent, /Install requires attention/)
  assert.equal(button(mounted, 'Run setup'), undefined)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/setup').length, 0)
})

for (const local of [false, true]) for (const updated of [true, false]) test(`desktop slash dialog handles ${updated ? 'completed' : 'pending'} checked updates (local=${local})`, async t => {
  const installedStatus = local
    ? { state: 'unmanaged_local', text: 'Local copy · published version unknown', installed_version: null, latest_version: 2 }
    : { state: 'installed', text: 'Installed v1 · Update available v2', installed_version: 1, latest_version: 2 }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', slug: 'one', latest_version: 2, installation_status: installedStatus }] }
    if (path === '/catalog/skill-1') return { id: 'skill-1', slug: 'one', latest_version: 2, installation_status: installedStatus }
    if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }] }
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/update/review') {
      assert.deepEqual(structuredClone(options.body), {
        view_id: 'view-remote-a-A', skill_id: 'skill-1', preserve_modified: true,
        accept_sensitive: false, accept_partial: false, accept_warnings: false
      })
      return local ? updateReview({
        from_version: null, local_baseline: 'unknown', sensitive_expansion: [],
        previous_installation: { version: null, target_provenance: { kind: 'local_source' } }
      }) : updateReview({ modified: false, sensitive_expansion: [], preservation: { action: 'none' } })
    }
    if (path === '/update/confirm') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', receipt: 'update-receipt' })
      return updated
        ? { updated: true, skill_id: 'skill-1', version: 2 }
        : { updated: false, pending: true, operation_id: 'pending-update', skill_id: 'skill-1' }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderComposerContribution(loaded)
  assert.equal(await submitComposer(loaded, { text: '/wisdom show skill-1' }), null)

  t.after(() => mounted.cleanup())
  assert.ok(mounted.container.textContent.includes(installedStatus.text))
  assert.equal(button(mounted, 'Install'), undefined)
  await click(button(mounted, 'Update'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/update/confirm').length, 1)
  if (!updated) {
    assert.match(mounted.container.textContent, /Update requires attention/)
    assert.doesNotMatch(mounted.container.textContent, /Install requires attention/)
    assert.match(mounted.container.textContent, /pending-update/)
    assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  }
  await mounted.cleanup()
})

test('Installed updates ask before running the new setup command', async () => {
  const installation = { skill_id: 'skill-1', name: 'one', version: 1, content_hash: hash('0'), target_path: '/managed/one' }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/bootstrap') return { ...bootstrap, installations: [installation] }
    if (path.startsWith('/updates/skill-1?')) return { skill_id: 'skill-1', state: 'update_available', latest_version: 2 }
    if (path === '/update/review') return updateReview({ modified: false, sensitive_expansion: [], preservation: { action: 'none' }, setup_commands: ['printf new-version'] })
    if (path === '/update/confirm') return { updated: true, skill_id: 'skill-1', version: 2, setup_required: true, ready_to_use: null }
    if (path === '/update/setup') return { updated: true, skill_id: 'skill-1', version: 2, setup_required: true,
      ready_to_use: null, setup_execution_authorized: true, setup: { state: 'passed', steps: 1 } }
    return baseHandler(path, options, owner)
  })
  const page = await renderPage(loaded)
  await click(button(page, 'Installed'))
  await click(button(page, 'Check for updates'))
  await click(button(page, 'Review update'))
  assert.match(page.container.textContent, /printf new-version/)
  await click(button(page, 'Acknowledge and update'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/update/setup').length, 0)
  assert.ok(button(page, 'Run setup'))
  assert.match(page.container.textContent, /files updated; setup incomplete/i)
  await click(button(page, 'Run setup'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/update/setup').length, 1)
  assert.match(page.container.textContent, /✅ one updatedSetup ran successfully/)
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  await page.cleanup()
})

for (const decision of ['Run setup', 'Not now']) test(`updated files require separate Desktop setup choice: ${decision}`, async () => {
  const installedStatus = { state: 'installed', text: 'Installed v1 · Update available v2', installed_version: 1, latest_version: 2 }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', slug: 'one', latest_version: 2, installation_status: installedStatus }] }
    if (path === '/catalog/skill-1') return { id: 'skill-1', slug: 'one', latest_version: 2, installation_status: installedStatus }
    if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }] }
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/update/review') return updateReview({ modified: false, sensitive_expansion: [], preservation: { action: 'none' }, setup_commands: ['verify new version'] })
    if (path === '/update/confirm') return { updated: true, skill_id: 'skill-1', version: 2, setup_required: true, ready_to_use: null }
    if (path === '/update/setup') return { updated: true, skill_id: 'skill-1', version: 2,
      setup_required: true, ready_to_use: null, setup_execution_authorized: true,
      setup: { state: 'passed', steps: 1 } }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderComposerContribution(loaded)
  assert.equal(await submitComposer(loaded, { text: '/wisdom show skill-1' }), null)
  await click(button(mounted, 'Update'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/update/confirm').length, 1)
  assert.match(mounted.container.textContent, /verify new version/)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/update/setup').length, 0)
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.match(mounted.container.textContent, /one.*files updated; setup incomplete/i)
  assert.ok(button(mounted, 'Run setup'))
  await click(button(mounted, decision))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/update/setup').length, decision === 'Run setup' ? 1 : 0)
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.match(mounted.container.textContent, decision === 'Run setup' ? /✅ one updatedSetup ran successfully/ : /files updated; setup incomplete/i)
  if (decision === 'Run setup') assert.match(mounted.container.textContent, /Have your agent confirm the skill is ready/i)
  await mounted.cleanup()
})

for (const contradiction of [{ pending: true }, { rejected: true }, { state: 'uncertain' }]) test(`Desktop does not offer update setup for ${JSON.stringify(contradiction)}`, async () => {
  const installedStatus = { state: 'installed', text: 'Installed v1 · Update available v2', installed_version: 1, latest_version: 2 }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', slug: 'one', latest_version: 2, installation_status: installedStatus }] }
    if (path === '/catalog/skill-1') return { id: 'skill-1', slug: 'one', latest_version: 2, installation_status: installedStatus }
    if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }] }
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/update/review') return updateReview({ modified: false, sensitive_expansion: [], preservation: { action: 'none' }, setup_commands: ['printf new-version'] })
    if (path === '/update/confirm') return { updated: true, skill_id: 'skill-1', setup_required: true, ...contradiction }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderComposerContribution(loaded)
  assert.equal(await submitComposer(loaded, { text: '/wisdom show skill-1' }), null)
  await click(button(mounted, 'Update'))
  assert.equal(button(mounted, 'Run setup'), undefined)
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  assert.equal(loaded.calls.rest.some(([path]) => path === '/update/setup'), false)
  await mounted.cleanup()
})

test('desktop slash dialog offers local updates and blocks unavailable installs', async () => {
  const skills = Array.from({ length: 7 }, (_, index) => ({
    id: `skill-${index + 1}`,
    name: `Helper ${index + 1}`,
    slug: `skill-${index + 1}`,
    latest_version: 1,
    installation_status: index === 5
      ? { state: 'unmanaged_local', text: 'Local copy · published version unknown', installed_version: null, latest_version: 1 }
      : index === 6 ? undefined : { state: 'not_installed', text: 'Not installed', installed_version: null, latest_version: 1 }
  }))
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path.startsWith('/catalog?')) return { skills }
    if (path === '/catalog/skill-6') return skills[5]
    if (path === '/catalog/skill-6/versions') return { versions: [{ version: 1, state: 'published' }] }
    if (path === '/catalog/skill-7') return skills[6]
    if (path === '/catalog/skill-7/versions') return { versions: [{ version: 1, state: 'published' }] }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderComposerContribution(loaded)
  assert.equal(await submitComposer(loaded, { text: '/wisdom' }), null)

  assert.ok(button(mounted, 'Previous').disabled)
  await click(button(mounted, 'Next'))
  assert.match(mounted.container.textContent, /Helper 6/)
  assert.doesNotMatch(mounted.container.textContent, /skill-6/)
  assert.equal(button(mounted, 'Next').disabled, true)
  assert.equal(button(mounted, 'Previous').disabled, false)
  await click(button(mounted, 'View'))
  assert.match(mounted.container.textContent, /Local copy · published version unknown/)
  assert.equal(button(mounted, 'Install'), undefined)
  assert.ok(button(mounted, 'Update'))
  await click(button(mounted, 'Back'))
  const unavailableRow = mounted.container.querySelector('[data-testid="catalog-skill-skill-7"]')
  await click([...unavailableRow.querySelectorAll('button')].find(item => item.textContent === 'View'))
  assert.match(mounted.container.textContent, /Installation status unavailable/)
  assert.equal(button(mounted, 'Install'), undefined)
  assert.equal(button(mounted, 'Update'), undefined)
  assert.equal(loaded.calls.rest.some(([path]) => path.includes('/install/')), false)
  await mounted.cleanup()
})

test('catalog page shows unavailable status without offering an install fallback', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ id: 'skill-1', slug: 'one', latest_version: 2 }] }
    if (path === '/catalog/skill-1') return { id: 'skill-1', slug: 'one', latest_version: 2 }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  assert.match(mounted.container.textContent, /Installation status unavailable/)
  assert.equal(button(mounted, 'Install'), undefined)
  await click(button(mounted, 'View'))
  assert.equal(button(mounted, 'Install v2'), undefined)
  assert.equal(loaded.calls.rest.some(([path]) => path.includes('/install/')), false)
  await mounted.cleanup()
})

test('composer middleware passes unrelated input through and only opens supported discovery commands', async () => {
  const loaded = await loadPlugin(baseHandler)
  const mounted = await renderComposerContribution(loaded)
  const ordinary = { text: 'hello' }
  const unrelatedSlash = { text: '/help' }
  const unsupportedWisdom = { text: '/wisdom status' }

  assert.equal(await submitComposer(loaded, ordinary), ordinary)
  assert.equal(await submitComposer(loaded, unrelatedSlash), unrelatedSlash)
  assert.equal(await submitComposer(loaded, unsupportedWisdom), unsupportedWisdom)
  assert.equal(mounted.container.querySelector('[role="dialog"]'), null)
  assert.equal(loaded.calls.rest.length, 0)
  await mounted.cleanup()
})

test('slash dialog refuses a focused owner the REST SDK cannot route without sending the command', async () => {
  const loaded = await loadPlugin(baseHandler, {
    focusedSessionOwner: { connectionId: 'remote-b', profile: 'B' }
  })
  const mounted = await renderComposerContribution(loaded)

  assert.equal(await submitComposer(loaded, { text: '/wisdom' }), null)
  await settle()
  assert.match(mounted.container.textContent, /cannot safely route/i)
  assert.match(mounted.container.textContent, /remote-b.*profile B/i)
  assert.equal(loaded.calls.rest.length, 0)
  await mounted.cleanup()
})

test('slash dialog invalidates on focus changes, ignores stale replies, and tears down cleanly', async () => {
  let resolveCatalog
  const delayedCatalog = new Promise(resolve => { resolveCatalog = resolve })
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path.startsWith('/catalog?')) return delayedCatalog
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderComposerContribution(loaded)
  assert.equal(await submitComposer(loaded, { text: '/wisdom' }), null)
  assert.ok(mounted.container.querySelector('[role="dialog"]'))
  await act(async () => loaded.focusedStoredSessionId.set('stored-2'))
  await settle()
  assert.equal(mounted.container.querySelector('[role="dialog"]'), null)
  resolveCatalog({ skills: [{ id: 'stale', slug: 'stale-result' }] })
  await settle()
  assert.doesNotMatch(mounted.container.textContent, /stale-result/)

  await act(async () => loaded.focusedStoredSessionId.set('stored-1'))
  assert.equal(await submitComposer(loaded, { text: '/wisdom' }), null)
  const dialog = mounted.container.querySelector('[role="dialog"]')
  assert.ok(dialog)
  await act(async () => dialog.dispatchEvent(new window.KeyboardEvent('keydown', { bubbles: true, key: 'Escape' })))
  await settle()
  assert.equal(mounted.container.querySelector('[role="dialog"]'), null)

  await act(async () => loaded.dispose())
  assert.equal((await submitComposer(loaded, { text: '/wisdom' })).text, '/wisdom')
  assert.equal(mounted.container.querySelector('[role="dialog"]'), null)
  await mounted.cleanup()
})

test('slash dialog uses theme-aware SDK classes in light and dark themes', async () => {
  const loaded = await loadPlugin(baseHandler)
  const mounted = await renderComposerContribution(loaded)
  for (const theme of ['light', 'dark']) {
    document.documentElement.dataset.theme = theme
    assert.equal(await submitComposer(loaded, { text: '/wisdom' }), null)
    await settle()
    const dialog = mounted.container.querySelector('[role="dialog"]')
    assert.ok(dialog)
    assert.match(dialog.innerHTML, /--ui-text-primary/)
    assert.match(dialog.innerHTML, /--ui-stroke-secondary/)
    assert.doesNotMatch(dialog.innerHTML, /#[0-9a-f]{3,8}|rgb\(|color:\s*(?:black|white)/i)
    await act(async () => dialog.dispatchEvent(new window.KeyboardEvent('keydown', { bubbles: true, key: 'Escape' })))
    await settle()
  }
  delete document.documentElement.dataset.theme
  await mounted.cleanup()
})

test('registers the native page and performs a checked one-click install', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', reference: 'skill-1@v2', accept_warnings: false })
      return installReview()
    }
    if (path === '/install/confirm') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A', receipt: 'install-receipt', accept_partial: false })
      return { installed: true, skill_id: 'skill-1', version: 2 }
    }
    return baseHandler(path, options, owner)
  })
  assert.equal(loaded.plugin.defaultEnabled, false)
  assert.deepEqual(loaded.calls.registrations.map(item => item.area), ['routes', 'sidebar.nav', 'palette', 'composer.top', 'composer.middleware'])
  const mounted = await renderPage(loaded)

  assert.match(mounted.container.textContent, /connection remote-a/)
  assert.match(mounted.container.textContent, /profile A/)
  await enter(mounted.container.querySelector('input[aria-label="Search shared skills"]'), 'useful')
  assert.ok(loaded.calls.rest.some(([path]) => path === '/catalog?q=useful'))
  await click(button(mounted, 'View'))
  assert.match(mounted.container.textContent, /Version history/)
  assert.match(mounted.container.textContent, /withdrawn/)

  await click(button(mounted, 'Install'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/review').length, 1)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 1)
  assert.equal(button(mounted, 'Confirm install'), undefined)
  assert.equal(loaded.calls.notify.at(-1)?.kind, 'success')
  assert.equal(loaded.calls.rest.some(([path]) => path.startsWith('/views/') && path !== '/views'), false, 'must not DELETE an old view through a newly routed ctx.rest')
  await mounted.cleanup()
})

test('catalog keeps native Install visible beside long descriptions at narrow widths', async () => {
  const longDescription = 'A very long publisher description '.repeat(80)
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) return { skills: [{ ...baseHandler(path, options, owner).skills[0], author_description: longDescription }] }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  mounted.container.style.width = '320px'
  const row = mounted.container.querySelector('[data-testid="catalog-skill-skill-1"]')
  const actions = row.querySelector('[data-testid="catalog-actions-skill-1"]')

  assert.ok(button(mounted, 'Install'))
  assert.ok(button(mounted, 'View'))
  assert.equal(row.style.gridTemplateColumns, 'minmax(0, 1fr) auto')
  assert.equal(actions.style.flexWrap, 'wrap')
  assert.equal(row.querySelector('p').style.overflowWrap, 'anywhere')
  await mounted.cleanup()
})

test('installed rows check first then expose exact Review update and local-edit protection', async () => {
  const managed = { ...bootstrap, installations: [{ skill_id: 'skill-1', slug: 'one', version: 1, modified: true, content_hash: hash('0') }] }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return managed
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/updates/skill-1?view_id=view-remote-a-A') return { skill_id: 'skill-1', state: 'update_available', installed_version: 1, latest_version: 2 }
    if (path === '/update/review') {
      assert.deepEqual(structuredClone(options.body), {
        view_id: 'view-remote-a-A', skill_id: 'skill-1', preserve_modified: true,
        accept_sensitive: false, accept_partial: false, accept_warnings: false
      })
      return updateReview({ sensitive_expansion: [] })
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Installed'))

  assert.match(mounted.container.textContent, /local edits detected · preservation required/)
  assert.equal(button(mounted, 'Review update'), undefined)
  await click(button(mounted, 'Check for updates'))
  assert.match(mounted.container.textContent, /v2 is available/)
  const reviewUpdate = button(mounted, 'Review update')
  assert.ok(reviewUpdate)
  const preserve = mounted.container.querySelector('[role="checkbox"][aria-label="Preserve locally modified files"]')
  await click(preserve)
  assert.equal(reviewUpdate.disabled, true)
  assert.match(mounted.container.textContent, /enable preservation to protect local edits/)
  await click(preserve)
  await click(button(mounted, 'Review update'))
  assert.match(mounted.container.textContent, /Update review/)
  assert.doesNotMatch(mounted.container.textContent, /update-receipt/)
  await mounted.cleanup()
})

test('installed rows state why update controls are unavailable', async () => {
  const managed = { ...bootstrap, installations: [{ skill_id: 'skill-1', slug: 'one', version: 1, modified: false }] }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return managed
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/updates/skill-1?view_id=view-remote-a-A') throw new Error('Update service is unavailable for this organization')
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Installed'))
  await click(button(mounted, 'Check for updates'))

  assert.match(mounted.container.textContent, /Update check unavailable: Update service is unavailable for this organization/)
  assert.equal(button(mounted, 'Review update'), undefined)
  await mounted.cleanup()
})

test('unknown required install review fields fail closed', async () => {
  const loaded = await loadPlugin((path, options, owner) => path === '/install/review'
    ? { receipt: 'looks-real', skill_id: 'skill-1', version: 2, allowed: true }
    : baseHandler(path, options, owner))
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.match(mounted.container.textContent, /confirmation blocked/)
  assert.match(mounted.container.textContent, /We could not check whether this skill will work here/)
  assert.equal(reviewInstallButton(mounted).disabled, true)
  await mounted.cleanup()
})

test('catalog version history selects an exact published version for install review', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/catalog/skill-1/versions') return { versions: [{ version: 2, state: 'published' }, { version: 1, state: 'published' }] }
    if (path === '/install/review') {
      assert.equal(options.body.reference, 'skill-1@v1')
      return installReview({ version: 1, receipt: 'install-v1' })
    }
    if (path === '/install/confirm') {
      assert.equal(options.body.receipt, 'install-v1')
      return { installed: true, skill_id: 'skill-1', version: 1 }
    }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'View'))
  await click(button(mounted, 'Install v1'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 1)
  await mounted.cleanup()
})

test('install collects exact warning and partial acknowledgements before confirmation', async () => {
  const finding = { severity: 'medium', category: 'network', path: 'SKILL.md', line: 7, message: 'Review host.' }
  let reviewCount = 0
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') {
      reviewCount += 1
      assert.equal(options.body.accept_warnings, reviewCount === 2)
      return installReview({
        receipt: reviewCount === 1 ? 'install-before-acks' : 'install-after-acks',
        compatibility: { outcome: 'compatible_after_setup', satisfied: [], setup_actions: ['Run a separate setup review'], limitations: ['Requires setup before use'], blocked: [] },
        local_scan: { allowed: reviewCount === 2, available: true, verdict: 'warning_required', findings: [finding] },
        policy: {
          requested_update_mode: null, requires_explicit_confirmation: true,
          scan_requires_confirmation: true,
          warning_acceptance: { accepted: reviewCount === 2 },
          compatibility_requires_confirmation: true
        },
        allowed: reviewCount === 2,
        permitted_actions: reviewCount === 2 ? ['confirm_install', 'recheck'] : ['recheck']
      })
    }
    if (path === '/install/confirm') {
      assert.deepEqual(structuredClone(options.body), {
        view_id: 'view-remote-a-A', receipt: 'install-after-acks', accept_partial: true
      })
      return { installed: true, skill_id: 'skill-1', version: 2 }
    }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.match(mounted.container.textContent, /Requires setup before use/)
  assert.match(mounted.container.textContent, /Review host/)
  assert.equal(reviewInstallButton(mounted).disabled, true)
  await click(mounted.container.querySelector('[role="checkbox"][aria-label="Acknowledge install scanner warnings"]'))
  await click(mounted.container.querySelector('[role="checkbox"][aria-label="Accept install partial compatibility"]'))
  await click(button(mounted, 'Continue review'))
  assert.doesNotMatch(mounted.container.textContent, /install-after-acks/)
  assert.equal(reviewInstallButton(mounted).disabled, false)
  await click(reviewInstallButton(mounted))
  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ one installed')
  await mounted.cleanup()
})

test('update re-review binds warning partial sensitive and preservation acknowledgements', async () => {
  const managed = { ...bootstrap, installations: [{ skill_id: 'skill-1', slug: 'one', version: 1, modified: true, content_hash: hash('0') }] }
  const finding = { severity: 'medium', category: 'network', path: 'SKILL.md', line: 9, message: 'Review update host.' }
  let reviews = 0
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return managed
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/updates/skill-1?view_id=view-remote-a-A') return { skill_id: 'skill-1', state: 'update_available', installed_version: 1, latest_version: 2 }
    if (path === '/update/review') {
      reviews += 1
      assert.equal(options.body.preserve_modified, true)
      assert.equal(options.body.accept_sensitive, reviews === 2)
      assert.equal(options.body.accept_partial, reviews === 2)
      assert.equal(options.body.accept_warnings, reviews === 2)
      return updateReview({
        receipt: reviews === 1 ? 'update-before-acks' : 'update-after-acks',
        compatibility: { outcome: 'partial', satisfied: [], setup_actions: [], limitations: ['Limited platform support'], blocked: [] },
        local_scan: { allowed: reviews === 2, available: true, findings: [finding] },
        policy: {
          requested_update_mode: 'MANUAL', requires_explicit_confirmation: true,
          scan_requires_confirmation: true,
          warning_acceptance: { accepted: reviews === 2 },
          compatibility_requires_confirmation: true, manual_only: true,
          accepted_sensitive: reviews === 2, accepted_partial: reviews === 2,
          accepted_warnings: reviews === 2, accepted_modified_preservation: true
        },
        allowed: reviews === 2,
        permitted_actions: reviews === 2 ? ['confirm_update', 'recheck'] : ['recheck']
      })
    }
    if (path === '/update/confirm') {
      assert.equal(options.body.receipt, 'update-after-acks')
      return { updated: true, skill_id: 'skill-1', version: 2 }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Installed'))
  const preserve = mounted.container.querySelector('[role="checkbox"][aria-label="Preserve locally modified files"]')
  assert.equal(preserve.getAttribute('aria-checked'), 'true')
  await click(button(mounted, 'Check for updates'))
  await click(button(mounted, 'Review update'))
  assert.match(mounted.container.textContent, /new network destinations/)
  assert.match(mounted.container.textContent, /Review update host/)
  assert.match(mounted.container.textContent, /edited copy will be kept separately/)
  assert.equal(button(mounted, 'Acknowledge and update').disabled, true)
  await click(mounted.container.querySelector('[role="checkbox"][aria-label="Acknowledge update scanner warnings"]'))
  await click(mounted.container.querySelector('[role="checkbox"][aria-label="Accept update partial compatibility"]'))
  await click(mounted.container.querySelector('[role="checkbox"][aria-label="Accept update sensitive capability expansion"]'))
  await click(button(mounted, 'Continue review'))
  assert.doesNotMatch(mounted.container.textContent, /update-after-acks/)
  assert.equal(button(mounted, 'Acknowledge and update').disabled, false)
  await click(button(mounted, 'Acknowledge and update'))
  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ one updated')
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
  assert.match(mounted.container.textContent, /Installed copy to remove/)
  assert.doesNotMatch(mounted.container.textContent, new RegExp(hash('0')))
  await click(button(mounted, 'Remove'))
  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ one removed')
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
  assert.match(mounted.container.textContent, /op-install-1/)
  assert.match(mounted.container.textContent, /Check its status before trying again/)
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  await mounted.cleanup()
})

test('desktop success notifications use the reviewed human name for completed removal', async () => {
  const installation = { skill_id: 'skill-1', slug: 'one', version: 1, content_hash: hash('0'), target_path: '/tmp/managed/one' }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/bootstrap') return { ...bootstrap, installations: [installation] }
    if (path === '/uninstall/review') return { review_id: 'uninstall-review', installation }
    if (path === '/uninstall/confirm') return { uninstalled: true, skill_id: 'skill-1' }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)

  await click(button(mounted, 'Installed'))
  await click(button(mounted, 'Uninstall'))
  await click(button(mounted, 'Remove'))

  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ one removed')
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

for (const [kind, diagnostic] of [
  ['packaging', 'Local package contains a blocked entry'],
  ['transport', 'The connection was interrupted']
]) {
  test(`${kind} preparation failure is accurate without a settings action or upload`, async () => {
    const loaded = await loadPlugin((path, options, owner) => {
      if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
      if (path === '/bootstrap') return bootstrap
      if (path.startsWith('/catalog?')) return { skills: [] }
      if (path === '/drafts/prepare') throw new Error(diagnostic)
      throw new Error(`unexpected ${path}`)
    })
    const mounted = await renderPage(loaded)
    await click(button(mounted, 'Contribute'))
    await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
    await click(button(mounted, 'Prepare draft'))
    const notification = loaded.calls.notify.at(-1)
    assert.equal(notification?.message, 'Share preparation failed — nothing was uploaded or shared')
    assert.equal(notification?.detail, diagnostic)
    assert.equal(notification?.action, undefined)
    assert.equal(loaded.calls.rest.some(([path]) => path.startsWith('/publication/')), false)
    await mounted.cleanup()
  })
}

test('publication inspects full bytes, renders checks/hashes/author copy, and reports moderated outcome accurately', async () => {
  const files = [
    { path: 'SKILL.md', content_utf8: '# Exact reviewed bytes' },
    { path: 'skill.manifest.json', content_utf8: '{"name":"local-skill"}' },
    { path: 'assets/logo.bin', content_base64: 'AAH/' }
  ]
  const hashes = { content: hash('3'), author_description: hash('4'), package_manifest: hash('5') }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:1', skill_id: 'local-skill', files: [{ path: 'SKILL.md' }] }
    if (path === '/drafts/local%3A1?view_id=view-remote-a-A') return { draft_id: 'local:1', author_description: 'Exact author copy for teammates.', files, expected_hashes: hashes }
    if (path === '/drafts/local%3A1/review') return { draft_id: 'local:1', drafted_description: 'Exact author copy for teammates.', files, hashes, local_scan: { allowed: true, findings: [] }, security_check: { upload_allowed: true, local_status: 'pass', findings: [] }, professionalism_check: { state: 'unavailable' } }
    if (path === '/publication/review') return {
      receipt: 'publication-receipt', draft_id: 'local:1', skill_id: 'local-skill', hashes,
      description: 'Exact author copy for teammates.',
      security_check: { upload_allowed: true, summary: 'Exact local security gate allows upload.', findings: [] },
      professionalism_check: { state: 'unavailable', summary: 'Optional model review unavailable.', findings: [] },
      local_scan: { allowed: true, summary: 'Host scan passed.', findings: [] },
      server_checks: { security_check: { status: 'advisory', verdict: 'advisory', summary: 'Gateway advisory.', findings: [{ severity: 'advisory', category: 'secret_assignment', path: 'SKILL.md', detail: 'Review Gateway finding.' }] } },
      warning_acceptance: { accepted: true },
      publication_mode: 'moderated', policy_version: 'policy-9', notice: 'Nothing has been uploaded.'
    }
    if (path === '/publication/confirm') return { operation_id: 'publication-op', state: 'done', publication_state: 'pending_moderation', remote_draft_id: 'remote-1' }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  assert.match(mounted.container.textContent, /Nothing has been uploaded/)
  await click(button(mounted, 'Continue to sharing review'))
  assert.match(mounted.container.textContent, /Exact author copy for teammates/)
  assert.match(mounted.container.textContent, /# Exact reviewed bytes/)
  assert.doesNotMatch(mounted.container.textContent, new RegExp(hash('3')))
  assert.match(mounted.container.textContent, /Local security: passed/)
  assert.match(mounted.container.textContent, /Gateway security scan/)
  assert.match(mounted.container.textContent, /Gateway: advisory/)
  assert.match(mounted.container.textContent, /Review Gateway finding/)
  assert.match(mounted.container.textContent, /Assessment: unavailable/)
  assert.match(mounted.container.textContent, /Moderated: confirmation submits for team moderation/)
  assert.equal(mounted.container.querySelector('a[download="logo.bin"]').getAttribute('href'), 'data:application/octet-stream;base64,AAH/')
  assert.equal(button(mounted, 'Share this exact reviewed package').disabled, false)
  await click(button(mounted, 'Share this exact reviewed package'))
  assert.equal(loaded.calls.notify.at(-1)?.message, 'local-skill submitted for team review')
  await mounted.cleanup()
})

for (const linked of [true, false]) {
  test(`publication compares hashes semantically and shows enrollment outcome (managed=${linked})`, async () => {
    const detail = 'Published successfully, but the original source is not linked to this version locally. Recover with: hermes wisdom publication enroll local-skill'
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
      if (path === '/drafts/local%3Aordered?view_id=view-remote-a-A') return {
        author_description: 'Exact author copy.', files, expected_hashes: expectedHashes
      }
      if (path === '/drafts/local%3Aordered/review') return { drafted_description: 'Exact author copy.', files, hashes: expectedHashes, local_scan: { allowed: true, findings: [] }, security_check: { upload_allowed: true, local_status: 'pass', findings: [] }, professionalism_check: { state: 'unavailable' } }
      if (path === '/publication/review') return {
        receipt: 'publication-receipt', draft_id: 'local:ordered', skill_id: 'local-skill',
        hashes: reviewedHashes, description: 'Exact author copy.', files,
        security_check: { upload_allowed: true, summary: 'Allowed.', findings: [] },
        professionalism_check: { state: 'unavailable', summary: 'Unavailable.', findings: [] },
        local_scan: { allowed: true, summary: 'Passed.', findings: [] },
        publication_mode: 'open', policy_version: 'policy-1'
      }
      if (path === '/publication/confirm') return {
        operation_id: 'publication-op', state: 'done', publication_state: 'published',
        managed_enrollment: { state: linked ? 'managed' : 'not_linked' },
        ...(linked ? {} : { detail })
      }
      throw new Error(`unexpected ${path}`)
    })
    const mounted = await renderPage(loaded)
    await click(button(mounted, 'Contribute'))
    await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
    await click(button(mounted, 'Prepare draft'))
    await click(button(mounted, 'Continue to sharing review'))

    assert.match(mounted.container.textContent, /ready to confirm/)
    assert.equal(button(mounted, 'Share this exact reviewed package').disabled, false)
    await click(button(mounted, 'Share this exact reviewed package'))
    if (linked) {
      assert.equal(loaded.calls.notify.at(-1)?.message, '✅ local-skill shared')
      assert.doesNotMatch(mounted.container.textContent, /Publication requires attention/)
    } else {
      assert.equal(loaded.calls.notify.length, 0)
      assert.match(mounted.container.textContent, /Publication requires attention for local-skill/)
      assert.ok(mounted.container.textContent.includes(detail))
      assert.doesNotMatch(mounted.container.textContent, /did not report a completed successful outcome/)
    }
    assert.equal(loaded.calls.rest.filter(([path]) => path === '/publication/confirm').length, 1)
    await mounted.cleanup()
  })
}

test('publication without an explicit security upload gate fails closed', async () => {
  const hashes = { content: hash('3'), author_description: hash('4'), package_manifest: hash('5') }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:1', skill_id: 'local-skill' }
    if (path === '/drafts/local%3A1?view_id=view-remote-a-A') return { author_description: 'Copy', files: [{ path: 'SKILL.md', content_utf8: '# bytes' }], expected_hashes: hashes }
    if (path === '/drafts/local%3A1/review') return { drafted_description: 'Copy', files: [{ path: 'SKILL.md', content_utf8: '# bytes' }], hashes, local_scan: { allowed: true, findings: [] }, security_check: { upload_allowed: true, local_status: 'pass', findings: [] }, professionalism_check: { state: 'unavailable' } }
    if (path === '/publication/review') return { receipt: 'publication-receipt', draft_id: 'local:1', skill_id: 'local-skill', hashes, description: 'Copy', files: [], security_check: { summary: 'Scanner unavailable.' }, professionalism_check: { state: 'unavailable' }, publication_mode: 'open', policy_version: 'p1' }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  await click(button(mounted, 'Continue to sharing review'))
  assert.match(mounted.container.textContent, /This skill is not cleared for sharing/)
  assert.equal(button(mounted, 'Share this exact reviewed package').disabled, true)
  await mounted.cleanup()
})

test('contribution review exposes complete files and warnings before a separate final Share', async () => {
  const longContent = `${'x'.repeat(20000)}LONG-FILE-END`
  const hashes = { content: hash('6'), author_description: hash('7'), package_manifest: hash('8') }
  const finding = { severity: 'medium', category: 'network', path: 'references/long.md', line: 42, message: 'Review this outbound host.' }
  const inspected = {
    draft_id: 'local:warning', skill_id: 'local-skill',
    author_description: 'Exact teammate description.', expected_hashes: hashes,
    files: [{ path: 'SKILL.md', content_utf8: '# Skill' }, { path: 'references/long.md', content_utf8: longContent }],
    local_scan: { allowed: false, verdict: 'warning_required', findings: [finding] },
    security_check: { local_status: 'warning_required', summary: 'Warnings require acknowledgement.', findings: [finding] }
  }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:warning', skill_id: 'local-skill' }
    if (path === '/drafts/local%3Awarning?view_id=view-remote-a-A') return inspected
    if (path === '/drafts/local%3Awarning/review') return { ...inspected, drafted_description: inspected.author_description, hashes, professionalism_check: { state: 'unavailable' } }
    if (path === '/publication/review') {
      assert.equal(options.body.accept_warnings, true)
      return {
        receipt: 'publication-warning-receipt', draft_id: 'local:warning', skill_id: 'local-skill', hashes,
        description: inspected.author_description,
        security_check: { upload_allowed: true, local_status: 'warning_accepted', summary: 'Exact warnings acknowledged.', findings: [finding] },
        professionalism_check: { state: 'unavailable', summary: 'Optional model review unavailable.', findings: [] },
        local_scan: inspected.local_scan, publication_mode: 'open', policy_version: 'policy-10'
      }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  assert.ok(button(mounted, 'Review and edit exact files'))
  assert.ok(button(mounted, 'Continue to sharing review'))
  assert.ok(button(mounted, 'Keep draft and exit'))

  await click(button(mounted, 'Review and edit exact files'))
  assert.match(mounted.container.textContent, /Review this outbound host/)
  assert.match(mounted.container.textContent, /medium · network · references\/long\.md:42/)
  assert.match(mounted.container.textContent, /references\/long\.md/)
  assert.match(mounted.container.textContent, /LONG-FILE-END/)
  const continueButton = button(mounted, 'Continue to sharing review')
  assert.equal(continueButton.disabled, true)
  const warningAck = mounted.container.querySelector('[role="checkbox"][aria-label="Acknowledge displayed scanner warnings for this exact package"]')
  await click(warningAck)
  assert.equal(continueButton.disabled, false)
  await click(continueButton)
  assert.ok(button(mounted, 'Share this exact reviewed package'))
  assert.ok(button(mounted, 'Keep draft; do not share'))
  assert.equal(loaded.calls.rest.some(([path]) => path === '/publication/confirm'), false)
  await click(button(mounted, 'Keep draft; do not share'))
  assert.match(mounted.container.textContent, /Draft kept/)
  assert.equal(loaded.calls.rest.some(([path]) => path === '/publication/confirm'), false)
  await mounted.cleanup()
})

test('changed scanner warning provenance requires a fresh acknowledgement', async t => {
  const hashes = { content: hash('6'), author_description: hash('7'), package_manifest: hash('8') }
  const initialFinding = { severity: 'advisory', category: 'network', path: 'SKILL.md', line: 4, detail: 'Review host A.' }
  const changedFinding = { ...initialFinding, detail: 'Review host B.' }
  const inspected = {
    draft_id: 'local:changed-warning', author_description: 'Copy', expected_hashes: hashes,
    files: [{ path: 'SKILL.md', content_utf8: '# Skill' }],
    local_scan: { allowed: false, findings: [initialFinding] },
    security_check: { local_status: 'warning_required', findings: [initialFinding] }
  }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:changed-warning', skill_id: 'local-skill' }
    if (path === '/drafts/local%3Achanged-warning?view_id=view-remote-a-A') return inspected
    if (path === '/drafts/local%3Achanged-warning/review') return { ...inspected, drafted_description: inspected.author_description, hashes, professionalism_check: { state: 'unavailable' } }
    if (path === '/publication/review') return {
      receipt: 'changed-warning-receipt', draft_id: inspected.draft_id, skill_id: 'local-skill', hashes,
      description: 'Copy', local_scan: { allowed: false, findings: [changedFinding] },
      security_check: { upload_allowed: true, local_status: 'warning_accepted', findings: [changedFinding] },
      professionalism_check: { state: 'unavailable' }, publication_mode: 'open', policy_version: 'p1'
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  t.after(() => mounted.cleanup())
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  await click(button(mounted, 'Review and edit exact files'))
  await click(mounted.container.querySelector('[role="checkbox"][aria-label="Acknowledge displayed scanner warnings for this exact package"]'))
  await click(button(mounted, 'Continue to sharing review'))

  assert.equal(mounted.container.querySelector('aside[aria-label="Exact action review"]'), null)
  assert.match(loaded.calls.notify.at(-1)?.detail || '', /scanner findings changed/i)
})

test('prepared editor saves and re-reviews exact add remove rename text and opaque files', async t => {
  const oldHashes = { content: hash('1'), author_description: hash('2'), package_manifest: hash('3') }
  const newHashes = { content: hash('4'), author_description: hash('5'), package_manifest: hash('6') }
  const inspected = {
    draft_id: 'local:edit', author_description: 'Before', expected_hashes: oldHashes,
    files: [
      { path: 'SKILL.md', content_utf8: '# Before\n' },
      { path: 'references/remove.md', content_utf8: 'remove me\n' },
      { path: 'assets/logo.bin', content_base64: 'AAH/' }
    ]
  }
  const reviewed = {
    draft_id: 'local:edit', drafted_description: 'After', hashes: newHashes,
    files: [
      { path: 'SKILL.md', mode: 'file', content_utf8: '# After\n' },
      { path: 'assets/renamed.bin', mode: 'file', content_base64: 'AAH/' },
      { path: 'references/new.md', mode: 'file', content_utf8: 'new file\n' }
    ],
    local_scan: { allowed: true, findings: [] },
    security_check: { upload_allowed: true, local_status: 'pass', findings: [] },
    professionalism_check: { state: 'unavailable', findings: [] }
  }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path.startsWith('/candidates?')) return { candidates: [{ event_id: 'candidate-1', skill_name: 'another-skill' }] }
    if (path === '/drafts/prepare') return { draft_id: 'local:edit', skill_id: 'local-skill' }
    if (path === '/drafts/local%3Aedit?view_id=view-remote-a-A') return inspected
    if (path === '/drafts/local%3Aedit/review') {
      const hashes = structuredClone(options.body.reviewed_hashes)
      assert.deepEqual(hashes, hashes.content === oldHashes.content ? oldHashes : newHashes)
      return hashes.content === oldHashes.content
        ? { ...reviewed, drafted_description: 'Before', hashes: oldHashes, files: inspected.files }
        : reviewed
    }
    if (path === '/drafts/local%3Aedit') {
      assert.equal(options.method, 'PUT')
      assert.equal(options.body.author_description, 'After')
      assert.deepEqual(structuredClone(options.body.expected_hashes), oldHashes)
      assert.deepEqual(structuredClone(options.body.files), [
        { path: 'SKILL.md', content_utf8: '# After\n' },
        { path: 'assets/renamed.bin', content_base64: 'AAH/' },
        { path: 'references/new.md', content_utf8: 'new file\n' }
      ])
      return reviewed
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  await click(button(mounted, 'Review and edit exact files'))

  t.after(() => mounted.cleanup())
  const opaqueExport = mounted.container.querySelector('a[download="logo.bin"]')
  assert.ok(opaqueExport)
  assert.equal(opaqueExport.getAttribute('href'), 'data:application/octet-stream;base64,AAH/')
  const draftExport = mounted.container.querySelector('a[download="wisdom-local-skill.json"]')
  const exportPayload = JSON.parse(decodeURIComponent(draftExport.getAttribute('href').split(',')[1]))
  assert.deepEqual(exportPayload, inspected)

  await enter(mounted.container.querySelector('textarea[aria-label="Author description"]'), 'Unsaved draft copy')
  assert.match(mounted.container.textContent, /Unsaved changes/)
  for (const label of ['Continue to sharing review', 'Keep draft and exit', 'Review and edit exact files', 'Prepare draft', 'Share']) {
    assert.equal(button(mounted, label).disabled, true, `${label} must not bypass unsaved edits`)
    await click(button(mounted, label))
  }
  assert.equal(loaded.calls.rest.some(([path]) => path === '/publication/review'), false)
  await click(button(mounted, 'Discard unsaved edits'))
  assert.equal(mounted.container.querySelector('textarea[aria-label="Author description"]').value, 'Before')
  assert.equal(button(mounted, 'Keep draft and exit').disabled, false)
  assert.doesNotMatch(mounted.container.textContent, /Unsaved changes/)

  await enter(mounted.container.querySelector('textarea[aria-label="Author description"]'), 'After')
  await enter(mounted.container.querySelector('textarea[aria-label="Content for SKILL.md"]'), '# After\n')
  await click(button(mounted, 'Remove references/remove.md'))
  await enter(mounted.container.querySelector('input[aria-label="Path for assets/logo.bin"]'), 'assets/renamed.bin')
  await click(button(mounted, 'Add text file'))
  await enter(mounted.container.querySelector('input[aria-label="Path for new file"]'), 'references/new.md')
  await enter(mounted.container.querySelector('textarea[aria-label="Content for new file"]'), 'new file\n')
  await click(button(mounted, 'Save and re-review'))

  assert.match(mounted.container.textContent, /Draft saved and re-reviewed/)
  assert.doesNotMatch(mounted.container.textContent, /Unsaved changes/)
  assert.equal(button(mounted, 'Continue to sharing review').disabled, false)
  assert.equal(button(mounted, 'Keep draft and exit').disabled, false)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/drafts/local%3Aedit').length, 1)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/drafts/local%3Aedit/review').length, 2)
  await mounted.cleanup()
})

test('hostile markup and control-like text stays inert in exact draft review', async () => {
  const hostile = '<img src=x onerror="globalThis.pwned=1"><script>globalThis.pwned=2</script>\u001b[31m'
  const hashes = { content: hash('a'), author_description: hash('b'), package_manifest: hash('c') }
  const inspected = {
    draft_id: 'local:hostile', author_description: hostile,
    files: [{ path: 'references/<hostile>.md', content_utf8: hostile }],
    expected_hashes: hashes
  }
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/drafts/prepare') return { draft_id: 'local:hostile', skill_id: 'local-skill' }
    if (path === '/drafts/local%3Ahostile?view_id=view-remote-a-A') return inspected
    if (path === '/drafts/local%3Ahostile/review') return {
      ...inspected, drafted_description: hostile, hashes,
      local_scan: { allowed: true, findings: [] },
      security_check: { upload_allowed: true, local_status: 'pass', findings: [] },
      professionalism_check: { state: 'unavailable' }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Contribute'))
  await enter(mounted.container.querySelector('input[aria-label="Local skill name"]'), 'local-skill')
  await click(button(mounted, 'Prepare draft'))
  await click(button(mounted, 'Review and edit exact files'))

  assert.match(mounted.container.textContent, /<img src=x onerror=/)
  assert.equal(mounted.container.querySelector('img'), null)
  assert.equal(mounted.container.querySelector('script'), null)
  assert.equal(globalThis.pwned, undefined)
  await mounted.cleanup()
})

test('Settings keeps simple notification controls without an Activity diagnostics tab', async t => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/settings?view_id=view-remote-a-A') return {
      delivery_mode: 'agent', mode_writable: true,
      mode_meanings: {
        agent: 'Eligible recommendations may be assessed and delivered.',
        fixed: 'Assessment and proactive delivery are off; manual review remains.'
      },
      gateway_status: 'available',
      gateway_mute: { org_id: 'org-1', muted: false, revision: 7 },
      mute_control: { org_id: 'org-1', muted: false, revision: 7 }
    }
    if (path === '/settings/mute') {
      assert.deepEqual(structuredClone(options.body), {
        view_id: 'view-remote-a-A', control_id: '7', duration: '1_week'
      })
      return { org_id: 'org-1', muted: true, duration: '1_week', muted_until: '2030-01-08T00:00:00Z', revision: 8 }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  t.after(mounted.cleanup)
  assert.equal(button(mounted, 'Activity'), undefined)
  assert.doesNotMatch(mounted.container.textContent, /Assessments|Unresolved operations/)
  await click(button(mounted, 'Settings'))
  assert.match(mounted.container.textContent, /Recommendations/)
  assert.ok(button(mounted, 'On'))
  assert.ok(button(mounted, 'Off'))
  assert.match(mounted.container.textContent, /for you, not your teammates/i)
  assert.doesNotMatch(mounted.container.textContent, /everyone|organization-wide/i)
  await click(button(mounted, 'Mute for 1 week'))
  assert.ok(loaded.calls.rest.some(([path]) => path === '/settings/mute'))
})

test('Contribute lists share candidates and persists Not now through the backend', async t => {
  let candidates = [
    { event_id: 'candidate-1', skill_name: 'incident-helper', qualification: 'high_usage', local_reasons: { consecutive_business_days: 5 } },
    { event_id: 'candidate-2', skill_name: 'release-helper', qualification: 'refinement', local_reasons: { meaningful_refinements: 3, stable_days: 14 } }
  ]
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views') return { view_id: `view-${owner.connectionId}-${owner.profile}` }
    if (path === '/bootstrap') return bootstrap
    if (path.startsWith('/catalog?')) return { skills: [] }
    if (path === '/candidates?view_id=view-remote-a-A') return { candidates }
    if (path === '/candidates/candidate-1/prepare') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A' })
      return { draft_id: 'local:candidate-1', skill_id: 'incident-helper' }
    }
    if (path === '/candidates/candidate-2/not-now') {
      assert.deepEqual(structuredClone(options.body), { view_id: 'view-remote-a-A' })
      candidates = candidates.filter(item => item.event_id !== 'candidate-2')
      return { deferred: true, event_id: 'candidate-2' }
    }
    throw new Error(`unexpected ${path}`)
  })
  const mounted = await renderPage(loaded)
  t.after(mounted.cleanup)
  await click(button(mounted, 'Contribute'))

  assert.match(mounted.container.textContent, /Worth sharing with your team/)
  assert.match(mounted.container.textContent, /incident-helper/)
  assert.match(mounted.container.textContent, /used on 5 consecutive business days/)
  const candidateRows = [...mounted.container.querySelectorAll('[data-testid^="share-candidate-"]')]
  await click([...candidateRows[0].querySelectorAll('button')].find(item => item.textContent === 'Share'))
  assert.match(mounted.container.textContent, /Private draft/)
  assert.match(mounted.container.textContent, /Nothing has been uploaded/)
  assert.equal(loaded.calls.rest.some(([path]) => path === '/publication/confirm'), false)

  await click([...candidateRows[1].querySelectorAll('button')].find(item => item.textContent === 'Not now'))
  assert.ok(loaded.calls.rest.some(([path]) => path === '/candidates/candidate-2/not-now'))
  assert.doesNotMatch(mounted.container.textContent, /release-helper/)
})

test('pending actions remain visible in their own view after reopening', async t => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/bootstrap') return { ...bootstrap, status: { ...bootstrap.status, pending_operations: [
      { operation_id: 'op-install', kind: 'install', stage: 'prepared', skill_id: 'opaque-install-id' },
      { operation_id: 'op-share', kind: 'publication', phase: 'draft_submission_unknown', skill_id: 'opaque-share-id' }
    ] } }
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  t.after(() => mounted.cleanup())
  await click(button(mounted, 'Installed'))
  assert.match(mounted.container.textContent, /Install requires attention for Name unavailable/)
  assert.doesNotMatch(mounted.container.textContent, /op-share/)
  assert.match(mounted.container.textContent, /Check its status before trying again/)
  await click(button(mounted, 'Contribute'))
  assert.match(mounted.container.textContent, /Publication requires attention for Name unavailable/)
  assert.doesNotMatch(mounted.container.textContent, /op-install/)
  assert.ok([...mounted.container.querySelectorAll('h3')].every(node => !node.textContent.includes('opaque-')))
  await click(button(mounted, 'Catalog'))
  await click(button(mounted, 'Installed'))
  assert.match(mounted.container.textContent, /Install requires attention/)
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
  assert.equal(loaded.calls.rest.some(([path]) => path === '/install/confirm'), false)
  const currentGeneration = mounted.container.textContent.match(/generation (\d+)/)[1]
  assert.ok(Number(currentGeneration) > Number(firstGeneration))
  assert.ok(loaded.calls.rest.filter(([path]) => path === '/views').length >= 3)
  await mounted.cleanup()
})

test('profile replacement clears an already-visible exceptional review', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview({
      receipt: `review-${owner.profile}`, allowed: false, permitted_actions: ['recheck']
    })
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.match(mounted.container.textContent, /confirmation blocked/)
  assert.doesNotMatch(mounted.container.textContent, /review-A/)

  await act(async () => loaded.profile.set('B'))
  await settle()

  assert.doesNotMatch(mounted.container.textContent, /confirmation blocked/)
  assert.match(mounted.container.textContent, /profile B/)
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

const transient = () => new Error('Error invoking remote method: 503: {"detail":"Collective Wisdom is temporarily unavailable."}')

test('page Retry refetches only failed bootstrap data, not a fresh view or effect', async () => {
  let failing = true
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/bootstrap' && failing) throw transient()
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  assert.match(mounted.container.textContent, /Collective Wisdom is temporarily unavailable\./)
  assert.doesNotMatch(mounted.container.textContent, /upstream credentials|Reload with a fresh review/)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/bootstrap').length, 1)
  failing = false
  await click(button(mounted, 'Retry'))
  assert.match(mounted.container.textContent, /Useful skill/)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/bootstrap').length, 2)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/views').length, 1)
  assert.equal(loaded.calls.rest.some(([, options]) => ['PATCH', 'PUT', 'DELETE'].includes(options.method) || options.method === 'POST' && options.body), false)
  await mounted.cleanup()
})

test('slash catalog Retry refetches read-only catalog and never invokes install', async () => {
  let failing = true
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?') && failing) throw transient()
    return baseHandler(path, options, owner)
  })
  const mounted = await renderComposerContribution(loaded)
  await submitComposer(loaded, { text: '/wisdom browse helper' })
  assert.match(mounted.container.textContent, /Collective Wisdom is temporarily unavailable\./)
  assert.equal(button(mounted, 'Install'), undefined)
  failing = false
  await click(button(mounted, 'Retry'))
  assert.match(mounted.container.textContent, /Useful skill/)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/catalog?q=helper').length, 2)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/views').length, 1)
  assert.equal(loaded.calls.rest.some(([path]) => path.includes('/install/')), false)
  await mounted.cleanup()
})

test('failed detail read hides approval and Retry only reads detail', async () => {
  let failing = true
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/catalog/skill-1' && failing) throw transient()
    return baseHandler(path, options, owner)
  })
  const mounted = await renderComposerContribution(loaded)
  await submitComposer(loaded, { text: '/wisdom show skill-1' })
  assert.equal(button(mounted, 'Install'), undefined)
  assert.match(mounted.container.textContent, /Collective Wisdom is temporarily unavailable\./)
  failing = false
  await click(button(mounted, 'Retry'))
  assert.ok(button(mounted, 'Install'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/catalog/skill-1').length, 2)
  assert.equal(loaded.calls.rest.some(([path]) => path.includes('/install/')), false)
  await mounted.cleanup()
})

test('transient effect failure never replays an Install confirmation or offers read Retry', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/install/review') return installReview()
    if (path === '/install/confirm') throw transient()
    return baseHandler(path, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Install'))
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/install/confirm').length, 1)
  assert.equal(button(mounted, 'Retry'), undefined)
  assert.equal(loaded.calls.notify.at(-1)?.message, 'Collective Wisdom is temporarily unavailable.')
  assert.equal(loaded.calls.notify.at(-1)?.detail, 'Collective Wisdom is temporarily unavailable.')
  assert.equal(loaded.calls.notify.some(item => item.kind === 'success'), false)
  await mounted.cleanup()
})

test('slash dialog retries failed view creation only while its origin remains current', async () => {
  let failing = true
  const loaded = await loadPlugin((path, options, owner) => {
    if (path === '/views' && failing) throw transient()
    return baseHandler(path, options, owner)
  })
  const mounted = await renderComposerContribution(loaded)
  await submitComposer(loaded, { text: '/wisdom' })
  assert.match(mounted.container.textContent, /Collective Wisdom is temporarily unavailable\./)
  failing = false
  await click(button(mounted, 'Retry'))
  assert.match(mounted.container.textContent, /Useful skill/)
  assert.equal(loaded.calls.rest.filter(([path]) => path === '/views').length, 2)
  assert.equal(loaded.calls.rest.some(([path]) => path.includes('/install/')), false)
  await mounted.cleanup()
})

test('auth failure in slash catalog is not treated as transient Retry', async () => {
  const loaded = await loadPlugin((path, options, owner) => {
    if (path.startsWith('/catalog?')) throw Object.assign(new Error('401 owner changed'), { status: 401 })
    return baseHandler(path, options, owner)
  })
  const mounted = await renderComposerContribution(loaded)
  await submitComposer(loaded, { text: '/wisdom' })
  assert.match(mounted.container.textContent, /401 owner changed/)
  assert.equal(button(mounted, 'Retry'), undefined)
  assert.equal(loaded.calls.rest.filter(([path]) => path.startsWith('/catalog?')).length, 1)
  await mounted.cleanup()
})

test('update-check Retry repeats only the owner-bound read, never update review or apply', async () => {
  let failing = true
  const installed = { skill_id: 'skill-1', name: 'one', version: 1 }
  const path = '/updates/skill-1?view_id=view-remote-a-A'
  const loaded = await loadPlugin((request, options, owner) => {
    if (request === '/bootstrap') return { ...bootstrap, installations: [installed] }
    if (request === path) {
      if (failing) throw transient()
      return { skill_id: 'skill-1', state: 'update_available', latest_version: 2 }
    }
    return baseHandler(request, options, owner)
  })
  const mounted = await renderPage(loaded)
  await click(button(mounted, 'Installed'))
  await click(button(mounted, 'Check for updates'))
  assert.match(mounted.container.textContent, /Collective Wisdom is temporarily unavailable\./)
  assert.equal(loaded.calls.rest.filter(([request]) => request === path).length, 1)
  failing = false
  await click(button(mounted, 'Retry'))
  assert.match(mounted.container.textContent, /v2 is available/)
  assert.equal(loaded.calls.rest.filter(([request]) => request === path).length, 2)
  assert.equal(loaded.calls.rest.some(([request]) => request.startsWith('/update/review') || request.startsWith('/update/confirm')), false)
  await mounted.cleanup()
})
