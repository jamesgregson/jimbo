# Risks

> **DANGER**: Jimbo gives a language model control of your computer. It can delete your files, damage your
> system, leak your secrets and credentials, spend your money, and act on remote systems in your name. Do not
> run it on a computer, account or project you care about.

Jimbo is a hobby project. Nobody has reviewed its security. It comes with no warranty and no claim that it is
fit for any purpose. If you run it, you accept every risk on this page.

This page lists the ways jimbo can cause harm, and the limits of the safety features it has. The list is not
complete.

## The model runs commands as you

The `shell` tool runs commands with bash, as your user, with your permissions. A command can do anything you
can do from a terminal: delete files, install software, change system settings, use your SSH keys and cloud
credentials, push to your repositories, and send data over the network.

The model decides which commands to run. Models make mistakes, misread instructions, and can be steered by
text they read (see [Prompt injection](#prompt-injection)). A command that looks harmless can do something
else, and a long command is easy to approve without reading it all.

## Approvals are the main protection, and they are easy to weaken

By default jimbo asks before it writes a file, runs a command or fetches a web page. That prompt is only as good
as your attention when you answer it.

- **"Always" adds up.** Answering `a` to a file write allows every file change for the rest of the session.
  Answering `a` to a command such as `make test` or `uv run pytest` allows that command for the rest of the
  session. Together, these let the model write any code into the project and then run it, with no further
  prompts.
- **Running tests and builds runs code.** A test, a build script, a `Makefile`, a `package.json` script or a
  `setup.py` can contain any code. If the model wrote or changed it, approving `pytest` or `npm test` runs the
  model's code.
- **`--auto` lets another model approve commands.** In auto mode, file changes need no approval, and a model
  rates each shell command. Commands rated low or medium run without asking. That model can be wrong or be
  misled. Fixed checks raise the rating for some known commands, but they are regular expressions and are
  easy to get around, for example with `python -c`, `bash -c`, or a script the model has just written. In
  practice, auto mode lets the model run code you have not seen.
- **`--yolo` removes approvals.** Every call runs without asking. Only deny rules, `secret_files` and the git
  rules still apply.
- **Deny rules are best effort.** `approval_deny` matches text patterns. `bash -c "sudo …"`, a script file,
  or another spelling of the same command gets past them.
- **`--ui json` with `--yolo` or `--auto` runs unattended.** Nobody sees the calls as they happen.

## The sandbox has gaps

`sandbox = "auto"` uses Podman, then Docker, then `sandbox-exec`, and falls back to **no sandbox** if none of
them works. Check the status line, or set `sandbox = "podman"`, `"docker"` or `"on"` to refuse to start
without one.

| Sandbox | Gaps |
|---------|------|
| none (`auto` with nothing available, or `off`) | Commands can read and write everything your user can. Only secret-looking environment variables are removed. |
| `sandbox-exec` on macOS (`auto` or `seatbelt`) | Commands can read everything outside your home folder, and in it the tool folders (`~/.cache`, `~/.local`, `~/.cargo`, `~/.npm` and others), folders on `PATH` (for a virtual environment's `bin` folder, the whole environment) and `~/.gitconfig`. They can see which files exist in your home folder and their sizes. They can write to the project, the scratch directories, cache folders and the macOS per-user temp folder. The network is open. Environment variables whose names don't look secret are passed on. git can read passwords saved in the macOS keychain. |
| `sandbox = "docker"` or `"podman"` | The project and jimbo's scratch directory are mounted read-write. The network is open unless `sandbox_network = false`. Folders added with `sandbox_writable` or `sandbox_readonly` are reachable. The container engine and the kernel are shared with your computer. |

No sandbox protects the project folder. An approved command can change or delete any file in it. The `.git`
folder is read-only in every sandbox (only `.git/hooks` and `.git/config` with `git_write = true`), but not
without one. `/undo` only reverts changes made with jimbo's file tools, not changes made by commands.

## The git rules read text

jimbo denies git commands that change the repository, and always denies commands that change a remote (see
Git in the reference). These rules read the command's text. A script, a test or `python -c` that runs git gets
past them. The sandbox's read-only `.git` and the missing credentials stop those, but without a sandbox only
the text rules apply, and with `sandbox-exec` git can still use passwords from the macOS keychain. A program
other than git or `gh` that talks to GitHub or another server is not covered.

## Changes in the project can run later, outside the sandbox

Every sandbox lets jimbo write to the project. Some files in a project run code when you, or your tools, use
the project later on your own computer, outside any sandbox:

- `.git/hooks/*` run when you commit, check out, merge or push.
- `.git/config` can set programs that git runs, such as `core.fsmonitor`, `core.hooksPath` and `core.pager`.
- `Makefile`, `package.json` scripts, `pyproject.toml` and `setup.py` build steps, and test files run when you
  build or test.
- `.envrc` (direnv), editor settings and task files (`.vscode/`, `.idea/`) can run commands when you open
  the folder.

Jimbo's file tools never write `.git`, and can't change the files in `protected_paths` unless
`protected_write = "ask"` and you approve each change. By default the list is
`.git/hooks`, `.git/config`, `.github/`, `.gitlab-ci.yml`, `.vscode/`, `.idea/`, `.envrc` (in any folder),
`.husky/` and `.claude/settings*.json`. In a sandbox, commands can't write them either, except that a container
only covers the ones that existed when it started. With no sandbox, commands can.

`Makefile`, `package.json`, `pyproject.toml`, `setup.py`, test files and other build files are not protected,
because changing them is normal work. A change made inside the container sandbox can run on your computer the
next time you build or open the project. After a session, check these files before you use the project outside
the sandbox.

## Data loss

- Commands can delete files, including untracked and ignored files that git cannot restore.
- Git commands can discard uncommitted work, rewrite history, delete branches and force-push.
- `/undo` covers file tool changes only. Commands are not recorded.
- Commit or back up your work before every session.

## Prompt injection

The model treats text it reads as possible instructions. Anyone who can put text in front of it can try to make
it do something else. This includes:

- code, comments, READMEs and data files in the project
- project files that jimbo loads into the model's instructions without asking: `AGENTS.md`, `JIMBO.md`,
  `.jimbo/memory.md`, skills in `.jimbo/skills/`, `.claude/skills/` and `.agents/skills/`, commands in
  `.jimbo/commands/` and agents in `.jimbo/agents/`
- web pages fetched with `web_fetch`
- command output, test output, error messages and language server messages
- files and text you paste or attach

A cloned repository can contain all of these. **Do not run jimbo on code you do not trust.** A workspace
`.jimbo/config.toml` cannot loosen approvals or the sandbox, but the project files above can still tell the
model what to do.

The `remember` tool saves notes that later sessions read. A note in `~/.jimbo/memory.md` is read in every
project. If the model is misled into saving a harmful note, and you approve it, the instruction stays in place
for future sessions.

## Secrets and credentials

- `secret_files` only stops `read_file`, `grep`, the language server tools and `show_image` from reading files
  such as `.env` and `*.pem`. An approved command such as `cat .env` can still read them, and so can a test
  or script, unless `secret_files_shell = true`. With that on, a container only hides the files that existed
  when it started.
- With no sandbox, commands can read your home folder: SSH keys, cloud credentials, `~/.netrc`,
  `~/.gitconfig`, shell history, browser data and other API keys.
- Environment variables are removed by name. A secret in a variable whose name doesn't contain `KEY`,
  `TOKEN`, `SECRET`, `PASSWORD` or similar is passed to commands, except in a container.
- jimbo hides secrets in tool results before the model and the session log see them: known formats (private
  keys, AWS, GitHub, GitLab, Slack, Stripe, Google, OpenAI, Anthropic and OpenRouter keys, JWTs), your provider
  API keys and the values of secret-looking environment variables. Any other secret in file contents or command
  output, such as a database password, is sent to the model provider and saved in the session log. Your own
  messages and `@path` attachments are not checked.

## What is sent to the model provider

Everything the model sees goes to the provider that runs it: your messages, the contents of files it reads,
command output, web pages, and the project instructions. With OpenRouter, the request can be passed on to
another company that hosts the model, with its own data policy. Shell commands are also sent to the
`jimbo-shell-assess` model. If you cannot send your code to a third party, use a local model.

## Data leaving your computer

- With the network open, an approved command can send any data it can read to any server, for example with
  `curl` or `python`.
- `web_fetch` asks once per domain when you answer `a`. After that, any URL on that domain can be fetched
  without asking, and a URL can carry data in its path or query string.
- `sandbox_network = false` blocks the network in the sandboxes. Without a sandbox, nothing blocks it.

## Language servers run project code

Jimbo asks once per session before it starts a language server. Some servers run code from the project. For
example, `rust-analyzer` runs build scripts and procedural macros. Approving a server for an untrusted project
can run that project's code.

## The browser interface

`--ui web` starts an HTTP server on `127.0.0.1`. The page URL contains a token, and anyone with the URL can
approve calls, change approval modes, and change providers and API keys. The URL can end up in browser
history. The connection is not encrypted. If you change `web_host` to an address other than `127.0.0.1`,
other computers on the network can reach the server.

## The VS Code extension

The extension in `vscode/` runs the same jimbo, with the same approvals, sandbox and risks. Answering an
approval in a VS Code notification is the same as answering it in the panel. `jimbo.path` and `jimbo.args` say
which command VS Code runs; they are read from user settings only, but anyone who can change your user settings
can make VS Code run another program.

## Saved sessions

Jimbo saves each session in `~/.jimbo/sessions`: the conversation, file contents the model read, command
output and tool calls. These files can contain secrets and private code. They stay on disk until you delete
them. Set `log_sessions = false` to stop saving sessions. This also turns off `--resume` and `/trace`.

## Cost

Each step is a paid request to the model provider. A single task can make up to `request_limit` requests (50 by
default), and agents and shell risk assessment make more. A model that loops, or a long session, can cost more
than you expect. The evals make many requests. Use an API key with a spending limit.

## Wrong results

The model can write wrong code, break working code, delete tests, change tests so they pass, and report that
work is done or tested when it is not. Read every change before you use it.

jimbo refuses a file change when the file changed since the model last read it, so it doesn't overwrite your
edits with an old copy. It only knows about files the model read or wrote with the file tools. A file changed by
a command and not read again is checked; a file the model never read is not.

## The software itself

Jimbo has bugs. Approval, path checks, the sandbox setup and the risk checks may not work as described in the
README and reference. The version number does not change between commits, so you cannot tell which version you
have installed. There is no security support and no promise that problems will be fixed.

## If you run it anyway

These steps reduce the risk. They do not remove it.

- Run it in a virtual machine or on a computer that holds nothing you care about, with no credentials you
  care about.
- Use `sandbox = "docker"` or `"podman"`, with `sandbox_network = false` unless the task needs the network.
- Use an API key with a low spending limit, used for nothing else.
- Only run it on code you trust. Read `AGENTS.md`, `.jimbo/` and any skill folders in a project first.
- Commit or back up your work before each session.
- Stay in the default approval mode. Read every command before you approve it. Avoid `a` for file changes and
  for commands that run code, such as tests and builds.
- Do not use `--auto` or `--yolo`.
- After a session, review the diff, and check `.git/hooks`, `.git/config` and build and editor files before you
  use the project outside the sandbox.
- Delete old session logs in `~/.jimbo/sessions`.
