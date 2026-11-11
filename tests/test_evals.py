import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage

from evals.grading import check_events, repeated_denials
from evals.html_report import write_html_report
from evals.report import compare_report, cost, passes, run_report
from evals.runner import (RunConfig, grade_run, jobs_for, make_settings, missing_requirements, request_limits,
                          run_suite, trials_for)
from jimbo.core.cost import estimate_cost
from evals.side_tasks import load_compaction_cases, load_shell_cases
from evals.tasks import FIXTURES_DIR, Suite, Task, load_suites, load_task, load_tasks
from evals.validate import check_task
from jimbo.core.events import Event

TASKS = [t for t in load_tasks(suites=["all"]) if t.suite.pytest]  # loading every suite also checks its files


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_task_checks_fail_unsolved_and_pass_solved(task):
    assert await check_task(task) == []


def test_side_task_data_loads():
    assert load_shell_cases() and load_compaction_cases()


def test_task_needs_a_way_to_pass(tmp_path):
    with pytest.raises(ValueError, match="no way to pass"):
        Task(prompt="x")
    (tmp_path / "task.toml").write_text('prompt = "x"\n[judge]\nrubric = "y"\n')
    with pytest.raises(ValueError, match="no repo"):
        load_task(tmp_path)


def ev(seq, kind, **data):
    return Event(seq=seq, session_id="s", kind=kind, data=data)


def test_event_checks_and_repeated_denials():
    events = [ev(1, "tool_call", id="a", tool="shell", args={"command": "git commit"}),
              ev(2, "approval_resolved", call_id="a", tool="shell", action="deny"),
              ev(3, "tool_call", id="b", tool="shell", args={"command": "git commit"}),
              ev(4, "tool_result", id="b", tool="shell", ok=False)]
    assert repeated_denials(events) == ['shell {"command": "git commit"}']
    task = Task(prompt="x", check={"no_repeated_denials": True,
                                   "events": [{"kind": "tool_call", "tool": "shell", "min": 1, "max": 2}]})
    out = check_events(task, events)
    assert not out.ok and "ok: tool_call tool='shell': 2" in out.text and "FAIL: denied calls repeated" in out.text


def fixing_model():
    """Fixes the paginate bug with one edit, then answers."""
    edit = {"path": "shop/paginate.py", "old": "end - 1]", "new": "end]"}

    def script(messages, info):
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            return ModelResponse(parts=[TextPart("fixed")])
        return ModelResponse(parts=[ToolCallPart("edit_file", edit)])

    async def stream(messages, info):
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            yield "fixed"
        else:
            yield {0: DeltaToolCall(name="edit_file", json_args=json.dumps(edit))}

    return FunctionModel(script, stream_function=stream)


async def test_run_suite_with_scripted_model(tmp_path):
    cfg = RunConfig(sandbox="off", user_config=tmp_path / "none.toml", main_model=fixing_model())
    run_dir = await run_suite(load_tasks(["bug-paginate", "bug-coupon-minimum"]), cfg, results_dir=tmp_path)
    results = {r["task"]: r for r in map(json.loads, (run_dir / "results.jsonl").read_text().splitlines())}
    fixed, other = results["bug-paginate"], results["bug-coupon-minimum"]
    assert fixed["pass"] and fixed["tool_calls"] == {"edit_file": 1} and fixed["requests"] == 2
    assert not other["pass"] and other["check_exit"] == 1  # the same edit doesn't fix the coupon bug
    assert fixed["cost"] is None  # a FunctionModel has no price
    trial = run_dir / "bug-paginate" / "1"
    assert "end]" in (trial / "diff.patch").read_text()
    assert (trial / "session.jsonl").is_file() and (trial / "trace.html").is_file()
    assert "passed" in (trial / "check.txt").read_text()
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["tasks"] == ["bug-coupon-minimum", "bug-paginate"] and "finished" in meta
    report = run_report(run_dir)
    assert "1/2 trials passed" in report and "bug-coupon-minimum #1" in report
    assert "A pass" in compare_report(run_dir, run_dir)



