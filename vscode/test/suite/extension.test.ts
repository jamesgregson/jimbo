import * as assert from "assert";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

type Api = { sessionFor(folder: vscode.WorkspaceFolder): any };

const folder = () => vscode.workspace.workspaceFolders![0];
const notes = () => path.join(folder().uri.fsPath, "notes.txt");

async function until<T>(what: string, check: () => T | undefined | false, ms = 10_000): Promise<T> {
  const end = Date.now() + ms;
  for (;;) {
    const got = check();
    if (got) return got;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 50));
  }
}

const tabLabels = () => vscode.window.tabGroups.all.flatMap((g) => g.tabs).map((t) => t.label);

// Dialogs can't be clicked in a test, so showWarningMessage is replaced with answers the test chooses.
type Asked = { message: string; modal: boolean; choices: string[] };
const asked: Asked[] = [];
let answer: (a: Asked) => Promise<string | undefined> = async () => undefined;
const realWarning = vscode.window.showWarningMessage;

describe("jimbo extension", () => {
  let api: Api;

  before(async () => {
    (vscode.window as any).showWarningMessage = (message: string, ...rest: any[]) => {
      const modal = typeof rest[0] === "object" && rest[0]?.modal === true;
      const a = { message, modal, choices: rest.filter((r) => typeof r === "string") };
      asked.push(a);
      return answer(a);
    };
    await vscode.workspace.getConfiguration("jimbo").update("path", process.env.FAKE_JIMBO, vscode.ConfigurationTarget.Global);
    const ext = vscode.extensions.all.find((e) => e.packageJSON.name === "jimbo" && e.packageJSON.publisher === "jimbo-local")!;
    api = await ext.activate();
  });

  after(() => { (vscode.window as any).showWarningMessage = realWarning; });

  it("starts jimbo and asks to accept the risks in a modal dialog", async () => {
    answer = async (a) => (a.modal && a.choices.includes("Accept") ? "Accept" : undefined);
    await vscode.commands.executeCommand("jimbo.open");
    const session = await until("the session", () => api.sessionFor(folder()));
    await until("the status event", () => session.state.model === "fake:model-1");
    const risk = asked.find((a) => /accept these risks/.test(a.message))!;
    assert.ok(risk.modal);
    assert.ok(tabLabels().includes("jimbo"));
  });

  it("shows a proposed file in a diff editor and writes it when allowed", async () => {
    const session = api.sessionFor(folder());
    let allow!: (choice: string) => void;
    answer = (a) => (a.message.startsWith("jimbo: create notes.txt") ? new Promise((r) => { allow = r; }) : Promise.resolve(undefined));
    session.send({ type: "send", text: "write notes" });
    await until("the diff editor", () => tabLabels().includes("jimbo: notes.txt (proposed)"));
    await until("the approval notification", () => allow);
    assert.ok(!fs.existsSync(notes()));
    allow("Allow once");
    await until("notes.txt", () => fs.existsSync(notes()));
    await until("the diff editor to close", () => !tabLabels().includes("jimbo: notes.txt (proposed)"));
    await until("the run to finish", () => !session.state.running);
    const received = fs.readFileSync(process.env.FAKE_JIMBO_LOG!, "utf-8").trim().split("\n").map((l) => JSON.parse(l));
    assert.deepStrictEqual(received.find((m) => m.type === "approve"), { type: "approve", id: "1", decision: "yes" });
  });

  it("shows the last turn's changes", async () => {
    await vscode.commands.executeCommand("jimbo.showChanges");
    await until("the changes diff", () => tabLabels().includes("jimbo: notes.txt (last turn)"));
  });

  it("reports a crash and restarts", async () => {
    const session = api.sessionFor(folder());
    session.send({ type: "send", text: "crash" });
    await until("the exit", () => !session.state.alive);
    session.restart();
    await until("the restart", () => session.state.alive && session.state.model === "fake:model-1");
  });

  it("stops when the risks are not accepted", async () => {
    const session = api.sessionFor(folder());
    answer = async () => undefined;
    session.restart();
    await until("the exit", () => !session.state.alive);
  });

  it("ends the session when its tab closes", async () => {
    answer = async (a) => (a.choices.includes("Accept") ? "Accept" : undefined);
    api.sessionFor(folder()).restart();
    const tab = vscode.window.tabGroups.all.flatMap((g) => g.tabs).find((t) => t.label === "jimbo")!;
    await vscode.window.tabGroups.close(tab);
    await until("the session to end", () => !api.sessionFor(folder()));
  });
});
