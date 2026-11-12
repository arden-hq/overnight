import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from "node:fs"
import { dirname, join, relative, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { mcpSdkRoot } from "./mcp-services.mjs"

const connectorDir = dirname(fileURLToPath(import.meta.url))
const overnightRepo = resolve(connectorDir, "../..")

const git = (repo, args) =>
  execFileSync("git", args, { cwd: repo, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] })

const gitCommit = (repo) => {
  try {
    const commit = git(repo, ["rev-parse", "HEAD"]).trim()
    return git(repo, ["status", "--porcelain"]).trim() ? `${commit}-dirty` : commit
  } catch {
    return null
  }
}

const filesUnder = (directory, include, ancestors = []) => {
  if (!existsSync(directory)) return []
  const real = realpathSync(directory)
  if (ancestors.includes(real)) throw new Error(`Provenance symlink cycle at ${directory}`)
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    let kind = entry
    if (entry.isSymbolicLink()) {
      try {
        kind = statSync(path)
      } catch {
        throw new Error(`Provenance cannot follow dangling symlink ${path}`)
      }
    }
    if (kind.isDirectory()) return filesUnder(path, include, [...ancestors, real])
    return kind.isFile() && include(entry.name) ? [path] : []
  })
}

const hashFiles = (root, paths) => {
  const hash = createHash("sha256")
  for (const path of paths.sort((a, b) => {
    const first = relative(root, a)
    const second = relative(root, b)
    return first < second ? -1 : first > second ? 1 : 0
  })) {
    const name = relative(root, path)
    const bytes = readFileSync(path)
    hash.update(`${Buffer.byteLength(name)}:${name}${bytes.length}:`)
    hash.update(bytes)
  }
  return hash.digest("hex")
}

const hashedConnectorFiles = () =>
  filesUnder(connectorDir, (name) => name.endsWith(".mjs") && !name.endsWith(".test.mjs"))

const hashedRuntimeFiles = (repo) => {
  const server = join(repo, "apps/server-ts")
  const libs = join(repo, "libs")
  const sdk = mcpSdkRoot(repo)
  const built = (name) => name.endsWith(".js") || name.endsWith(".json")
  const libraryDist = readdirSync(libs)
    .filter((name) => statSync(join(libs, name)).isDirectory())
    .flatMap((name) => filesUnder(join(libs, name, "dist"), built))
  const required = [
    join(server, "package.json"),
    join(repo, "libs/headless/package.json"),
    join(sdk, "package.json"),
    join(repo, "package.json"),
    join(repo, "bun.lock")
  ]
  for (const path of required) {
    if (!existsSync(path)) throw new Error(`Arden runtime fingerprint is missing ${relative(repo, path)}`)
  }
  const sdkModules = filesUnder(join(sdk, "dist/esm"), built)
  if (sdkModules.length === 0) throw new Error("Arden MCP SDK executable build is missing")
  return [...filesUnder(join(server, "dist"), built), ...libraryDist, ...sdkModules, ...required]
}

const overnightVersion = () =>
  readFileSync(join(overnightRepo, "pyproject.toml"), "utf8").match(/^version = "([^"]+)"/m)?.[1] ?? null

export const ardenProvenance = (config) => ({
  implementation: "arden",
  code: {
    git_commit: gitCommit(overnightRepo),
    sha256: hashFiles(overnightRepo, hashedConnectorFiles()),
    version: overnightVersion()
  },
  runtime: {
    git_commit: gitCommit(config.repo),
    sha256: hashFiles(config.repo, hashedRuntimeFiles(config.repo)),
    version: JSON.parse(readFileSync(join(config.repo, "apps/server-ts/package.json"), "utf8"))
      .version ?? null
  },
  model: {
    name: config.model,
    settings: {},
    uses_runtime_defaults: true
  }
})

export const writeArdenProvenance = (path, config) => {
  if (!path) return
  writeFileSync(path, JSON.stringify(ardenProvenance(config)), { flag: "wx", mode: 0o600 })
}