async def test_retry_failed_only_stops_at_the_first_pass(tmp_path):
    cfg = RunConfig(sandbox="off", user_config=tmp_path / "none.toml", main_model=fixing_model(), trials=3,
                    retry_failed_only=True)
    run_dir = await run_suite(load_tasks(["bug-paginate", "bug-coupon-minimum"]), cfg, results_dir=tmp_path)
    results = list(map(json.loads, (run_dir / "results.jsonl").read_text().splitlines()))
    trials = sorted((r["task"], r["trial"], r["pass"]) for r in results)
    assert trials == [("bug-coupon-minimum", 1, False), ("bug-coupon-minimum", 2, False),
                      ("bug-coupon-minimum", 3, False), ("bug-paginate", 1, True)]
    assert json.loads((run_dir / "run.json").read_text())["retry_failed_only"] is True
    report = run_report(run_dir)
    assert "1/2 tasks passed (50%) in 4 trials" in report and "FAIL x3" in report
    assert "of tasks" in compare_report(run_dir, run_dir)
    assert "1/2 tasks (50%) in 4 trials" in write_html_report(run_dir).read_text()


def test_retry_pass_labels():
    t = lambda n, ok: {"trial": n, "pass": ok}
    assert passes([t(1, True)], retry=True) == "pass"
    assert passes([t(1, False), t(2, True)], retry=True) == "pass on #2"
    assert passes([t(1, False)], retry=True) == "FAIL"
    assert passes([t(1, False), t(2, True)]) == "1/2"

def test_cost_column():
    assert cost([{"cost": None}]) == "-"
    assert cost([{"cost": 0.001}, {"cost": 0.003}, {"cost": None}]) == "$0.0020"
    assert cost([{"cost": 0.001}, {"cost": 0.003}], total=True) == "$0.0040"
    assert cost([{}]) == "-"  # results written before cost was recorded


def test_estimate_cost_falls_back_to_listed_prices():
    prices = {"prompt": 1e-6, "completion": 2e-6, "input_cache_read": 1e-7}
    usage = RunUsage(requests=1, input_tokens=1000, cache_read_tokens=400, output_tokens=100)
    assert estimate_cost(usage, prices) == pytest.approx(600e-6 + 400e-7 + 200e-6)
    assert estimate_cost(usage, None) is None
    assert estimate_cost(RunUsage(), prices) is None  # no requests: nothing to price
    assert estimate_cost(None, prices) is None


def test_requests_per_minute_override(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[providers.work]\nbase_url = "http://x/v1"\nrequests_per_minute = 5\n')

    def limits(rpm):
        s = make_settings(RunConfig(user_config=config, requests_per_minute=rpm), None, tmp_path,
                          tmp_path / f"home-{rpm}", tmp_path / "logs")
        return request_limits(s, {"main": "work:m", "judge": "openrouter:j"})

    assert limits(None) == {"work": 5, "openrouter": None}
    assert limits(20) == {"work": 20, "openrouter": 20}
    assert limits(0) == {"work": None, "openrouter": None}


@pytest.mark.parametrize("cmd", ["run", "assess", "compaction", "check_task"])
@pytest.mark.parametrize("flag,answer,refused", [(True, None, False), (False, "y", False), (False, "n", True),
                                                  (False, EOFError, True)])
def test_eval_commands_need_the_risks_accepted(cmd, flag, answer, refused, monkeypatch, capsys):
    import evals.__main__ as cli
    from jimbo.core.runner import RISK_WARNING

    def fake_input(prompt):
        if answer is None:
            raise AssertionError("asked although the flag was given")
        if answer is EOFError:
            raise EOFError
        return answer

    ran = []

    async def fake_cmd(args):
        ran.append(cmd)
        return 0

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(cli, f"cmd_{cmd}", fake_cmd)
    monkeypatch.setattr("sys.argv", ["evals", cmd.replace("_", "-"), *(["--user-accepts-risks"] if flag else [])])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert RISK_WARNING in capsys.readouterr().err
    assert (ran == []) == refused
    assert ("--user-accepts-risks" in str(e.value.code)) == refused


def test_report_and_compare_do_not_ask(monkeypatch):
    import evals.__main__ as cli
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    monkeypatch.setattr("sys.argv", ["evals", "compare", "no-such-a", "no-such-b"])
    with pytest.raises(SystemExit, match="no run"):
        cli.main()


# ---- suites ----

def test_every_current_task_is_in_core_which_runs_by_default():
    suites = load_suites()
    assert suites["core"].default
    default = load_tasks()
    assert {t.suite_name for t in default} == {"core"} and len(default) == len(load_tasks(suites=["core"]))


def write_task(root, suite, task, toml='prompt = "x"\nfixture = "shop"\ntags = ["t"]\n[judge]\nrubric = "y"\n'):
    d = root / suite / "tasks" / task
    d.mkdir(parents=True)
    (d / "task.toml").write_text(toml)


def make_suites(root, **suites):
    """suites: name -> suite.toml text."""
    for name, text in suites.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "suite.toml").write_text(text)
    return root


