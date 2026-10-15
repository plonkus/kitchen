import { atom, read, update } from 'claude-code'
import type { Hook, Register } from 'claude-code'

import type { Cook, Decision } from '../types'

type Engine = Parameters<Hook<'session.start'>>[0]

const cooks = atom({ plugin: 'sous', key: 'cooks' } as const, [])
const decisions = atom({ plugin: 'sous', key: 'decisions' } as const, [])
// The last non-DONE report status per cook, held until the cook works again.
const alerts = atom({ plugin: 'sous', key: 'alerts' } as const, {})
const PANE = 'decisions'

// STATUS_DIR is the kitchen's state dir. Decisions live at its top level,
// not in notes/, which `kitchen close` wipes.
const stateDir = async ($: Engine) => {
  const dir = await $.env.get('STATUS_DIR')
  if (!dir) throw new Error('STATUS_DIR is unset: launch the sous with `kitchen open`')
  return dir
}

const ctxOf = (tokens?: { input?: number; max?: number }) =>
  tokens?.input && tokens.max ? ` ${Math.round((100 * tokens.input) / tokens.max)}%` : ''

const readCooks = async ($: Engine, dir: string, prefix: string): Promise<Cook[]> => {
  const files = (await $.fs.list(dir)).filter(f => f.name.endsWith('.json'))
  return Promise.all(files.map(async f => {
    const d = JSON.parse(await $.fs.read(`${dir}/${f.name}`))
    return { name: prefix + f.name.slice(0, -5), status: d.status, ctx: ctxOf(d.tokens) }
  }))
}

// This kitchen's cooks, then the cooks of every child kitchen whose
// kitchen.json names this one as its parent.
const pollCooks = async ($: Engine) => {
  const base = await stateDir($)
  const root = base.slice(0, base.lastIndexOf('/'))
  const name = base.slice(base.lastIndexOf('/') + 1)
  const list = (await $.fs.exists(`${base}/cooks`)) ? await readCooks($, `${base}/cooks`, '') : []
  for (const kitchen of await $.fs.list(root)) {
    const kj = `${root}/${kitchen.name}/kitchen.json`
    if (kitchen.kind !== 'directory' || !(await $.fs.exists(kj))) continue
    if (JSON.parse(await $.fs.read(kj)).parent !== name) continue
    if (await $.fs.exists(`${root}/${kitchen.name}/cooks`)) {
      list.push(...(await readCooks($, `${root}/${kitchen.name}/cooks`, `${kitchen.name}/`)))
    }
  }
  await update($, cooks, () => list)
  const working = list.filter(c => c.status === 'working').map(c => c.name)
  const held = await read($, alerts)
  if (working.some(name => name in held)) {
    await update($, alerts, current => Object.fromEntries(Object.entries(current).filter(([name]) => !working.includes(name))))
  }
}

const saveDecisions = async ($: Engine, change: (list: Decision[]) => Decision[]) => {
  let saved: Decision[] = []
  await update($, decisions, list => (saved = change(list)))
  await $.fs.write(`${await stateDir($)}/decisions.json`, JSON.stringify(saved, null, 2) + '\n')
}

