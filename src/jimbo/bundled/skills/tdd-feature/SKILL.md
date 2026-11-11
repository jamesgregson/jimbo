---
name: tdd-feature
description: Build a new feature test-first, from requirements through specification to reviewed code. Use when the user asks to build a feature with TDD, or to resume an in-flight TDD feature by its slug
request_limit: 300
---

Build a feature in four phases. You run the workflow: you talk to the user, start one agent for each piece of work, and keep the state. The agents write the specification, the tests, the code and the reviews. You never write those yourself.

Each feature has a slug and keeps its files in `.tdd/`:

| File | Written by | Contents |
|---|---|---|
| `.tdd/in-flight.json` | you | where each unfinished feature is in the workflow |
| `.tdd/<slug>/requirements.md` | you, in Phase 2 | numbered requirements and constraints |
| `.tdd/<slug>/specification.md` | `tdd-spec` | the specification, split into steps S1, S2, … |
| `.tdd/<slug>/specification-critique.md` | `tdd-critique` | the latest critique, or "APPROVED" |
| `.tdd/<slug>/tests.md` | `tdd-tests` | each spec step and the tests that cover it |
| `.tdd/<slug>/review.md` | `tdd-review` | the latest review, or "APPROVED" |

Never change project files yourself. Never commit: the user handles commits, and the project may not use git.

## State

`.tdd/in-flight.json` has this format:

```json
{
  "features": {
    "<slug>": {
      "summary": "One line describing the feature",
      "phase": "requirements",
      "round": 1,
      "done": [],
      "step": null,
      "steps_done": [],
      "changed_files": [],
      "updated": "2026-11-10T17:42:00+00:00"
    }
  }
}
```

- `phase`: `requirements`, `specification` or `implementation`.
- `round`: the current round of the phase. It starts at 1 when a phase starts, and goes up by 1 when a new round starts. In Phase 4 it counts the rounds on the current spec step, and starts at 1 again when `step` changes.
- `done`: the parts finished in the current round, in order. It is cleared when a new round, phase or spec step starts. The parts are:
  - `requirements`: `interview`, `confirm`
  - `specification`: `write`, `critique`, `confirm`
  - `implementation`: `tests`, `code`, `review`
- `step`: the spec step being built in Phase 4, such as `"S3"`, or `"final"` for the last review. It is `null` before Phase 4.
- `steps_done`: the spec steps the review has approved, in order.
- `changed_files`: the project files changed in Phase 4, taken from the `Files changed:` line of each agent's report. Each file is listed once.
- `updated`: when the entry last changed, as an ISO 8601 timestamp with a UTC offset. Get the time from `date -u +%Y-%m-%dT%H:%M:%S+00:00`.

These fields are the whole state. Don't add other fields, notes or plans to the file. If the file has a field that isn't listed here, ignore it and remove it when you next write the file.

Only you update this file. Update it after each agent finishes, before you start the next one.

## Every turn

Do this before anything else in every turn, including short replies such as "carry on". Don't rely on what you remember from an earlier turn.

1. Read `.tdd/in-flight.json`.
2. Pick the feature:
   - If the user gives a slug, use that feature.
   - If the user gives no slug and one feature is in flight, resume it and tell the user which slug you chose.
   - If the user gives no slug and several features are in flight, list them and ask which one.
   - If the user describes a new feature, go to Phase 1.
3. Continue from the phase and round in the file, and in Phase 4 from its `step`, at the first part that is not in `done`.

Don't restart at Phase 1 for a feature that is already in the file. Keep going through the phases until the feature is finished, you need the user, or you are told to wrap up. When you are told to wrap up, update `in-flight.json` and stop. The user's next message continues from the file.

## Agents

Start each agent with `run_agent`. Use only these agents for this workflow:

| Work | Agent | Task |
|---|---|---|
| Phase 3a: write the specification | `tdd-spec` | the slug, and the user's feedback if there is any |
| Phase 3b: critique it | `tdd-critique` | the slug |
| Phase 4a: tests for a step | `tdd-tests` | the slug and the step |
| Phase 4b: code for a step | `tdd-code` | the slug and the step |
| Phase 4c: review | `tdd-review` | the slug, the step or `final`, the files to review, and the `Disputed tests:` line from `tdd-code`'s report if it has one |

