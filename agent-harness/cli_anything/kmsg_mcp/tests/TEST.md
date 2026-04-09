# TEST.md — cli-anything-kmsg-mcp Test Plan & Results

## Test Inventory Plan

| File | Description | Estimated Tests |
|------|-------------|-----------------|
| `test_core.py` | Unit tests (synthetic data, no external deps) | ~30 |
| `test_full_e2e.py` | E2E tests (real kmsg binary) + subprocess tests | ~12 |

## Unit Test Plan (`test_core.py`)

### messaging.py
- `read_chat()`: empty chat name, valid call structure, format_messages_human
- `send_text()`: empty chat, empty message, valid call structure
- `send_image_file()`: empty chat, empty path, valid call structure
- `format_messages_human()`: empty list, messages with/without timestamps
- Edge cases: whitespace-only inputs, long messages

### session.py
- `Session.__init__()`: default values, custom values
- `Session.set_chat()`: updates active_chat
- `Session.log_read/log_send/log_send_image()`: appends entries
- `Session.to_dict()`: serialization correctness
- `Session.save() / Session.load()`: round-trip persistence
- `Session.get_history()`: with/without chat filter, limit
- `list_sessions()`: empty dir, multiple sessions
- `delete_session()`: existing session, non-existent session
- Edge cases: long messages truncated in log, unicode chat names

### status.py
- `get_full_status()`: delegates to backend correctly
- `get_kmsg_version()`: delegates to backend correctly

### kmsg_backend.py
- `find_kmsg()`: KMSG_BIN env, which fallback, not found error
- `run_kmsg()`: timeout handling, OSError handling
- `_classify_error()`: all error codes (BIN_NOT_FOUND, WINDOW_UNAVAILABLE, CHAT_NOT_FOUND, ACCESSIBILITY, UNKNOWN)
- `CommandResult`: dataclass fields
- Edge cases: empty stdout/stderr, unicode in output

## E2E Test Plan (`test_full_e2e.py`)

### Real Backend Tests (require kmsg + KakaoTalk)
- `test_status_check`: Run `kmsg status` and verify JSON output
- `test_version_check`: Run `kmsg --version` and verify output
- `test_read_messages`: Read from a real chat (requires active KakaoTalk)
- `test_send_message`: Send to a real chat (requires active KakaoTalk)

### CLI Subprocess Tests
- `test_help`: `cli-anything-kmsg-mcp --help` exits 0
- `test_version`: `cli-anything-kmsg-mcp --version` shows version
- `test_json_status`: `cli-anything-kmsg-mcp --json status` returns valid JSON
- `test_json_read`: `cli-anything-kmsg-mcp --json read <chat>` returns JSON
- `test_session_list`: `cli-anything-kmsg-mcp session list` exits 0
- `test_session_lifecycle`: create session via REPL, list, show, delete

## Realistic Workflow Scenarios

### Workflow 1: Message Monitoring
**Simulates**: Agent periodically checking a chat for new messages
**Operations**: status check → read messages → read again with higher limit
**Verified**: JSON output structure, message count, latency

### Workflow 2: Send and Verify
**Simulates**: Agent sending a message and confirming delivery
**Operations**: status check → send message → read messages → verify sent message appears
**Verified**: Send success, message appears in read results

### Workflow 3: Session Tracking
**Simulates**: Agent conducting a multi-step conversation across sessions
**Operations**: create session → set chat → send multiple → save → load → verify history
**Verified**: Session persistence, history filtering, log completeness

---

## Test Results

Run with: `CLI_ANYTHING_FORCE_INSTALLED=1 python3 -m pytest cli_anything/kmsg_mcp/tests/ -v --tb=no`

```
platform darwin -- Python 3.9.6, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/<user>/kmsg-mcp/agent-harness

cli_anything/kmsg_mcp/tests/test_core.py::TestCommandResult::test_fields PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestCommandResult::test_timeout_field PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestClassifyError::test_bin_not_found PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestClassifyError::test_window_unavailable PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestClassifyError::test_chat_not_found PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestClassifyError::test_accessibility PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestClassifyError::test_unknown PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestClassifyError::test_empty_text PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFindKmsg::test_env_variable PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFindKmsg::test_which_fallback PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFindKmsg::test_not_found_raises PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestRunKmsg::test_successful_run PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestRunKmsg::test_nonexistent_command PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestRunKmsg::test_timeout PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFormatMessagesHuman::test_empty_list PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFormatMessagesHuman::test_with_timestamps PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFormatMessagesHuman::test_without_timestamps PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestFormatMessagesHuman::test_missing_fields PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestReadChat::test_empty_chat_name PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestReadChat::test_whitespace_chat_name PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestReadChat::test_valid_call PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSendText::test_empty_chat PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSendText::test_empty_message PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSendText::test_valid_call PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSendImageFile::test_empty_chat PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSendImageFile::test_empty_path PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSendImageFile::test_valid_call PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_init_defaults PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_init_custom PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_set_chat PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_log_read PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_log_send PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_log_send_truncates_long_message PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_log_send_image PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_to_dict PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_save_and_load PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_load_not_found PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_get_history_all PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_get_history_filtered PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_get_history_limit PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestSession::test_unicode_chat_name PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestListSessions::test_empty PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestListSessions::test_multiple PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestDeleteSession::test_existing PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestDeleteSession::test_nonexistent PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestStatus::test_get_full_status PASSED
cli_anything/kmsg_mcp/tests/test_core.py::TestStatus::test_get_kmsg_version PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestKmsgBackendReal::test_find_kmsg_binary PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestKmsgBackendReal::test_version_check PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestKmsgBackendReal::test_status_check PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_version_flag PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_version PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_help_read PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_help_send PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_help_send_image PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_status_command PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_status PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_session_list PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_session_list PASSED
cli_anything/kmsg_mcp/tests/test_full_e2e.py::TestSessionE2E::test_session_create_save_load PASSED

============================== 61 passed in 0.63s ==============================
```

### Summary Statistics

| Metric | Value |
|--------|-------|
| Total tests | 61 |
| Passed | 61 |
| Failed | 0 |
| Pass rate | 100% |
| Execution time | 0.63s |
| Unit tests (test_core.py) | 47 |
| E2E tests (test_full_e2e.py) | 14 |

### Subprocess Test Verification

```
[_resolve_cli] Using installed command: /Users/<user>/Library/Python/3.9/bin/cli-anything-kmsg-mcp
```

Confirms subprocess tests ran against the real installed binary (not module fallback).

### Coverage Notes

- All core modules fully covered (messaging, session, status, backend)
- All error classification codes tested
- Session round-trip (save/load) with unicode verified
- Real kmsg binary (v0.2.6) tested for version and status
- All CLI subcommands tested via subprocess (help, version, status, session)
- Read/send with real KakaoTalk chats not tested (would send real messages)
