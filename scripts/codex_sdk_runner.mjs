import { Codex } from "@openai/codex-sdk";

function emit(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

function parseRequest() {
  const raw = process.argv[2];
  if (!raw) {
    throw new Error("missing JSON request argument");
  }
  if (raw === "-") {
    return new Promise((resolve, reject) => {
      let body = "";
      process.stdin.setEncoding("utf8");
      process.stdin.on("data", (chunk) => {
        body += chunk;
      });
      process.stdin.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
      process.stdin.on("error", reject);
    });
  }
  return JSON.parse(raw);
}

async function main() {
  const req = await parseRequest();
  const codex = new Codex();
  const threadOptions = {
    workingDirectory: req.cwd,
    skipGitRepoCheck: req.skipGitRepoCheck ?? true,
  };

  if (req.model) {
    threadOptions.model = req.model;
  }
  if (req.sandbox) {
    threadOptions.sandboxMode = req.sandbox;
  }
  if (req.approvalPolicy) {
    threadOptions.approvalPolicy = req.approvalPolicy;
  }

  const thread = req.resumeId
    ? codex.resumeThread(req.resumeId, threadOptions)
    : codex.startThread(threadOptions);

  const { events } = await thread.runStreamed(req.prompt);
  for await (const event of events) {
    emit(event);
  }
}

main().catch((error) => {
  emit({
    type: "error",
    message: error?.stack || error?.message || String(error),
  });
  process.exitCode = 1;
});
