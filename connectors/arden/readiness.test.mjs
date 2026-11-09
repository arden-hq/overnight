import assert from "node:assert/strict"
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"
import { DatabaseSync } from "node:sqlite"
import { McpServices } from "./mcp-services.mjs"
import { ArdenTrial } from "./trial.mjs"
import { trialConfig } from "./arden-process.mjs"
import { NativeState } from "./native-state.mjs"

const evidence = { write() {} }

for (const state of [
  "operator",
  "scheduler",
  "future-scheduler",
  "busy",
  "approval",
  "native-ask"
]) {
  test(`horizon exhaustion remains an unconstrained wait with ${state} work`, async () => {
    const trial = new ArdenTrial({}, evidence)
    trial.time = 10
    trial.arden = { check() {} }
    trial.pollNative = async () => {}
    trial.actions = { items: [], take: async () => undefined }
    trial.bridge = {
      calls: new Map(),
      awaitingReceipt: () => state === "operator",
      hasNativeApprovals: () => state === "approval"
    }
    trial.native = {
      nextWake: () => (state === "scheduler" ? 10 : state === "future-scheduler" ? 20 : undefined),
      busy: () => state === "busy",
      hasPendingDeliveries: () => false, hasPendingQuestions: () => state === "native-ask",
      hasPendingAsks: () => state === "native-ask"
    }
    assert.deepEqual(await trial.nextAction(10), { kind: "wait" })
    assert.deepEqual(trial.status(), {
      busy: state === "scheduler" || state === "busy",
      pending: true,
      next_wake: state === "future-scheduler" ? 20 : null
    })
  })
}

test("settled native work completes before the horizon", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 10
  trial.arden = { check() {} }
  trial.pollNative = async () => {}
  trial.actions = { take: async () => undefined }
  trial.bridge = { awaitingReceipt: () => false, hasNativeApprovals: () => false }
  trial.native = { nextWake: () => undefined, busy: () => false, hasPendingDeliveries: () => false, hasPendingQuestions: () => false }
  assert.deepEqual(await trial.nextAction(20), { kind: "done" })
})

test("a ready action is delivered at the horizon", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 10
  trial.arden = { check() {} }
  trial.pollNative = async () => {}
  trial.actions = { take: async () => ({ kind: "report", message: "completed" }) }
  assert.deepEqual(await trial.nextAction(10), { kind: "report", message: "completed" })
})

async function services(t, catalog, operations = { shop: [{ name: "read" }, { name: "write" }] }) {
  const repo = await mkdtemp(join(tmpdir(), "overnight-catalog-"))
  t.after(() => rm(repo, { recursive: true, force: true }))
  const dir = join(repo, "apps/server-ts/dist")
  await mkdir(dir, { recursive: true })
  await writeFile(
    join(dir, "index.js"),
    "export const mcpToolName = (service, name) => `native_${service}_${name.toUpperCase()}`\n"
  )
  const mcp = new McpServices(repo, operations, {}, evidence)
  mcp.endpoint = "http://127.0.0.1:1234"
  mcp.token = "test-token"
  const patches = []
  const registrations = []
  const arden = {
    async request(method, path, body) {
      if (method === "POST") { registrations.push(body); return {} }
      if (method === "PATCH") {
        patches.push(body)
        return {}
      }
      if (path === "/mcp/servers")
        return { servers: [{ name: "shop", status: "connected", tools: [{}] }] }
      return { tools: catalog }
    }
  }
  return { mcp, arden, patches, registrations }
}

test("complete expected native tool names own the approval overrides", async (t) => {
  const { mcp, arden, patches, registrations } = await services(t, [
    { name: "native_shop_READ", source: "mcp:shop" },
    { name: "native_shop_WRITE", source: "mcp:shop" },
    { name: "other", source: "builtin" }
  ])
  await mcp.register(arden)
  assert.deepEqual(registrations, [{ name: "shop", transport: {
    type: "http", url: "http://127.0.0.1:1234/shop", auth: "headers",
    headers: { set: { Authorization: "Bearer test-token" }, retain: [] }
  } }])
  assert.deepEqual(
    [...mcp.toolOwners],
    [
      ["native_shop_READ", { serviceId: "shop", operation: "read" }],
      ["native_shop_WRITE", { serviceId: "shop", operation: "write" }]
    ]
  )
  assert.deepEqual(patches, [
    { tool_overrides: { native_shop_READ: "ask", native_shop_WRITE: "ask" } }
  ])
})

test("a partial connected catalog times out with missing names before configuration", async (t) => {
  const { mcp, arden, patches } = await services(t, [
    { name: "native_shop_READ", source: "mcp:shop" }
  ])
  let now = 0
  t.mock.method(Date, "now", () => {
    now += 31_000
    return now
  })
  await assert.rejects(() => mcp.register(arden), /missing: native_shop_WRITE/)
  assert.deepEqual(patches, [])
  assert.equal(mcp.toolOwners.size, 0)
})

