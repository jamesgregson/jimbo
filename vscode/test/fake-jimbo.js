#!/usr/bin/env node
// A stand-in for `jimbo --ui stdio` in the extension's tests. It speaks the protocol, asks for the risk
// warning, and for the message "write notes" asks to write notes.txt and writes it when allowed.
// The message "crash" makes it exit with code 3. Every message it receives is appended to $FAKE_JIMBO_LOG.

const fs = require("fs");
const path = require("path");
const readline = require("readline");

const workspace = process.argv[process.argv.indexOf("--workspace") + 1];
const logFile = process.env.FAKE_JIMBO_LOG;
let seq = 0;
const out = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");
const event = (kind, data) => out({ type: "event", event: { seq: ++seq, ts: new Date().toISOString(), session_id: "fake", kind, data } });

out({ type: "hello", protocol: 1, version: "0.0.0-fake", workspace });
out({ type: "confirm", id: "risk", message: "jimbo can harm your system.\n\nDo you understand and accept these risks?" });

let accepted = false;
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const msg = JSON.parse(line);
  if (logFile) fs.appendFileSync(logFile, line + "\n");
  if (msg.type === "confirm_answer" && msg.id === "risk") {
    out({ type: "confirm_closed", id: "risk" });
    if (!msg.value) {
      process.stderr.write("jimbo: you didn't accept the risks\n");
      process.exit(1);
    }
    accepted = true;
    event("warning", { text: "accepted", accepted: "prompt" });
    event("status", { model: "fake:model-1", mode: "normal", auto: false, yolo: false, sandboxed: true });
  } else if (msg.type === "send" && accepted) {
    event("user_message", { text: msg.text, attachments: [] });
    if (msg.text === "crash") {
      process.stderr.write("boom\n");
      process.exit(3);
    }
    if (msg.text === "write notes") {
      const file = path.join(workspace, "notes.txt");
      event("tool_call", { id: "c1", tool: "write_file", args: { path: "notes.txt", content: "hello\n" } });
      out({ type: "approval", request: { id: "1", call_id: "c1", tool: "write_file", summary: "create notes.txt",
            detail: "+hello", always: "this session", title: "Approve write_file", allow_label: "Allow once",
            deny_label: "Deny", note_prompt: "Why?", markdown: false, proposed: { path: file, text: "hello\n" } } });
    } else {
      event("assistant_text", { text: `you said ${msg.text}` });
      event("run_finished", { ok: true, cancelled: false });
    }
  } else if (msg.type === "approve" && msg.id === "1") {
    out({ type: "approval_closed", id: "1" });
    if (msg.decision !== "no") fs.writeFileSync(path.join(workspace, "notes.txt"), "hello\n");
    event("tool_result", { id: "c1", ok: msg.decision !== "no", content: msg.decision !== "no" ? "wrote notes.txt" : "denied" });
    event("assistant_text", { text: "done" });
    event("run_finished", { ok: true, cancelled: false });
  } else if (msg.type === "changes_get") {
    out({ type: "changes", files: [{ path: path.join(workspace, "notes.txt"), before: null }] });
  }
});
process.stdin.on("end", () => process.exit(0));
