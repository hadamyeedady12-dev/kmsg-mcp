#!/usr/bin/env python3
"""MCP stdio server that exposes kmsg read/send/send-image tools for OpenClaw.

This server intentionally uses only Python's standard library so it can run
without extra package installation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

JSONDict = Dict[str, Any]


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    latency_ms: int
    timed_out: bool = False


class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Optional[JSONDict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class KmsgRunner:
    def __init__(self) -> None:
        self.kmsg_bin = self._resolve_kmsg_bin()

    def _resolve_kmsg_bin(self) -> str:
        env_bin = os.environ.get("KMSG_BIN", "").strip()
        if env_bin:
            return env_bin

        which_bin = shutil.which("kmsg")
        if which_bin:
            return which_bin

        fallback = os.path.expanduser("~/.local/bin/kmsg")
        return fallback

    def run(self, args: List[str], timeout_sec: float) -> CommandResult:
        start = time.time()
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            latency_ms = int((time.time() - start) * 1000)
            return CommandResult(
                returncode=127,
                stdout="",
                stderr=str(exc),
                latency_ms=latency_ms,
                timed_out=False,
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            latency_ms = int((time.time() - start) * 1000)
            return CommandResult(
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                latency_ms=latency_ms,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            latency_ms = int((time.time() - start) * 1000)
            return CommandResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                latency_ms=latency_ms,
                timed_out=True,
            )

    def check_ready(self) -> Tuple[bool, JSONDict]:
        version = self.run([self.kmsg_bin, "--version"], timeout_sec=2.0)
        if version.returncode != 0:
            return False, {
                "stage": "version",
                "message": "kmsg binary not executable",
                "stdout": version.stdout,
                "stderr": version.stderr,
                "kmsg_bin": self.kmsg_bin,
            }

        status = self.run([self.kmsg_bin, "status"], timeout_sec=4.0)
        if status.returncode != 0:
            return False, {
                "stage": "status",
                "message": "kmsg status check failed",
                "stdout": status.stdout,
                "stderr": status.stderr,
                "kmsg_bin": self.kmsg_bin,
            }

        return True, {
            "kmsg_bin": self.kmsg_bin,
            "version": version.stdout.strip(),
        }


def _json_rpc_error(req_id: Any, code: int, message: str, data: Optional[JSONDict] = None) -> JSONDict:
    payload: JSONDict = {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data:
        payload["error"]["data"] = data
    return payload


def _json_rpc_result(req_id: Any, result: JSONDict) -> JSONDict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    }


def _make_text_content(text: str) -> List[JSONDict]:
    return [{"type": "text", "text": text}]


class OpenClawKmsgMCPServer:
    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self) -> None:
        self.runner = KmsgRunner()
        self.shutdown = False
        self.initialized = False
        self._write_lock = threading.Lock()

        deep_recovery_default = os.environ.get("KMSG_DEFAULT_DEEP_RECOVERY", "false").lower() == "true"
        trace_default = os.environ.get("KMSG_TRACE_DEFAULT", "false").lower() == "true"

        self.defaults = {
            "deep_recovery": deep_recovery_default,
            "trace_ax": trace_default,
        }
        self.server_version = self._resolve_server_version()

    def _resolve_server_version(self) -> str:
        explicit = os.environ.get("KMSG_MCP_VERSION", "").strip()
        if explicit:
            return explicit

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        version_path = os.path.join(repo_root, "VERSION")
        try:
            with open(version_path, "r", encoding="utf-8") as fp:
                for line in fp:
                    candidate = line.strip()
                    if candidate:
                        return candidate
        except OSError:
            pass

        return "0.0.0"

    def _read_message(self) -> Optional[JSONDict]:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        decoded = line.decode("utf-8", errors="replace").strip()
        if not decoded:
            return None
        # Auto-detect: Content-Length header (LSP) vs newline-delimited JSON
        if decoded.lower().startswith("content-length"):
            content_length = int(decoded.split(":", 1)[1].strip())
            # Consume blank line separator
            sys.stdin.buffer.readline()
            body = sys.stdin.buffer.read(content_length)
            if not body:
                return None
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return None
        else:
            # Newline-delimited JSON (Claude Code MCP SDK format)
            try:
                return json.loads(decoded)
            except json.JSONDecodeError:
                return None

    def _write_message(self, payload: JSONDict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            sys.stdout.buffer.write(line.encode("utf-8"))
            sys.stdout.buffer.flush()

    def _tool_definitions(self) -> List[JSONDict]:
        return [
            {
                "name": "kmsg_read",
                "description": "Read recent KakaoTalk messages from a chat via kmsg.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Chat room or user name"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                        "deep_recovery": {
                            "type": "boolean",
                            "default": self.defaults["deep_recovery"],
                            "description": "Enable deep recovery mode for window resolution",
                        },
                        "keep_window": {
                            "type": "boolean",
                            "default": False,
                            "description": "Keep auto-opened KakaoTalk window",
                        },
                        "trace_ax": {
                            "type": "boolean",
                            "default": self.defaults["trace_ax"],
                            "description": "Include AX tracing logs",
                        },
                    },
                    "required": ["chat"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "kmsg_send",
                "description": "Send a KakaoTalk message via kmsg. Default sends immediately; confirm=true triggers confirmation-required response.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Chat room or user name"},
                        "message": {"type": "string", "description": "Message body"},
                        "confirm": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, do not send and return CONFIRMATION_REQUIRED",
                        },
                        "deep_recovery": {
                            "type": "boolean",
                            "default": self.defaults["deep_recovery"],
                            "description": "Enable deep recovery mode for window resolution",
                        },
                        "keep_window": {
                            "type": "boolean",
                            "default": False,
                            "description": "Keep auto-opened KakaoTalk window",
                        },
                        "trace_ax": {
                            "type": "boolean",
                            "default": self.defaults["trace_ax"],
                            "description": "Include AX tracing logs",
                        },
                    },
                    "required": ["chat", "message"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "kmsg_send_image",
                "description": "Send an image to a KakaoTalk chat via kmsg. Default sends immediately; confirm=true triggers confirmation-required response.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Chat room or user name"},
                        "image_path": {"type": "string", "description": "Path to the image file"},
                        "confirm": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, do not send and return CONFIRMATION_REQUIRED",
                        },
                        "deep_recovery": {
                            "type": "boolean",
                            "default": self.defaults["deep_recovery"],
                            "description": "Enable deep recovery mode for window resolution",
                        },
                        "keep_window": {
                            "type": "boolean",
                            "default": False,
                            "description": "Keep auto-opened KakaoTalk window",
                        },
                        "trace_ax": {
                            "type": "boolean",
                            "default": self.defaults["trace_ax"],
                            "description": "Include AX tracing logs",
                        },
                    },
                    "required": ["chat", "image_path"],
                    "additionalProperties": False,
                },
            },
        ]

    def _error_payload(
        self,
        code: str,
        message: str,
        hint: str,
        raw_stdout: str,
        raw_stderr: str,
        latency_ms: int,
    ) -> JSONDict:
        return {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "hint": hint,
                "raw_stdout": raw_stdout,
                "raw_stderr": raw_stderr,
            },
            "meta": {
                "latency_ms": latency_ms,
            },
        }

    def _extract_error_code(self, combined_text: str) -> str:
        lowered = combined_text.lower()
        if "no such file or directory" in lowered or "not found" in lowered:
            return "KMSG_BIN_NOT_FOUND"
        if "WINDOW_NOT_READY" in combined_text:
            return "KAKAO_WINDOW_UNAVAILABLE"
        if "SEARCH_MISS" in combined_text:
            return "CHAT_NOT_FOUND"
        if "Accessibility" in combined_text or "손쉬운 사용" in combined_text:
            return "ACCESSIBILITY_PERMISSION_DENIED"
        return "UNKNOWN_EXEC_FAILURE"

    def _map_hint(self, code: str) -> str:
        if code == "KMSG_BIN_NOT_FOUND":
            return "Set a valid KMSG_BIN path or install kmsg into PATH."
        if code == "KAKAO_WINDOW_UNAVAILABLE":
            return "KakaoTalk window was not ready. Open KakaoTalk and retry (or enable deep_recovery)."
        if code == "CHAT_NOT_FOUND":
            return "Chat was not found in search results. Verify chat name spacing and visibility."
        if code == "ACCESSIBILITY_PERMISSION_DENIED":
            return "Grant Accessibility permission in System Settings > Privacy & Security > Accessibility."
        return "Check raw_stdout/raw_stderr and rerun with trace_ax=true for details."

    def _call_kmsg_read(self, arguments: JSONDict) -> JSONDict:
        chat = str(arguments.get("chat", "")).strip()
        if not chat:
            return self._error_payload(
                code="INVALID_ARGUMENT",
                message="chat is required",
                hint="Provide a non-empty chat name.",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )

        raw_limit = arguments.get("limit", 20)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return self._error_payload(
                code="INVALID_ARGUMENT",
                message="limit must be an integer",
                hint="Use integer range 1..100 for limit.",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )
        limit = max(1, min(limit, 100))

        deep_recovery = bool(arguments.get("deep_recovery", self.defaults["deep_recovery"]))
        keep_window = bool(arguments.get("keep_window", False))
        trace_ax = bool(arguments.get("trace_ax", self.defaults["trace_ax"]))

        cmd = [self.runner.kmsg_bin, "read", chat, "--json", "--limit", str(limit)]
        if deep_recovery:
            cmd.append("--deep-recovery")
        if keep_window:
            cmd.append("--keep-window")
        if trace_ax:
            cmd.append("--trace-ax")

        timeout_sec = 40.0 if deep_recovery else 20.0
        first = self.runner.run(cmd, timeout_sec=timeout_sec)

        if first.timed_out:
            return self._error_payload(
                code="PROCESS_TIMEOUT",
                message="kmsg read timed out",
                hint="Increase stability (keep KakaoTalk open/focused) and retry.",
                raw_stdout=first.stdout,
                raw_stderr=first.stderr,
                latency_ms=first.latency_ms,
            )

        if first.returncode != 0:
            combined = f"{first.stdout}\n{first.stderr}"
            code = self._extract_error_code(combined)

            if code == "CHAT_NOT_FOUND" and not deep_recovery:
                retry_cmd = cmd + ["--deep-recovery"]
                retry = self.runner.run(retry_cmd, timeout_sec=15.0)
                if retry.returncode == 0 and not retry.timed_out:
                    first = retry
                else:
                    retry_combined = f"{retry.stdout}\n{retry.stderr}"
                    retry_code = self._extract_error_code(retry_combined)
                    return self._error_payload(
                        code=retry_code,
                        message="kmsg read failed after deep-recovery retry",
                        hint=self._map_hint(retry_code),
                        raw_stdout=retry.stdout,
                        raw_stderr=retry.stderr,
                        latency_ms=retry.latency_ms,
                    )
            else:
                return self._error_payload(
                    code=code,
                    message="kmsg read failed",
                    hint=self._map_hint(code),
                    raw_stdout=first.stdout,
                    raw_stderr=first.stderr,
                    latency_ms=first.latency_ms,
                )

        # Empty chat room: kmsg exits 0 but prints a text message instead of JSON
        if "No message rows found" in first.stdout:
            response: JSONDict = {
                "ok": True,
                "chat": chat,
                "fetched_at": None,
                "count": 0,
                "messages": [],
                "meta": {
                    "latency_ms": first.latency_ms,
                    "empty_chat": True,
                },
            }
            if trace_ax and first.stderr.strip():
                response["meta"]["stderr_trace"] = first.stderr
            return response

        try:
            payload = json.loads(first.stdout)
        except json.JSONDecodeError:
            return self._error_payload(
                code="INVALID_JSON_OUTPUT",
                message="kmsg returned non-JSON output for read --json",
                hint="Run kmsg read manually and confirm JSON-only stdout.",
                raw_stdout=first.stdout,
                raw_stderr=first.stderr,
                latency_ms=first.latency_ms,
            )

        response: JSONDict = {
            "ok": True,
            "chat": payload.get("chat", chat),
            "fetched_at": payload.get("fetched_at"),
            "count": payload.get("count", 0),
            "messages": payload.get("messages", []),
            "meta": {
                "latency_ms": first.latency_ms,
            },
        }

        if trace_ax and first.stderr.strip():
            response["meta"]["stderr_trace"] = first.stderr

        return response

    def _call_kmsg_send(self, arguments: JSONDict) -> JSONDict:
        chat = str(arguments.get("chat", "")).strip()
        message = str(arguments.get("message", "")).strip()
        confirm = bool(arguments.get("confirm", False))

        if not chat or not message:
            return self._error_payload(
                code="INVALID_ARGUMENT",
                message="chat and message are required",
                hint="Provide both chat and message.",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )

        if confirm:
            return self._error_payload(
                code="CONFIRMATION_REQUIRED",
                message="kmsg_send blocked because confirm=true requests pre-send confirmation",
                hint="Ask user for explicit approval, then call again with confirm=false (or omit confirm).",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )

        deep_recovery = bool(arguments.get("deep_recovery", self.defaults["deep_recovery"]))
        keep_window = bool(arguments.get("keep_window", False))
        trace_ax = bool(arguments.get("trace_ax", self.defaults["trace_ax"]))

        cmd = [self.runner.kmsg_bin, "send", chat, message]
        if deep_recovery:
            cmd.append("--deep-recovery")
        if keep_window:
            cmd.append("--keep-window")
        if trace_ax:
            cmd.append("--trace-ax")

        timeout_sec = 18.0 if deep_recovery else 10.0
        run = self.runner.run(cmd, timeout_sec=timeout_sec)

        if run.timed_out:
            return self._error_payload(
                code="PROCESS_TIMEOUT",
                message="kmsg send timed out",
                hint="Retry after ensuring KakaoTalk is responsive.",
                raw_stdout=run.stdout,
                raw_stderr=run.stderr,
                latency_ms=run.latency_ms,
            )

        if run.returncode != 0:
            combined = f"{run.stdout}\n{run.stderr}"
            code = self._extract_error_code(combined)
            return self._error_payload(
                code=code,
                message="kmsg send failed",
                hint=self._map_hint(code),
                raw_stdout=run.stdout,
                raw_stderr=run.stderr,
                latency_ms=run.latency_ms,
            )

        response: JSONDict = {
            "ok": True,
            "chat": chat,
            "sent": True,
            "meta": {
                "latency_ms": run.latency_ms,
                "stdout": run.stdout,
            },
        }

        if trace_ax and run.stderr.strip():
            response["meta"]["stderr_trace"] = run.stderr

        return response

    def _call_kmsg_send_image(self, arguments: JSONDict) -> JSONDict:
        chat = str(arguments.get("chat", "")).strip()
        image_path = str(arguments.get("image_path", "")).strip()
        confirm = bool(arguments.get("confirm", False))

        if not chat or not image_path:
            return self._error_payload(
                code="INVALID_ARGUMENT",
                message="chat and image_path are required",
                hint="Provide both chat and image_path.",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )

        if confirm:
            return self._error_payload(
                code="CONFIRMATION_REQUIRED",
                message="kmsg_send_image blocked because confirm=true requests pre-send confirmation",
                hint="Ask user for explicit approval, then call again with confirm=false (or omit confirm).",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )

        if not os.path.isfile(image_path):
            return self._error_payload(
                code="INVALID_ARGUMENT",
                message="image_path must point to an existing file",
                hint="Provide a valid local image file path.",
                raw_stdout="",
                raw_stderr="",
                latency_ms=0,
            )

        deep_recovery = bool(arguments.get("deep_recovery", self.defaults["deep_recovery"]))
        keep_window = bool(arguments.get("keep_window", False))
        trace_ax = bool(arguments.get("trace_ax", self.defaults["trace_ax"]))

        cmd = [self.runner.kmsg_bin, "send-image", chat, image_path]
        if deep_recovery:
            cmd.append("--deep-recovery")
        if keep_window:
            cmd.append("--keep-window")
        if trace_ax:
            cmd.append("--trace-ax")

        timeout_sec = 20.0 if deep_recovery else 12.0
        run = self.runner.run(cmd, timeout_sec=timeout_sec)

        if run.timed_out:
            return self._error_payload(
                code="PROCESS_TIMEOUT",
                message="kmsg send-image timed out",
                hint="Retry after ensuring KakaoTalk is responsive.",
                raw_stdout=run.stdout,
                raw_stderr=run.stderr,
                latency_ms=run.latency_ms,
            )

        if run.returncode != 0:
            combined = f"{run.stdout}\n{run.stderr}"
            code = self._extract_error_code(combined)
            return self._error_payload(
                code=code,
                message="kmsg send-image failed",
                hint=self._map_hint(code),
                raw_stdout=run.stdout,
                raw_stderr=run.stderr,
                latency_ms=run.latency_ms,
            )

        response: JSONDict = {
            "ok": True,
            "chat": chat,
            "sent": True,
            "meta": {
                "latency_ms": run.latency_ms,
                "stdout": run.stdout,
            },
        }

        if trace_ax and run.stderr.strip():
            response["meta"]["stderr_trace"] = run.stderr

        return response

    def _handle_tools_call(self, arguments: JSONDict) -> JSONDict:
        name = arguments.get("name")
        call_args = arguments.get("arguments", {})
        if not isinstance(call_args, dict):
            raise MCPError(code=-32602, message="tool arguments must be an object")

        if name == "kmsg_read":
            result_obj = self._call_kmsg_read(call_args)
        elif name == "kmsg_send":
            result_obj = self._call_kmsg_send(call_args)
        elif name == "kmsg_send_image":
            result_obj = self._call_kmsg_send_image(call_args)
        else:
            raise MCPError(code=-32601, message=f"Unknown tool: {name}")

        return {
            "content": _make_text_content(json.dumps(result_obj, ensure_ascii=False, indent=2, sort_keys=True)),
            "isError": not result_obj.get("ok", False),
            "structuredContent": result_obj,
        }

    def _handle_request(self, request: JSONDict) -> Optional[JSONDict]:
        method = request.get("method")
        req_id = request.get("id")

        if method == "initialize":
            self.initialized = True
            return _json_rpc_result(
                req_id,
                {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {},
                    },
                    "serverInfo": {
                        "name": "openclaw-kmsg-mcp",
                        "version": self.server_version,
                    },
                    "instructions": (
                        "Use kmsg_read for read-only operations. "
                        "Use kmsg_send and kmsg_send_image with confirm=false (or omitted) for sending. "
                        "Use confirm=true to intentionally require a confirmation step."
                    ),
                },
            )

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return _json_rpc_result(req_id, {})

        if not self.initialized:
            raise MCPError(code=-32002, message="Server not initialized")

        if method == "tools/list":
            return _json_rpc_result(req_id, {"tools": self._tool_definitions()})

        if method == "tools/call":
            result = self._handle_tools_call(request.get("params", {}))
            return _json_rpc_result(req_id, result)

        if method == "shutdown":
            self.shutdown = True
            return _json_rpc_result(req_id, {})

        if method == "exit":
            self.shutdown = True
            return None

        raise MCPError(code=-32601, message=f"Method not found: {method}")

    def serve_forever(self) -> None:
        while not self.shutdown:
            request = self._read_message()
            if request is None:
                break

            if "method" not in request:
                continue

            req_id = request.get("id")
            try:
                response = self._handle_request(request)
            except MCPError as err:
                if req_id is None:
                    continue
                response = _json_rpc_error(req_id, err.code, err.message, err.data)
            except Exception as err:  # noqa: BLE001
                if req_id is None:
                    continue
                response = _json_rpc_error(
                    req_id,
                    -32000,
                    "Internal server error",
                    {"detail": str(err)},
                )

            if response is not None:
                self._write_message(response)


def main() -> int:
    server = OpenClawKmsgMCPServer()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
