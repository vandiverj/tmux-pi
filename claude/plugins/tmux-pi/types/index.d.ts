export type TaskRow = {
  id: string
  status: string
  role: string | null
  permission: unknown
  capability_request: unknown
  capability_pending: boolean
  reason: string | null
  result_status: string | null
  failing_tests?: true
  // Set for a task that lives in another session's state directory (tp-agent list --main).
  state_dir?: string
}

export type TaskSummary = {
  active: number
  toCollect: number
  paused: number
  pausedWithStatus: number
  failed: number
  refused: number
  needsInput: string[]
}

export type NotifierState = {
  epoch: number
  isIndexed: boolean
  delivered: string[]
  pending: TaskRow[]
}

declare module 'claude-code' {
  interface PluginState {
    'tmux-pi': { tasks: TaskSummary | null; notifier: NotifierState }
  }
}
