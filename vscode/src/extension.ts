// Commands, the status bar, and where the panel opens. See session.ts for how a panel talks to jimbo.

import * as vscode from "vscode";
import { SCHEME, VirtualFiles, relative } from "./approvals";
import { Session, State } from "./session";

interface Open { session: Session; panel?: vscode.WebviewPanel }

let output: vscode.OutputChannel;
let files: VirtualFiles;
let status: vscode.StatusBarItem;
let extensionPath: string;
const sessions = new Map<string, Open>();  // workspace folder uri -> its session
let current: Session | undefined;           // the session commands act on: the one used last
let sidebar: vscode.WebviewView | undefined;
let sidebarSession: Session | undefined;

export function activate(context: vscode.ExtensionContext): { sessionFor: typeof sessionFor } {
  extensionPath = context.extensionPath;
  output = vscode.window.createOutputChannel("Jimbo");
  files = new VirtualFiles();
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "jimbo.open";
  showState(undefined);
  status.show();

  const command = (name: string, fn: (...args: any[]) => any) =>
    context.subscriptions.push(vscode.commands.registerCommand(name, fn));
  command("jimbo.open", () => open());
  command("jimbo.resume", () => resume());
  command("jimbo.askSelection", () => askAbout(true));
  command("jimbo.addFile", (uri?: vscode.Uri) => askAbout(false, uri));
  command("jimbo.showChanges", () => withSession((s) => s.send({ type: "changes_get" })));
  command("jimbo.undo", () => withSession(async (s) => {
    const yes = await vscode.window.showWarningMessage("Undo the file changes of jimbo's last turn?", { modal: true }, "Undo");
    if (yes) s.send({ type: "send", text: "/undo" });
  }));
  command("jimbo.stop", () => withSession((s) => s.send({ type: "cancel" })));
  command("jimbo.endSession", () => withSession((s) => end(s)));
  command("jimbo.showLog", () => output.show());

  context.subscriptions.push(
    output, status,
    vscode.workspace.registerTextDocumentContentProvider(SCHEME, files),
    vscode.window.registerWebviewViewProvider("jimbo.chat", { resolveWebviewView: (view) => resolveSidebar(view) }),
    { dispose: () => { for (const { session } of sessions.values()) session.dispose(); sessions.clear(); } },
  );
  return { sessionFor };  // for the extension's tests
}

export function deactivate(): void {
  for (const { session } of sessions.values()) session.dispose();
  sessions.clear();
}

/** For tests: the session for a folder. */
export function sessionFor(folder: vscode.WorkspaceFolder): Session | undefined {
  return sessions.get(folder.uri.toString())?.session;
}

function location(): "editor" | "sidebar" {
  return vscode.workspace.getConfiguration("jimbo").get("preferredLocation") === "sidebar" ? "sidebar" : "editor";
}

async function pickFolder(uri?: vscode.Uri): Promise<vscode.WorkspaceFolder | undefined> {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (!folders.length) {
    vscode.window.showErrorMessage("jimbo: open a folder first.");
    return undefined;
  }
  const near = uri ?? vscode.window.activeTextEditor?.document.uri;
  const owner = near && vscode.workspace.getWorkspaceFolder(near);
  if (owner) return owner;
  if (folders.length === 1) return folders[0];
  if (current) return current.folder;
  return vscode.window.showWorkspaceFolderPick({ placeHolder: "Folder for jimbo to work on" });
}

/** Show the session for the folder, starting jimbo if it isn't running. */
async function open(extra: string[] = [], uri?: vscode.Uri): Promise<Session | undefined> {
  const folder = await pickFolder(uri);
  if (!folder) return undefined;
  const key = folder.uri.toString();
  let found = sessions.get(key);
  if (!found) {
    if (location() === "sidebar" && sidebarSession) end(sidebarSession);  // the sidebar shows one session
    const session = new Session(folder, extensionPath, files, output);
    session.onState((state) => { if (session === current) showState(state); });
    found = { session };
    sessions.set(key, found);
    session.start(extra);
  }
  use(found.session);
  if (location() === "sidebar") {
    sidebarSession = found.session;
    if (sidebar) found.session.attach(sidebar.webview, (fn) => sidebar!.onDidChangeVisibility(() => fn(sidebar!.visible)));
    await vscode.commands.executeCommand("jimbo.chat.focus");
  } else if (found.panel) {
    found.panel.reveal();
  } else {
    found.panel = makePanel(found.session);
  }
  return found.session;
}

