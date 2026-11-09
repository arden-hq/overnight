import { appendFileSync, statSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export default async function (pi) {
  const config = JSON.parse(await readFile(process.env.OVERNIGHT_PI_TOOLS, "utf8"));
  const clients = [];
  // pi-acp starts one pi process per ACP session (new, restored or loaded), so the budget is a
  // byte-per-request file shared by every process of this run rather than a process counter.
  const countRequest = () => {
    appendFileSync(config.request_counter, "x");
    return statSync(config.request_counter).size;
  };
  pi.on("before_provider_request", (event) => {
    const requests = countRequest();
    if (requests > Number(process.env.OVERNIGHT_PI_MAX_REQUESTS) ||
        Buffer.byteLength(JSON.stringify(event.payload)) > Number(process.env.OVERNIGHT_PI_MAX_BYTES)) {
      process.stderr.write("Overnight model request limit reached\n");
      process.exit(1);
    }
  });
  pi.on("after_provider_response", (event) => {
    if (event.status >= 400) {
      process.stderr.write(`Overnight provider response: HTTP ${event.status}\n`);
      process.exit(1);
    }
  });
  for (const [service, operations] of Object.entries(config.operations)) {
    const client = new Client({ name: "overnight-pi", version: "1" });
    await client.connect(new StreamableHTTPClientTransport(new URL(`${config.endpoint}/${service}`), {
      requestInit: { headers: { Authorization: `Bearer ${config.token}` } },
    }));
    clients.push(client);
    const listing = await client.listTools();
    for (const operation of operations) {
      const tool = listing.tools.find((item) => item.name === operation.name);
      if (!tool) throw new Error(`Missing MCP tool: ${service}/${operation.name}`);
      pi.registerTool({
        name: `${service}__${tool.name}`,
        label: `${service}/${tool.name}`,
        executionMode: "sequential",
        description: tool.description,
        parameters: tool.inputSchema,
        async execute(toolCallId, args, signal, _onUpdate, context) {
          const result = await client.callTool({
            name: tool.name,
            arguments: args,
            _meta: { "arden/execution": {
              run_id: context.sessionManager.getSessionId(),
              tool_call_id: toolCallId,
              chat_id: config.chat_id,
            } },
          }, undefined, { signal });
          if (result.isError) {
            throw new Error(result.content.filter((block) => block.type === "text").map((block) => block.text).join("\n"));
          }
          return { content: result.content, details: { isError: result.isError ?? false } };
        },
      });
    }
  }
  await writeFile(join(config.binding_receipts, `${process.pid}.json`), JSON.stringify({
    pid: process.pid,
    tools: Object.entries(config.operations).flatMap(([service, operations]) =>
      operations.map((operation) => `${service}__${operation.name}`)),
  }), { mode: 0o600, flag: "wx" });
  pi.on("session_shutdown", async () => {
    await Promise.all(clients.map((client) => client.close()));
  });
}
