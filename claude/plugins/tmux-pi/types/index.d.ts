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
  // HB4a request records of the task (tp-agent list `requests`, newest 20); absent for a task with none.
  requests?: RequestRef[]
  // Set on a notice row the notifier made for one request: announced under `req:<id>:open` / `req:<id>:resolved`.
  notice?: RequestNotice
}

// opened/resolved: tp-agent's times (lib/hub_records.py row_requests), the order a row's transitions are announced in.
export type RequestRef = { id: string; kind: string; status: string; summary: string; how?: string; opened?: string; resolved?: string }

// isClosed: an opening announced after its request had already closed (both between two polls), so there is nothing to answer.
export type RequestNotice = { id: string; kind: string; state: 'open' | 'resolved'; summary: string; how?: string; isClosed?: boolean }

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
  // Request ids already closed at this notifier's first read: never replayed. Absent until that read (and in older values).
  history?: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'tmux-pi': { tasks: TaskSummary | null; notifier: NotifierState }
  }
}
