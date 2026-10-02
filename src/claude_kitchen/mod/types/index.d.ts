export type Cook = { name: string; status: string; ctx: string }

export type Decision = { id: string; question: string; options: string[]; recommendation: string }

declare module 'claude-code' {
  interface PluginState {
    sous: { cooks: Cook[]; decisions: Decision[]; alerts: Record<string, string> }
  }
}
