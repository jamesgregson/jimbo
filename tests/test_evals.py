import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RunUsage

from evals.grading import check_events, repeated_denials
from evals.html_report import write_html_report
from evals.report import compare_report, cost, passes, run_report
from evals.runner import RunConfig, make_settings, request_limits, run_suite
from jimbo.core.cost import estimate_cost
from evals.side_tasks import load_compaction_cases, load_shell_cases
from evals.tasks import Task, load_task, load_tasks
from evals.validate import check_task
from jimbo.core.events import Event

TASKS = load_tasks()


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
    cfg = RunConfig(sandbox=False, user_config=tmp_path / "none.toml", main_model=fixing_model())
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
    cfg = RunConfig(sandbox=False, user_config=tmp_path / "none.toml", main_model=fixing_model(), trials=3,
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
