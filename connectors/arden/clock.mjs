const configured = process.env.OVERNIGHT_NATIVE_TIME_MS
if (configured !== undefined) {
  const MachineDate = globalThis.Date
  let now = Number(configured)
  if (!Number.isSafeInteger(now)) throw new Error("Invalid native evaluation time")
  function WorldDate(...args) {
    if (new.target === undefined) return new MachineDate(now).toString()
    return Reflect.construct(MachineDate, args.length === 0 ? [now] : args, new.target)
  }
  Object.setPrototypeOf(WorldDate, MachineDate)
  WorldDate.prototype = MachineDate.prototype
  WorldDate.now = () => now
  globalThis.Date = WorldDate
  process.on("message", message => {
    if (!Number.isSafeInteger(message?.overnightClockMs)) return
    const requested = message.overnightClockMs
    if (requested < now) {
      process.send?.({ overnightClockMs: requested, ok: false, error: "Native world clock cannot move backwards" })
      return
    }
    now = requested
    process.send?.({ overnightClockMs: now })
  })
}
