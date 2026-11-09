import { existsSync } from "node:fs"
import { DatabaseSync } from "node:sqlite"

export class NativeState {
  constructor(databasePath, epochMs) {
    this.databasePath = databasePath
    this.epochMs = epochMs
    this.reportedRuns = new Set()
    this.automationStatuses = new Map()
  }

  rows(sql, ...parameters) {
    if (!existsSync(this.databasePath)) return []
    const db = new DatabaseSync(this.databasePath, { readOnly: true })
    try {
      return db.prepare(sql).all(...parameters)
    } finally {
      db.close()
    }
  }

  runningToolCall(context) {
    return this.rows(`SELECT call.run_id, call.tool_call_id, call.session_id AS chat_id,
        call.tool_name, call.arguments_json
      FROM tool_calls AS call JOIN chat_runs AS run ON run.run_id = call.run_id
      WHERE call.run_id = ? AND call.tool_call_id = ? AND call.session_id = ?
        AND call.status = 'running' AND run.ended_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM tool_approvals AS approval
          WHERE approval.run_id = call.run_id AND approval.tool_call_id = call.tool_call_id
            AND approval.status = 'pending')`,
      context.run_id, context.tool_call_id, context.chat_id)[0]
  }

  pendingApprovals() {
    return this
      .rows(`SELECT approval.run_id, approval.tool_call_id, call.session_id AS chat_id, call.tool_name, call.arguments_json
      FROM tool_approvals AS approval JOIN tool_calls AS call
        ON call.run_id = approval.run_id AND call.tool_call_id = approval.tool_call_id
      WHERE approval.status = 'pending'`)
  }

  terminalToolCalls() {
    return this.rows(`SELECT call.run_id, call.tool_call_id
      FROM tool_calls AS call JOIN chat_runs AS run ON run.run_id = call.run_id
      WHERE call.status IN ('completed', 'uncertain')
        OR run.status IN ('failed', 'cancelled', 'error')`)
  }

  completedRuns() {
    return this.rows(
      "SELECT run_id, status, error_code, error_message FROM chat_runs WHERE ended_at IS NOT NULL ORDER BY ended_at"
    ).filter((row) => !this.reportedRuns.has(row.run_id))
  }

  markReported(runId) {
    this.reportedRuns.add(runId)
  }

  changedAutomations() {
    return this.rows(
      "SELECT id, task_id, status, chat_run_id, started_at, ended_at FROM automation_runs WHERE purpose = 'user_automation' ORDER BY id"
    ).filter((row) => {
      if (this.automationStatuses.get(row.id) === row.status) return false
      this.automationStatuses.set(row.id, row.status)
      return true
    })
  }

  busy() {
    const activeChatRuns = this.rows(
      `SELECT COUNT(*) AS n FROM chat_runs WHERE status IN ('pending','running','backgrounded')`
    )[0]?.n
    if (activeChatRuns > 0) return true

    const activeAutomations = this.rows(
      "SELECT COUNT(*) AS n FROM automation_runs WHERE purpose = 'user_automation' AND ended_at IS NULL"
    )[0]?.n
    if (activeAutomations > 0) return true

    return this.hasPendingDeliveries()
  }

  hasPendingDeliveries() {
    return this.rows("SELECT EXISTS (SELECT 1 FROM automation_deliveries WHERE status = 'pending') AS pending")[0]?.pending > 0
  }

  hasPendingQuestions() {
    return this.rows("SELECT EXISTS (SELECT 1 FROM run_questions WHERE resolution IS NULL AND closed_at IS NULL) AS pending")[0]?.pending > 0
  }

  hasPendingAsks() {
    return this.hasPendingQuestions() || this.rows(
      "SELECT EXISTS (SELECT 1 FROM tool_approvals WHERE status = 'pending') AS pending"
    )[0]?.pending > 0
  }

  nextWake() {
    const row = this.rows(`SELECT next_run_at FROM scheduled_tasks
      WHERE enabled = 1 AND builtin = 0 AND purpose = 'user_automation'
      AND next_run_at IS NOT NULL ORDER BY next_run_at LIMIT 1`)[0]
    return row ? Math.ceil((Date.parse(row.next_run_at) - this.epochMs) / 1000) : undefined
  }
}
