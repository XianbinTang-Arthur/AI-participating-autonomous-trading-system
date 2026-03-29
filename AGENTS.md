# AGENTS.md

## Working mode
Act as a careful implementation and review agent for this repository.

## Before editing
1. First, read the relevant documents, such as the project brief (if available). If none is available, please search online to find out what specifications you need to follow for this task.
2. Summarize the current behavior briefly.
3. If the task is non-trivial, propose a short plan before making changes.
4. Avoid unrelated refactors.

## Implementation rules
- Prefer minimal changes. However, do not cobble together a solution just to implement a small feature. If the current architecture cannot adequately support the feature, development should be halted and recommendations for a refactor should be proposed.
- Follow the existing code style and folder structure.
- Preserve backward compatibility unless explicitly told otherwise.
- Add or update tests for behavior changes.
- Do not silently change public APIs.
- All text displayed on the front end must be written in clean UTF-8 Chinese; be sure to avoid encoding issues;

## Validation
After making code changes, run:
1. lint
2. unit tests
3. the narrowest integration test affected by the change
4. The project runtime environment is: .\.venv\Scripts\python.exe
5. The database connection settings used by the project are located in the file: .\.env.derivatives.live, on line 19

If any command fails, explain the failure clearly. Do not claim success without running the command.

## Review checklist
Always check:
- correctness
- edge cases
- security
- performance regressions
- maintainability
- test coverage

## Final response format
Return:
1. what changed
2. risks / caveats
3. tests run and results
4. next steps only if necessary