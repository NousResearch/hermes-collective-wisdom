import assert from 'node:assert/strict'
import test from 'node:test'

// Loaded after plugin.test.mjs shares its harness through globalThis.__wisdomHarness.
const harness = () => globalThis.__wisdomHarness

const pendingCard = {
  state: 'pending', title: '📤 Share this skill?', summary: 'Hermes thinks incident could help your team.',
  items: [{ title: 'incident', detail: "Review incident notes.\nHermes's reason: Teams need it." }],
  prompt: 'Share it with your team?',
  actions: [{ label: 'Not now', choice: 'dismiss', primary: false, link: false }, { label: 'Share', choice: 'share', primary: true, link: false }, { label: "Don't ask again for this skill", choice: 'never', primary: false, link: true }],
  notice: ''
}
const settled = summary => ({ state: 'settled', title: 'Not shared', summary, items: [], actions: [], notice: '' })
const ID = 'sugg-abcdef123'

function cardHandler(overrides = {}) {
  return (path, options, owner) => {
    if (path === '/suggestions/desktop-card') return { ready: true }
    if (path.startsWith(`/suggestions/${ID}?`)) {
      if (overrides.read) return overrides.read(path)
      return pendingCard
    }
    if (path === `/suggestions/${ID}/choice`) return overrides.choice(JSON.parse(JSON.stringify(options.body)))
    return harness().baseHandler(path, options, owner)
  }
}

async function mountDirective(loaded, props) {
  const { React, QueryClient, QueryClientProvider, createRoot, act, settle } = harness()
  const contribution = loaded.calls.registrations.find(item => item.area === 'transcript.directives')
  assert.ok(contribution, 'directive should be registered')
  const container = document.createElement('div')
  document.body.append(container)
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false }, mutations: { gcTime: 0, retry: false } } })
  const root = createRoot(container)
  const render = next => act(async () => { root.render(React.createElement(QueryClientProvider, { client }, contribution.data.render(next))) })
  await render(props)
  await settle()
  return { container, render: async next => { await render(next); await settle() }, cleanup: async () => { await act(async () => root.unmount()); container.remove() } }
}

const withArea = { sdk: { TRANSCRIPT_DIRECTIVE_AREA: 'transcript.directives' } }
const buttons = mounted => [...mounted.container.querySelectorAll('button')]

test('share directive registers and checks in only when the SDK has the area', async () => {
  const stock = await harness().loadPlugin(cardHandler())
  assert.equal(stock.calls.registrations.some(item => item.area === 'transcript.directives'), false)
  assert.equal(stock.calls.rest.some(([path]) => path === '/suggestions/desktop-card'), false)
  stock.dispose()
  const loaded = await harness().loadPlugin(cardHandler(), withArea)
  const contribution = loaded.calls.registrations.find(item => item.area === 'transcript.directives')
  assert.equal(contribution.data.name, 'wisdom-share')
  assert.ok(loaded.calls.rest.some(([path, options]) => path === '/suggestions/desktop-card' && options.method === 'POST'))
  loaded.dispose()
})

