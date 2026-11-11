# Coding standards

Follow the project's own conventions first. Use these where the project has none.

## Design

- Build only what a requirement asks for (YAGNI). Don't add options, hooks or abstractions for uses that don't exist yet.
- Choose the simplest design that works (KISS). Prefer plain functions and data over class hierarchies and patterns.
- Don't repeat knowledge (DRY). Reuse existing code before writing new code. Wait for the third copy before you abstract.
- Give each function, class and module one job.
- Keep side effects (files, network, time, randomness) at the edges, so the core logic is easy to test.
- Depend on existing libraries in the project before adding new ones.

## Code

- Use names that say what a thing is or does. Avoid abbreviations the reader has to guess.
- Keep functions short and nesting shallow. Return early instead of nesting conditions.
- Make values immutable and scope variables tightly where the language allows.
- Don't use magic numbers or strings. Name them as constants.
- Write comments that explain why, not what. Delete commented-out code.
- Match the surrounding code's formatting. Run the project's formatter and linter.

## Errors and input

- Validate input at system boundaries, such as user input, files and network responses. Trust it inside.
- Fail early with a clear message that says what went wrong and where.
- Don't swallow errors. Handle them where you can act on them, or let them propagate.
- Release resources (files, connections, locks) on every path, including errors.
- Never hard-code secrets or log them.

## Tests

- Write the test first and watch it fail for the expected reason.
- Test behaviour through the public interface, not implementation details.
- Keep tests fast, independent and deterministic. Control time, randomness and external services.
- Cover the normal case, the edges and the error cases.
- Write one behaviour per test, with a name that says what it checks.

## Changes

- Make small changes that each leave the build and tests passing.
- Keep refactoring separate from changes in behaviour.
- Don't change code that the feature doesn't need.
- Update docs and examples that the change makes wrong.
