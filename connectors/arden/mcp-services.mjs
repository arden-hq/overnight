import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { join, resolve } from "node:path"
import { setTimeout as sleep } from "node:timers/promises"
import { pathToFileURL } from "node:url"

export const mcpSdkRoot = (repo) =>
  join(repo, "apps/server-ts/node_modules/@modelcontextprotocol/sdk")

export class McpServices {
  constructor(repo, operations, bridge, evidence) {
    this.repo = repo
    this.operations = operations
    this.bridge = bridge
    this.evidence = evidence
    this.toolOwners = new Map()
  }

  async listen() {
    if (!process.env.OVERNIGHT_PYTHON) throw new Error("OVERNIGHT_PYTHON must name the runner interpreter")
    this.child = spawn(process.env.OVERNIGHT_PYTHON, ["-m", "overnight.execution.tool_sidecar"], {
      cwd: resolve(import.meta.dirname, "../.."),
      stdio: ["pipe", "pipe", "inherit"]
    })
    this.exited = new Promise((resolveExit) => this.child.once("close", resolveExit))
    const ready = new Promise((resolveReady, reject) => {
      this.child.once("error", reject)
      this.child.once("close", (code) => {
        this.failure = new Error(`World MCP sidecar exited: ${code}`)
        reject(this.failure)
      })
      this.lines = createInterface({ input: this.child.stdout })
      this.lines.on("line", (line) => {
        try {
          const message = JSON.parse(line)
          if (message.kind === "ready") {
            this.endpoint = message.endpoint
            this.token = message.token
            resolveReady()
          } else if (message.kind === "invoke") void this.dispatch(message)
          else throw new Error(`Unknown MCP sidecar message: ${message.kind}`)
        } catch (error) {
          this.failure = error
          reject(error)
        }
      })
    })
    this.child.stdin.on("error", (error) => { this.failure = error })
    this.child.stdin.write(JSON.stringify({
      kind: "setup", operations: this.operations, identity_required: true
    }) + "\n")
    await Promise.race([
      ready,
      sleep(20_000, undefined, { ref: false }).then(() => { throw new Error("World MCP sidecar startup timed out") })
    ])
  }

  async dispatch(message) {
    try {
      const receipt = await this.bridge.invoke(
        message.service, message.operation, message.arguments, undefined, message.context
      )
      this.child.stdin.write(JSON.stringify({ kind: "result", id: message.id, result: receipt }) + "\n")
    } catch (error) {
      this.failure = error
      this.child.stdin.write(JSON.stringify({
        kind: "result", id: message.id,
        result: { status: "failed", error: String(error?.message ?? error) }
      }) + "\n")
    }
  }

  check() {
    if (this.failure) throw this.failure
  }

  async register(arden) {
    const names = Object.keys(this.operations)
    for (const name of names) {
      await arden.request("POST", "/mcp/servers", {
        name,
        transport: {
          type: "http",
          url: `${this.endpoint}/${encodeURIComponent(name)}`,
          auth: "headers",
          headers: { set: { Authorization: `Bearer ${this.token}` }, retain: [] }
        }
      })
    }
    const { mcpToolName } = await import(
      pathToFileURL(join(this.repo, "apps/server-ts/dist/index.js"))
    )
    const expected = new Map()
    for (const [serviceId, operations] of Object.entries(this.operations)) {
      for (const operation of operations) {
        const name = mcpToolName(serviceId, operation.name)
        if (expected.has(name)) throw new Error(`Duplicate Arden trial MCP tool: ${name}`)
        expected.set(name, { serviceId, operation: operation.name })
      }
    }
    const owned = await this.waitForCatalog(arden, names, expected)
    this.toolOwners = expected
    const overrides = Object.fromEntries([...expected.keys()].map((name) => [name, "ask"]))
    await arden.request("PATCH", "/config", { tool_overrides: overrides })
    this.evidence.write("catalog", {
      tools: owned.map((tool) => ({ name: tool.name, source: tool.source })),
      overrides
    })
  }

  async waitForCatalog(arden, names, expected) {
    const deadline = Date.now() + 30_000
    const serviceNames = new Set(names)
    for (;;) {
      const servers = (await arden.request("GET", "/mcp/servers")).servers
      const catalog = (await arden.request("GET", "/tools")).tools
      const owned = catalog.filter((tool) => {
        if (typeof tool.source !== "string" || !tool.source.startsWith("mcp:")) return false
        return serviceNames.has(tool.source.slice(4))
      })
      const missing = [...expected]
        .filter(([name, owner]) =>
          !owned.some((tool) => tool.name === name && tool.source === `mcp:${owner.serviceId}`)
        )
        .map(([name]) => name)
      const unexpected = owned
        .filter((tool) => expected.get(tool.name)?.serviceId !== tool.source.slice(4))
        .map((tool) => tool.name)
      const disconnected = names.filter(
        (name) => !servers.some((entry) => entry.name === name && entry.status === "connected")
      )
      if (
        missing.length === 0 &&
        unexpected.length === 0 &&
        disconnected.length === 0 &&
        owned.length === expected.size
      )
        return owned
      if (Date.now() >= deadline)
        throw new Error(
          `Arden trial MCP catalog not ready; missing: ${missing.join(", ") || "none"}; unexpected: ${unexpected.join(", ") || "none"}; disconnected: ${disconnected.join(", ") || "none"}`
        )
      await sleep(200)
    }
  }

  async close() {
    if (!this.child) return
    this.child.stdin.end()
    const done = await Promise.race([this.exited.then(() => true), sleep(5_000, false, { ref: false })])
    if (!done) {
      this.child.kill("SIGKILL")
      await this.exited
    }
    this.lines?.close()
  }
}
