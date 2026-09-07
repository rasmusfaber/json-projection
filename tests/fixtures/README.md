# Test fixtures

`inspect_legacy_log.json` is `tests/log/test_eval_log/log_formats.json` from
[inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) at commit 6594b06c69, MIT License,
Copyright (c) 2024 UK AI Security Institute. It is a version-2 log in the legacy format: the sample carries
a `transcript` object (11 events plus content) instead of `events`/`attachments`, reductions live under
`results.sample_reductions`, and `eval` has `task_args` but no `task_args_passed`. `tests/test_inspect_ai.py`
loads it through inspect_ai's own migrations when inspect_ai is installed.