def test_choosing_suites_and_tasks(tmp_path):
    root = make_suites(tmp_path, base="default = true\n", big="", other="")
    write_task(root, "base", "a")
    write_task(root, "big", "b")
    write_task(root, "other", "c", 'prompt = "x"\nfixture = "shop"\ntags = ["u"]\n[judge]\nrubric = "y"\n')
    ids = lambda **kw: [t.id for t in load_tasks(root=root, **kw)]
    assert ids() == ["a"]  # only the default suites
    assert ids(suites=["big"]) == ["b"]
    assert ids(suites=["all"]) == ["a", "b", "c"]
    assert ids(ids=["b"]) == ["b"]  # --task finds a task in an optional suite
    assert ids(tags=["t"]) == ["a"]  # --tag picks from the default suites
    assert ids(tags=["t"], suites=["all"]) == ["a", "b"]
    with pytest.raises(ValueError, match="unknown suite: nope"):
        load_tasks(suites=["nope"], root=root)
    write_task(root, "other", "a")
    with pytest.raises(ValueError, match="'a' is in suites base and other"):
        load_tasks(suites=["all"], root=root)


def test_suite_defaults_fill_tasks(tmp_path):
    root = make_suites(tmp_path, s='[defaults]\ntimeout = 900\ndeny = ["shell:a*"]\n'
                                   '[defaults.settings]\nrequest_limit = 60\nshell_timeout = 5\n')
    write_task(root, "s", "plain")
    write_task(root, "s", "own", 'prompt = "x"\nfixture = "shop"\ntimeout = 30\ndeny = ["shell:b*"]\n'
                                 '[settings]\nrequest_limit = 10\n[judge]\nrubric = "y"\n')
    tasks = {t.id: t for t in load_tasks(suites=["s"], root=root)}
    assert tasks["plain"].timeout == 900 and tasks["own"].timeout == 30
    assert tasks["plain"].deny == ["shell:a*"] and tasks["own"].deny == ["shell:a*", "shell:b*"]
    assert tasks["own"].settings == {"request_limit": 10, "shell_timeout": 5}
    assert tasks["plain"].repo == FIXTURES_DIR / "shop"
    (root / "s" / "fixtures" / "shop").mkdir(parents=True)  # a suite's own fixture comes first
    assert load_tasks(["plain"], root=root)[0].repo == root / "s" / "fixtures" / "shop"


def test_trials_and_jobs_come_from_the_suite_unless_given():
    a = Suite(name="a", defaults={"trials": 3, "jobs": 2})
    b = Suite(name="b", defaults={"jobs": 6})
    task = Task(prompt="x", suite=a, judge={"rubric": "y"})
    assert trials_for(task, RunConfig()) == 3 and trials_for(task, RunConfig(trials=1)) == 1
    assert trials_for(Task(prompt="x", judge={"rubric": "y"}), RunConfig()) == 1
    assert jobs_for([a, b], RunConfig()) == 2 and jobs_for([a, b], RunConfig(jobs=8)) == 8
    assert jobs_for([Suite(name="c")], RunConfig()) == 4


def test_missing_requirements(tmp_path):
    suites = [Suite(name="net", requires={"network": True, "sandbox": ["docker"]}),
              Suite(name="cmd", requires={"commands": ["sh", "no-such-command-x"]})]
    settings = make_settings(RunConfig(user_config=tmp_path / "none.toml"), None, tmp_path, tmp_path / "home",
                             tmp_path / "logs")
    missing = missing_requirements(suites, RunConfig(sandbox="off", network=False), settings)
    assert missing == ["net: needs --sandbox docker", "net: needs network access; leave out --no-network",
                       "cmd: needs no-such-command-x on PATH"]
    asked = []

    def find(image, names):
        asked.append(image)
        return {"no-such-command-x"}

    missing = missing_requirements(suites, RunConfig(sandbox="docker"), settings, find=find)
    assert missing == [f"cmd: needs sh in the image {asked[0]}"] and asked[0].startswith("jimbo-sandbox")


