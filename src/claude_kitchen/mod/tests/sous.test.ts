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
  const clock = mock.clock(on)
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
  on('command.register', ($, e) => ({ value: { command: e.name } }) as never)
  on('fs.list', ($, e) => ({ value: children(e.path!) }))
  on('fs.exists', ($, e) => ({ value: files.has(e.path) || children(e.path).length > 0 }))
  on('fs.read', ($, e) => ({ value: files.get(e.path)! }))
  on('fs.write', ($, e) => (files.set(e.path, e.text), { value: undefined }))
  return clock
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
  const logged: string[] = []
  on('ui.log', ($, e) => (logged.push(e.text), { value: undefined }) as never)
  await start($)
  expect(logged).toEqual(['mod loaded'])
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
    expect((await band.find({ key: 'inbox' }))?.text).toBe('⚑ 1 decision pending · /inbox')
    await band.unmount()
  }

  const pane = await $.ui.mount({
    plugin: 'sous', surface: 'terminal', component: 'Pane', requestId: 'decisions',
    props: { title: 'Decisions', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20, contentRows: 0 }, view: {} } as never,
  })
  const recommended = await pane.find({ key: `${id}-0` })
  expect(recommended?.text).toBe('Ship (recommended)')
  expect(recommended?.props.autoFocus).toBe(true)
  expect((await pane.find({ key: `${id}-1` }))?.props.autoFocus).toBeUndefined()
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
  expect((await band.find({ key: 'inbox' }))?.text).toBe('⚑ 1 decision pending · /inbox')
  await band.unmount()

  await $.tool.call({ tool: 'mcp__sous__resolve_decision', id: 'abc' } as never)
  expect(JSON.parse(files.get(`${BASE}/decisions.json`)!)).toEqual([])
})

test('/inbox opens the decision pane holding the keyboard', async ($, on) => {
  mockKitchen(on, kitchenFiles())
  const opened: unknown[] = []
  on('ui.open', ($, e) => (opened.push(e), { value: { isPlaced: true } }) as never)
  await start($)
  await $.command.run({ command: 'inbox', args: '' } as never)
  expect(opened).toEqual([{ id: 'decisions', title: 'Decisions', focus: true, closeOnEscape: true }])
})

// A report reaches the conversation as a prompt row while the sous is idle,
// and as a queued_command attachment when it lands mid-turn.
const report = (cook: string, status: string, type: 'user' | 'attachment') => ({
  door: type === 'user' ? 'prompt' : 'delivery',
  origin: { kind: 'engine' },
  uuid: crypto.randomUUID(),
  message: {
    type,
    ...(type === 'attachment' ? { name: 'queued_command' } : { role: 'user' }),
    content: [{ type: 'text', text: `<channel source="kitchen" cook="${cook}" ts="t">\nreport\n\nSTATUS: ${status}\n</channel>` }],
  },
}) as never

test('a cook report toasts and marks the row red unless DONE, until the cook works again', async ($, on) => {
  const files = kitchenFiles()
  const clock = mockKitchen(on, files)
  const toasts: string[] = []
  on('ui.toast', ($, e) => (toasts.push(e.text), { value: undefined }) as never)
  // Nothing in a test stores a row: the mod acts on it before its next(e),
  // which then rejects for want of an engine beneath.
  const append = (row: never) =>
    $.session.append(row).catch((err: Error) => expect(err.message).toContain('no implementation for session.append'))
  const row = async () => {
    const band = await $.ui.mount({ plugin: 'sous', surface: 'terminal', ...BAND })
    const texts = (await band.findAll({ type: 'Text' })).map(t => t.text)
    await band.unmount()
    return texts
  }
  await start($)

  await append(report('rev', 'DONE', 'user'))
  await append(report('eng', 'NEEDS_CONTEXT', 'attachment'))
  await append(report('rev', 'BLOCKED', 'user'))
  await append({ ...report('qa', 'BLOCKED', 'user'), message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'STATUS: BLOCKED typed by a person' }] } } as never)
  expect(toasts).toEqual(['eng: NEEDS_CONTEXT', 'rev: BLOCKED'])
  expect(await row()).toEqual(['brigade', 'eng:NEEDS_CONTEXT 18%', 'rev:BLOCKED', 'widget-child/qa:idle'])

  await append(report('rev', 'DONE', 'user'))
  files.set(`${BASE}/cooks/eng.json`, JSON.stringify({ status: 'working', tokens: { input: 180000, max: 1000000 } }))
  await clock.advance(2000)
  expect(await row()).toEqual(['brigade', 'eng:working 18%', 'rev:idle', 'widget-child/qa:idle'])
})
