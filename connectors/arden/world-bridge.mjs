import { randomUUID } from "node:crypto"
import { EventEmitter, once } from "node:events"

export class ActionQueue {
  items = []
  changed = new EventEmitter()

  push(action) {
    this.items.push(action)
    this.changed.emit("action")
  }
  withdraw(id) {
    const index = this.items.findIndex((action) => action.id === id)
    if (index < 0) return false
    this.items.splice(index, 1)
    return true
  }
  async take(timeoutMs = 250) {
    if (this.items.length === 0) {
      try {
        await once(this.changed, "action", { signal: AbortSignal.timeout(timeoutMs) })
      } catch (error) {
        if (error.name !== "AbortError") throw error
      }
    }
    return this.items.shift()
  }
}

const canonicalJson = (value) => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`
  if (value !== null && typeof value === "object")
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`
  return JSON.stringify(value)
}
const identity = ({ run_id, tool_call_id, chat_id }) => JSON.stringify([run_id, tool_call_id, chat_id])
const signature = ({ serviceId, operation, args }) => canonicalJson([serviceId, operation, args])
const allowed = (receipt) => receipt.status === "allowed" || receipt.status === "approved"
const unsettled = (receipt) => receipt.status === "pending" || receipt.status === "waiting"

export class WorldBridge {
  constructor(client, actions, evidence, lookupNativeCall) {
    this.client = client
    this.actions = actions
    this.evidence = evidence
    this.sequence = 0
    this.native = new Map()
    this.authorized = new Map()
    this.calls = new Map()
    this.lookupNativeCall = lookupNativeCall
    this.dispatched = new Set()
  }

  nextId() {
    return `arden-${++this.sequence}-${randomUUID().slice(0, 8)}`
  }

  retireAuthorizations(terminalCalls) {
    const terminal = new Set(
      terminalCalls.map((call) => JSON.stringify([call.run_id, call.tool_call_id]))
    )
    for (const [key, item] of this.authorized) {
      if (!terminal.has(JSON.stringify([item.run_id, item.tool_call_id]))) continue
      this.authorized.delete(key)
      this.evidence.write("native_approval_retired", {
        invocation_id: item.id,
        run_id: item.run_id,
        tool_call_id: item.tool_call_id
      })
    }
  }

  requestApproval(row, owner) {
    const outstanding = [...this.native.values(), ...this.authorized.values()]
    const alreadyTracked = outstanding.some(
      (item) => item.run_id === row.run_id && item.tool_call_id === row.tool_call_id
    )
    if (alreadyTracked) return
    const item = {
      ...owner,
      args: JSON.parse(row.arguments_json),
      run_id: row.run_id,
      tool_call_id: row.tool_call_id,
      chat_id: row.chat_id,
      id: this.nextId(),
      waiting: false
    }
    this.native.set(item.id, item)
    this.evidence.write("native_approval_needed", {
      run_id: item.run_id,
      tool_call_id: item.tool_call_id,
      invocation_id: item.id,
      service_id: item.serviceId,
      operation: item.operation,
      args: item.args
    })
    this.actions.push({
      kind: "request_approval",
      id: item.id,
      service_id: item.serviceId,
      operation: item.operation,
      args: item.args
    })
  }

  invoke(serviceId, operation, args, signal, context) {
    if (signal?.aborted) return Promise.reject(new Error("MCP request closed"))
    if (!context || ![context.run_id, context.tool_call_id, context.chat_id].every(
      (value) => typeof value === "string" && value.length > 0
    )) return Promise.reject(new Error("Missing trusted native execution identity"))
    const key = identity(context)
    if (this.dispatched.has(key))
      return Promise.reject(new Error("Unknown or unapproved native execution identity: already dispatched"))
    const approved = this.authorized.get(key) ?? this.lookupNativeCall?.(context)
    if (!approved) return Promise.reject(new Error("Unknown or unapproved native execution identity"))
    if (signature(approved) !== signature({ serviceId, operation, args }))
      return Promise.reject(new Error("Native execution identity does not match approved operation"))
    this.dispatched.add(key)
    this.authorized.delete(key)
    const id = approved.id ?? this.nextId()
    const action = { kind: "invoke", id, service_id: serviceId, operation, args }
    this.evidence.write("mcp_call", { invocation_id: id, service_id: serviceId, operation, args, ...context })
    return new Promise((resolve, reject) => {
      const abort = () => {
        if (this.actions.withdraw(id)) this.calls.delete(id)
        reject(new Error("MCP request closed"))
      }
      this.calls.set(id, {
        action,
        waiting: false,
        resolve: (receipt) => {
          signal?.removeEventListener("abort", abort)
          resolve(receipt)
        }
      })
      signal?.addEventListener("abort", abort, { once: true })
      this.actions.push(action)
    })
  }

  async answerNative(item, approved) {
    const key = identity(item)
    if (approved) this.authorized.set(key, item)
    try {
      await this.client.answerApproval({
        runId: item.run_id,
        toolId: item.tool_call_id,
        approved,
        result: approved ? "Approved by trial operator" : "Denied by trial operator"
      })
    } catch (error) {
      this.authorized.delete(key)
      throw error
    }
    this.native.delete(item.id)
    this.evidence.write("native_approval_answer", { invocation_id: item.id, approved })
  }

  async receive(id, receipt) {
    this.evidence.write("world_receipt", { invocation_id: id, receipt })
    const native = this.native.get(id)
    if (native) {
      if (receipt.status === "pending") native.waiting = true
      else await this.answerNative(native, allowed(receipt))
      return
    }
    const call = this.calls.get(id)
    if (!call) throw new Error(`No pending world invocation for ${id}`)
    if (unsettled(receipt)) {
      call.waiting = true
      return
    }
    if (allowed(receipt)) {
      call.waiting = false
      this.actions.push(call.action)
      return
    }
    call.resolve(receipt)
    this.calls.delete(id)
  }

  async settleDecisions(receipts) {
    const decisions = new Map(
      receipts
        .filter((receipt) => ["approved", "denied"].includes(receipt.status))
        .map((receipt) => [receipt.invocation_id, receipt])
    )
    for (const item of this.native.values()) {
      const decision = decisions.get(item.id)
      if (item.waiting && decision) await this.answerNative(item, allowed(decision))
    }
    const latest = new Map(receipts.map((receipt) => [receipt.invocation_id, receipt]))
    for (const [id, call] of [...this.calls]) {
      const receipt = latest.get(id)
      if (!call.waiting || !receipt || unsettled(receipt)) continue
      await this.receive(id, receipt)
    }
  }

  awaitingReceipt() {
    return [...this.native.values(), ...this.calls.values()].some((item) => item.waiting)
  }
  hasNativeApprovals() {
    return this.native.size > 0
  }

  close() {
    for (const [id, call] of this.calls)
      call.resolve({ status: "failed", invocation_id: id, error: "Trial host closed" })
    this.calls.clear()
    this.native.clear()
    this.authorized.clear()
  }
}
