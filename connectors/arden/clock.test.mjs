import assert from "node:assert/strict"
import { fork } from "node:child_process"
import { once } from "node:events"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { setTimeout as sleep } from "node:timers/promises"
import { test } from "node:test"
import { pathToFileURL } from "node:url"

test("native Date is frozen between monotonic world ticks and preserves Date semantics", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "overnight-clock-"))
  t.after(() => rm(root, { recursive: true, force: true }))
  const script = join(root, "probe.mjs")
  await writeFile(script, `
    const sample = () => process.send({ now: Date.now(), constructed: new Date().getTime(),
      explicit: new Date(0).getTime(), instance: new Date() instanceof Date,
      parsed: Date.parse("1970-01-01T00:00:00.000Z"), utc: Date.UTC(1970,0,1),
      callable: typeof Date() });
    process.on("message", message => { if (message.sample) sample() });
    sample();
  `)
  const initial = 1792755094000
  const child = fork(script, [], {
    execArgv: ["--import", pathToFileURL(join(import.meta.dirname, "clock.mjs")).href],
    env: { OVERNIGHT_NATIVE_TIME_MS: String(initial) },
    stdio: ["ignore", "ignore", "ignore", "ipc"]
  })
  t.after(() => child.kill())
  const [first] = await once(child, "message")
  assert.deepEqual(first, { now: initial, constructed: initial, explicit: 0,
    instance: true, parsed: 0, utc: 0, callable: "string" })
  await sleep(20)
  const unchanged = once(child, "message")
  child.send({ sample: true })
  assert.deepEqual((await unchanged)[0], first)
  const advanced = once(child, "message")
  child.send({ overnightClockMs: initial + 1000 })
  assert.deepEqual((await advanced)[0], { overnightClockMs: initial + 1000 })
  const refused = once(child, "message")
  child.send({ overnightClockMs: initial })
  assert.equal((await refused)[0].ok, false)
  const final = once(child, "message")
  child.send({ sample: true })
  assert.equal((await final)[0].now, initial + 1000)
})
