import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Ask, Summary } from '../types'

const summary = atom({ plugin: 'hermie', key: 'summary' } as const, null)
const isHidden = atom({ plugin: 'hermie', key: 'isHidden' } as const, false)

const asks = atom({ plugin: 'hermie', key: 'asks' } as const, [] as Ask[])

// `hermie claude` puts its own `hermie` first on Claude Code's PATH. Started another way, Claude Code's PATH must
// find `hermie`; otherwise put the full path here (for example a conda env's bin/hermie).
const HERMIE = 'hermie'

async function refresh($: any) {
  try {
    const ran = await $.process.run([HERMIE, 'status', '--json'], { timeoutMs: 5000 })
    // the wording comes from `hermie status --json` (hermie/explain.py), so every surface says the same
    const parsed = JSON.parse(ran.stdout)
    const next: Summary | null = parsed.last_text ? { text: parsed.last_text } : null
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
      // the view (title, why, excerpt, replaced values, options and what each does) comes from `hermie asks --json`
      const a = waiting[0]
      const v = a.view
      if (!v) return next(e)
      const left = v.deadline ? Math.max(0, Math.round(v.deadline - Date.now() / 1000)) : null
      const pairs = v.pairs.map(([d, ph]) => (ph ? (d ? `${d} -> ${ph}` : ph) : d)).join(', ')
      return (
        <Box flexDirection="column" borderStyle="round" borderColor="permission" paddingX={1}>
          <Text bold color="warning">
            {v.title}
          </Text>
          <Text>Why: {v.why}</Text>
          {v.excerpt ? (
            <Box marginTop={1}>
              <Text>"{v.excerpt}"</Text>
            </Box>
          ) : null}
          {pairs ? <Text dimColor>Already replaced: {pairs}</Text> : null}
          <Box flexDirection="column" marginTop={1}>
            {v.options.map((o, i) => (
              <Box key={`row-${o.value}`} flexDirection="row" gap={2}>
                <Button
                  key={o.value}
                  label={`${o.key}. ${o.label}`}
                  hotkey={o.key}
                  variant={i === 0 ? 'primary' : 'secondary'}
                  autoFocus={i === 0 ? true : undefined}
                  onPress={() => answer($, a.id, o.value)}
                />
                <Text dimColor>{o.note}</Text>
              </Box>
            ))}
          </Box>
          {left !== null ? (
            <Text dimColor>
              No answer in {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')} {'->'} Don't send
            </Text>
          ) : null}
        </Box>
      )
    }

    const s = await read($, summary)
    if (e.props.hasSurvey || s === null || (await read($, isHidden))) {
      return next(e)
    }

    const { Box, Button, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="row" gap={2}>
        <Text dimColor>Hermie · last reply: {s.text}</Text>
        <Button key="hide" label="Hide" plain hotkey="h" onPress={() => update($, isHidden, () => true)} />
      </Box>
    )
  })
}
