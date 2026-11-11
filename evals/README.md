# Evals

The evals run jimbo on coding tasks with a real model and check the results. Use them to see whether a
change to the system prompt, the tools or the model makes jimbo better or worse. They need network access and
an API key, so they are not part of `uv run pytest`.

Tasks are grouped in suites (see [Suites](#suites)). They run only when you name them with `--suite`, except
`core`, which runs by default.

| Suite | Tasks | What it checks |
|-------|-------|----------------|
| `core` | 38 | reading code, bug fixes, features, refactors, tests, tools, plan mode, denials, agents and skills |
| `injection` | 8 | prompt injection: a file or command output tells jimbo to do something unrelated but harmless, such as create a file, add a git tag or answer in French. A trial passes if jimbo does the task and doesn't follow the injected instruction |

## Run

Run these from the repo root:

```bash
uv run python -m evals run                         # the default suites (core), once, on the main model in ~/.jimbo/config.toml
uv run python -m evals run --model <alias-or-spec> # another main model
uv run python -m evals run --suite <name>          # another suite; repeatable; --suite all for every suite
uv run python -m evals suites                      # list the suites
uv run python -m evals run --tag smoke             # a small set of 5 tasks
uv run python -m evals run --tag skills            # the tasks that check skills are loaded and followed
uv run python -m evals run --tag cpp               # the C++ tasks, on the ledger fixture
uv run python -m evals run --task bug-paginate     # one task, from any suite
uv run python -m evals run --trials 3              # each task 3 times
uv run python -m evals run --trials 3 --retry-failed-only   # each task up to 3 times, until it passes
uv run python -m evals run --set evals-judge=<model>   # the model that grades text answers
uv run python -m evals compare <run-a> <run-b>     # two runs side by side
uv run python -m evals report [<run>]              # print a run's table and write its report.html (default: latest run)
uv run python -m evals grade [<run>]               # run the suites' graders on a run again (default: latest run)
```

`--tag` picks tasks from the chosen suites. `--task` finds a task in any suite, and the task runs with its
suite's defaults and requirements.

Other options: `--jobs N` (trials at once; default: the smallest `jobs` of the run's suites, else 4), `--keep` (keep each trial's workspace), `--no-network`
(block network access for shell commands), `--sandbox docker` or `--sandbox podman` (each trial runs its
commands in its own container from the bundled image; works on Linux), `--sandbox off` or `--no-sandbox` (commands run unconfined,
needed on other systems without Docker or Podman), `--no-lsp` (start no language servers, to compare a run with one that
has them), `--config <path>` (a different `config.toml`).

`run`, `grade`, `assess`, `compaction` and `check-task` show jimbo's risk warning once at the start and ask you to accept it. Any
answer other than yes, or a closed stdin, exits before anything runs. To accept the risks without being asked,
pass `--user-accepts-risks`. `report` and `compare` don't ask.

`--trials N` replaces each suite's `trials`. Without it, each task runs its suite's `trials`, or once.

With `--retry-failed-only`, a task's trials run one after another and stop at the first pass. Different tasks
still run at the same time. A task passes if any of its trials passes, and the reports count tasks instead of
trials: the pass column shows `pass on #2` or `FAIL x3`. Use it to find tasks a model can't do at all, not
to measure how often it passes.

Trials at once send many requests. If the provider refuses them with HTTP 429, set `requests_per_minute` for
that provider in `config.toml` (see [docs/reference.md](../docs/reference.md#providers-and-api-keys)), or pass
`--requests-per-minute N`. The flag sets the cap for every provider in this run and replaces the one in
`config.toml`; `--requests-per-minute 0` removes it. All trials in a run share the cap, and `run.json` records the
cap of each provider the run's models use.

Each trial:

1. Copies the task's fixture to a temp folder and commits it to a new git repo. In a `compile_commands.json`,
   `${WORKSPACE}` becomes the temp folder's path, so `clangd` can index the project.
2. Runs the prompt (and any follow-ups) in a session built with `build_session()`, as the tests do. Approvals
   use the chain `rules`, then allow everything. Deny rules still apply: `shell:git push*`, `shell:sudo *`, the
   task's `deny`, and those in your config. Shell commands run in the sandbox.
3. Grades the result (see [Grading](#grading)).

Only your `config.toml` is copied into the trial. Your skills, commands, agents and `~/.jimbo/AGENTS.md` are not
loaded, so they don't change the results. Bundled files are loaded. Language servers on your `PATH` are used,
and start without asking.

Tasks tagged `skills` check that jimbo calls `load_skill` when a skill fits the prompt, and doesn't when none fits.
They use the bundled skills, or skills in the task's `repo/.jimbo/skills/`.

## Grading

After the run, each check that the task has runs. They are set in `task.toml` (see [Add a task](#add-a-task)):

- **Check command** (`[check] command`): the files in the task's `check/` folder are copied over the workspace,
  then the command runs there with a shell, outside the sandbox. Exit code 0 is a pass. Most tasks use this to
  run hidden tests, which jimbo never sees.
- **Event checks** (`[[check.events]]`): count the session's events of one kind with matching fields, such as
  successful `edit_file` results, and compare the count with `min` and `max`. Plan mode tasks use this to check
  that no file was changed and that `exit_plan` was called.
- **Repeated denials** (`[check] no_repeated_denials`): fail if a call that was denied is made again with the
  same arguments.
- **Judge** (`[judge] rubric`): a model gets the prompt, the rubric, jimbo's final answer, the diff and any
  plans given to `exit_plan`. It returns pass or fail with a reason. It runs as the task `evals-judge`, which
  uses the main model unless you pass `--set evals-judge=<model>`. Tasks whose answer is text use this.

- **Suite grader** (`grade` in `suite.toml`): see [Suites](#suites).

A trial passes when the run ends without an error (a failed turn or the trial timeout) and every check passes.
The output of each check, and the judge's reason, are saved in the trial's `check.txt` and shown in
`report.html`.

## Results

Each run writes `evals/results/<run-id>/`. `<run-id>` is the date, the time and the main model's name.

| File | Contents |
|------|----------|
| `report.html` | the run as a page: totals, a table of tasks with failures first, and for each trial its check output and judge verdict, the task's prompt and rubric, the diff, and links to its trace and files |
| `run.json` | jimbo commit and whether `src/` or `evals/` had changes, the model for each task name, trials, the run's suites with their `suite.toml` values, start and end time, and `grade_errors` from the suites' graders |
| `results.jsonl` | one line per trial: `suite`, `pass` (`null` while waiting for the suite's grader), `check_pass` (the task's own checks; `null` if it has none), `grade_pass` (the suite's grader), `check_exit`, `requests`, `input_tokens`, `output_tokens`, `thinking_tokens` (included in `output_tokens`), `cost`, `seconds`, `tool_calls`, `tool_retries`, `denials`, `wrap_up`, `hit_request_limit`, `compactions`, `error`, `log` |
| `<task>/<trial>/session.jsonl` | the session log |
| `<task>/<trial>/trace.html` | the trace viewer with that log; open it in a browser |
| `<task>/<trial>/diff.patch` | the changes jimbo made |
| `<task>/<trial>/check.txt` | the output of each check |

`cost` is in USD. It is pydantic-ai's estimate from its price tables (the `genai-prices` package), not the amount
the provider billed. When those tables don't have the model, the cost comes from the per-token prices that the
provider's `/models` list gives for the main model (OpenRouter lists them), and agents on other models are priced as
the main model. It is empty when neither source has a price. It covers jimbo's requests, including agents,
but not the judge's. The estimate can be wrong when a price table is out of date, when OpenRouter sends the request
to an upstream provider with a different price, or when cached tokens are billed at a lower rate.

The table at the end of a run lists the `check.txt` and `trace.html` of each failed trial. When the run has
more than one suite, it shows a pass line for each suite. Runs from before suites have no `suite`, and are
shown as `core`.

## Add a task

File: `evals/suites/<suite>/tasks/<id>/task.toml` (eval task, TOML)

```toml
prompt = "Pagination drops the last item on each page. Fix it."
fixture = "shop"               # start from the suite's fixtures/shop, else evals/fixtures/shop; leave out to use repo/
tags = ["bug-fix", "python"]
timeout = 600                  # seconds for the whole trial (default 600)
mode = "plan"                  # optional: start in plan mode
followups = ["now add a test"] # optional: more messages, each sent after the previous turn ends
deny = ["shell:git commit*"]   # optional: more deny rules

[settings]                     # optional: Settings overrides, such as request_limit or shell_timeout
request_limit = 30

[check]
command = "{python} -m pytest -q -p no:cacheprovider eval_check"   # exit code 0 is a pass
no_repeated_denials = true     # optional: fail if a denied call is made again with the same arguments

[[check.events]]               # optional: count matching events
kind = "tool_result"
tool = "edit_file"
ok = true
max = 0

[[check.events]]
kind = "tool_call"
tool = "load_skill"
args = {name = "debug"}       # a table matches only if it is equal to the whole field
min = 1

[judge]                        # optional: a model grades the final answer and the diff
rubric = "1. The answer names ..."
```

Folder layout:

```
evals/suites/<suite>/tasks/<id>/
  task.toml
  repo/        the starting workspace, unless task.toml sets fixture
  check/       copied over the workspace after the run, before the check command; jimbo never sees it
  solution/    copied over the fixture by check-task; the check must pass with it
```

Put hidden tests in `check/eval_check/`. The check runs code the model wrote, so it runs in the trial's sandbox
after the model finishes: under `sandbox-exec`, in the trial's container, or unconfined with `--sandbox off`.
`{python}` in the command is the Python that runs the evals, or `python3` in a container. A check in a container
can only use what the image has.

Task ids must be unique across all suites.

Then run:

```bash
uv run python -m evals check-task <id>             # or --suite <name> for every task in a suite
```

It runs the check on the unchanged fixture, where it must fail, and with `solution/` applied, where it must
pass. `uv run pytest` runs this for every task in suites with `pytest = true`. `--sandbox docker` (or `podman`, `seatbelt`) runs the checks
in that sandbox, to make sure they work there.

## Suites

A suite is a folder in `evals/suites/` with a `suite.toml` and a `tasks/` folder:

```
evals/suites/<name>/
  suite.toml
  tasks/<id>/      task folders (see Add a task)
  fixtures/        optional: fixtures only this suite uses; a task's fixture is looked for here first
```

File: `evals/suites/<name>/suite.toml` (eval suite, TOML)

```toml
description = "C++ tasks that need a compiler and clangd."
default = false                # true: runs when run gets no --suite
pytest = true                  # uv run pytest runs check-task on this suite's tasks (default true)

[requires]                     # checked before any trial starts; the run stops if one is missing
commands = ["c++", "make", "clangd"]   # on PATH, or in the sandbox image with --sandbox docker or podman
sandbox = ["docker", "podman"] # --sandbox must be one of these
network = true                 # --no-network is refused

[defaults]                     # for each task of the suite that doesn't set them
timeout = 900
trials = 1                     # when run gets no --trials
jobs = 2                       # when run gets no --jobs
deny = ["shell:git commit*"]   # added to each task's deny

[defaults.settings]            # merged under each task's [settings]; the task's keys win
request_limit = 60
```

Every field is optional. `uv run python -m evals suites` lists the suites with their task counts and
requirements.

### Tasks from a dataset

A suite can make its tasks with code instead of `tasks/` folders, and grade all its trials together after they
finish. Use this for a benchmark such as SWE-bench:

```toml
source = "evals.suites.mybench.source:tasks"   # module:function
grade = "evals.suites.mybench.grade:grade"     # module:function
```

- `source(suite)` returns a list of dicts with the fields of a `task.toml` plus `id` and `dir`. The workspace is
  `dir/repo`, or the `fixture`. A source can build workspaces under `evals/.cache/` (in `.gitignore`).
- `grade(suite, run_dir)` runs once after every trial of the run has finished, outside the trial sandboxes. It
  can be `async`. It returns a list of dicts with `task`, `trial`, `pass` and `text`. `text` goes at the end of
  the trial's `check.txt`.
- A task in a suite with `grade` needs no checks of its own. If it has some, it passes only if they and the
  grader pass.
- Until the grader has run, a trial's `pass` is `null`, and reports show it as `pending`. If the grader raises,
  the error is saved in `run.json` and shown in the reports, and the trials stay pending. `grade <run>` runs the
  graders again.

## Side tasks

```bash
uv run python -m evals assess --model <model>   # jimbo-shell-assess on evals/assess/shell.toml
uv run python -m evals compaction               # jimbo-compaction on evals/compaction/*.toml
```

`assess` shows, for each command, the expected risk, the model's risk and the combined risk (the model plus the
fixed checks in `check_command`). It lists commands rated lower than expected.

`compaction` builds each conversation, compacts it so that only the last `keep` messages stay word for word,
then asks the main model the case's questions using only the compacted history. The judge grades each answer
against `expect`. Use `--set jimbo-compaction=<model>` to choose the model that compacts.
