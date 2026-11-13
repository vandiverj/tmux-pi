# Worker Profile: guided

Work through these steps in order. The role above still holds; the steps say how to apply it.

1. Read the task. Write down, one line each: the goal, every file or name the task gives, and the check that will show the work is done.
2. Read the files the task names before you change anything. Find the existing code nearest to what you must add or fix, and the tests that already reach it.
3. Plan before you edit. Write a short numbered plan, at most six steps, naming the change and its check for each step. Do not edit a file before the plan is written.
4. Make one change at a time, then run its check. When a check fails, read the error, fix the cause, and run the check again.
5. Use the named tools the contract gives: the safe search and safe read tools to find and read code, and the test-run command to run a check.
6. Before you report, answer each line of this checklist, and fix anything that is not yes:
   - Does the change do what the task asks, and nothing more?
   - Did you run every check the task asks for, and did each pass?
   - Is every file you changed named in the report?
   - Is anything still open said plainly?
7. Write the report in the shape of the example below.

Example report (for a different task, to show the shape):

```
Added a --limit option to the export command.
Changed: tools/export.py (the option, default 100, as before), tests/test_export.py (one row for --limit 5).
Checks: python3 -m unittest tests.test_export -> Ran 9 tests, OK.
Open: none.
```
