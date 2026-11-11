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
  // HB4a: every request transition of the task, in order (tp-agent list `request_log`, lib/hub_records.py request_log): what
  // notices are delivered from. Absent for a task with none. Never kept on a held notice row.
  requestLog?: RequestEvent[]
  // How main can answer an open request on this task now (tp-agent list `answer_route`).
  answerRoute?: AnswerRoute
  // Set on a notice row the notifier made for one request: announced under `req:<id>:open` / `req:<id>:resolved`.
  notice?: RequestNotice
}

// seq: the transition's place among the task's request transitions, from 1; a notifier's cursor is the last seq it queued.
export type RequestEvent = { seq: number; id: string; change: 'opened' | 'closed'; kind: string; summary: string; how?: string }

// message: `tp-agent message` reaches the worker; retry: it cannot and `tp-agent retry` takes the task; none: neither does.
export type AnswerRoute = { by: 'message' | 'retry' | 'none'; why?: string }

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
  // Per task (`<state_dir>#<id>`), how many of its request transitions (request_log seq) have been queued as notices. Absent
  // until the startup snapshot (and in older values, which carried `history` instead: a new module snapshots again).
  cursors?: Record<string, number>
}

declare module 'claude-code' {
  interface PluginState {
    'tmux-pi': { tasks: TaskSummary | null; notifier: NotifierState }
  }
}