test("an unexpected trial tool prevents catalog acceptance", async (t) => {
  const { mcp, arden, patches } = await services(t, [
    { name: "native_shop_READ", source: "mcp:shop" },
    { name: "native_shop_WRITE", source: "mcp:shop" },
    { name: "native_shop_EXTRA", source: "mcp:shop" }
  ])
  let now = 0
  t.mock.method(Date, "now", () => {
    now += 31_000
    return now
  })
  await assert.rejects(() => mcp.register(arden), /unexpected: native_shop_EXTRA/)
  assert.deepEqual(patches, [])
})

for (const operations of [{ shop: [] }, {}]) {
  test(`an empty operation catalog registers without phantom tools (${Object.keys(operations).length} services)`, async (t) => {
    const { mcp, arden, patches } = await services(t, [], operations)
    await mcp.register(arden)
    assert.equal(mcp.toolOwners.size, 0)
    assert.deepEqual(patches, [{ tool_overrides: {} }])
  })
}

for (const [label, resolution, closed, approval, expected] of [
  ["open question", null, null, "denied", true],
  ["answered question", "{}", null, "denied", false],
  ["closed question", null, "2026-10-22", "denied", false],
  ["native approval", "{}", null, "pending", true]
]) {
  test(`native pending asks distinguish ${label}`, async (t) => {
    const directory = await mkdtemp(join(tmpdir(), "overnight-native-asks-"))
    t.after(() => rm(directory, { recursive: true, force: true }))
    const path = join(directory, "native.db")
    const db = new DatabaseSync(path)
    try {
      db.exec(
        "CREATE TABLE run_questions (resolution TEXT, closed_at TEXT); CREATE TABLE tool_approvals (status TEXT)"
      )
      db.prepare("INSERT INTO run_questions VALUES (?, ?)").run(resolution, closed)
      db.prepare("INSERT INTO tool_approvals VALUES (?)").run(approval)
    } finally {
      db.close()
    }
    assert.equal(new NativeState(path, 0).hasPendingAsks(), expected)
  })
}

test("explicit episode namespaces isolate databases without changing single-chat paths", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "overnight-chat-config-"))
  t.after(() => rm(root, { recursive: true, force: true }))
  const dist = join(root, "repo/apps/server-ts/dist")
  await mkdir(dist, { recursive: true })
  await writeFile(join(dist, "index.js"), "")
  const env = { OVERNIGHT_ARDEN_REPO: join(root, "repo"), OVERNIGHT_ARDEN_RUN_DIR: join(root, "run") }
  const single = trialConfig(env)
  assert.equal(single.databasePath, join(root, "run/arden.db"))
  await writeFile(single.databasePath, "untouched")
  const first = trialConfig({ ...env, OVERNIGHT_CHAT_ID: "one", OVERNIGHT_CHAT_NAMESPACE: "a".repeat(24) })
  const second = trialConfig({ ...env, OVERNIGHT_CHAT_ID: "two", OVERNIGHT_CHAT_NAMESPACE: "b".repeat(24) })
  assert.equal(first.databasePath, join(root, "run/chats", "a".repeat(24), "arden.db"))
  assert.equal(second.databasePath, join(root, "run/chats", "b".repeat(24), "arden.db"))
  assert.throws(() => trialConfig(env), /already exists/)
  assert.throws(() => trialConfig({ ...env, OVERNIGHT_CHAT_ID: "bad", OVERNIGHT_CHAT_NAMESPACE: "../escape" }), /Invalid episode/)
})


test("accepted native work remains busy until its terminal evidence is consumed", () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 0
  trial.operator = { pendingRuns: new Set(["accepted-run"]) }
  trial.bridge = { calls: new Map(), awaitingReceipt: () => false, hasNativeApprovals: () => false }
  trial.native = { nextWake: () => undefined, busy: () => false, hasPendingDeliveries: () => false, hasPendingQuestions: () => false }
  assert.deepEqual(trial.status(), { busy: true, pending: true, next_wake: null })
})


test("native operator wait yields to the runner's next event instead of polling simulation seconds", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 675
  trial.arden = { check() {} }
  trial.pollNative = async () => {}
  trial.actions = { items: [], take: async () => undefined }
  trial.bridge = { calls: new Map(), awaitingReceipt: () => false, hasNativeApprovals: () => false }
  trial.native = { nextWake: () => undefined, busy: () => false, hasPendingDeliveries: () => false, hasPendingQuestions: () => true }
  assert.deepEqual(await trial.nextAction(2400), { kind: "wait", until: 2400 })
  assert.deepEqual(trial.status(), { busy: false, pending: true, next_wake: null })
})


test("native approval arriving after polling is admitted before advancing world time", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 1
  trial.arden = { check() {} }
  let polls = 0
  let arrival = false
  trial.pollNative = async () => { polls += 1 }
  trial.actions = { take: async () => {
    if (arrival) return { kind: "request_approval", id: "native-call" }
    arrival = true
    return undefined
  } }
  trial.bridge = { awaitingReceipt: () => false, hasNativeApprovals: () => false }
  trial.native = { nextWake: () => undefined, busy: () => true,
    hasPendingAsks: () => arrival, hasPendingDeliveries: () => false, hasPendingQuestions: () => false }
  assert.deepEqual(await trial.nextAction(1800), { kind: "request_approval", id: "native-call" })
  assert.equal(polls, 2)
})


