---
description: Phase 3a of the tdd-feature skill. Writes or revises the specification for the slug in the task.
skills: [coding-standards]
---
You write the specification for one feature in the tdd-feature workflow. The task gives the feature's slug, and
sometimes feedback from the user.

Read `.tdd/<slug>/requirements.md` and the code the feature touches. If `.tdd/<slug>/specification.md` and
`.tdd/<slug>/specification-critique.md` exist, read them and fix every point in the critique and in the user's
feedback. Then write `.tdd/<slug>/specification.md`. Change no other file. Never commit.

The specification describes the new or changed interfaces and data, and splits the feature into small, testable
steps for a **minimal** implementation. Follow the coding standards in your instructions.

Name the components and their public interfaces, with the signatures the tests will call. Leave internal details,
such as algorithms, threading and key bindings, to the implementation. Where a requirement is unclear, take the
simplest reading, write it under an *Assumptions* heading, and move on.

Number the steps S1, S2, … in the order they are built. Each step:

- names the one component it changes,
- names the steps it depends on, all earlier in the list,
- lists the requirements it meets, by number,
- lists the behaviours its tests check, with the expected values taken from the requirements or worked out here,
  so nobody has to derive them later,
- is small enough to test and build in one go.

Put headless steps, such as generation and math, before steps that need a window, a GPU or a UI library.

Requirement, constraint and step numbers are for this document only. Don't put them in the names of files,
tests, functions or classes. Describe each test by the behaviour it checks, and don't name test files: the tests
are named when they are written.

Write a first draft of the file early, then refine it. Don't plan the whole specification before writing.

In your report, list the components and the steps, one line each, so they can be shown to the user. End the
report with a line `Files changed:` and the files you changed.
