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

test("Don't ask again calls the choice route and settles the card", async () => {
  const sent = []
  const loaded = await harness().loadPlugin(cardHandler({ choice: body => { sent.push(body.choice); return settled("Hermes won't suggest incident again.") } }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  await harness().click(buttons(mounted)[2])
  assert.deepEqual(sent, ['never'])
  assert.match(mounted.container.textContent, /Hermes won't suggest incident again\./)
  await mounted.cleanup()
  loaded.dispose()
})

// The in-card share review: the Contribute page's exact review routes, answered in the card.
const HASHES = { content: `sha256:${'3'.repeat(64)}`, author_description: `sha256:${'4'.repeat(64)}`, package_manifest: `sha256:${'5'.repeat(64)}` }
const FILES = [{ path: 'SKILL.md', content_utf8: '# incident' }, { path: 'skill.manifest.json', content_utf8: '{"name":"incident"}' }]
const HANDLING = { open: 'Shared with your team right away. The server scans it again before publishing.', moderated: 'A team reviewer approves it before it is available.', managed: "Your team's policy decides how it is published." }
const confirmed = { state: 'confirmed', draft_id: 'local:d1', skill: 'incident', handling: HANDLING, title: 'Hermes Collective Wisdom', summary: 'incident: you chose Share.', items: [], actions: [], notice: '' }
const cleanScan = { local_scan: { allowed: true, findings: [] }, security_check: { upload_allowed: true, local_status: 'pass', findings: [] } }
const warningScan = { local_scan: { allowed: false, guard: { allowed: false, findings: [{ severity: 'high', category: 'network', path: 'SKILL.md', message: 'curl with a token' }] } }, security_check: { upload_allowed: true, local_status: 'warning_required', findings: [] } }

function shareHandler({ scan = cleanScan, confirm, choice, read, publication = {} } = {}) {
  const views = []
  const handler = cardHandler({
    read: path => (read ? read(path) : confirmed),
    choice: body => (choice ? choice(body) : confirmed)
  })
  return Object.assign((path, options, owner) => {
    if (path === '/views') { views.push(views.length); return { view_id: `view-${views.length}` } }
    if (path.startsWith('/drafts/local%3Ad1?view_id=')) return { draft_id: 'local:d1', author_description: 'Review incident notes.', files: FILES, expected_hashes: HASHES }
    if (path === '/drafts/local%3Ad1/review') return { draft_id: 'local:d1', files: FILES, hashes: HASHES, ...scan }
    if (path === '/publication/review') return {
      receipt: 'pub-receipt', draft_id: 'local:d1', slug: 'incident', skill_name: 'incident', hashes: HASHES,
      description: 'Review incident notes.', files: FILES, professionalism_check: { state: 'unavailable' },
      publication_mode: 'open', policy_version: 'p1', ...cleanScan, ...publication
    }
    if (path === '/publication/confirm') return confirm(options.body)
    return handler(path, options, owner)
  }, { views })
}

const restPaths = loaded => loaded.calls.rest.map(([path]) => path)

test('Share runs the share review and publishes inside the card without leaving the chat', async () => {
  let shared = false
  const confirms = []
  const loaded = await harness().loadPlugin(shareHandler({
    read: () => (shared ? { ...settled('incident'), title: 'Hermes Collective Wisdom', items: [{ title: 'Published', detail: 'Available to your team.' }] } : pendingCard),
    choice: body => { assert.equal(body.choice, 'share'); return confirmed },
    confirm: body => { confirms.push(body); shared = true; return { state: 'done', publication_state: 'published', skill_name: 'incident', managed_enrollment: { state: 'managed' } } }
  }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  await harness().click(buttons(mounted).find(item => item.textContent === 'Share'))
  const card = mounted.container.querySelector('[data-testid="wisdom-share-card"]')
  assert.match(card.textContent, /📤 Share skill/)
  assert.match(card.textContent, /Review incident notes\./)
  assert.match(card.textContent, /Skillincident/)
  assert.match(card.textContent, /Files2 files/)
  assert.match(card.textContent, /Checks🔒 Passed/)
  assert.match(card.textContent, /Shared with your team right away/)
  assert.deepEqual(buttons(mounted).map(item => [item.textContent, item.dataset.variant]), [['Cancel', 'secondary'], ['Share', 'default']])
  assert.ok(restPaths(loaded).includes('/publication/review'))
  assert.equal(restPaths(loaded).includes('/publication/confirm'), false, 'nothing is shared before the second Share')
  await harness().click(buttons(mounted).find(item => item.textContent === 'Share'))
  assert.equal(confirms.length, 1)
  assert.equal(confirms[0].receipt, 'pub-receipt')
  const reviewView = loaded.calls.rest.find(([path]) => path === '/publication/review')[1].body.view_id
  assert.equal(confirms[0].view_id, reviewView, 'confirmed through the view that froze the receipt')
  assert.equal(loaded.calls.notify.at(-1)?.message, '✅ incident shared')
  assert.match(mounted.container.textContent, /Published/)
  assert.match(mounted.container.textContent, /Available to your team\./)
  assert.equal(buttons(mounted).length, 0)
  assert.deepEqual(loaded.calls.navigate, [])
  await mounted.cleanup()
  loaded.dispose()
})

test('scanner warnings show one line and one button that opens the findings page', async () => {
  const loaded = await harness().loadPlugin(shareHandler({ scan: warningScan, confirm: () => assert.fail('no publish') }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  assert.match(mounted.container.textContent, /This skill needs a security review before it is shared\./)
  assert.deepEqual(buttons(mounted).map(item => item.textContent), ['View and acknowledge findings'])
  assert.doesNotMatch(mounted.container.textContent, /curl with a token/)
  assert.equal(restPaths(loaded).includes('/publication/review'), false)
  assert.deepEqual(loaded.calls.navigate, [])
  await harness().click(buttons(mounted)[0])
  assert.deepEqual(loaded.calls.navigate, ['/collective-wisdom'])
  await mounted.cleanup()
  loaded.dispose()
})

test('a Gateway review that needs acknowledgement after reload also opens the findings page', async () => {
  const loaded = await harness().loadPlugin(shareHandler({ read: () => ({ state: 'review_required', draft_id: 'local:d1', skill: 'incident', title: 'Hermes Collective Wisdom', summary: 'incident', items: [{ title: 'Security review needed', detail: 'This skill needs a security review before it is shared.' }], actions: [], notice: '' }) }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  assert.deepEqual(buttons(mounted).map(item => item.textContent), ['View and acknowledge findings'])
  await harness().click(buttons(mounted)[0])
  assert.deepEqual(loaded.calls.navigate, ['/collective-wisdom'])
  await mounted.cleanup()
  loaded.dispose()
})

test('Cancel keeps the private draft and settles to Draft kept', async () => {
  const sent = []
  const kept = { ...settled('incident'), title: 'Hermes Collective Wisdom', items: [{ title: 'Draft kept', detail: 'This skill is still a local draft. Nothing was shared. Resume it from Contribute in Collective Wisdom.' }] }
  const loaded = await harness().loadPlugin(shareHandler({ choice: body => { sent.push(body.choice); return kept }, confirm: () => assert.fail('no publish') }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  await harness().click(buttons(mounted).find(item => item.textContent === 'Cancel'))
  assert.deepEqual(sent, ['keep'])
  assert.match(mounted.container.textContent, /Draft kept/)
  assert.match(mounted.container.textContent, /Nothing was shared/)
  assert.equal(buttons(mounted).length, 0)
  assert.deepEqual(loaded.calls.navigate, [])
  await mounted.cleanup()
  loaded.dispose()
})

test('a refused preparation stays in the card with Retry', async () => {
  const attention = { state: 'attention', title: 'Skill needs attention', summary: 'incident', items: [{ title: 'Not shared', detail: "SKILL.md: the skill's folder holds a file Gateway refuses." }], actions: [{ label: 'Retry', choice: 'share', primary: true, link: false }], notice: "Nothing has been uploaded. Fix this in the skill's folder, then press Retry." }
  const sent = []
  const loaded = await harness().loadPlugin(cardHandler({ choice: body => { sent.push(body.choice); return attention } }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  await harness().click(buttons(mounted).find(item => item.textContent === 'Share'))
  assert.match(mounted.container.textContent, /Skill needs attention/)
  assert.match(mounted.container.textContent, /Gateway refuses/)
  assert.deepEqual(buttons(mounted).map(item => item.textContent), ['Retry'])
  await harness().click(buttons(mounted)[0])
  assert.deepEqual(sent, ['share', 'share'])
  assert.deepEqual(loaded.calls.navigate, [])
  await mounted.cleanup()
  loaded.dispose()
})

test('a failed answer says so in the card and keeps it answerable', async () => {
  const loaded = await harness().loadPlugin(cardHandler({ choice: () => { throw Object.assign(new Error('409 conflict'), { status: 409 }) } }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  await harness().click(buttons(mounted).find(item => item.textContent === 'Share'))
  assert.match(mounted.container.textContent, /Couldn't complete that\. Please try again\./)
  assert.ok(buttons(mounted).some(item => item.textContent === 'Share'))
  assert.deepEqual(loaded.calls.navigate, [])
  await mounted.cleanup()
  loaded.dispose()
})

test('a reloaded chat restores the in-card review of a confirmed draft', async () => {
  const loaded = await harness().loadPlugin(shareHandler({ confirm: () => assert.fail('no publish') }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  assert.match(mounted.container.textContent, /📤 Share skill/)
  assert.deepEqual(buttons(mounted).map(item => item.textContent), ['Cancel', 'Share'])
  assert.equal(restPaths(loaded).some(path => path === `/suggestions/${ID}/choice`), false)
  await mounted.cleanup()
  loaded.dispose()
})

test('a review the card cannot confirm names why and offers no Share', async () => {
  const loaded = await harness().loadPlugin(shareHandler({ publication: { security_check: { upload_allowed: false } }, confirm: () => assert.fail('no publish') }), withArea)
  const mounted = await mountDirective(loaded, { attrs: { id: ID }, sessionId: 'chat-7', streaming: false, source: '' })
  assert.match(mounted.container.textContent, /This skill is not cleared for sharing\./)
  assert.match(mounted.container.textContent, /Not ready to share/)
  assert.equal(buttons(mounted).find(item => item.textContent === 'Share').disabled, true)
  await mounted.cleanup()
  loaded.dispose()
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
