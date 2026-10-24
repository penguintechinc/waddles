# Final fix report
- Fix 1: command_owner now excludes command='*' (services/app_command_service.py); test_command_owner_excludes_wildcard_rows (failed before, passes after).
- Fix 2: seeder backfill-sweep error log now logs type(exc).__name__ only; dropped message from extra and exc_info (traceback would echo the message). Test asserts message absent.
- Tests: 98 passed. ruff check/format clean. mypy --strict: only pre-existing missing types-PyYAML stub at seed_core_bundles.py:68.