test('pending card asks the question with Not now, Share and a right-aligned link', async () => {
  const loaded = await harness().loadPlugin(cardHandler(), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  const card = mounted.container.querySelector('[data-testid="wisdom-share-card"]')
  const question = [...card.querySelectorAll('p')].find(item => item.textContent === 'Share it with your team?')
  assert.ok(question, 'question line is shown')
  assert.doesNotMatch(card.textContent, /Nothing is shared until/)
  const [row, linkRow] = [...card.querySelectorAll('div.justify-end')]
  assert.deepEqual([...row.querySelectorAll('button')].map(item => item.textContent), ['Not now', 'Share'])
  assert.deepEqual([...linkRow.querySelectorAll('button')].map(item => [item.textContent, item.dataset.variant]), [["Don't ask again for this skill", 'link']])
  assert.ok(question.compareDocumentPosition(row) & 4, 'question comes before the buttons')
  await mounted.cleanup()
  loaded.dispose()
})

test('pending card renders the messaging copy and three actions bound to the owning chat', async () => {
  const choices = []
  const loaded = await harness().loadPlugin(cardHandler({ choice: body => { choices.push(body); return settled("Hermes won't suggest this version of incident again.") } }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  assert.match(mounted.container.textContent, /📤 Share this skill\?/)
  assert.match(mounted.container.textContent, /Hermes's reason: Teams need it\./)
  assert.deepEqual(buttons(mounted).map(item => [item.textContent, item.dataset.variant]), [
    ['Not now', 'secondary'], ['Share', 'default'], ["Don't ask again for this skill", 'link']])
  const read = loaded.calls.rest.find(([path]) => path.startsWith(`/suggestions/${ID}?`))[0]
  assert.match(read, /session_id=chat-7/)
  await harness().click(buttons(mounted)[0])
  assert.equal(choices.length, 1)
  assert.equal(choices[0].choice, 'dismiss')
  assert.equal(choices[0].session_id, 'chat-7')
  assert.match(mounted.container.textContent, /Not shared/)
  assert.equal(buttons(mounted).length, 0)
  await mounted.cleanup()
  loaded.dispose()
})

test('Never and Confirm share call the choice route; Confirm opens the draft review page', async () => {
  for (const [index, choice] of [[2, 'never'], [1, 'share']]) {
    const sent = []
    const loaded = await harness().loadPlugin(cardHandler({ choice: body => {
      sent.push(body.choice)
      return choice === 'share'
        ? { state: 'confirmed', draft_id: 'local:d1', skill: 'incident', title: 'Hermes Collective Wisdom', summary: 'incident: you chose Confirm share.', items: [], actions: [], notice: '' }
        : settled("Hermes won't suggest incident again.")
    } }), withArea)
    const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
    await harness().click(buttons(mounted)[index])
    assert.deepEqual(sent, [choice])
    if (choice === 'share') {
      assert.deepEqual(loaded.calls.navigate, ['/collective-wisdom'])
      assert.ok(buttons(mounted).some(item => item.textContent === 'Open share review'))
    }
    await mounted.cleanup()
    loaded.dispose()
  }
})

test('streaming, malformed attrs and missing chats render nothing and call nothing', async () => {
  const loaded = await harness().loadPlugin(cardHandler(), withArea)
  for (const props of [
    { attrs: { id: ID }, sessionId: 'chat-7', streaming: true },
    { attrs: { id: '../../views' }, sessionId: 'chat-7', streaming: false },
    { attrs: {}, sessionId: 'chat-7', streaming: false },
    { attrs: { id: ID }, sessionId: null, streaming: false }
  ]) {
    const mounted = await mountDirective(loaded, props)
    assert.equal(mounted.container.textContent, '')
    await mounted.cleanup()
  }
  assert.equal(loaded.calls.rest.some(([path]) => path.startsWith('/suggestions/') && path !== '/suggestions/desktop-card'), false)
  loaded.dispose()
})

test('foreign-session or expired card shows the stale-control line; settled cards have no actions', async () => {
  const loaded = await harness().loadPlugin(cardHandler({ read: () => { throw Object.assign(new Error('404 not found'), { status: 404 }) } }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'other-chat', streaming: false })
  assert.match(mounted.container.textContent, /This control is no longer available\./)
  await mounted.cleanup()
  loaded.dispose()
  const done = await harness().loadPlugin(cardHandler({ read: () => ({ ...settled('Suggestion for incident closed.'), title: 'Hermes Collective Wisdom' }) }), withArea)
  const card = await mountDirective(done, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false })
  assert.match(card.container.textContent, /Suggestion for incident closed\./)
  assert.equal(buttons(card).length, 0)
  await card.cleanup()
  done.dispose()
})
