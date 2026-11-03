---
name: debug
description: Find the cause of a bug or failing test before changing code
---
1. Reproduce the problem. Run the failing command or test and read the full output.
2. Find the cause. Follow the failure back through the code until you can say why it happens.
3. Fix the cause, not the symptom. Make the smallest change that does this.
4. Add a test that fails without the fix, if the project has tests.
5. Run the related tests and report the cause, the fix and the results.

If you can't reproduce the problem, say so and report what you tried. Don't guess at a fix.
