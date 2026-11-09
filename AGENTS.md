# Overnight agent guide

Read [README.md](README.md) for setup and commands, then the reference docs it links. For harness work, read [harness integration](docs/harness-integration.md) and [adapter authoring](docs/adapter-authoring.md).

## Working rules

- Use Python 3.14 and uv, typed Pydantic boundaries and the existing package owners. Prefer straightforward code and established interfaces.
- Keep world behavior free of external I/O; adapters own transport and persistence. Preserve explicit permissions and evaluator privacy.
- Keep evaluation code, cases and documentation in this repository. Arden owns its runtime hooks.
- Preserve unrelated working changes. Do not edit generated files by hand (for example `ui/src/api/` types; regenerate them with `npm run gen:api`).
- Keep docs as present-tense reference for the current code. Work history, run evidence and review notes stay out of git (use the ignored `.local/`).
- Run checks appropriate to the change ([test scope](docs/test-scope.md)). Live model or native-harness runs require explicit authorization; documentation changes do not imply it. Never infer a pass from configuration or a checked item.
- Before completion, have an independent reviewer check the requested scope against the actual changes and evidence.
- Keep tests focused on owning critical contracts and reproduced failures; use representative shared-schema cases instead of per-field, default, example-inventory or diagnostic-wording tests.
- Prefer GPT-6 Luna for live model tests. Use 6.1 Luna only when the actual provider catalog lists that route; do not infer availability from its name. Label evidence with the model actually used.

## Harnesses and connectors

- Keep one MCP exposure owner (`execution/tool_server.py`) and one runner policy owner. Preserve trusted native-call identity and truthful lifecycle capabilities.
- Native connectors live under `connectors/{arden,feather,pi,prime,opencode}`. ACP entry points export `Factory` objects, not bare prepare functions.
- Native launch and configuration, tool bindings and harness-specific lifecycle/error parsing belong in the external connector. Shared adapters own protocol and dispatch mechanics. Runner, policy, world and evaluator must not branch on harness names or native payloads. Add shared capabilities only for demonstrated transport or contract needs.
- Feather is optional: install its extra and external package, then use `.venv/bin` during live runs so concurrent uv commands do not prune dependencies or editable connectors.
- Steering control IDs are opaque host identifiers. The shared host validates them; native hooks address their bound native session. Never compare or forward host IDs as native IDs. Native steering must reject new-turn fallback. See [active-turn control](docs/adapter-authoring.md#active-turn-control).

## UI rules

- Build every primitive on shadcn (Tailwind v4, Radix base) and write Overnight's components on top. Use other UI libraries as visual reference only; never copy their components. Never ship a native `<select>`; use the shadcn `Select` in `ui/src/components/ui/select.tsx`.
- Use the design tokens: one page gutter (`--gutter`), one timeline label column (`--tl-gutter`), one radius ladder, one shadow ladder for elevation. Do not hand-type offsets.
- Status is a word, or a tinted icon plus a word; never a dot or pip. Avoid `a · b · c` metadata chains; give each fact its own place.
- A selected item gets one state cue, not two. Check the selected state of every variant in a capture before reporting.
- Name CSS classes by component (`.transport-clock`, `.fsm-node.now`) so they cannot collide with each other or with Tailwind utilities.
- Keep layers apart: raising an overlay's z-index raises its children. Make chips opaque and fade with color, never opacity, where something is drawn underneath.
- Never animate a layout property on something that reflows a heavy page; snap it. A transition-disabling class must be removed with a timer after a forced reflow, not on `requestAnimationFrame`. Set the theme attribute before React's first paint.
- Measure alignment from the DOM (`getBoundingClientRect` on edges and pane tops), not by eye. When one element looks off, read its computed styles before guessing.
- A page that leaves half the screen empty is a layout defect; fill it with the data the page is about, not decoration.