Each agent already has its instructions and reads the files in `.tdd/<slug>/` itself. Keep the task to what the table lists, such as `slug: sky-model, step: S3`. Don't paste this skill, the specification or code into it, and don't add your own instructions or findings. The one thing you pass on is a `Disputed tests:` line: copy it from `tdd-code`'s report into the `tdd-review` task as it is, so the review can rule on it.

Don't read the specification, the code or the tests yourself in Phases 3 and 4. Read `in-flight.json` and the agents' reports. That keeps your requests for running the workflow.

Start one agent at a time and wait for its report. A report ends with `Files changed:`, which may say `none` when the agent had nothing to do, and a critique or review also has `Result: APPROVED` or `Result: PROBLEMS`.

If an agent stops early, such as a report that says it didn't finish or was cut off, or a report with no `Files changed:` line:

- Don't do its work yourself, and don't write what is left into `in-flight.json`.
- Add any files its report names to `changed_files`, then start the same agent again with the same task. It continues from the files on disk.
- If it stops early twice on the same task, stop and tell the user what the report said. In Phase 4 this usually means the step is too large, and the specification needs another round to split it.

If an agent reports that it stopped because something is missing or wrong, such as an expected value, an interface or a package, tell the user and ask what to do.

## Phase 1: Start

Pick a short kebab-case slug of 2–3 words that doesn't match an existing folder in `.tdd/`. The slug is for the workflow only. Create `.tdd/<slug>/`, add the feature to `in-flight.json` with `phase: "requirements"`, and tell the user the slug.

## Phase 2: Requirements

You do this phase yourself, because it needs the user. Only write `requirements.md`.

1. Read the user's description of the feature. If there is none, ask for one. Read the README, CONTRIBUTING and other top-level docs for context. Don't read other features in `.tdd/`.
2. Load the `grill-me` skill and interview the user. Ask about user-facing behaviour: UI/UX, user stories and constraints. Don't ask about implementation unless the user raises it.
3. When the interview ends, write the decisions to `requirements.md` as numbered requirements (R1, R2, …) and constraints (C1, C2, …). Leave out implementation choices unless the user asked for them. Show the file to the user and ask them to confirm it. If they correct it, update the file, interview again if the change opens new questions, and ask again.

Done when the user has confirmed `requirements.md`.

## Phase 3: Specification

Each round runs `tdd-spec`, then `tdd-critique`.

If the critique's result is `PROBLEMS`, start another round: `tdd-spec` fixes the points in the critique file, and `tdd-critique` checks the result. Don't fix the specification yourself.

A phase has at most 3 rounds. If the critique in round 3 still says `PROBLEMS`, stop: show the user the problems in the critique file and ask whether to accept them, change the requirements, or run one more round. Don't start round 4 unless the user asks for it.

Then show the user the components and steps from `tdd-spec`'s latest report and the path of the specification, and ask them to confirm it. If they ask for changes, start another round and give `tdd-spec` their feedback.

Done when the latest critique says "APPROVED", or the user has accepted the remaining problems, and the user has confirmed the specification. Don't start Phase 4 before that.

## Phase 4: Implementation

Build the spec steps one at a time, in order. When Phase 4 starts, set `step` to the first step.

Each round on a step runs `tdd-tests`, then `tdd-code`, then `tdd-review`, each with the slug and that step. Give `tdd-review` the files the first two changed for the step. After each agent finishes, add the files in its `Files changed:` line to `changed_files` and add the part to `done`.

If the review's result is `PROBLEMS`, start another round on the same step. Skip `tdd-tests` and mark `tests` done if the review lists no test problems. Always run `tdd-code`: a corrected test can show a fault in the code, and `tdd-code` stops at once when there is nothing to do. 

A step has at most 3 rounds. If the review in round 3 still says `PROBLEMS`, stop: show the user the problems in `review.md` and ask whether to accept them or run one more round. Don't start round 4 unless the user asks for it.

When the review approves the step, or the user accepts its remaining problems, add it to `steps_done`, set `step` to the next step, and start again at round 1. Don't start a step before the one before it is done.

When every step is in `steps_done`, set `step` to `"final"` and run `tdd-review` once with `final` and all of `changed_files`. If its result is `PROBLEMS`, run rounds of `tdd-tests`, `tdd-code` and `tdd-review` with `final`, with the same limit of 3 rounds and the same stop.

Done when every step is in `steps_done` and the final review says "APPROVED", or the user has accepted the remaining problems.

## Finish

Remove the feature from `in-flight.json`. Tell the user what was built, the files in `changed_files`, the test results from the final review, and that `.tdd/<slug>/` still holds the requirements, specification and review.
