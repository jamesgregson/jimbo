// Runs the tests in a VS Code it downloads (Insiders, so it can run while VS Code itself is open).

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  delete process.env.ELECTRON_RUN_AS_NODE;  // set when this runs from a terminal inside VS Code; VS Code would start as Node
  const root = path.resolve(__dirname, "..", "..");
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "jimbo-ext-test-"));
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "jimbo-ext-user-"));
  try {
    await runTests({
      version: "insiders",
      extensionDevelopmentPath: root,
      extensionTestsPath: path.join(__dirname, "suite", "index.js"),
      launchArgs: [workspace, "--disable-extensions", "--disable-workspace-trust", `--user-data-dir=${userData}`],
      extensionTestsEnv: { FAKE_JIMBO: path.join(root, "test", "fake-jimbo.js"), FAKE_JIMBO_LOG: path.join(workspace, "received.jsonl") },
    });
  } finally {
    fs.rmSync(workspace, { recursive: true, force: true });
    fs.rmSync(userData, { recursive: true, force: true });
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
