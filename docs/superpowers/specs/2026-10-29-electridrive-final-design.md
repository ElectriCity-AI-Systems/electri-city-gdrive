# ElectriDrive 2.1.3 test finalization

The owner's mission is the specification: retain all existing files and features,
incorporate PR #19, test on Ubuntu 26.04, commit and push a separate branch and
open a PR against main. Never merge or release. Never activate personal pairs,
change personal cloud data or automatically delete cloud objects.

## Approach

Use a linked worktree of the verified existing repository at the exact PR #19
head, rather than replaying its already integrated predecessor patches or creating
an independent repository. Keep the historical 2.1.2 package outside the worktree.
Retain the existing Python/PySide6 architecture. Correct reproducible remaining
safety defects with regression tests, not broad refactoring.

## Acceptance

- Reviewed Drive IDs remain immutable until explicit local re-selection/reset.
- Ambiguous root folders and legacy binary lookups cannot choose the first ID.
- Download-only Workspace conflicts preserve local edits without cloud writes.
- Repeated API page tokens fail visibly instead of hanging.
- Offline and GUI checks use a fresh token-free XDG profile and failing keyring.
- Any live QA action validates fixture identity, bytes and parent IDs first;
  report skipped or blocked checks truthfully.
- Test/package artifacts, credentials and user settings stay outside Git.