function makePanel(session: Session): vscode.WebviewPanel {
  const multi = (vscode.workspace.workspaceFolders?.length ?? 0) > 1;
  const panel = vscode.window.createWebviewPanel("jimbo.chat", multi ? `jimbo: ${session.folder.name}` : "jimbo",
                                                 { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
                                                 { enableScripts: true, localResourceRoots: [] });
  panel.iconPath = vscode.Uri.joinPath(vscode.Uri.file(extensionPath), "media", "icon.svg");
  session.attach(panel.webview, (fn) => panel.onDidChangeViewState((e) => fn(e.webviewPanel.visible)));
  panel.onDidChangeViewState((e) => { if (e.webviewPanel.active) use(session); });
  panel.onDidDispose(() => {  // closing the tab ends the session (S9)
    const entry = sessions.get(session.folder.uri.toString());
    if (entry?.session === session) {
      entry.panel = undefined;
      end(session);
    }
  });
  return panel;
}

function resolveSidebar(view: vscode.WebviewView): void {
  sidebar = view;
  view.onDidDispose(() => { if (sidebar === view) sidebar = undefined; });
  const attach = (s: Session) => s.attach(view.webview, (fn) => view.onDidChangeVisibility(() => fn(view.visible)));
  if (sidebarSession) attach(sidebarSession);
  else void open();  // the view was opened directly: start a session for it
}

function end(session: Session): void {
  const key = session.folder.uri.toString();
  const entry = sessions.get(key);
  if (entry?.session === session) {
    sessions.delete(key);
    entry.panel?.dispose();
  }
  if (sidebarSession === session) sidebarSession = undefined;
  session.dispose();
  if (current === session) {
    current = [...sessions.values()][0]?.session;
    showState(current?.state);
  }
}

function use(session: Session): void {
  current = session;
  showState(session.state);
}

function showState(state: State | undefined): void {
  if (!state) {
    status.text = "$(hubot) jimbo";
    status.tooltip = "Open jimbo";
    return;
  }
  const model = state.model.split(/[:/]/).pop() || "starting";
  status.text = `${state.running ? "$(sync~spin)" : "$(hubot)"} jimbo: ${state.alive ? model : "stopped"} · ${state.mode}`;
  status.tooltip = `jimbo ${state.alive ? `${state.running ? "working" : "idle"} with ${state.model}` : "stopped"}; approvals: ${state.mode}`;
}

async function withSession(fn: (s: Session) => unknown): Promise<void> {
  if (!current) {
    vscode.window.showInformationMessage("jimbo is not running. Run Jimbo: Open first.");
    return;
  }
  await fn(current);
}

async function resume(): Promise<void> {
  const folder = await pickFolder();
  if (!folder) return;
  const running = sessions.get(folder.uri.toString());
  if (running) {
    const yes = await vscode.window.showWarningMessage("End the current jimbo session and resume the last saved one?",
                                                       { modal: true }, "Resume");
    if (!yes) return;
    end(running.session);
  }
  await open(["--resume"], folder.uri);
}

/** `@path` for the current file, with the selected lines for Ask About Selection (S4). */
async function askAbout(selection: boolean, uri?: vscode.Uri): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  const target = uri ?? editor?.document.uri;
  if (!target || target.scheme !== "file") {
    vscode.window.showInformationMessage("jimbo: open a file first.");
    return;
  }
  const session = await open([], target);
  if (!session) return;
  const rel = relative(session.folder.uri.fsPath, target.fsPath);
  let text = `@${rel} `;
  if (selection && editor && !editor.selection.isEmpty) {
    const { start, end: stop } = editor.selection;
    const last = stop.character === 0 && stop.line > start.line ? stop.line : stop.line + 1;
    text = `@${rel} lines ${start.line + 1}-${last}: `;
  }
  session.insert(text);
}