HOOKS = '''
FAIL = False

def tasks(suite):
    return [{"id": f"src-{n}", "dir": str(suite.dir), "prompt": "fix it", "fixture": "shop"} for n in (1, 2)]

def grade(suite, run_dir):
    if FAIL:
        raise RuntimeError("harness broke")
    out = []
    for trial in sorted(run_dir.glob("src-*/*")):
        ok = trial.parent.name == "src-1" and "end]" in (trial / "diff.patch").read_text()
        out.append({"task": trial.parent.name, "trial": int(trial.name), "pass": ok, "text": "graded"})
    return out
'''


async def test_a_suite_with_a_source_and_a_grader(tmp_path, monkeypatch):
    import sys
    (tmp_path / "fake_suite_hooks.py").write_text(HOOKS)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "fake_suite_hooks", raising=False)
    import fake_suite_hooks
    root = make_suites(tmp_path / "suites", ds='source = "fake_suite_hooks:tasks"\ngrade = "fake_suite_hooks:grade"\n')
    tasks = load_tasks(suites=["ds"], root=root)
    assert [t.id for t in tasks] == ["src-1", "src-2"] and tasks[0].suite_grade == "fake_suite_hooks:grade"
    with pytest.raises(ValueError, match="no way to pass"):  # without the grader, the tasks can't be graded
        (root / "ds" / "suite.toml").write_text('source = "fake_suite_hooks:tasks"\n')
        load_tasks(suites=["ds"], root=root)
    (root / "ds" / "suite.toml").write_text('source = "fake_suite_hooks:tasks"\ngrade = "fake_suite_hooks:grade"\n')

    # the grader breaks: trials stay pending
    fake_suite_hooks.FAIL = True
    cfg = RunConfig(sandbox="off", user_config=tmp_path / "none.toml", main_model=fixing_model())
    run_dir = await run_suite(tasks, cfg, results_dir=tmp_path / "results")
    rows = {r["task"]: r for r in map(json.loads, (run_dir / "results.jsonl").read_text().splitlines())}
    assert rows["src-1"]["pass"] is None and rows["src-1"]["suite"] == "ds"
    report = run_report(run_dir)
    assert "pending" in report and "harness broke" in report and "0/0 trials passed (0%), 2 pending" in report
    assert "pending" in write_html_report(run_dir).read_text()

    # grade again: src-1's fix passes, src-2 fails
    fake_suite_hooks.FAIL = False
    suite = load_suites(root)["ds"]
    assert await grade_run(run_dir, {"ds": suite}) == []
    rows = {r["task"]: r for r in map(json.loads, (run_dir / "results.jsonl").read_text().splitlines())}
    assert rows["src-1"]["pass"] is True and rows["src-2"]["pass"] is False
    assert "1/2 trials passed" in run_report(run_dir)
    await grade_run(run_dir, {"ds": suite})  # grading twice doesn't repeat the grader's output
    assert (run_dir / "src-1" / "1" / "check.txt").read_text().count("graded") == 1
    assert json.loads((run_dir / "run.json").read_text())["suites"]["ds"]["grade"] == "fake_suite_hooks:grade"


def test_reports_show_a_line_per_suite(tmp_path):
    row = lambda task, suite, ok: {"task": task, "suite": suite, "trial": 1, "pass": ok, "requests": 1,
                                   "input_tokens": 1, "output_tokens": 1, "seconds": 1, "cost": None}
    for name, rows in (("a", [row("x", "core", True), row("y", "big", False)]),
                       ("b", [row("x", "core", True), row("y", "big", True)])):
        (tmp_path / name).mkdir()
        (tmp_path / name / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    report = run_report(tmp_path / "a")
    assert "big: 0/1 trials passed (0%)\ncore: 1/1 trials passed (100%)" in report
    assert "  big: A 0%, B 100%" in compare_report(tmp_path / "a", tmp_path / "b")
    one = tmp_path / "old"  # a run from before suites: one suite, no suite lines
    one.mkdir()
    (one / "results.jsonl").write_text(json.dumps({k: v for k, v in row("x", "", True).items() if k != "suite"}) + "\n")
    assert "core:" not in run_report(one) and "\nx      core  pass" in run_report(one)
