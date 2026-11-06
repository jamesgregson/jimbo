---
description: Reads a repo and writes specs/charter.md, which says what the project is and how mature it is.
tools: [read_file, list_dir, glob, grep, shell, write_file]
---
Read this repo and write `specs/charter.md`. If it exists, read it first and update it. Keep what is still correct.

Read the README, the build and test config, the top-level layout, a sample of the source and tests, and the
recent git log. Run the tests if there is a quick command for them. Don't change any other file.

Pick one maturity level:

| Level | Signs |
|---|---|
| prototype | exploring an idea; few or no tests; code may be thrown away |
| proof of concept | shows one approach works end to end; happy path only; little error handling |
| early product | used by some people; tests for the main paths; a README people can follow; some rough edges |
| production | relied on by users; broad tests and CI; error handling, docs and a release process; changes must not break users |

Write the charter with these sections:

1. **Purpose**: what the project does and for whom, in two or three sentences.
2. **Maturity**: the level, and the evidence for it from the repo, such as tests, CI, docs, packaging, error
   handling and commit history.
3. **What this level means for new work**: how much testing, error handling, docs and backwards compatibility a
   change here needs, and what it doesn't need yet.
4. **Stack and layout**: languages, frameworks, main directories, and the exact build, test and run commands.
5. **Conventions**: style, naming and patterns that new code should follow.

Include only what you found in the repo. Keep it under 80 lines. Report the maturity level and the file path.
