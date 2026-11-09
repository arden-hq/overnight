import assert from "node:assert/strict"
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { setTimeout as sleep } from "node:timers/promises"
import { test } from "node:test"
import { ArdenProcess, NativeEvidence } from "./arden-process.mjs"

const alive = (pid) => {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

test("closing the Arden host kills its whole process group, including grandchildren", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "overnight-process-"))
  const repo = join(root, "repo")
  const runDir = join(root, "run")
  await mkdir(join(repo, "apps/server-ts/dist"), { recursive: true })
  await mkdir(join(repo, "libs/headless/dist"), { recursive: true })
  await mkdir(runDir)
  const home = join(root, "home")
  await mkdir(join(home, ".codex"), { recursive: true })
  const claims = Buffer.from(JSON.stringify({ exp: 4102444800 })).toString("base64url")
  await writeFile(join(home, ".codex", "auth.json"), JSON.stringify({ tokens: { access_token: `x.${claims}.y` } }))
  const realHome = process.env.HOME
  process.env.HOME = home
  await writeFile(join(repo, "apps/server-ts/dist/package.json"), '{"type":"module"}')
  await writeFile(join(repo, "apps/server-ts/dist/index.js"), `
    import { spawn } from "node:child_process"
    import { writeFileSync } from "node:fs"
    import { join } from "node:path"
    export const makeOpenAiCodexRuntime = async () => ({ provider: {}, modelHost: {} })
    export const startServer = async () => {
      const grandchild = spawn("sleep", ["60"], { stdio: "ignore" })
      writeFileSync(join(process.env.ARDEN_DIR, "grandchild.pid"), String(grandchild.pid))
      return { url: new URL("http://127.0.0.1:1"), stop: async () => {} }
    }
  `)
  await writeFile(join(repo, "libs/headless/dist/package.json"), '{"type":"module"}')
  await writeFile(join(repo, "libs/headless/dist/index.js"), "export class HeadlessClient {}\n")
  const evidence = new NativeEvidence(runDir)
  const arden = new ArdenProcess(
    { repo, runDir, databasePath: join(runDir, "arden.db"), epochMs: 0 }, evidence
  )
  let grandchild
  t.after(async () => {
    process.env.HOME = realHome
    if (grandchild && alive(grandchild)) process.kill(grandchild, "SIGKILL")
    evidence.close()
    await rm(root, { recursive: true, force: true })
  })
  await arden.start(0)
  grandchild = Number(await readFile(join(runDir, "grandchild.pid"), "utf8"))
  assert.equal(alive(grandchild), true)
  await arden.close()
  await arden.close()
  for (let attempt = 0; attempt < 40 && alive(grandchild); attempt++) await sleep(50)
  assert.equal(alive(grandchild), false)
})
