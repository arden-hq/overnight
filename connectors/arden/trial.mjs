import { mkdirSync } from "node:fs"
import { join } from "node:path"
import { ArdenProcess } from "./arden-process.mjs"
import { McpServices } from "./mcp-services.mjs"
import { NativeState } from "./native-state.mjs"
import { OperatorMessages } from "./operator-messages.mjs"
import { ActionQueue, WorldBridge } from "./world-bridge.mjs"

export class ArdenTrial {
  constructor(config, evidence) {
    this.config = config
    this.evidence = evidence
    this.generation = 0
    this.initialize(config)
  }

  initialize(config) {
    this.runtimeConfig = config
    this.arden = new ArdenProcess(config, this.evidence)
    this.native = new NativeState(config.databasePath, config.epochMs)
    this.actions = new ActionQueue()
    this.waitSince = 0
  }

  async start(observation) {
    await this.arden.start(observation.time)
    this.bridge = new WorldBridge(this.arden.client, this.actions, this.evidence, context => {
      const call = this.native.runningToolCall(context)
      if (!call) return undefined
      const owner = this.mcp.toolOwners.get(call.tool_name)
      if (!owner) return undefined
      return { ...call, ...owner, args: JSON.parse(call.arguments_json) }
    })
    this.mcp = new McpServices(
      this.config.repo,
      observation.operations ?? {},
      this.bridge,
      this.evidence
    )
    await this.mcp.listen()
    await this.mcp.register(this.arden)
    await this.arden.request("PATCH", "/config", {
      chat_model: this.config.model,
      model_roles: {
        background: { model: this.config.model },
        helper: { model: this.config.model }
      }
    })
    const session = await this.arden.client.createSession({
      name: "Overnight isolated trial",
      chat_model: this.config.model
    })
    const sessionId = session.session_id
    this.sessionId = sessionId
    const workspace = `${this.runtimeConfig.runDir}.workspace`
    mkdirSync(workspace, { recursive: true, mode: 0o700 })
    await this.arden.request("PATCH", `/permissions/sessions/${encodeURIComponent(sessionId)}`, {
      approval_mode: "allow", local_folders: [workspace]
    })
    this.operator = new OperatorMessages(
      this.arden, sessionId, this.config.epochMs, this.evidence, [...this.mcp.toolOwners.keys()]
    )
    this.evidence.write("provenance", {
      repo: this.config.repo,
      database: this.runtimeConfig.databasePath,
      workspace,
      model: this.config.model,
      epoch_ms: this.config.epochMs,
      host: "Arden startServer + HeadlessClient",
      mcp: Object.keys(this.mcp.operations)
    })
    this.time = observation.time
  }

  async observe(observation) {
    if (this.time === undefined) await this.start(observation)
    else if (observation.time > this.time) await this.arden.moveClock(observation.time)
    else if (observation.time < this.time) throw new Error("World clock moved backwards")
    this.time = observation.time
    this.mcp.operations = observation.operations ?? {}
    const receipts = observation.receipts ?? []
    if (this.lastAction?.id) {
      const id = this.lastAction.id
      const receipt = [...receipts]
        .reverse()
        .find((item) => item.invocation_id === id || item.id === id)
      if (!receipt) throw new Error(`No receipt for ${id}`)
      await this.bridge.receive(id, receipt)
    }
    this.lastAction = undefined
    await this.bridge.settleDecisions(receipts)
    await this.operator.submit(observation.messages ?? [], this.time, observation)
    this.lastAction = await this.nextAction(observation.horizon)
    return this.lastAction
  }

  async pollNative() {
    this.bridge.retireAuthorizations(this.native.terminalToolCalls())
    for (const row of this.native.pendingApprovals()) {
      const owner = this.mcp.toolOwners.get(row.tool_name)
      if (owner) this.bridge.requestApproval(row, owner)
      else {
        await this.arden.client.answerApproval({
          runId: row.run_id,
          toolId: row.tool_call_id,
          approved: false,
          result: "This trial authorizes simulated service tools and private local workspace; use available world MCP tools for service access."
        })
        this.evidence.write("native_approval_denied", {
          run_id: row.run_id, tool_call_id: row.tool_call_id, tool_name: row.tool_name,
          reason: "outside_trial_authority"
        })
      }
    }
    for (const row of this.native.completedRuns()) {
      this.evidence.write("native_run", row)
      const submitted = this.operator?.pendingRuns.delete(row.run_id)
      if (submitted && row.status !== "completed")
        throw new Error(`Submitted Arden run ${row.run_id} ended ${row.status}: ${row.error_message ?? row.error_code ?? ""}`)
      if (row.status === "completed") {
        const run = await this.arden.client.getRun(row.run_id)
        const output = run.result?.text
        if (typeof output === "string" && output.trim())
          this.actions.push({ kind: "report", message: output.trim() })
      }
      this.native.markReported(row.run_id)
    }
    if (this.operator && this.native.hasPendingQuestions())
      for (const message of await this.operator.newQuestionReports(this.time))
        this.actions.push({ kind: "report", message })
    for (const row of this.native.changedAutomations())
      this.evidence.write("native_automation_run", row)
  }

