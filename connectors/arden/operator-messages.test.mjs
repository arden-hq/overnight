import assert from "node:assert/strict"
import { test } from "node:test"
import { OperatorMessages } from "./operator-messages.mjs"

test("native operator input includes filtered world context without replaying old messages or case secrets", async () => {
  const sent = []
  const native = {
    client: { submit: async input => { sent.push(input); return { runId: "run" } } },
    request: async () => ({ questions: [] })
  }
  const messages = new OperatorMessages(native, "chat", 0, { write() {} }, ["mcp_mail_read"])
  const old = { message: "private prior codeword" }
  await messages.submit([old], 0, { world: {}, messages: [old] })
  const caseData = {
    checks: [{ secret: "private evaluator criterion" }],
    observation: {
      time: 10, operations: { mail: [{ name: "read" }] },
      world: { mail: { source: { subject: "visible source" } } },
      messages: [old, { message: "Read the source." }]
    }
  }
  await messages.submit(caseData.observation.messages, 10, caseData.observation)
  assert.match(sent[0].message, /mcp_mail_read/)
  assert.match(sent[1].message, /visible source/)
  assert.match(sent[1].message, /"operations"/)
  assert.doesNotMatch(sent[1].message, /private prior codeword|private evaluator criterion/)
  assert.equal(sent.length, 2)
})


test("questions in worker-owned chats belong to the isolated trial operator", async () => {
  const requests = []
  const native = {
    client: { submit: async () => ({ runId: "initial" }) },
    request: async (method, path, body) => {
      requests.push({ method, path, body })
      return method === "GET" ? { questions: [
        { question_id: "worker-question", run_id: "worker-run", session_id: "worker-uuid", destination_session_id: "own-worker-chat" }
      ] } : { continuation: { run_id: "continued-worker" } }
    }
  }
  const messages = new OperatorMessages(native, "chat", 0, { write() {} })
  await messages.submit([{ message: "initial" }, { message: "approved" }], 1200, { world: { large: "x".repeat(9000) } })
  assert.equal(requests[1].path, "/chat/questions/worker-question/answer")
  assert.deepEqual(requests[1].body.answer, { kind: "text", text: "approved" })
  assert.equal(messages.pendingRuns.has("continued-worker"), true)
})

test("multiple actionable native questions require explicit routing", async () => {
  const native = {
    client: { submit: async () => ({ runId: "initial" }) },
    request: async () => ({ questions: [{ question_id: "one" }, { question_id: "two" }] })
  }
  const messages = new OperatorMessages(native, "chat", 0, { write() {} })
  await assert.rejects(messages.submit([{ message: "initial" }, { message: "approved" }], 1200), /explicit routing/)
})

test("each new actionable native question becomes one report with its prompt, detail and choices", async () => {
  const written = []
  const question = {
    question_id: "q1", run_id: "run",
    content: { prompt: "Book it for Sunday 8am?", detail: null, choices: [{ id: "y", label: "Yes" }, { id: "n", label: "No" }] }
  }
  const native = { request: async () => ({ questions: [question] }) }
  const messages = new OperatorMessages(native, "chat", 0, { write: (kind, value) => written.push([kind, value]) })
  assert.deepEqual(await messages.newQuestionReports(5), ["Book it for Sunday 8am?\n\nChoices: Yes; No"])
  assert.deepEqual(await messages.newQuestionReports(6), [])
  assert.deepEqual(written, [["native_question_reported", { question_id: "q1", asking_run_id: "run", time: 5 }]])
})
