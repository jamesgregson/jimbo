// File changes in VS Code's diff editor: proposed changes while jimbo asks (S3), and the last turn's changes (S6).

import * as path from "path";
import * as vscode from "vscode";

export const SCHEME = "jimbo-file";

/** Read-only documents holding text jimbo sent: a proposed file, or a file as it was before a turn. */
export class VirtualFiles implements vscode.TextDocumentContentProvider {
  private texts = new Map<string, string>();
  private n = 0;
  private changed = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changed.event;

  /** A document showing `text`, named like `file` so the editor picks its language. */
  add(kind: string, file: string, text: string): vscode.Uri {
    const uri = vscode.Uri.from({ scheme: SCHEME, path: `/${kind}/${++this.n}/${path.basename(file)}` });
    this.texts.set(uri.toString(), text);
    return uri;
  }

  remove(uri: vscode.Uri): void { this.texts.delete(uri.toString()); }

  provideTextDocumentContent(uri: vscode.Uri): string { return this.texts.get(uri.toString()) ?? ""; }
}

/** Close the diff editors whose right side is `uri`. */
export async function closeDiff(uri: vscode.Uri): Promise<void> {
  const tabs = vscode.window.tabGroups.all.flatMap((g) => g.tabs).filter((t) =>
    t.input instanceof vscode.TabInputTextDiff && t.input.modified.toString() === uri.toString());
  if (tabs.length) await vscode.window.tabGroups.close(tabs, true);
}

/** The column for diff editors: where the user last edited, not the jimbo tab. */
function editorColumn(): vscode.ViewColumn {
  return vscode.window.activeTextEditor?.viewColumn ?? vscode.window.visibleTextEditors[0]?.viewColumn ?? vscode.ViewColumn.One;
}

export async function exists(uri: vscode.Uri): Promise<boolean> {
  try {
    return (await vscode.workspace.fs.stat(uri)).type === vscode.FileType.File;
  } catch {
    return false;
  }
}

export function relative(folder: string, file: string): string {
  const rel = path.relative(folder, file);
  return rel.startsWith("..") || path.isAbsolute(rel) ? file : rel;
}

/** True if VS Code has unsaved changes to `file`. */
export function isDirty(file: string): boolean {
  return vscode.workspace.textDocuments.some((d) => d.uri.scheme === "file" && d.uri.fsPath === file && d.isDirty);
}

/** Open the diff of the file on disk (or nothing, for a new file) against `proposed`. Returns the right side. */
export async function showProposed(files: VirtualFiles, folder: string, proposed: { path: string; text: string }): Promise<vscode.Uri> {
  const disk = vscode.Uri.file(proposed.path);
  const left = await exists(disk) ? disk : files.add("new", proposed.path, "");
  const right = files.add("proposed", proposed.path, proposed.text);
  await vscode.commands.executeCommand("vscode.diff", left, right, `jimbo: ${relative(folder, proposed.path)} (proposed)`,
                                       { preview: true, preserveFocus: true, viewColumn: editorColumn() });
  return right;
}

/** One diff editor per file the last turn changed: the file before the turn against the file now. */
export async function showChanges(files: VirtualFiles, folder: string, changed: { path: string; before: string | null }[]): Promise<void> {
  if (!changed.length) {
    vscode.window.showInformationMessage("jimbo has not changed any files yet.");
    return;
  }
  for (const f of changed) {
    const before = files.add("before", f.path, f.before ?? "");
    const disk = vscode.Uri.file(f.path);
    const now = await exists(disk) ? disk : files.add("deleted", f.path, "");
    await vscode.commands.executeCommand("vscode.diff", before, now, `jimbo: ${relative(folder, f.path)} (last turn)`,
                                         { preview: false, preserveFocus: true, viewColumn: editorColumn() });
  }
}