test("native approvals outside trial authority are denied through the native API", async () => {
  const trial = new ArdenTrial({}, evidence)
  const decisions = []
  const mapped = []
  let pending = [
    { run_id: "run", tool_call_id: "real", tool_name: "calendar_search" },
    { run_id: "run", tool_call_id: "world", tool_name: "mcp_calendar_list" }
  ]
  trial.native = { terminalToolCalls: () => [], pendingApprovals: () => pending,
    completedRuns: () => [], changedAutomations: () => [] }
  trial.mcp = { toolOwners: new Map([["mcp_calendar_list", { serviceId: "calendar", operation: "list_events" }]]) }
  trial.bridge = { retireAuthorizations() {}, requestApproval: row => mapped.push(row) }
  trial.arden = { client: { answerApproval: async decision => {
    decisions.push(decision)
    pending = pending.filter(row => row.tool_call_id !== decision.toolId)
  } } }
  await trial.pollNative()
  await trial.pollNative()
  assert.equal(decisions.length, 1)
  assert.equal(decisions[0].toolId, "real")
  assert.equal(decisions[0].approved, false)
  assert.equal(mapped.length, 2)
  assert.equal(mapped[0].tool_call_id, "world")
})


test("queued native answer delivery settles before an unrelated question advances time", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 1200
  trial.arden = { check() {} }
  trial.pollNative = async () => {}
  let attempts = 0
  trial.actions = { items: [], take: async () => ++attempts === 2 ? { kind: "report", message: "continued" } : undefined }
  trial.bridge = { calls: new Map(), awaitingReceipt: () => false, hasNativeApprovals: () => false }
  trial.native = { nextWake: () => undefined, busy: () => true,
    hasPendingQuestions: () => true, hasPendingDeliveries: () => true }
  assert.equal(trial.status().busy, true)
  assert.deepEqual(await trial.nextAction(1800), { kind: "report", message: "continued" })
})

test("trial cancellation stops and restores independent native chats as well as root", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 5
  trial.sessionId = "root"
  trial.pauseAutomations = async () => [{ id: "schedule" }]
  const requests = []
  trial.arden = { request: async (method, path) => requests.push([method, path]) }
  trial.native = { rows: () => [{ session_id: "root" }, { session_id: "automation-own-chat" }],
    busy: () => false, hasPendingAsks: () => false, nextWake: () => undefined }
  trial.bridge = { close() {} }
  trial.actions = { items: [] }
  trial.operator = { pendingRuns: new Set(["old"]) }
  await trial.cancel()
  assert.deepEqual(requests, [
    ["DELETE", "/sessions/automation-own-chat?stop=true"],
    ["POST", "/sessions/automation-own-chat/restore"],
    ["DELETE", "/sessions/root?stop=true"],
    ["POST", "/sessions/root/restore"]
  ])
  assert.equal(trial.operator.pendingRuns.size, 0)
})


test("a failed run fetch keeps the completed native report for the next poll", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "overnight-reports-"))
  t.after(() => rm(root, { recursive: true, force: true }))
  const databasePath = join(root, "arden.db")
  const db = new DatabaseSync(databasePath)
  db.exec("CREATE TABLE chat_runs (run_id TEXT, status TEXT, error_code TEXT, error_message TEXT, ended_at INTEGER)")
  db.exec("INSERT INTO chat_runs VALUES ('run', 'completed', NULL, NULL, 1)")
  db.close()
  const trial = new ArdenTrial({}, evidence)
  trial.native = new NativeState(databasePath, 0)
  trial.bridge = { retireAuthorizations() {} }
  trial.native.terminalToolCalls = () => []
  trial.native.pendingApprovals = () => []
  trial.native.changedAutomations = () => []
  let attempts = 0
  trial.arden = { client: { getRun: async () => {
    if (++attempts === 1) throw new Error("transient")
    return { result: { text: " done " } }
  } } }
  trial.actions = { items: [], push(action) { this.items.push(action) } }
  await assert.rejects(trial.pollNative(), /transient/)
  await trial.pollNative()
  await trial.pollNative()
  assert.equal(attempts, 2)
  assert.deepEqual(trial.actions.items, [{ kind: "report", message: "done" }])
})

test("an open native question reaches the runner as a report", async () => {
  const trial = new ArdenTrial({}, evidence)
  trial.time = 3
  const pushed = []
  trial.actions = { push: action => pushed.push(action) }
  trial.native = { terminalToolCalls: () => [], pendingApprovals: () => [], completedRuns: () => [],
    changedAutomations: () => [], hasPendingQuestions: () => true }
  trial.bridge = { retireAuthorizations() {} }
  trial.operator = { newQuestionReports: async time => time === 3 ? ["Sunday 8am fine?"] : [] }
  await trial.pollNative()
  assert.deepEqual(pushed, [{ kind: "report", message: "Sunday 8am fine?" }])
})
