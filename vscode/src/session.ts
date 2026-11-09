// One jimbo process for one workspace folder, and the webview that shows it.
//
// Messages from jimbo go to the page as {type: "from_jimbo", msg}, except the ones the host answers itself
// (confirm, secret, trace, changes). Messages from the page are {type: "to_jimbo", msg}, passed on as they
// are, or host_* messages the host handles (dialogs, links, the trace). The host keeps what it passed to the
// page and sends it all again when the page loads, because the webview's content is dropped while it is hidden.

import * as vscode from "vscode";
import { VirtualFiles, closeDiff, isDirty, relative, showChanges, showProposed } from "./approvals";
import { existing, openPath } from "./links";
import { chatHtml, openTrace } from "./panel";
import { Exit, JimboProcess, Msg } from "./process";

const MAX_HISTORY = 20_000;
const TRANSIENT = new Set(["files", "models", "image_data"]);  // replies to one request; not replayed

export interface State { model: string; mode: string; running: boolean; alive: boolean; sandbox: string; sandboxDetail: string }

export class Session implements vscode.Disposable {
  private proc: JimboProcess | undefined;
  private webview: vscode.Webview | undefined;
  private webviewSubs: vscode.Disposable[] = [];
  private pageReady = false;
  private history: Msg[] = [];
  private inserts: string[] = [];  // text for the input box, waiting for the page
  private approvals = new Map<string, vscode.Uri | null>();  // open approval id -> its proposed diff, if shown
  private stateChanged = new vscode.EventEmitter<State>();
  readonly onState = this.stateChanged.event;
  state: State = { model: "", mode: "default", running: false, alive: false, sandbox: "", sandboxDetail: "" };

  constructor(readonly folder: vscode.WorkspaceFolder, private extensionPath: string, private files: VirtualFiles,
              private output: vscode.OutputChannel) {}

  // ---- process ----
  start(extra: string[] = []): void {
    const config = vscode.workspace.getConfiguration("jimbo");
    // jimbo.path and jimbo.args have machine scope, so a workspace's settings can't choose what runs.
    const proc: JimboProcess = new JimboProcess({
      command: config.get<string>("path") || "jimbo",
      args: config.get<string[]>("args") ?? [],
      folder: this.folder.uri.fsPath,
      log: (line) => this.output.appendLine(line),
    }, (msg) => { if (this.proc === proc) void this.fromJimbo(msg); },  // a process being replaced is ignored
       (exit) => { if (this.proc === proc) this.exited(exit); });
    this.proc = proc;
    this.setState({ alive: true, running: false });
    proc.start(extra);
  }

  restart(extra: string[] = []): void {
    this.proc?.stop();
    this.history = [];
    this.closeApprovals();
    this.post({ type: "host_reset" });
    this.start(extra);
  }

  send(msg: Msg): void { this.proc?.send(msg); }

  dispose(): void {
    this.proc?.stop();
    this.closeApprovals();
    this.detach();
    this.stateChanged.dispose();
  }

  private exited(exit: Exit): void {
    this.closeApprovals();
    this.setState({ alive: false, running: false });
    this.forward({ type: "host_exit", text: exit.text, stderr: exit.stderr }, true);
  }

  // ---- webview ----
  attach(webview: vscode.Webview, visibility: vscode.Event<boolean>): void {
    this.detach();
    this.webview = webview;
    this.pageReady = false;
    webview.options = { enableScripts: true, localResourceRoots: [] };
    this.webviewSubs.push(
      webview.onDidReceiveMessage((m) => this.fromPage(m)),
      visibility((visible) => { if (!visible) this.pageReady = false; }),  // the page reloads when shown again
    );
    try {
      webview.html = chatHtml(this.extensionPath, webview);
    } catch (e) {
      webview.html = `<p>${String(e)}</p>`;
    }
  }

  detach(): void {
    this.webviewSubs.forEach((d) => d.dispose());
    this.webviewSubs = [];
    this.webview = undefined;
    this.pageReady = false;
  }

  /** Put text in the panel's input box, now or when the page has loaded. */
  insert(text: string): void {
    if (this.pageReady) this.post({ type: "host_insert", text });
    else this.inserts.push(text);
  }

  private post(msg: Msg): void {
    if (this.webview && this.pageReady) void this.webview.postMessage(msg);
  }

  /** Pass a message to the page, and keep it for when the page loads again. */
  private forward(msg: Msg, host = false): void {
    if (!TRANSIENT.has(msg.type)) {
      this.history.push(host ? msg : { type: "from_jimbo", msg });
      if (this.history.length > MAX_HISTORY) this.history.splice(0, this.history.length - MAX_HISTORY);
    }
    this.post(host ? msg : { type: "from_jimbo", msg });
  }

