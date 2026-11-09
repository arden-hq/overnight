import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import assert from "node:assert/strict"
import { test } from "node:test"
import { ArdenProcess } from "./arden-process.mjs"
import { ActionQueue, WorldBridge } from "./world-bridge.mjs"

const evidence = { write() {} }
const owner = { serviceId: "shop", operation: "cancel_order" }
const args = { order_id: "original" }
const nativeCall = {
  run_id: "native-run", tool_call_id: "native-call", chat_id: "native-chat",
  arguments_json: JSON.stringify(args)
}
const approve = async (bridge, actions, row = nativeCall) => {
  bridge.requestApproval(row, owner)
  const action = await actions.take()
  await bridge.receive(action.id, { status: "allowed" })
  return action
}

test("identical concurrent approvals remain bound to their exact run, call and chat", async () => {
  const actions = new ActionQueue()
  const bridge = new WorldBridge({ answerApproval: async () => {} }, actions, evidence)
  const other = { ...nativeCall, tool_call_id: "second-call" }
  const first = await approve(bridge, actions)
  const second = await approve(bridge, actions, other)
  const secondResult = bridge.invoke("shop", "cancel_order", args, undefined, other)
  assert.equal((await actions.take()).id, second.id)
  const firstResult = bridge.invoke("shop", "cancel_order", args, undefined, nativeCall)
  assert.equal((await actions.take()).id, first.id)
  await bridge.receive(first.id, { status: "succeeded" })
  await bridge.receive(second.id, { status: "succeeded" })
  assert.deepEqual(await Promise.all([firstResult, secondResult]), [
    { status: "succeeded" }, { status: "succeeded" }
  ])
  await assert.rejects(() => bridge.invoke("shop", "cancel_order", args, undefined, nativeCall), /unapproved/)
  bridge.close()
})

test("a released native approval installs identity before the API response", async () => {
  let releaseResponse
  const bridge = new WorldBridge({ answerApproval: () => new Promise((resolve) => { releaseResponse = resolve }) }, new ActionQueue(), evidence)
  bridge.requestApproval(nativeCall, owner)
  const approval = await bridge.actions.take()
  const answering = bridge.receive(approval.id, { status: "allowed" })
  const result = bridge.invoke("shop", "cancel_order", args, undefined, nativeCall)
  assert.equal((await bridge.actions.take()).id, approval.id)
  releaseResponse()
  await answering
  await bridge.receive(approval.id, { status: "succeeded" })
  await result
  bridge.close()
})

for (const [label, context, changedArgs] of [
  ["missing identity", undefined, args],
  ["unknown call", { ...nativeCall, tool_call_id: "unknown" }, args],
  ["other run", { ...nativeCall, run_id: "other" }, args],
  ["other chat", { ...nativeCall, chat_id: "other" }, args],
  ["changed arguments", nativeCall, { order_id: "other" }]
]) test(`native approval rejects ${label} without consuming its authorization`, async () => {
  const actions = new ActionQueue()
  const bridge = new WorldBridge({ answerApproval: async () => {} }, actions, evidence)
  const approval = await approve(bridge, actions)
  await assert.rejects(() => bridge.invoke("shop", "cancel_order", changedArgs, undefined, context))
  const result = bridge.invoke("shop", "cancel_order", args, undefined, nativeCall)
  assert.equal((await actions.take()).id, approval.id)
  await bridge.receive(approval.id, { status: "succeeded" })
  await result
  bridge.close()
})

for (const dispatched of [false, true]) test(`MCP disconnect preserves only dispatched receipt slots (${dispatched})`, async () => {
  const actions = new ActionQueue()
  const bridge = new WorldBridge({ answerApproval: async () => {} }, actions, evidence)
  const approval = await approve(bridge, actions)
  const request = new AbortController()
  const result = bridge.invoke("shop", "cancel_order", args, request.signal, nativeCall)
  const rejected = assert.rejects(result, /MCP request closed/)
  if (dispatched) await actions.take()
  request.abort()
  await rejected
  if (dispatched) await bridge.receive(approval.id, { status: "succeeded" })
  else await assert.rejects(() => bridge.receive(approval.id, { status: "succeeded" }), /No pending/)
  bridge.close()
})

