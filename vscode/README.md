# Jimbo for VS Code (prototype)

This extension runs jimbo in a VS Code panel. It is a prototype. It ships inside the jimbo package and is not
on the VS Code Marketplace. It has the same risks as jimbo; read [RISKS.md](../RISKS.md) first.

The extension starts `jimbo --ui stdio` for the open folder. The panel is jimbo's web page, in VS Code's
colors. File changes are shown in VS Code's diff editor while jimbo asks to make them.

## Install

You need jimbo installed (see the [README](../README.md#install)). A jimbo release includes the extension:

```bash
jimbo vscode install              # uses the code command; or name another: cursor, codium, code-insiders
```

Run it again after you upgrade jimbo, so the extension matches.

To build it from a clone instead, you need Node.js 20 or later. From the jimbo folder:

```bash
jimbo vscode build                # builds src/jimbo/bundled/jimbo.vsix; installs npm packages in vscode/ the first time
jimbo vscode install              # then install it, as above
```

`jimbo vscode build` runs `npm run bundle` in `vscode/`. A later `uv build` then includes the extension in the package. To install a
VSIX you built some other way, run `code --install-extension <file>.vsix` yourself.

To run it from source instead, open the `vscode` folder in VS Code and press F5. A second VS Code window opens
with the extension loaded.

If VS Code can't find `jimbo`, set `jimbo.path` in your user settings to the full path, for example
`~/.venv/bin/jimbo`. On macOS, VS Code started from the Dock may not have your shell's `PATH` and environment
variables such as `OPENROUTER_API_KEY`; start it from a terminal with `code`, or set the key with
`/providers key openrouter`.

## Use

- **Jimbo: Open** (or the jimbo icon in the editor title bar) opens the panel beside the current editor and
  starts jimbo. VS Code asks you to accept the risk warning each time jimbo starts. When the panel is alone in
  its editor group, the group is locked (the lock icon in the group's title bar), so files you open go to
  another group. Click the lock to unlock it.
- Type a task in the panel and press Enter. The panel works like `jimbo --ui web`.
- When jimbo asks to write or edit a file, VS Code opens a diff of the file and the proposed change, and shows
  a notification with **Allow once**, **Always** and **Deny**. You can also answer in the panel.
- File paths in jimbo's answers and tool calls are links that open the file.
- **Jimbo: Ask About Selection** (editor context menu) adds `@path lines 40-58:` to the input.
  **Jimbo: Add File to Chat** adds `@path`.
- **Jimbo: Show Changes** opens a diff for each file the last turn changed. **Jimbo: Undo Last Turn** sends
  `/undo` after you confirm.
- **Jimbo: Stop the Current Task** stops the run. **Jimbo: End Session**, or closing the panel, stops jimbo.
- **Jimbo: Resume Last Session** starts `jimbo --resume`.
- **Jimbo: Show Log** shows jimbo's stderr.
- The status bar shows the model and the approval mode. Click it to open the panel.

With several folders open, each folder has its own session and panel.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `jimbo.path` | `jimbo` | the command to run, such as `~/.venv/bin/jimbo` or `uvx` |
| `jimbo.args` | `[]` | arguments before `--ui stdio`, such as `["--sandbox", "docker"]`, or `["--from", "jimbo-agent", "jimbo"]` with `uvx` |
| `jimbo.diffEditorForApprovals` | `true` | open a proposed file change in a diff editor while jimbo asks |
| `jimbo.preferredLocation` | `editor` | `editor`: a tab beside the current editor; `sidebar`: a view in the Jimbo sidebar |

`jimbo.path` and `jimbo.args` are read from user settings only, so a workspace's `.vscode/settings.json` can't
choose the command that runs.

## Develop

```bash
npm run build       # copies ../src/jimbo/frontends/web_static/index.html into media/ and bundles dist/extension.js
npm run typecheck
npm test            # downloads VS Code Insiders into .vscode-test/ and runs test/suite with test/fake-jimbo.js
```

| File | Role |
|---|---|
| `src/extension.ts` | commands, status bar, where the panel opens |
| `src/session.ts` | one jimbo process and its webview: passes messages both ways, answers dialogs, keeps history for replay |
| `src/process.ts` | starts jimbo, reads and writes JSON lines, checks the `hello` line |
| `src/panel.ts` | the chat page with its Content Security Policy, and the trace tab |
| `src/approvals.ts` | diff editors for proposed changes and the last turn's changes |
| `src/links.ts` | checks and opens file paths |

The protocol is described in `src/jimbo/frontends/bridge.py` and `src/jimbo/frontends/stdio.py`.
