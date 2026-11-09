import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"
import { ardenProvenance, writeArdenProvenance } from "./provenance.mjs"

const fakeArden = (root) => {
  const server = join(root, "apps/server-ts")
  const headless = join(root, "libs/headless")
  const sdk = join(server, "node_modules/@modelcontextprotocol/sdk")
  mkdirSync(join(server, "dist"), { recursive: true })
  mkdirSync(join(headless, "dist"), { recursive: true })
  mkdirSync(join(sdk, "dist/esm"), { recursive: true })
  writeFileSync(join(server, "dist/index.js"), "export const build = 1\n")
  writeFileSync(join(server, "package.json"), '{"version":"1.2.3"}')
  writeFileSync(join(headless, "dist/index.js"), "export {}\n")
  writeFileSync(join(headless, "package.json"), '{"version":"1.2.3"}')
  writeFileSync(join(sdk, "dist/esm/index.js"), "export {}\n")
  writeFileSync(join(sdk, "package.json"), '{"version":"1.0.0"}')
  writeFileSync(join(root, "package.json"), "{}")
  writeFileSync(join(root, "bun.lock"), "lock")
  return { server, sdk }
}

test("provenance identifies changed build bytes and writes a private credential-free sidecar", () => {
  const root = mkdtempSync(join(tmpdir(), "overnight-provenance-"))
  try {
    const { server, sdk } = fakeArden(root)
    const config = { repo: root, model: "codex/example", credential: "TOP_SECRET" }
    const first = ardenProvenance(config)
    assert.equal(first.implementation, "arden")
    assert.equal(first.runtime.version, "1.2.3")
    assert.deepEqual(first.model, {
      name: "codex/example",
      settings: {},
      uses_runtime_defaults: true
    })
    writeFileSync(join(server, "dist/index.js"), "export const build = 2\n")
    const second = ardenProvenance(config)
    assert.notEqual(second.runtime.sha256, first.runtime.sha256)
    writeFileSync(join(sdk, "dist/esm/index.js"), "export const sdk = 2\n")
    assert.notEqual(ardenProvenance(config).runtime.sha256, second.runtime.sha256)

    const path = join(root, "provenance.json")
    writeArdenProvenance(path, config)
    assert.equal(statSync(path).mode & 0o777, 0o600)
    assert.equal(readFileSync(path, "utf8").includes("TOP_SECRET"), false)
    assert.throws(() => writeArdenProvenance(path, config), { code: "EEXIST" })
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("provenance marks a dirty runtime checkout and follows symlinked build files", () => {
  const root = mkdtempSync(join(tmpdir(), "overnight-provenance-"))
  try {
    const { server } = fakeArden(root)
    const git = (...args) =>
      execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@example.test", ...args], {
        cwd: root, stdio: "ignore"
      })
    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "x")
    const config = { repo: root, model: "codex/example" }
    const clean = ardenProvenance(config).runtime.git_commit
    assert.match(clean, /^[0-9a-f]{40}$/)
    writeFileSync(join(server, "dist/index.js"), "export const build = 2\n")
    assert.equal(ardenProvenance(config).runtime.git_commit, `${clean}-dirty`)

    const target = join(root, "outside.js")
    writeFileSync(target, "export const linked = 1\n")
    symlinkSync(target, join(server, "dist/linked.js"))
    const first = ardenProvenance(config).runtime.sha256
    writeFileSync(target, "export const linked = 2\n")
    assert.notEqual(ardenProvenance(config).runtime.sha256, first)

    symlinkSync(join(root, "missing.js"), join(server, "dist/dangling.js"))
    assert.throws(() => ardenProvenance(config), /dangling symlink/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