test("only exact terminal native identity retires an unused approval grant", async () => {
  const actions = new ActionQueue()
  const bridge = new WorldBridge({ answerApproval: async () => {} }, actions, evidence)
  const approval = await approve(bridge, actions)
  bridge.retireAuthorizations([{ run_id: "other-run", tool_call_id: "native-call" }])
  const result = bridge.invoke("shop", "cancel_order", args, undefined, nativeCall)
  assert.equal((await actions.take()).id, approval.id)
  await bridge.receive(approval.id, { status: "succeeded" })
  await result
  await approve(bridge, actions)
  bridge.retireAuthorizations([nativeCall])
  await assert.rejects(() => bridge.invoke("shop", "cancel_order", args, undefined, nativeCall), /unapproved/)
  bridge.close()
})

test("failed child spawn closes without waiting for an exit event", { timeout: 10_000 }, async (t) => {
  const runDir = await mkdtemp(join(tmpdir(), "overnight-spawn-"))
  t.after(() => rm(runDir, { recursive: true, force: true }))
  const arden = new ArdenProcess({ repo: "/unused", runDir, epochMs: 0 }, evidence)
  const executable = process.execPath
  try {
    process.execPath = "/nonexistent/overnight-node"
    await assert.rejects(() => arden.start(0), { code: "ENOENT" })
  } finally {
    process.execPath = executable
    await arden.close()
  }
})

test("native calls without an approval prompt still require exact running-call identity and runner receipts", async () => {
  const actions = new ActionQueue()
  const lookup = context => context.run_id === nativeCall.run_id &&
    context.tool_call_id === nativeCall.tool_call_id && context.chat_id === nativeCall.chat_id
    ? { ...nativeCall, ...owner, args } : undefined
  const bridge = new WorldBridge({}, actions, evidence, lookup)
  await assert.rejects(() => bridge.invoke("shop", "cancel_order", args, undefined,
    { ...nativeCall, tool_call_id: "forged" }), /unapproved/)
  await assert.rejects(() => bridge.invoke("shop", "cancel_order", { order_id: "changed" }, undefined,
    nativeCall), /does not match/)
  const result = bridge.invoke("shop", "cancel_order", args, undefined, nativeCall)
  const action = await actions.take()
  assert.equal(action.kind, "invoke")
  await bridge.receive(action.id, { status: "pending" })
  assert.equal(bridge.awaitingReceipt(), true)
  await bridge.settleDecisions([{ invocation_id: action.id, status: "approved" }])
  assert.deepEqual(await actions.take(), action)
  await bridge.receive(action.id, { status: "succeeded", value: { changed: true } })
  assert.deepEqual(await result, { status: "succeeded", value: { changed: true } })
  await assert.rejects(() => bridge.invoke("shop", "cancel_order", args, undefined, nativeCall), /already dispatched/)
  bridge.close()
})

for (const status of ["approved", "allowed"])
  test(`a direct native call's ${status} policy receipt dispatches execution instead of returning success`, async () => {
    const actions = new ActionQueue()
    const bridge = new WorldBridge({}, actions, evidence, () => ({ ...nativeCall, ...owner, args }))
    const result = bridge.invoke("shop", "cancel_order", args, undefined, nativeCall)
    let resolved = false
    void result.then(() => { resolved = true })
    const action = await actions.take()
    await bridge.receive(action.id, { status })
    assert.equal(resolved, false)
    assert.deepEqual(await actions.take(), action)
    const failure = { status: "failed", error: "Temporary shop outage" }
    await bridge.receive(action.id, failure)
    assert.deepEqual(await result, failure)
    bridge.close()
  })

test("a world wait holds its call open until its result receipt arrives", async () => {
  const actions = new ActionQueue()
  const waitArgs = { timeout: 600 }
  const call = { ...nativeCall, arguments_json: JSON.stringify(waitArgs) }
  const lookup = () => ({ ...call, serviceId: "system", operation: "wait_for_notification", args: waitArgs })
  const bridge = new WorldBridge({}, actions, evidence, lookup)
  const result = bridge.invoke("system", "wait_for_notification", waitArgs, undefined, call)
  let resolved = false
  void result.then(() => { resolved = true })
  const action = await actions.take()
  const waiting = { id: action.id, invocation_id: action.id, status: "waiting", until: 600 }
  await bridge.receive(action.id, waiting)
  await bridge.settleDecisions([waiting])
  assert.equal(resolved, false)
  assert.equal(bridge.awaitingReceipt(), true)
  const settled = {
    id: action.id, invocation_id: action.id, status: "succeeded",
    value: { timed_out: false, notifications: [{ kind: "chat_message" }] }
  }
  await bridge.settleDecisions([waiting, settled])
  assert.deepEqual(await result, settled)
  assert.equal(bridge.awaitingReceipt(), false)
  bridge.close()
})
