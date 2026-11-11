---
name: tdd-feature
description: Build a new feature test-first, from requirements through specification to reviewed code. Use when the user asks to build a feature with TDD, or to resume an in-flight TDD feature by its slug
---

Build a feature in four phases. Each feature has a slug and keeps its state in `.tdd/`:

| File | Written by | Contents |
|---|---|---|
| `.tdd/in-flight.json` | you (main session) | where each unfinished feature is in the workflow |
| `.tdd/<slug>/requirements.md` | Phase 2 | numbered requirements and constraints |
| `.tdd/<slug>/specification.md` | Phase 3a | the specification |
| `.tdd/<slug>/specification-critique.md` | Phase 3b | the latest critique, or "APPROVED" |
| `.tdd/<slug>/tests.md` | Phase 4a | each spec step and the tests that cover it |
| `.tdd/<slug>/review.md` | Phase 4c | the latest review, or "APPROVED" |

Never change project files before Phase 4. Never commit: the user handles commits, and the project may not use git.

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
      "changed_files": [],
      "updated": "2026-11-10T17:42:00+00:00"
    }
  }
}
```

- `phase`: `requirements`, `specification` or `implementation`.
- `round`: the current round of the phase. It starts at 1 when a phase starts, and goes up by 1 when a new round starts.
- `done`: the steps finished in the current round, in order. It is cleared when a new round or phase starts. The steps are:
  - `requirements`: `interview`, `confirm`
  - `specification`: `write`, `critique`, `confirm`
  - `implementation`: `tests`, `code`, `review`
- `changed_files`: the project files changed in Phase 4, from the agents' reports. Each file is listed once.
- `updated`: when the entry last changed, as an ISO 8601 timestamp with a UTC offset.

Only you update this file, after each step finishes. Agents never change it.

To resume, the user gives a slug. Continue the phase and round in the file, with the first step that is not in `done`. If the user asks to resume but gives no slug, list the features in the file and ask which one.

## Agents

Run Phases 3a, 3b, 4a, 4b and 4c each in a new `tdd` agent. The agent starts with no context, so its task must contain the step's section of this skill, the slug, and the full path of `references/coding-standards.md` in this skill's directory. Phase 4 tasks also need `changed_files` and the latest `review.md` if there is one.

## Phase 1: Start

Pick a short kebab-case slug of 2–3 words that doesn't match an existing folder in `.tdd/`. The slug is for the workflow only. Don't use it in code. Create `.tdd/<slug>/`, add the feature to `in-flight.json` with `phase: "requirements"`, and tell the user the slug.

## Phase 2: Requirements

Run this phase in the main session, because it needs the user. Only write `requirements.md`.

1. Read the user's description of the feature. If there is none, ask for one. Read the README, CONTRIBUTING and other top-level docs for context. Don't read other features in `.tdd/`.
2. Load the `grill-me` skill and interview the user. Ask about user-facing behaviour: UI/UX, user stories and constraints. Don't ask about implementation unless the user raises it.
3. When the interview ends, write the decisions to `requirements.md` as numbered requirements (R1, R2, …) and constraints (C1, C2, …). Leave out implementation choices unless the user asked for them. Show the file to the user and ask them to confirm it. If they correct it, update the file, interview again if the change opens new questions, and ask again.

Done when the user has confirmed `requirements.md`.

## Phase 3: Specification

Each round runs Phase 3a, then Phase 3b. Each agent may only write its own output file.

If the critique lists problems, start another round. After 3 rounds, show the user the remaining problems. Then show the user the specification and ask them to confirm it. If they ask for changes, start another round with their feedback.

Done when the latest critique says "APPROVED", or the user has accepted the remaining problems, and the user has confirmed the specification.

### Phase 3a: Write

Read `requirements.md`, the coding standards and the code the feature touches. If `specification.md` and `specification-critique.md` exist, read them and fix every point in the critique. Write `specification.md`. It describes the new or changed interfaces and data, and splits the feature into small, testable steps for a **minimal** implementation. Each step names the requirements it meets and the tests that prove it.

Name the components and their public interfaces. Leave internal details, such as algorithms, threading and key bindings, to Phase 4. Where a requirement is unclear, take the simplest reading, write it under an *Assumptions* heading, and move on. The critique and the user will catch a wrong reading.

Write a first draft of `specification.md` early, then refine it. Don't plan the whole specification before writing the file.

### Phase 3b: Critique

Assume the specification is careless. Check it against `requirements.md` and the coding standards, in particular DRY, KISS and YAGNI. Look hardest for over-engineering. Check that each assumption is a reasonable reading of the requirements. Don't report internal details as missing: they belong in Phase 4. The project's own conventions come first. Overwrite `specification-critique.md` with each problem, or with "APPROVED" if there are none. Don't propose fixes.

## Phase 4: Implementation

Each round runs Phase 4a, then 4b, then 4c. Each agent may only change the files its phase allows. After each agent finishes, add the files its report lists to `changed_files`.

If the review lists problems, start another round. Skip Phase 4a and mark `tests` done if the review lists no test problems. After 5 rounds, show the user the remaining problems.

Done when every step in `specification.md` has tests listed in `tests.md`, the full test suite passes, and the latest review says "APPROVED" or the user has accepted the remaining problems.

### Phase 4a: Tests

Read `specification.md`, `tests.md` and `review.md` if they exist, and the existing tests. Load the `write-tests` skill and follow the project's test style. Write tests for every spec step that has none, and fix any test problem in the review. Run the new tests and check that each one fails because the feature is missing, not because of a syntax, import or setup error. Write `tests.md`, listing each spec step and the tests that cover it. Only change test files and `tests.md`.

### Phase 4b: Code

Read `specification.md`, `tests.md`, `review.md` if it exists, and the code the feature touches. Write the minimal code that makes the failing tests pass and fixes any code problem in the review. Follow the coding standards. Run the full test suite. Don't change test files. If a test looks wrong, say why in your report and leave it as it is.

### Phase 4c: Review

Assume the code and tests are careless. Read every file in `changed_files` in full, and check them against `specification.md`, `tests.md` and the coding standards. Check that:

  - Every spec step has tests, and each test checks the behaviour its step describes.
  - Every test in `tests.md` still exists and is not skipped or disabled.
  - The code does no more than the specification asks for.
  - The full test suite passes. Run it.

Overwrite `review.md` with each problem, marked as a test problem or a code problem, or with "APPROVED" if there are none. Don't propose fixes. Don't change any other file.

## Finish

Remove the feature from `in-flight.json`. Tell the user what was built, the files in `changed_files`, the test results, and that `.tdd/<slug>/` still holds the requirements, specification and review.
