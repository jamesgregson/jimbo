// Starts `jimbo --ui stdio` and exchanges JSON lines with it. Knows nothing about VS Code's UI.

import { ChildProcess, spawn } from "child_process";
import * as os from "os";
import * as path from "path";
import * as readline from "readline";

export const PROTOCOL = 1;
const HELLO_TIMEOUT_MS = 30_000;
const KILL_AFTER_MS = 5_000;
const STDERR_TAIL = 50;

export type Msg = { type: string; [key: string]: any };

export interface ProcessOptions {
  command: string;  // jimbo.path
  args: string[];   // jimbo.args, then the extra args for this start (such as --resume)
  folder: string;   // the workspace folder
  log: (line: string) => void;  // stderr and notes, for the output channel
}

export interface Exit {
  text: string;     // what happened, for the panel
  stderr: string;   // the last lines of stderr
}

export function expandHome(p: string): string {
  return p === "~" ? os.homedir() : p.startsWith("~/") ? path.join(os.homedir(), p.slice(2)) : p;
}

export class JimboProcess {
  private child: ChildProcess | undefined;
  private tail: string[] = [];
  private hello: Msg | undefined;
  private helloTimer: NodeJS.Timeout | undefined;
  private problem: string | undefined;  // why we stopped it, if we did
  private exited = false;

  constructor(private opts: ProcessOptions, private onMessage: (msg: Msg) => void,
              private onExit: (exit: Exit) => void) {}

  get running(): boolean { return this.child !== undefined && !this.exited; }

  start(extra: string[] = []): void {
    const args = [...this.opts.args, "--ui", "stdio", "--workspace", this.opts.folder, ...extra];
    const command = expandHome(this.opts.command);
    this.opts.log(`starting: ${command} ${args.join(" ")}`);
    const child = spawn(command, args, { cwd: this.opts.folder, env: process.env, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    child.on("error", (err: NodeJS.ErrnoException) => {
      this.problem = err.code === "ENOENT"
        ? `jimbo was not found: "${command}". Install it with pip install git+https://github.com/jamesgregson/jimbo.git, ` +
          `or set jimbo.path in your user settings. If jimbo is installed, start VS Code from a terminal so it has your PATH.`
        : `jimbo could not start: ${err.message}`;
      this.finish(null);
    });
    readline.createInterface({ input: child.stdout! }).on("line", (line) => this.onLine(line));
    readline.createInterface({ input: child.stderr! }).on("line", (line) => {
      this.opts.log(line);
      this.tail.push(line);
      if (this.tail.length > STDERR_TAIL) this.tail.shift();
    });
    child.on("exit", (code, signal) => this.finish(code, signal));
    child.stdin!.on("error", () => {});  // the process is gone; exit reports it
    this.helloTimer = setTimeout(() => {
      if (!this.hello) this.fail(`jimbo did not start within ${HELLO_TIMEOUT_MS / 1000} seconds`);
    }, HELLO_TIMEOUT_MS);
  }

  send(msg: Msg): void {
    if (this.running && this.child!.stdin!.writable) this.child!.stdin!.write(JSON.stringify(msg) + "\n");
  }

  /** Close stdin, which tells jimbo to quit, and kill it if it is still running after a few seconds. */
  stop(): void {
    const child = this.child;
    if (!child || this.exited) return;
    child.stdin!.end();
    setTimeout(() => { if (!this.exited) child.kill(); }, KILL_AFTER_MS).unref();
  }

  private onLine(line: string): void {
    let msg: Msg;
    try {
      msg = JSON.parse(line);
    } catch {
      this.opts.log(`not a protocol line: ${line}`);
      return;
    }
    if (!this.hello) {
      if (msg.type !== "hello") return this.fail(`jimbo did not say hello first; got ${line.slice(0, 200)}`);
      this.hello = msg;
      clearTimeout(this.helloTimer);
      if (msg.protocol !== PROTOCOL)
        return this.fail(`this extension needs jimbo protocol ${PROTOCOL}; jimbo ${msg.version} reports ${msg.protocol}`);
      this.opts.log(`jimbo ${msg.version}, protocol ${msg.protocol}, workspace ${msg.workspace}`);
    }
    this.onMessage(msg);
  }

  private fail(problem: string): void {
    this.problem = problem;
    this.opts.log(problem);
    this.child?.kill();
  }

  private finish(code: number | null, signal?: NodeJS.Signals | null): void {
    if (this.exited) return;
    this.exited = true;
    clearTimeout(this.helloTimer);
    const text = this.problem ?? (code === 0 ? "jimbo stopped." : `jimbo stopped (${signal ? `signal ${signal}` : `exit code ${code}`}).`);
    this.onExit({ text, stderr: this.tail.join("\n") });
  }
}
