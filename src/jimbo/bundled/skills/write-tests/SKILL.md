---
name: write-tests
description: Write tests for new or existing code in the style the project already uses
---
1. Find the existing tests for nearby code. Use the same framework, file layout and helpers.
2. List the behavior to cover: the normal case, edge cases and error cases.
3. Test behavior through the public interface, not implementation details.
4. Run the new tests. If a test fails, find out whether the test or the code is wrong.
5. Check that the tests can fail. For each behavior in your list, break it in the code, run the tests and
   check that one fails. Restore the code after each change. Add a test for any break that no test catches.

Report what you covered and the test results.
