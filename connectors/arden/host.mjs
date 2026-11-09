#!/usr/bin/env node
import { createInterface } from "node:readline"
import { NativeEvidence, trialConfig } from "./arden-process.mjs"
import { writeArdenProvenance } from "./provenance.mjs"
import { ArdenTrial } from "./trial.mjs"

const config = trialConfig()
writeArdenProvenance(process.env.OVERNIGHT_PROVENANCE_PATH, config)
const evidence = new NativeEvidence(config.runDir)
const trial = new ArdenTrial(config, evidence)
const signals = new Map([
  ["SIGTERM", 143],
  ["SIGINT", 130],
  ["SIGHUP", 129]
])
let stopping = false
for (const [signal, code] of signals) {
  process.on(signal, async () => {
    if (stopping) return
    stopping = true
    try {
      await trial.close()
    } finally {
      evidence.close()
      process.exit(code)
    }
  })
}

const capabilities = {
  simulated_clock: true, chat_isolation: true, cancel: true, reset_scope: "chat"
}
let negotiated = false
let chatId = config.chatId
let terminal = false

try {
  for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    if (!line.trim()) continue
    const request = JSON.parse(line)
    let reply
    switch (request.kind) {
      case "hello": {
        if (negotiated || request.version !== 1) throw new Error("Unsupported harness handshake")
        for (const key of ["simulated_clock", "chat_isolation", "cancel"])
          if (request.required?.[key] && !capabilities[key]) throw new Error(`Unsupported capability: ${key}`)
        if (request.required?.reset_scope && !["none", "chat"].includes(request.required.reset_scope))
          throw new Error("Arden supports chat reset only")
        negotiated = true
        reply = { kind: "ready", version: 1, capabilities }
        break
      }
      case "observe": {
        if (!negotiated || terminal) throw new Error("Harness session is not active")
        if (typeof request.chat_id !== "string" || !request.chat_id)
          throw new Error("Harness chat identity is required")
        chatId ??= request.chat_id
        if (request.chat_id !== chatId) throw new Error("Arden adapter owns one isolated chat")
        const action = await trial.observe(request.observation)
        reply = { kind: "action", action, status: trial.status() }
        break
      }
      case "cancel":
      case "close":
      case "reset": {
        if (!negotiated || terminal || request.scope !== (request.kind === "reset" ? "chat" : "none") || (chatId && request.chat_id !== chatId))
          throw new Error("Invalid harness lifecycle request")
        if (request.kind === "reset") await trial.reset()
        else if (request.kind === "cancel") await trial.cancel()
        else {
          await trial.close()
          terminal = true
        }
        reply = { kind: request.kind, scope: request.scope }
        break
      }
      default:
        throw new Error("Unknown harness request")
    }
    evidence.write("harness_lifecycle", { request: request.kind, chat_id: request.chat_id, reply })
    process.stdout.write(JSON.stringify(reply) + "\n")
  }
} catch (error) {
  evidence.write("host_error", { message: String(error?.message ?? error) })
  process.stderr.write(`${error?.stack ?? error}\n`)
  process.exitCode = 1
} finally {
  try {
    await trial.close()
  } finally {
    evidence.close()
    process.stdin.destroy()
  }
}
