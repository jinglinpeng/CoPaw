const readline = require("node:readline");

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  let result;
  switch (request.method) {
    case "initialize":
      result = {
        protocolVersion: request.params.protocolVersion,
        capabilities: { tools: {} },
        serverInfo: { name: "artifact-node", version: "1.0.0" },
      };
      break;
    case "tools/list":
      result = {
        tools: [{
          name: "artifact_echo",
          description: "Echo test data",
          inputSchema: {
            type: "object",
            properties: { text: { type: "string" } },
            required: ["text"],
          },
        }],
      };
      break;
    case "tools/call":
      result = {
        content: [{ type: "text", text: request.params.arguments.text }],
        isError: false,
      };
      break;
    default:
      result = {};
  }
  console.log(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }));
});
