// The webview pages: the chat panel (jimbo's web UI page) and the trace viewer.

import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

/** The web UI page, copied into media/ by `npm run build`; from the repo when running from source without a build. */
function pageSource(extensionPath: string): string {
  for (const p of [path.join(extensionPath, "media", "index.html"),
                   path.join(extensionPath, "..", "src", "jimbo", "frontends", "web_static", "index.html")]) {
    if (fs.existsSync(p)) return fs.readFileSync(p, "utf-8");
  }
  throw new Error("jimbo's page (media/index.html) is missing; run npm run build");
}

/** The page with a Content Security Policy: its one script by nonce, no network, images from data and blob URLs. */
export function chatHtml(extensionPath: string, webview: vscode.Webview): string {
  const nonce = crypto.randomBytes(16).toString("base64");
  const csp = [
    "default-src 'none'",
    `img-src data: blob: ${webview.cspSource}`,
    `style-src 'unsafe-inline' ${webview.cspSource}`,
    `script-src 'nonce-${nonce}'`,
    `font-src ${webview.cspSource}`,
  ].join("; ");
  const html = pageSource(extensionPath);
  if (!html.includes("<script>")) throw new Error("jimbo's page has no <script> tag to add the nonce to");
  return html
    .replace("<head>", `<head>\n<meta http-equiv="Content-Security-Policy" content="${csp}">`)
    .replace("<script>", `<script nonce="${nonce}">`);
}

/** The session trace in its own tab. The trace page is self-contained; it gets inline scripts but no network. */
export function openTrace(html: string): void {
  const panel = vscode.window.createWebviewPanel("jimbo.trace", "jimbo trace", vscode.ViewColumn.Active,
                                                 { enableScripts: true, localResourceRoots: [] });
  const csp = "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'";
  panel.webview.html = html.replace(/<head>/i, `<head>\n<meta http-equiv="Content-Security-Policy" content="${csp}">`);
}
