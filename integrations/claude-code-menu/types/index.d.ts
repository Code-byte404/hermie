export type Ask = { id: string; kind: string; reason: string; size: number; local?: boolean; preview?: string }

export type Summary = {
  replaced: Record<string, number>
  withheld: string[]
  held: string | null
}

declare module 'claude-code' {
  interface PluginState {
    hermie: { summary: Summary | null; isHidden: boolean; asks: Ask[] }
  }
}
