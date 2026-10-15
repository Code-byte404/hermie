export type Option = { value: string; key: string; label: string; note: string }

export type View = {
  id: string
  title: string
  why: string
  excerpt: string
  pairs: [string, string][]
  options: Option[]
  deadline: number | null
}

export type Ask = {
  id: string
  kind: string
  reason: string
  size: number
  local?: boolean
  preview?: string
  view?: View
}

export type Summary = { text: string }

declare module 'claude-code' {
  interface PluginState {
    hermie: { summary: Summary | null; isHidden: boolean; asks: Ask[] }
  }
}