const answer = async ($: Engine, d: Decision, choice: string) => {
  await saveDecisions($, list => list.filter(one => one.id !== d.id))
  if ((await read($, decisions)).length === 0) await $.ui.close({ id: PANE })
  await $.prompt.submit({ text: `Head chef decided ${d.id} ("${d.question}"): ${choice}` })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // First, so a sous without this line visibly lacks the mod. Shown
    // prefixed with the plugin name: "sous: mod loaded".
    $.ui.log('mod loaded')
    const file = `${await stateDir($)}/decisions.json`
    const filed: Decision[] = (await $.fs.exists(file)) ? JSON.parse(await $.fs.read(file)) : []
    await update($, decisions, () => filed)
    await $.tool.register({
      name: 'file_decision',
      description:
        'File a decision you need from the head chef in their decision inbox. They answer with one ' +
        'press and the answer reaches you as a prompt. Returns the decision id.',
      inputSchema: {
        type: 'object',
        properties: {
          question: { type: 'string', description: 'The decision, standing alone.' },
          options: { type: 'array', items: { type: 'string' }, minItems: 2, description: 'Short option labels.' },
          recommendation: { type: 'string', description: 'The option you recommend, verbatim.' },
        },
        required: ['question', 'options', 'recommendation'],
      },
    })
    await $.tool.register({
      name: 'resolve_decision',
      description: 'Remove a decision from the head chef inbox once they answered it in chat.',
      inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
    })
    await $.command.register({ name: 'inbox', description: 'Open the head-chef decision inbox' })
    await pollCooks($)
    $.clock.every(2000, () => pollCooks($))
    return next(e)
  })

  on('command.run', { command: 'inbox' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Decisions', focus: true, closeOnEscape: true })
    return { text: 'Decision inbox opened: Enter takes the highlighted option, Tab moves, Esc closes.' }
  })

  on('tool.call', { tool: 'mcp__sous__file_decision' }, async ($, e) => {
    const { question, options, recommendation } = e as unknown as Decision
    const id = crypto.randomUUID().slice(0, 8)
    await saveDecisions($, list => [...list, { id, question, options, recommendation }])
    return { result: `Filed decision ${id}; the head chef sees it in the inbox.` }
  })

  on('tool.call', { tool: 'mcp__sous__resolve_decision' }, async ($, e) => {
    const { id } = e as unknown as { id: string }
    if (!(await read($, decisions)).some(d => d.id === id)) return { deny: `No pending decision ${id}.` }
    await saveDecisions($, list => list.filter(d => d.id !== id))
    return { result: `Resolved decision ${id}.` }
  })

  // A cook report reaches the conversation as a prompt row while the sous is
  // idle and as a queued attachment mid-turn; both carry the channel tag.
  on('session.append', async ($, e, next) => {
    const text = JSON.stringify(e.message.content)
    const cook = /<channel source=\\"kitchen\\" cook=\\"([^"\\]+)\\"/.exec(text)?.[1]
    const status = [...text.matchAll(/STATUS:\W*([A-Z_]+)/g)].at(-1)?.[1]
    if (cook && status) {
      await update($, alerts, held => {
        const { [cook]: _, ...rest } = held
        return status === 'DONE' ? rest : { ...rest, [cook]: status }
      })
      if (status !== 'DONE') $.ui.toast(`${cook}: ${status}`, { timeoutMs: 15000 })
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, cooks)
    const held = await read($, alerts)
    const pending = (await read($, decisions)).length
    if (e.props.hasSurvey || (list.length === 0 && pending === 0)) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="row" gap={2} flexWrap="wrap">
        {pending > 0 && (
          <Button
            key="inbox"
            hotkey="d"
            variant="primary"
            label={`⚑ ${pending} decision${pending === 1 ? '' : 's'} pending · /inbox`}
            onPress={() => $.ui.open({ id: PANE, title: 'Decisions', focus: true, closeOnEscape: true })}
          />
        )}
        <Text bold>brigade</Text>
        {list.map(c =>
          held[c.name] ? (
            <Text color="red" bold>{c.name}:{held[c.name]}{c.ctx}</Text>
          ) : (
            <Text color={c.status === 'working' ? 'yellow' : undefined} dimColor={c.status === 'idle'}>
              {c.name}:{c.status}{c.ctx}
            </Text>
          ),
        )}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const list = await read($, decisions)
    const { Box, Button, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column" gap={1}>
        {list.length === 0 && <Text dimColor>No pending decisions.</Text>}
        {list.map(d => (
          <Box key={d.id} flexDirection="column">
            <Text bold>{d.question}</Text>
            <Box flexDirection="row" gap={1} flexWrap="wrap">
              {d.options.map((option, i) => (
                <Button
                  key={`${d.id}-${i}`}
                  label={option === d.recommendation ? `${option} (recommended)` : option}
                  variant={option === d.recommendation ? 'primary' : 'secondary'}
                  autoFocus={option === d.recommendation ? true : undefined}
                  onPress={() => answer($, d, option)}
                />
              ))}
            </Box>
          </Box>
        ))}
      </Box>
    )
  })
}
