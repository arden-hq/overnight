import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { resolve } from "node:path"

const root = resolve(import.meta.dirname, "../..")

const venv = resolve(root, ".venv/bin/overnight")
const overnight = existsSync(venv) ? venv : "overnight"
const api = spawn(overnight, ["serve"], { stdio: "inherit", cwd: root })
const web = spawn("npm", ["run", "dev:web"], { stdio: "inherit" })
const children = [api, web]

api.once("error", (err) => {
  console.error(`[dev] could not start \`overnight serve\` (${err.message}); is \`overnight\` installed or the repository venv synced? The UI will show "could not reach service".`)
})
api.once("exit", (code) => {
  if (code) console.error(`[dev] \`overnight serve\` exited with ${code} (the serve command may not exist yet). Vite keeps running.`)
})

let stopping = false
const stop = (signal = "SIGTERM") => {
  if (stopping) return
  stopping = true
  for (const child of children) child.kill(signal)
}

web.once("exit", (code) => {
  stop()
  if (code && code !== 143) process.exitCode = code
})
process.once("SIGINT", () => stop("SIGINT"))
process.once("SIGTERM", () => stop("SIGTERM"))
