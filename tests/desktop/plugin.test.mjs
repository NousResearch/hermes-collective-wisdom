import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const PLUGIN_URL = new URL('../../desktop/plugin.js', import.meta.url)

function syntheticModule(context, values) {
  const module = new vm.SyntheticModule(
    Object.keys(values),
    function initialize() {
      for (const [name, value] of Object.entries(values)) {
        this.setExport(name, value)
      }
    },
    { context }
  )

  return module
}

async function loadPlugin({ clipboardResult = true, profile = 'default' } = {}) {
  const calls = {
    clipboard: [],
    navigate: [],
    notify: [],
    registrations: [],
    requests: [],
    rest: [],
    sockets: []
  }
  const context = vm.createContext({ console })
  const atom = value => ({ get: () => value })
  const host = {
    state: {
      gateway: atom('open'),
      profile: atom(profile)
    },
    navigate(path) {
      calls.navigate.push(path)
    },
    notify(payload) {
      calls.notify.push(payload)
    },
    request(...args) {
      calls.requests.push(args)
      throw new Error('unexpected gateway request')
    }
  }
  const sdk = {
    Badge: 'SDK.Badge',
    Button: 'SDK.Button',
    Codicon: 'SDK.Codicon',
    PALETTE_AREA: 'palette',
    ROUTES_AREA: 'routes',
    ScrollArea: 'SDK.ScrollArea',
    Separator: 'SDK.Separator',
    SIDEBAR_NAV_AREA: 'sidebar.nav',
    host,
    useValue: source => source.get()
  }
  const react = {
    useState(initial) {
      let value = typeof initial === 'function' ? initial() : initial
      return [value, next => {
        value = typeof next === 'function' ? next(value) : next
      }]
    }
  }
  const jsxRuntime = {
    jsx: (type, props, key) => ({ key, props: props ?? {}, type }),
    jsxs: (type, props, key) => ({ key, props: props ?? {}, type })
  }
  const source = await readFile(PLUGIN_URL, 'utf8')
  const pluginModule = new vm.SourceTextModule(source, {
    context,
    identifier: fileURLToPath(PLUGIN_URL)
  })

  await pluginModule.link(specifier => {
    if (specifier === '@hermes/plugin-sdk') return syntheticModule(context, sdk)
    if (specifier === 'react') return syntheticModule(context, react)
    if (specifier === 'react/jsx-runtime') return syntheticModule(context, jsxRuntime)
    throw new Error(`unsupported plugin import: ${specifier}`)
  })
  await pluginModule.evaluate()

  const ctx = {
    os: {
      async writeClipboard(command) {
        calls.clipboard.push(command)
        if (clipboardResult instanceof Error) throw clipboardResult
        return clipboardResult
      }
    },
    register(contribution) {
      calls.registrations.push(contribution)
      return () => undefined
    },
    registerMany(contributions) {
      calls.registrations.push(...contributions)
      return () => undefined
    },
    rest(...args) {
      calls.rest.push(args)
      throw new Error('unexpected plugin REST request')
    },
    socket(...args) {
      calls.sockets.push(args)
      return () => undefined
    }
  }
  const plugin = pluginModule.namespace.default
  plugin.register(ctx)

  return { calls, plugin }
}

function expand(node) {
  if (Array.isArray(node)) return node.map(expand)
  if (node == null || typeof node !== 'object') return node
  if (typeof node.type === 'function') return expand(node.type(node.props ?? {}))
  return {
    ...node,
    props: {
      ...node.props,
      children: expand(node.props?.children)
    }
  }
}

function walk(node, visit) {
  if (Array.isArray(node)) {
    node.forEach(child => walk(child, visit))
    return
  }
  if (node == null || typeof node !== 'object') return
  visit(node)
  walk(node.props?.children, visit)
}

function textContent(node) {
  if (Array.isArray(node)) return node.map(textContent).join(' ')
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node !== 'object') return String(node)
  return textContent(node.props?.children)
}

function renderPage(calls) {
  const route = calls.registrations.find(item => item.area === 'routes')
  assert.ok(route, 'route contribution should be registered')
  return expand(route.render())
}

function commandButtons(tree) {
  const buttons = []
  walk(tree, node => {
    if (node.type === 'SDK.Button' && node.props['data-command']) buttons.push(node)
  })
  return buttons
}

test('registers an opt-in Wisdom page with sidebar and palette navigation', async () => {
  const { calls, plugin } = await loadPlugin()

  assert.equal(plugin.id, 'hermes-collective-wisdom')
  assert.equal(plugin.defaultEnabled, false)
  assert.deepEqual(
    calls.registrations.map(item => item.area),
    ['routes', 'sidebar.nav', 'palette']
  )

  const route = calls.registrations[0]
  const nav = calls.registrations[1]
  const palette = calls.registrations[2]
  assert.equal(route.data.path, '/collective-wisdom')
  assert.equal(nav.data.codicon, 'library')
  assert.equal(nav.data.label, 'Wisdom')
  assert.equal(nav.data.path, '/collective-wisdom')
  palette.data.run()
  assert.deepEqual(calls.navigate, ['/collective-wisdom'])
})

