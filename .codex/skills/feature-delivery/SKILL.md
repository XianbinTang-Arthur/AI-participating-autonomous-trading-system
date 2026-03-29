---
name: feature-delivery
description: Use this when implementing a feature or bugfix that requires planning, code changes, validation, and a concise delivery summary.
---

# Purpose
Implement scoped changes safely and predictably.

# Workflow
1. Read the relevant files and summarize the current behavior.(under \AIParticipatingAutonomousTradingSystem\docs)
2. Create a brief plan that clearly outlines each sub-step, then generate a statement of work and save it in the directory: \AIParticipatingAutonomousTradingSystem\docs. 
Requirements:
Business objectives and boundaries
Module responsibilities and domain model
Input/output interfaces
Database schema / tables / indexes / constraints
Transactions, Consistency, Concurrency
Authorization, Authentication, Data Security
Error Handling and Idempotency
State Transition and Lifecycle
Caching and Performance
Logging, Monitoring, Auditing
Testing Strategy
Migration, Rollback, Compatibility
Configuration and Environment Isolation
Code Organization and Dependencies
Documentation and Operations Manual
Deployment and Acceptance Criteria
3. Make the smallest correct change.
4. Add or update tests.
5. Run validation commands.
6. Return a concise delivery report:
   - files changed
   - behavior changed
   - tests run
   - remaining risks

# Constraints
- Avoid unrelated refactors.
- Preserve public APIs unless explicitly instructed otherwise.
- Stop and explain if requirements conflict with the current architecture.

# Validation
Run `bash .codex/skills/feature-delivery/scripts/verify.sh`