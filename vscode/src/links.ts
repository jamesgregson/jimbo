// Paths in jimbo's answers and tool calls: which ones exist, and opening them at a line (S5).

import * as path from "path";
import * as vscode from "vscode";
import { editorColumn, exists } from "./approvals";

function resolve(folder: string, p: string): vscode.Uri {
  return vscode.Uri.file(path.isAbsolute(p) ? p : path.join(folder, p));
}

/** The paths, of those given, that are files. */
export async function existing(folder: string, paths: string[]): Promise<string[]> {
  const found = await Promise.all(paths.slice(0, 200).map(async (p) => (await exists(resolve(folder, p))) ? p : null));
  return found.filter((p): p is string => p !== null);
}

export async function openPath(folder: string, p: string, line?: number): Promise<void> {
  const uri = resolve(folder, p);
  if (!(await exists(uri))) {
    vscode.window.showWarningMessage(`jimbo: ${p} was not found.`);
    return;
  }
  const options: vscode.TextDocumentShowOptions = { preview: true, viewColumn: editorColumn() };
  if (line && line > 0) {
    const pos = new vscode.Position(line - 1, 0);
    options.selection = new vscode.Range(pos, pos);
  }
  await vscode.window.showTextDocument(uri, options);
}
