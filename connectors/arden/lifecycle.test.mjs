import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"

const exchange = async (t, messages, endInput = true) => {
  const root = await mkdtemp(join(tmpdir(), "overnight-lifecycle-"))
  t.after(() => rm(root, { recursive: true, force: true }))
  const dist = join(root, "repo/apps/server-ts/dist")
  await mkdir(dist, { recursive: true })
  await writeFile(join(dist, "index.js"), "")
  const child = spawn(process.execPath, [join(import.meta.dirname, "host.mjs")], {
    env: { ...process.env, OVERNIGHT_PROVENANCE_PATH: "",
      OVERNIGHT_ARDEN_REPO: join(root, "repo"), OVERNIGHT_ARDEN_RUN_DIR: join(root, "run") },
    stdio: ["pipe", "pipe", "pipe"]
  })
  t.after(() => child.kill("SIGKILL"))
  let stdout = ""
  let stderr = ""
  child.stdout.on("data", (chunk) => { stdout += chunk })
  child.stderr.on("data", (chunk) => { stderr += chunk })
  const done = new Promise((resolve, reject) => {
    child.once("error", reject)
    child.once("close", (code) => resolve(code))
  })
  const input = messages.map((message) => JSON.stringify(message)).join("\n") + "\n"
  if (endInput) child.stdin.end(input)
  else child.stdin.write(input)
  const code = await done
  return { code, stderr, replies: stdout.trim() ? stdout.trim().split("\n").map(JSON.parse) : [] }
}

const hello = { kind: "hello", version: 1, required: { simulated_clock: true,
  chat_isolation: true, cancel: true, reset_scope: "none" } }

test("Arden v1 lifecycle negotiates truthful capabilities and closes before startup", async (t) => {
  const result = await exchange(t, [hello, { kind: "cancel", scope: "none", chat_id: "default" },
    { kind: "reset", scope: "chat", chat_id: "default" },
    { kind: "cancel", scope: "none", chat_id: "default" },
    { kind: "close", scope: "none", chat_id: "default" }])
  assert.equal(result.code, 0, result.stderr)
  assert.deepEqual(result.replies, [
    { kind: "ready", version: 1, capabilities: { simulated_clock: true,
      chat_isolation: true, cancel: true, reset_scope: "chat" } },
    { kind: "cancel", scope: "none" }, { kind: "reset", scope: "chat" },
    { kind: "cancel", scope: "none" }, { kind: "close", scope: "none" }
  ])
})

test("unsupported external reset refusal closes even while its parent keeps stdin open", { timeout: 10_000 }, async (t) => {
  const result = await exchange(t, [{ ...hello, required: { ...hello.required, reset_scope: "external" } }], false)
  assert.equal(result.code, 1)
  assert.deepEqual(result.replies, [])
  assert.match(result.stderr, /supports chat reset only/)
})

test("SIGHUP shuts the host down like SIGTERM and repeated signals are idempotent", { timeout: 10_000 }, async (t) => {
  const root = await mkdtemp(join(tmpdir(), "overnight-lifecycle-"))
  t.after(() => rm(root, { recursive: true, force: true }))
  await mkdir(join(root, "repo/apps/server-ts/dist"), { recursive: true })
  await writeFile(join(root, "repo/apps/server-ts/dist/index.js"), "")
  const child = spawn(process.execPath, [join(import.meta.dirname, "host.mjs")], {
    env: { ...process.env, OVERNIGHT_PROVENANCE_PATH: "",
      OVERNIGHT_ARDEN_REPO: join(root, "repo"), OVERNIGHT_ARDEN_RUN_DIR: join(root, "run") },
    stdio: ["pipe", "pipe", "pipe"]
  })
  t.after(() => child.kill("SIGKILL"))
  const done = new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })))
  const ready = new Promise((resolve) => child.stdout.once("data", resolve))
  child.stdin.write(JSON.stringify(hello) + "\n")
  await ready
  child.kill("SIGHUP")
  await new Promise((resolve) => setTimeout(resolve, 5))
  child.kill("SIGTERM")
  child.kill("SIGHUP")
  assert.deepEqual(await done, { code: 129, signal: null })
})