  nativeBusy() {
    return this.native.busy() || this.operator?.pendingRuns.size > 0
  }

  async nextAction(horizon) {
    const deadline = Date.now() + 210_000
    const wait = (until) => ({
      kind: "wait",
      until: Math.min(horizon, Math.max(this.time + 1, until))
    })
    for (;;) {
      this.arden.check()
      this.mcp?.check()
      await this.pollNative()
      const action = await this.actions.take()
      if (action) return action
      const wake = this.native.nextWake()
      const busy = this.nativeBusy()
      if (this.time >= horizon) {
        const unresolved =
          busy ||
          wake !== undefined ||
          this.bridge.awaitingReceipt() ||
          this.bridge.hasNativeApprovals() ||
          this.native.hasPendingAsks()
        return { kind: unresolved ? "wait" : "done" }
      }
      if ((this.bridge.awaitingReceipt() || this.native.hasPendingQuestions()) &&
        !this.native.hasPendingDeliveries()) return wait(horizon)
      const wakeIsDue = wake !== undefined && wake <= this.time
      if (busy || wakeIsDue) {
        if (Date.now() > deadline) throw new Error("Arden run or scheduler did not settle")
        if (wakeIsDue && !busy) {
          if (this.waitSince === 0) this.waitSince = Date.now()
          if (Date.now() - this.waitSince > 70_000) return wait(this.time + 1)
        }
        continue
      }
      this.waitSince = 0
      if (this.bridge.hasNativeApprovals()) return wait(horizon)
      return wake === undefined ? { kind: "done" } : wait(wake)
    }
  }

  status() {
    const wake = this.native.nextWake()
    const due = wake !== undefined && wake <= this.time
    const waiting =
      this.bridge.awaitingReceipt() ||
      this.bridge.hasNativeApprovals() ||
      this.native.hasPendingQuestions()
    const status = {
      busy: due || this.native.hasPendingDeliveries() || (this.nativeBusy() && !waiting),
      pending:
        this.nativeBusy() || waiting || wake !== undefined ||
        this.bridge.calls.size > 0 || this.actions.items.length > 0,
      next_wake: wake !== undefined && wake > this.time ? wake : null
    }
    this.evidence.write("native_status", {
      time: this.time, native_next_wake: wake ?? null, ...status
    })
    return status
  }

  async pauseAutomations() {
    const schedules = this.native.rows(
      "SELECT task_id AS id FROM scheduled_tasks WHERE enabled = 1 AND builtin = 0 AND purpose = 'user_automation'"
    )
    for (const task of schedules)
      await this.arden.request("PATCH", `/automations/${encodeURIComponent(task.id)}`, { enabled: false })
    return schedules
  }

  async cancel() {
    if (this.time === undefined) return
    const schedules = await this.pauseAutomations()
    const ownedSessions = this.native.rows(
      "SELECT session_id FROM sessions WHERE archived_at IS NULL ORDER BY session_id"
    ).map(row => row.session_id)
    const sessions = ownedSessions.filter(id => id !== this.sessionId)
    if (ownedSessions.includes(this.sessionId)) sessions.push(this.sessionId)
    for (const sessionId of sessions) {
      await this.arden.request("DELETE", `/sessions/${encodeURIComponent(sessionId)}?stop=true`)
      await this.arden.request("POST", `/sessions/${encodeURIComponent(sessionId)}/restore`)
    }
    this.bridge.close()
    this.actions.items.length = 0
    this.lastAction = undefined
    if (this.native.busy() || this.native.hasPendingAsks() || this.native.nextWake() !== undefined)
      throw new Error("Arden cancellation left native work pending")
    this.operator?.pendingRuns.clear()
    this.evidence.write("native_cancel", {
      session_id: this.sessionId, native_sessions: sessions, schedules: schedules.map(row => row.id),
      control: "native_chat_stop_restore"
    })
  }

  async reset() {
    await this.closeResources()
    const runDir = join(this.config.runDir, "resets", String(++this.generation))
    mkdirSync(runDir, { recursive: true, mode: 0o700 })
    const config = { ...this.config, runDir, databasePath: join(runDir, "arden.db") }
    this.initialize(config)
    this.time = undefined
    this.operator = undefined
    this.mcp = undefined
    this.bridge = undefined
    this.lastAction = undefined
    this.closing = undefined
    this.evidence.write("native_chat_reset", { generation: this.generation, database: config.databasePath })
  }

  close() {
    this.closing ??= this.closeResources()
    return this.closing
  }

  async closeResources() {
    this.bridge?.close()
    try {
      await this.arden.close()
    } finally {
      await this.mcp?.close()
    }
  }
}
