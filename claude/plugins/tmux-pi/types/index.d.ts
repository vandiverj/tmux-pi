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
  // Lines under the counts: a poll that fails, a wake that cannot be sent, workers of another main.
  notes: string[]
}

// A row of this session that `tp-agent list --main` left out because another main's id is on it.
export type OtherRow = { id: string; status: string; main: string }

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