test('renders bounded setup, browse, installed, review, and contribution guidance', async () => {
  const { calls } = await loadPlugin()
  const page = renderPage(calls)
  const text = textContent(page)

  for (const expected of [
    'Setup & status',
    'Browse & installed',
    'Review plans',
    'Contribution guidance',
    'Complete actions in a terminal',
    'hermes wisdom setup',
    'hermes wisdom status --json',
    'hermes wisdom browse --json',
    'hermes wisdom installed --json',
    "hermes wisdom install '<skill-id>@v1' --plan --json",
    'hermes wisdom review-week --dry-run --json',
    "hermes wisdom publication review '<local-draft-id>' --json",
    "hermes wisdom publication submit '<publication-receipt>' --json"
  ]) {
    assert.match(text, new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }

  assert.match(text, /hermes wisdom suggest/)
  assert.match(text, /hermes wisdom prepared save/)
  assert.match(text, /does not execute commands/i)
  assert.doesNotMatch(text, /button only reads status/i)
  assert.match(text, /run the copied command in your terminal to read status/i)
  assert.match(text, /select the command manually/i)
  const copyable = commandButtons(page).map(button => button.props['data-command'])
  assert.deepEqual(copyable, [
    'hermes wisdom status --json',
    'hermes wisdom browse --json',
    'hermes wisdom installed --json',
    'hermes wisdom inbox --json',
    "hermes wisdom install '<skill-id>@v1' --plan --json",
    "hermes wisdom update '<skill-id>' --plan --json",
    "hermes wisdom installed-setup inspect '<skill-id>' --json",
    'hermes wisdom review-week --dry-run --json',
    "hermes wisdom prepared inspect '<local-draft-id>' --json",
    "hermes wisdom publication review '<local-draft-id>' --json"
  ])
  assert.ok(copyable.every(command => !command.includes('--apply-receipt')))
  assert.ok(copyable.every(command => !command.includes('publication submit')))
  assert.ok(!copyable.includes('hermes wisdom setup'))
})

test('mounting and rendering performs no network, execution, clipboard, or navigation work', async () => {
  const { calls } = await loadPlugin()
  renderPage(calls)

  assert.deepEqual(calls.requests, [])
  assert.deepEqual(calls.rest, [])
  assert.deepEqual(calls.sockets, [])
  assert.deepEqual(calls.clipboard, [])
  assert.deepEqual(calls.navigate, [])
  assert.deepEqual(calls.notify, [])
})

test('a user click copies only the exact static command and reports true success', async () => {
  const { calls } = await loadPlugin()
  const page = renderPage(calls)
  const button = commandButtons(page).find(
    item => item.props['data-command'] === 'hermes wisdom status --json'
  )

  assert.ok(button)
  await button.props.onClick()
  assert.deepEqual(calls.clipboard, ['hermes wisdom status --json'])
  assert.equal(calls.notify.length, 1)
  assert.equal(calls.notify[0].kind, 'success')
  assert.equal(calls.notify[0].message, 'Command copied. Run it in the intended terminal.')
})

test('clipboard false reports failure and leaves manual-copy guidance visible', async () => {
  const { calls } = await loadPlugin({ clipboardResult: false })
  const page = renderPage(calls)
  const button = commandButtons(page)[0]

  await button.props.onClick()
  assert.equal(calls.notify.at(-1).kind, 'error')
  assert.match(calls.notify.at(-1).message, /select it manually/i)
  assert.match(textContent(page), /select the command manually/i)
})

test('clipboard rejection is contained and reports the same safe fallback', async () => {
  const { calls } = await loadPlugin({ clipboardResult: new Error('clipboard denied') })
  const page = renderPage(calls)
  const button = commandButtons(page)[0]

  await assert.doesNotReject(button.props.onClick())
  assert.equal(calls.notify.at(-1).kind, 'error')
  assert.match(calls.notify.at(-1).message, /select it manually/i)
})

test('display profile is never interpolated into a copied shell command', async () => {
  const hostileProfile = 'remote; touch /tmp/wisdom-unsafe'
  const { calls } = await loadPlugin({ profile: hostileProfile })
  const page = renderPage(calls)
  const text = textContent(page)

  assert.match(text, new RegExp(hostileProfile.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  assert.match(text, /Desktop display profile only/i)
  assert.match(text, /remote terminal/i)

  for (const button of commandButtons(page)) {
    await button.props.onClick()
  }
  assert.ok(calls.clipboard.length > 0)
  assert.ok(calls.clipboard.every(command => !command.includes(hostileProfile)))
})