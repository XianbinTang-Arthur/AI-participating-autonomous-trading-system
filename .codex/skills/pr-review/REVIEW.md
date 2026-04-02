---
name: pr-review
description: Use this when reviewing a branch, commit, or uncommitted changes for correctness, bugs, security, maintainability, and missing tests.
---

# Review method
Review like a strict senior engineer.

# Always check
- Functional correctness
- Edge cases
- Error handling
- Security issues
- Async / race-condition hazards
- Test sufficiency
- Unnecessary complexity

# Output format
Return:
1. Critical issues
2. Important issues
3. Minor issues
4. Suggested fixes
5. Whether the patch is safe to merge