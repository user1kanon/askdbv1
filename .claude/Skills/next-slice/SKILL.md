---
name: next-slice
description: Implement the next unticked slice in PLAN.md, verify it, commit, record learnings, and stop.
---

Work on exactly one slice.

1. Open docs/PLAN.md. Find the first unticked slice. State its number and goal in one line.
2. Read every file the slice lists before changing anything. Read any notes left under the slice by earlier runs.
3. Implement the slice. Stay inside its scope; if something outside it needs changing, note it under the relevant later slice in docs/PLAN.md instead of doing it now.
4. Run the slice's verification steps. Fix failures. Do not weaken or skip a test to get green.
5. If the slice cannot be finished in this session, split it into two entries in docs/PLAN.md, finish the first, and stop after it.
6. Record what a future slice needs to know:
   - A design decision, constraint, or gotcha tied to a specific later slice → add a one-line note under that slice in docs/PLAN.md.
   - Something about how this project works that is not derivable from the code (a tool quirk, a convention the user asked for, a mistake not to repeat) → save it to auto memory.
   - Nothing worth recording is a valid outcome. Do not write filler.
7. Tick the slice in docs/PLAN.md. Commit everything as `slice N: <goal>`.
8. Log the implementation summaryto a file named slice-n-log.txt in the docs folder, where n is the slice number.
9. Stop. Do not start the next slice. Reply with three lines: what changed, what was verified, any note recorded.
