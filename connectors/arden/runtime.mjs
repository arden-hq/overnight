import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"

export const makeRuntime = async (repo) => {
  const auth = JSON.parse(readFileSync(join(homedir(), ".codex", "auth.json"), "utf8"))
  const accessToken = auth?.tokens?.access_token
  if (typeof accessToken !== "string" || accessToken.length === 0)
    throw new Error("Codex login has no access token")
  const claims = JSON.parse(Buffer.from(accessToken.split(".")[1] ?? "", "base64url").toString())
  if (typeof claims.exp === "number" && claims.exp * 1000 < Date.now() + 10 * 60_000)
    throw new Error("Codex access token expires too soon; refresh the Codex login")
  const accountId = auth?.tokens?.account_id
  const credential = {
    accessToken,
    ...(typeof accountId === "string" ? { headers: { "chatgpt-account-id": accountId } } : {})
  }
  const module = await import(
    pathToFileURL(join(repo, "apps/server-ts/dist/index.js"))
  )
  const runtime = await module.makeOpenAiCodexRuntime({
    credentials: { get: async () => credential },
    initialCredential: credential,
    appVersion: "overnight-eval"
  })
  if (runtime === null) throw new Error("Codex login was rejected")
  return runtime
}
