import { join } from "node:path"
import { pathToFileURL } from "node:url"

const repo = process.env.OVERNIGHT_ARDEN_REPO
if (!repo) throw new Error("OVERNIGHT_ARDEN_REPO is required")
const { startServer } = await import(pathToFileURL(join(repo, "apps/server-ts/dist/index.js")).href)

let runtime
let server

const documents = new Map()
const transportSecrets = new Map()
const transportSecretKey = (target) => JSON.stringify([target.secretKey, target.binding])
const nothing = async () => null
const vault = {
  readDocument: async (target) => documents.get(target.documentKey) ?? null,
  writeDocument: async (target, value) => void documents.set(target.documentKey, value),
  clearDocument: async (target) => void documents.delete(target.documentKey),
  read: nothing,
  write: async () => {},
  clear: async () => {},
  resolveClient: nothing,
  readTransportSecrets: async (target) => transportSecrets.get(transportSecretKey(target)) ?? null,
  writeTransportSecrets: async (target, secrets) => {
    transportSecrets.set(transportSecretKey(target), secrets)
  },
  clearTransportSecrets: async (target) => {
    transportSecrets.delete(transportSecretKey(target))
  }
}

const handle = async (message) => {
  switch (message.op) {
    case "start": {
      const spec = message.runtime
      runtime ??= await (await import(pathToFileURL(spec.module).href))[spec.export](...spec.args)
      server = await startServer({
        ...message.options,
        credentialVault: vault,
        provider: runtime.provider,
        modelHost: runtime.modelHost
      })
      return { url: server.url.href }
    }
    case "stop":
      await server?.stop()
      server = undefined
      return {}
    default:
      throw new Error(`Unknown host message ${message.op}`)
  }
}

process.on("message", (message) => {
  if (message?.op === undefined) return
  handle(message).then(
    (result) => process.send({ id: message.id, ok: true, ...result }),
    (error) => process.send({ id: message.id, ok: false, error: String(error?.stack ?? error) })
  )
})
process.send({ op: "ready" })
