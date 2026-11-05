# Evals

The eval suite runs jimbo on coding tasks with a real model and checks the results. Use it to see whether a
change to the system prompt, the tools or the model makes jimbo better or worse. It needs network access and
an API key, so it is not part of `uv run pytest`.

## Run

Run these from the repo root:

```bash
uv run python -m evals run                         # every task, once, on the main model in ~/.jimbo/config.toml
uv run python -m evals run --model <alias-or-spec> # another main model
uv run python -m evals run --tag smoke             # a small set of 5 tasks
uv run python -m evals run --task bug-paginate     # one task
uv run python -m evals run --trials 3              # each task 3 times
uv run python -m evals run --set evals-judge=<model>   # the model that grades text answers
uv run python -m evals compare <run-a> <run-b>     # two runs side by side
uv run python -m evals report [<run>]              # print a run's table and write its report.html (default: latest run)
```

Other options: `--jobs N` (trials at once, default 4), `--keep` (keep each trial's workspace), `--no-network`
(block network access for shell commands), `--no-sandbox` (needed where there is no sandbox, which is anywhere
but macOS), `--config <path>` (a different `config.toml`).

Trials at once send many requests. If the provider refuses them with HTTP 429, set `requests_per_minute` for
that provider in `config.toml` (see [docs/reference.md](../docs/reference.md#providers-and-api-keys)), or pass
`--requests-per-minute N`. The flag sets the cap for every provider in this run and replaces the one in
`config.toml`; `--requests-per-minute 0` removes it. All trials in a run share the cap, and `run.json` records the
cap of each provider the run's models use.

Each trial:

1. Copies the task's fixture to a temp folder and commits it to a new git repo.
2. Runs the prompt (and any follow-ups) in a session built with `build_session()`, as the tests do. Approvals
   use the chain `rules`, then allow everything. Deny rules still apply: `shell:git push*`, `shell:sudo *`, the
   task's `deny`, and those in your config. Shell commands run in the sandbox.
3. Grades the result (see [Grading](#grading)).

Only your `config.toml` is copied into the trial. Your skills, commands, agents and `~/.jimbo/AGENTS.md` are not
loaded, so they don't change the results. Bundled files are loaded.

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

A trial passes when the run ends without an error (a failed turn or the trial timeout) and every check passes.
The output of each check, and the judge's reason, are saved in the trial's `check.txt` and shown in
`report.html`.

## Results

Each run writes `evals/results/<run-id>/`. `<run-id>` is the date, the time and the main model's name.

| File | Contents |
|------|----------|
| `report.html` | the run as a page: totals, a table of tasks with failures first, and for each trial its check output and judge verdict, the task's prompt and rubric, the diff, and links to its trace and files |
| `run.json` | jimbo commit and whether `src/` or `evals/` had changes, the model for each task name, trials, start and end time |
| `results.jsonl` | one line per trial: `pass`, `check_exit`, `requests`, `input_tokens`, `output_tokens`, `cost`, `seconds`, `tool_calls`, `tool_retries`, `denials`, `wrap_up`, `hit_request_limit`, `compactions`, `error`, `log` |
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

The table at the end of a run lists the `check.txt` and `trace.html` of each failed trial.

## Add a task

File: `evals/tasks/<id>/task.toml` (eval task, TOML)

```toml
prompt = "Pagination drops the last item on each page. Fix it."
fixture = "shop"               # start from evals/fixtures/shop; leave out to use this task's repo/ folder
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

[judge]                        # optional: a model grades the final answer and the diff
rubric = "1. The answer names ..."
```

Folder layout:

```
evals/tasks/<id>/
  task.toml
  repo/        the starting workspace, unless task.toml sets fixture
  check/       copied over the workspace after the run, before the check command; jimbo never sees it
  solution/    copied over the fixture by check-task; the check must pass with it
```

Put hidden tests in `check/eval_check/`. `{python}` in the command is the Python that runs the evals.

Then run:

```bash
uv run python -m evals check-task <id>
```

It runs the check on the unchanged fixture, where it must fail, and with `solution/` applied, where it must
pass. `uv run pytest` runs this for every task.

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