  private async fromPage(m: Msg): Promise<void> {
    switch (m?.type) {
      case "to_jimbo":
        if (m.msg && typeof m.msg.type === "string") this.send(m.msg);
        break;
      case "host_ready":
        this.pageReady = true;
        // One message, so the page renders the history in one pass instead of one paint per event.
        if (this.history.length) void this.webview?.postMessage({ type: "host_replay", msgs: this.history });
        for (const text of this.inserts.splice(0)) this.post({ type: "host_insert", text });
        break;
      case "host_confirm": {
        const yes = await vscode.window.showWarningMessage(String(m.message), { modal: true }, "Yes");
        this.post({ type: "host_confirm_answer", id: m.id, value: yes === "Yes" });
        break;
      }
      case "host_check_paths":
        if (Array.isArray(m.paths))
          this.post({ type: "host_paths", id: m.id, ok: await existing(this.folder.uri.fsPath, m.paths.map(String)) });
        break;
      case "host_open":
        if (typeof m.path === "string") await openPath(this.folder.uri.fsPath, m.path, Number(m.line) || undefined);
        break;
      case "host_trace":
        this.send({ type: "trace_get" });
        break;
      case "host_restart":
        this.restart();
        break;
    }
  }

  // ---- messages from jimbo ----
  private async fromJimbo(msg: Msg): Promise<void> {
    switch (msg.type) {
      case "hello":
        return;
      case "confirm": {  // the risk warning, or turning on auto/yolo
        const accept = /accept these risks/i.test(msg.message) ? "Accept" : "Yes";
        const choice = await vscode.window.showWarningMessage(msg.message, { modal: true }, accept);
        this.send({ type: "confirm_answer", id: msg.id, value: choice === accept });
        return;
      }
      case "secret": {
        const value = await vscode.window.showInputBox({ prompt: msg.message, password: true, ignoreFocusOut: true });
        this.send({ type: "secret_answer", id: msg.id, value: value ?? null });
        return;
      }
      case "confirm_closed":
      case "secret_closed":
        return;
      case "trace":
        if (msg.html) openTrace(msg.html);
        else vscode.window.showWarningMessage(`jimbo: ${msg.error}`);
        return;
      case "changes":
        await showChanges(this.files, this.folder.uri.fsPath, msg.files ?? []);
        return;
      case "approval":
        return this.approval(msg);
      case "approval_closed":
        this.closeApproval(msg.id);
        break;
      case "event":
        this.track(msg.event);
        break;
    }
    this.forward(msg);
  }

  private track(ev: { kind: string; data: any }): void {
    const d = ev.data ?? {};
    if (ev.kind === "status") this.setState({ model: d.model ?? "", mode: d.yolo ? "yolo" : d.auto ? "auto" : d.mode === "plan" ? "plan" : "default", ...sandboxState(d) });
    else if (ev.kind === "user_message" && !d.steer) this.setState({ running: true });
    else if (ev.kind === "run_finished") this.setState({ running: false });
    else if (ev.kind === "busy") this.setState({ running: !!d.on });
  }

  private setState(change: Partial<State>): void {
    this.state = { ...this.state, ...change };
    this.stateChanged.fire(this.state);
  }

  // ---- approvals (S3) ----
  private async approval(msg: Msg): Promise<void> {
    const req = msg.request;
    const proposed: { path: string; text: string } | undefined = req.proposed ?? undefined;
    delete req.proposed;  // the page shows the diff; the whole file would only fill the history
    const folder = this.folder.uri.fsPath;
    if (proposed && isDirty(proposed.path))
      req.summary += ` (${relative(folder, proposed.path)} has unsaved changes in the editor; jimbo will overwrite the file on disk)`;
    this.approvals.set(req.id, null);
    this.forward(msg);
    if (!proposed) return;
    if (vscode.workspace.getConfiguration("jimbo").get<boolean>("diffEditorForApprovals", true)) {
      const uri = await showProposed(this.files, folder, proposed);
      if (this.approvals.has(req.id)) this.approvals.set(req.id, uri);
      else await this.dropDiff(uri);  // answered while the diff was opening
    }
    const choices = [req.allow_label, ...(req.always ? ["Always"] : []), req.deny_label];
    const choice = await vscode.window.showWarningMessage(`jimbo: ${req.summary}`, ...choices);
    if (!choice || !this.approvals.has(req.id)) return;  // dismissed, or answered in the panel
    if (choice === req.deny_label) {
      const note = await vscode.window.showInputBox({ prompt: `${req.note_prompt} (optional)` });
      if (this.approvals.has(req.id)) this.send({ type: "approve", id: req.id, decision: "no", note: note ?? "" });
    } else {
      this.send({ type: "approve", id: req.id, decision: choice === "Always" ? "always" : "yes" });
    }
  }

  private closeApproval(id: string): void {
    const uri = this.approvals.get(id);
    this.approvals.delete(id);
    if (uri) void this.dropDiff(uri);
  }

  private closeApprovals(): void {
    for (const id of [...this.approvals.keys()]) this.closeApproval(id);
  }

  private async dropDiff(uri: vscode.Uri): Promise<void> {
    await closeDiff(uri);
    this.files.remove(uri);
  }
}

/** The sandbox fields of State, from a status event: "podman", "seatbelt (no network)" or "NOT sandboxed". */
export function sandboxState(d: any): Pick<State, "sandbox" | "sandboxDetail"> {
  const sandbox = d.sandboxed ? `${d.sandbox}${d.sandbox_network === false ? " (no network)" : ""}` : "NOT sandboxed";
  const skipped = (d.sandbox_skipped ?? []).map((x: any) => `skipped ${x.kind}: ${x.reason}`);
  return { sandbox, sandboxDetail: [d.sandbox_detail, ...skipped].filter(Boolean).join("\n") };
}
