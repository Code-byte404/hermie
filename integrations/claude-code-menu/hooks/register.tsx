import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Ask, Summary } from '../types'

const summary = atom({ plugin: 'hermie', key: 'summary' } as const, null)
const isHidden = atom({ plugin: 'hermie', key: 'isHidden' } as const, false)

const asks = atom({ plugin: 'hermie', key: 'asks' } as const, [] as Ask[])

// Claude Code's own PATH must find `hermie`; otherwise put the full path here (for example a conda env's bin/hermie).
const HERMIE = 'hermie'

async function refresh($: any) {
  try {
    const ran = await $.process.run([HERMIE, 'status', '--json'], { timeoutMs: 5000 })
    const last = JSON.parse(ran.stdout).last
    const next: Summary | null = last
      ? { replaced: last.replaced ?? {}, withheld: last.withheld ?? [], held: last.held ?? null }
      : null
    await update($, summary, () => next)
  } catch {
    await update($, summary, () => null)
  }
}

async function pollAsks($: any) {
  try {
    const ran = await $.process.run([HERMIE, 'asks', '--json'], { timeoutMs: 5000 })
    const rows: Ask[] = JSON.parse(ran.stdout)
    await update($, asks, () => rows)
  } catch {
    await update($, asks, () => [])
  }
}

async function answer($: any, id: string, value: string) {
  await $.process.run([HERMIE, 'answer', id, value])
  await pollAsks($)
}

function reasonText(reason: string): string {
  if (reason.startsWith('smuggling')) return 'looks like encoded data'
  if (reason === 'judge') return 'judged sensitive'
  return 'held for review'
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    $.clock.every(1000, () => pollAsks($))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const waiting = await read($, asks)
    if (waiting.length > 0) {
      const { Box, Button, Text } = $.ui.resolve(e)
      const a = waiting[0]
      const more = waiting.length > 1 ? ` (+${waiting.length - 1} more)` : ''
      if (a.kind === 'rewrite') {
        const reviewChoices = [
          { value: 'reject', label: 'Discard', hotkey: '1', note: 'send nothing' },
          { value: 'again', label: 'Rewrite again', hotkey: '2', note: 'ask the local model for another version' },
          { value: 'send', label: 'Send', hotkey: '3', note: 'send exactly this text' },
        ]
        return (
          <Box flexDirection="column" borderStyle="round" borderColor="permission" paddingX={1}>
            <Text bold color="warning">
              Local rewrite of message {a.id}: this is what would be sent
            </Text>
            <Box marginTop={1}>
              <Text>{a.preview ?? ''}</Text>
            </Box>
            <Box flexDirection="column" marginTop={1}>
              {reviewChoices.map((c, i) => (
                <Box key={`row-${c.value}`} flexDirection="row" gap={2}>
                  <Button
                    key={c.value}
                    label={`${c.hotkey}. ${c.label}`}
                    hotkey={c.hotkey}
                    variant={i === 0 ? 'primary' : 'secondary'}
                    autoFocus={i === 0 ? true : undefined}
                    onPress={() => answer($, a.id, c.value)}
                  />
                  <Text dimColor>{c.note}</Text>
                </Box>
              ))}
            </Box>
          </Box>
        )
      }
      const choices = [
        { value: 'reject', label: 'Reject', hotkey: '1', note: 'keep it on this machine' },
        { value: 'send', label: 'Send as is', hotkey: '2', note: 'release this one message' },
        { value: 'session', label: 'Allow session', hotkey: '3', note: 'release judge holds until serve restarts' },
        ...(a.local
          ? [{ value: 'local', label: 'Rewrite locally', hotkey: '4', note: 'local model removes the specifics, then send' }]
          : []),
      ]

      return (
        <Box flexDirection="column" borderStyle="round" borderColor="permission" paddingX={1}>
          <Text bold color="warning">
            Hermie held a message before it left your machine{more}
          </Text>
          <Text dimColor>
            id {a.id} · {a.size} chars · {reasonText(a.reason)}
          </Text>
          <Box flexDirection="column" marginTop={1}>
            {choices.map((c, i) => (
              <Box key={`row-${c.value}`} flexDirection="row" gap={2}>
                <Button
                  key={c.value}
                  label={`${c.hotkey}. ${c.label}`}
                  hotkey={c.hotkey}
                  variant={i === 0 ? 'primary' : 'secondary'}
                  autoFocus={i === 0 ? true : undefined}
                  onPress={() => answer($, a.id, c.value)}
                />
                <Text dimColor>{c.note}</Text>
              </Box>
            ))}
          </Box>
        </Box>
      )
    }

    const s = await read($, summary)
    if (e.props.hasSurvey || s === null || (await read($, isHidden))) {
      return next(e)
    }

    const { Box, Button, Text } = $.ui.resolve(e)
    const total = Object.values(s.replaced).reduce((a, b) => a + b, 0)
    const pending = Array.from(new Set(s.held ? [s.held, ...s.withheld] : s.withheld))

    return (
      <Box flexDirection="row" gap={2}>
        <Text dimColor>Hermie · {total} replaced</Text>
        {pending.length > 0 ? (
          <Text color="warning">
            {pending.length} withheld · hermie allow {pending[0]}
          </Text>
        ) : null}
        <Button key="hide" label="Hide" plain hotkey="h" onPress={() => update($, isHidden, () => true)} />
      </Box>
    )
  })
}
