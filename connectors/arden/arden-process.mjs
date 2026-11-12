import { fork } from "node:child_process"
import { appendFileSync, chmodSync, closeSync, existsSync, mkdirSync, openSync } from "node:fs"
import { join, resolve } from "node:path"
import { setTimeout as sleep } from "node:timers/promises"
import { pathToFileURL } from "node:url"

export const trialConfig = (env = process.env) => {
  if (!env.OVERNIGHT_ARDEN_REPO || !env.OVERNIGHT_ARDEN_RUN_DIR)
    throw new Error("OVERNIGHT_ARDEN_REPO and OVERNIGHT_ARDEN_RUN_DIR are required")
  const repo = resolve(env.OVERNIGHT_ARDEN_REPO)
  const baseRunDir = resolve(env.OVERNIGHT_ARDEN_RUN_DIR)
  const namespace = env.OVERNIGHT_CHAT_NAMESPACE
  const chatId = env.OVERNIGHT_CHAT_ID
  if ((namespace === undefined) !== (chatId === undefined))
    throw new Error("Episode chat identity and namespace must be supplied together")
  if (namespace !== undefined && (!/^(?:[a-f0-9]{2})+$/.test(namespace) || !chatId))
    throw new Error("Invalid episode chat namespace")
  const runDir = namespace === undefined ? baseRunDir : join(baseRunDir, "chats", namespace)
  if (!existsSync(join(repo, "apps/server-ts/dist/index.js")))
    throw new Error("Build the Arden server before this trial")
  const databasePath = join(runDir, "arden.db")
  if (existsSync(databasePath)) throw new Error("Trial Arden database already exists")
  mkdirSync(runDir, { recursive: true, mode: 0o700 })
  chmodSync(runDir, 0o700)
  return {
    repo,
    runDir,
    databasePath,
    chatId,
    model: env.OVERNIGHT_ARDEN_MODEL ?? "openai-codex/gpt-6-luna",
    epochMs: Math.floor(Date.now() / 1000) * 1000
  }
}

export class NativeEvidence {
  constructor(runDir) {
    this.fd = openSync(join(runDir, "arden-native.jsonl"), "a", 0o600)
  }
  write(kind, value = {}) {
    appendFileSync(this.fd, JSON.stringify({ kind, ...value }) + "\n")
  }
  close() {
    if (this.fd === undefined) return
    closeSync(this.fd)
    this.fd = undefined
  }
}

const OWN_PROCESS_GROUP = { detached: true }

const signalProcessGroup = (pid, signal) => {
  if (!pid) return
  try {
    process.kill(-pid, signal)
  } catch (error) {
    if (error.code !== "ESRCH") throw error
  }
}

export class ArdenProcess {
  constructor(config, evidence) {
    this.config = config
    this.evidence = evidence
    this.commandNumber = 0
    this.pending = new Map()
    this.onParentExit = () => this.killHostAndItsDescendants()
  }

  killHostAndItsDescendants() {
    signalProcessGroup(this.child?.pid, "SIGKILL")
  }

  async start(time) {
    const { repo, runDir, databasePath, epochMs } = this.config
    const serverLog = openSync(join(runDir, "server.log"), "a", 0o600)
    try {
      this.child = fork(join(import.meta.dirname, "arden-host.mjs"), [], {
      execArgv: ["--import", pathToFileURL(join(import.meta.dirname, "clock.mjs")).href],
      env: {
        PATH: process.env.PATH,
        HOME: process.env.HOME,
        TZ: "UTC",
        ARDEN_DIR: runDir,
        OVERNIGHT_ARDEN_REPO: repo,
        OVERNIGHT_NATIVE_TIME_MS: String(epochMs + time * 1000)
      },
      stdio: ["ignore", serverLog, serverLog, "ipc"],
      ...OWN_PROCESS_GROUP
      })
    } finally {
      closeSync(serverLog)
    }
    process.once("exit", this.onParentExit)
    // Node: a failed spawn emits error + close without exit, so close is the lifetime end.
    this.exited = new Promise((resolveExit) =>
      this.child.once("close", () => {
        this.closed = true
        resolveExit()
      })
    )
    this.child.on("message", (message) => this.receive(message))
    this.child.once("error", (error) => this.fail(error))
    this.child.once("exit", (code, signal) =>
      this.fail(new Error(`Arden host exited: ${code ?? signal}`))
    )
    await this.waitFor("ready", 20_000)
    const started = await this.send({
      op: "start",
      options: { port: 0, databasePath, sharedSkillRoots: [] },
      runtime: {
        module: join(import.meta.dirname, "runtime.mjs"),
        export: "makeRuntime",
        args: [repo]
      }
    })
    this.base = new URL(started.url)
    const { HeadlessClient } = await import(
      pathToFileURL(join(repo, "libs/headless/dist/index.js"))
    )
    this.client = new HeadlessClient({ baseUrl: this.base })
  }

  receive(message) {
    let key = message?.id
    if (message?.op === "ready") key = "ready"
    else if (message?.overnightClockMs !== undefined)
      key = `clock:${message.overnightClockMs}`
    const pending = this.pending.get(key)
    if (!pending) return
    this.pending.delete(key)
    clearTimeout(pending.timer)
    if (message.ok === false) pending.reject(new Error(message.error))
    else pending.resolve(message)
  }

  fail(error) {
    this.failure = error
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(error)
    }
    this.pending.clear()
  }

  check() {
    if (this.failure) throw this.failure
  }

  waitFor(key, timeoutMs) {
    this.check()
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(key)
        reject(new Error(`Arden host timed out: ${key}`))
      }, timeoutMs)
      this.pending.set(key, { resolve, reject, timer })
    })
  }

  send(message, timeoutMs = 120_000) {
    const id = `bridge-${++this.commandNumber}`
    const key =
      message.overnightClockMs === undefined ? id : `clock:${message.overnightClockMs}`
    const answer = this.waitFor(key, timeoutMs)
    this.child.send({ id, ...message }, (error) => {
      if (error) this.fail(error)
    })
    return answer
  }

  async moveClock(time) {
    const target = this.config.epochMs + time * 1000
    const answer = await this.send({ overnightClockMs: target }, 20_000)
    this.evidence.write("clock", { time, native_time_ms: answer.overnightClockMs })
  }

  async request(method, path, body) {
    this.check()
    const response = await fetch(new URL(path, this.base), {
      method,
      headers: { "content-type": "application/json" },
      signal: AbortSignal.timeout(120_000),
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    })
    const text = await response.text()
    if (!response.ok) throw new Error(`${method} ${path}: ${response.status} ${text.slice(0, 400)}`)
    return text ? JSON.parse(text) : undefined
  }

  async close() {
    process.off("exit", this.onParentExit)
    if (!this.child) return
    signalProcessGroup(this.child.pid, "SIGTERM")
    await Promise.race([this.exited, sleep(5_000, undefined, { ref: false })])
    this.killHostAndItsDescendants()
    if (!this.closed) {
      await Promise.race([this.exited, sleep(1_000, undefined, { ref: false })])
      if (!this.closed) throw new Error("Arden host did not close after SIGKILL")
    }
  }
}
