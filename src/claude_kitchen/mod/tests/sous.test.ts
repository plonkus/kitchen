import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'

const ROOT = '/k'
const BASE = `${ROOT}/widget`

// An in-memory ~/.claude-kitchen: this kitchen with two cooks, a child
// kitchen with one, and an unrelated kitchen whose cook must not show.
const kitchenFiles = (): Map<string, string> => new Map([
  [`${BASE}/cooks/eng.json`, JSON.stringify({ status: 'working', tokens: { input: 180000, max: 1000000 } })],
  [`${BASE}/cooks/rev.json`, JSON.stringify({ status: 'idle' })],
  [`${BASE}/cooks/eng.send_keys.log`, 'noise'],
  [`${ROOT}/widget-child/kitchen.json`, JSON.stringify({ parent: 'widget' })],
  [`${ROOT}/widget-child/cooks/qa.json`, JSON.stringify({ status: 'idle' })],
  [`${ROOT}/other/kitchen.json`, JSON.stringify({})],
  [`${ROOT}/other/cooks/stray.json`, JSON.stringify({ status: 'working' })],
])

const mockKitchen = (on: On, files: Map<string, string>) => {
  mock.env(on, { STATUS_DIR: BASE })
  mock.clock(on)
  const children = (dir: string) => {
    const names = new Map<string, 'file' | 'directory'>()
    for (const path of files.keys()) {
      if (!path.startsWith(`${dir}/`)) continue
      const [first, ...rest] = path.slice(dir.length + 1).split('/')
      names.set(first!, rest.length ? 'directory' : 'file')
    }
    return [...names].map(([name, kind]) => ({ name, kind, size: 1, mtimeMs: 0, isLink: false }))
  }
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('tool.register', ($, e) => ({ value: { tool: `mcp__sous__${e.name}` } }) as never)
  on('fs.list', ($, e) => ({ value: children(e.path!) }))
  on('fs.exists', ($, e) => ({ value: files.has(e.path) || children(e.path).length > 0 }))
  on('fs.read', ($, e) => ({ value: files.get(e.path)! }))
  on('fs.write', ($, e) => (files.set(e.path, e.text), { value: undefined }))
}

const BAND = {
  component: 'AbovePrompt',
  props: {
    hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 160,
    scroll: { offset: 0, bodyRows: 10, contentRows: 0 }, view: {},
  },
} as never

const start = ($: Engine) =>
  $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

test('the brigade row lists this kitchen and its child kitchens, on terminal and desktop', async ($, on) => {
  mockKitchen(on, kitchenFiles())
  await start($)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'sous', surface, ...BAND })
    const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
    expect(texts).toEqual(['brigade', 'eng:working 18%', 'rev:idle', 'widget-child/qa:idle'])
    await ui.unmount()
  }
})

test('a filed decision shows in the band, persists, and its answer reaches the sous as a prompt', async ($, on) => {
  const files = kitchenFiles()
  mockKitchen(on, files)
  const submitted: string[] = []
  on('prompt.submit', ($, e) => (submitted.push(e.text), { text: e.text }))
  on('ui.open', () => ({ value: { isPlaced: true } }) as never)
  on('ui.close', () => ({ value: undefined }) as never)
  await start($)

  const filed = await $.tool.call({
    tool: 'mcp__sous__file_decision',
    question: 'Ship the fix today?',
    options: ['Ship', 'Wait'],
    recommendation: 'Ship',
  } as never)
  const id = /decision (\w+)/.exec(String((filed as { result: unknown }).result))![1]!
  expect(JSON.parse(files.get(`${BASE}/decisions.json`)!)).toEqual([
    { id, question: 'Ship the fix today?', options: ['Ship', 'Wait'], recommendation: 'Ship' },
  ])

  for (const surface of ['terminal', 'desktop'] as const) {
    const band = await $.ui.mount({ plugin: 'sous', surface, ...BAND })
    expect((await band.find({ key: 'inbox' }))?.text).toBe('⚑ 1 decision pending')
    await band.unmount()
  }

  const pane = await $.ui.mount({
    plugin: 'sous', surface: 'terminal', component: 'Pane', requestId: 'decisions',
    props: { title: 'Decisions', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20, contentRows: 0 }, view: {} } as never,
  })
  expect((await pane.find({ key: `${id}-0` }))?.text).toBe('Ship (recommended)')
  await pane.press({ key: `${id}-1` })
  expect(submitted).toEqual([`Head chef decided ${id} ("Ship the fix today?"): Wait`])
  expect(JSON.parse(files.get(`${BASE}/decisions.json`)!)).toEqual([])
})

test('a decision filed before a restart is pending after it, and the sous can resolve it', async ($, on) => {
  const files = kitchenFiles()
  files.set(`${BASE}/decisions.json`, JSON.stringify([{ id: 'abc', question: 'Q?', options: ['A', 'B'], recommendation: 'A' }]))
  mockKitchen(on, files)
  await start($)
  const band = await $.ui.mount({ plugin: 'sous', surface: 'terminal', ...BAND })
  expect((await band.find({ key: 'inbox' }))?.text).toBe('⚑ 1 decision pending')
  await band.unmount()

  await $.tool.call({ tool: 'mcp__sous__resolve_decision', id: 'abc' } as never)
  expect(JSON.parse(files.get(`${BASE}/decisions.json`)!)).toEqual([])
})

test('a cook report toasts unless its status is DONE', async ($, on) => {
  mockKitchen(on, kitchenFiles())
  const toasts: string[] = []
  on('ui.toast', ($, e) => (toasts.push(e.text), { value: undefined }) as never)
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await start($)
  const channel = { kind: 'channel', server: 'kitchen' }
  await $.prompt.submit({ text: 'did it\n\nSTATUS: DONE', origin: channel } as never)
  await $.prompt.submit({ text: 'which table?\n\nSTATUS: NEEDS_CONTEXT', origin: channel } as never)
  await $.prompt.submit({ text: 'STATUS: BLOCKED from a person', origin: { kind: 'composer' } } as never)
  expect(toasts).toEqual(['Cook report: NEEDS_CONTEXT'])
})
