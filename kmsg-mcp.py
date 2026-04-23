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
        self._send_file_coord_cache: Dict[str, Tuple[int, int, float]] = {}

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
            {
                "name": "kmsg_send_file",
                "description": (
                    "Send a file (any type) to a KakaoTalk chat. "
                    "Uses macOS clipboard paste method (NSPasteboard → Cmd+V → Enter). "
                    "Works for documents, archives, videos — not just images."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Chat room or user name"},
                        "file_path": {"type": "string", "description": "Absolute path to the file to send"},
                        "confirm": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, do not send and return CONFIRMATION_REQUIRED",
                        },
                        "keep_window": {
                            "type": "boolean",
                            "default": False,
                            "description": "Keep auto-opened KakaoTalk window",
                        },
                    },
                    "required": ["chat", "file_path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "kmsg_download_file",
                "description": (
                    "Download a file attachment from a KakaoTalk chat. "
                    "Finds the file via accessibility tree, right-clicks it to open "
                    "the context menu, picks a save option, and overrides the Save "
                    "panel path so save_dir is honored. Scrolls the chat message area "
                    "up automatically when the file is not in the current viewport."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Chat room or user name"},
                        "filename": {
                            "type": "string",
                            "description": "Target filename to download (substring match, e.g. '회의록.txt'). If omitted, downloads the most recent file attachment found.",
                        },
                        "save_dir": {
                            "type": "string",
                            "default": "~/Downloads",
                            "description": "Directory to save downloaded file. If different from KakaoTalk's default, the Save panel is driven via Cmd+Shift+G.",
                        },
                        "keep_window": {
                            "type": "boolean",
                            "default": False,
                            "description": "Keep auto-opened KakaoTalk window",
                        },
                        "max_scroll": {
                            "type": "integer",
                            "default": 8,
                            "minimum": 0,
                            "maximum": 30,
                            "description": "Max scroll-up attempts to find file (0 = no scroll, default 8)",
                        },
                        "stable_timeout_sec": {
                            "type": "number",
                            "default": 20.0,
                            "minimum": 1.0,
                            "maximum": 300.0,
                            "description": "Seconds to wait for the downloaded file to appear and stabilize.",
                        },
                    },
                    "required": ["chat"],
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

    # ── File-ops helpers ─────────────────────────────────────────────

    def _osascript(self, script: str, timeout_sec: float = 10.0) -> CommandResult:
        """Run an AppleScript and return CommandResult."""
        start = time.time()
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            ms = int((time.time() - start) * 1000)
            return CommandResult(127, "", str(exc), ms, False)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            ms = int((time.time() - start) * 1000)
            return CommandResult(proc.returncode, stdout, stderr, ms, False)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            ms = int((time.time() - start) * 1000)
            return CommandResult(124, stdout, stderr, ms, True)

    def _get_kakao_window_id(self) -> Optional[str]:
        """Get CGWindowID of KakaoTalk's main window via Quartz."""
        try:
            r = subprocess.run(
                ["python3", "-c",
                 "import Quartz\n"
                 "for w in Quartz.CGWindowListCopyWindowInfo("
                 "Quartz.kCGWindowListOptionOnScreenOnly,"
                 "Quartz.kCGNullWindowID):\n"
                 " if w.get('kCGWindowOwnerName')=='KakaoTalk'"
                 " and w.get('kCGWindowLayer',999)==0:\n"
                 "  print(w['kCGWindowNumber']);break"],
                capture_output=True, text=True, timeout=5,
            )
            wid = r.stdout.strip()
            return wid if wid and wid.isdigit() else None
        except Exception:
            return None

    def _screenshot_window(self, save_path: str) -> bool:
        """Take a screenshot of KakaoTalk's front window."""
        self._osascript('tell application "KakaoTalk" to activate', 3.0)
        time.sleep(0.3)
        wid = self._get_kakao_window_id()
        if wid:
            r = subprocess.run(
                ["screencapture", "-o", "-x", "-l", wid, save_path],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0 and os.path.isfile(save_path):
                return True
        r = subprocess.run(
            ["screencapture", "-o", "-x", save_path],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0 and os.path.isfile(save_path)
        messages = payload.get("messages") or []
        if not messages:
            return False
        last_msg = messages[-1] if isinstance(messages[-1], dict) else {}
        text = ""
        for key in ("text", "message", "content", "body"):
            value = last_msg.get(key)
            if isinstance(value, str):
                text = value
                break
        if not text and isinstance(last_msg.get("items"), list):
            text = " ".join(str(item) for item in last_msg.get("items", []) if item is not None)
        if not text:
            return True
        if "파일을 보냈습니다." in text:
            return True
        ts_value = None
        for key in ("sent_at", "created_at", "timestamp", "time"):
            candidate = last_msg.get(key)
            if candidate is not None:
                ts_value = candidate
                break
        if ts_value is not None:
            try:
                ts_num = float(ts_value)
                if ts_num > 1e12:
                    ts_num /= 1000.0
                return start_ts <= ts_num <= (start_ts + 20.0)
            except (TypeError, ValueError):
                pass
        return False

    def _get_kakao_window_id(self) -> Optional[str]:
        """Get CGWindowID of KakaoTalk's chat window (or main window) via Quartz.

        When a chat is open, KakaoTalk has 2+ windows: the chat list and the
        conversation.  We want the conversation window, which has the highest
        window number (most recently created).  The owner name may be either
        'KakaoTalk' (English locale) or '카카오톡' (Korean locale).
        """
        try:
            r = subprocess.run(
                ["/usr/bin/python3", "-c",
                 "import Quartz\n"
                 "wins=[]\n"
                 "for w in Quartz.CGWindowListCopyWindowInfo("
                 "Quartz.kCGWindowListOptionOnScreenOnly,"
                 "Quartz.kCGNullWindowID):\n"
                 " if w.get('kCGWindowOwnerName') in ('KakaoTalk','\\uce74\\uce74\\uc624\\ud1a1')"
                 " and w.get('kCGWindowLayer',999)==0:\n"
                 "  wins.append(w['kCGWindowNumber'])\n"
                 "if wins:print(max(wins))"],
                capture_output=True, text=True, timeout=5,
            )
            wid = r.stdout.strip()
            return wid if wid and wid.isdigit() else None
        except Exception:
            return None

    def _screenshot_window(self, save_path: str, window_id: Optional[str] = None) -> bool:
        """Take a screenshot of KakaoTalk's front window."""
        self._osascript('tell application "KakaoTalk" to activate', 3.0)
        time.sleep(0.3)
        wid = window_id or self._get_kakao_window_id()
        if wid:
            r = subprocess.run(
                ["screencapture", "-o", "-x", "-l", wid, save_path],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0 and os.path.isfile(save_path):
                return True
        r = subprocess.run(
            ["screencapture", "-o", "-x", save_path],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0 and os.path.isfile(save_path)

    def _get_kakao_window_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """Get (x, y, width, height) of KakaoTalk's front window."""
        r = self._osascript(
            'tell application "System Events"\n'
            '  tell process "KakaoTalk"\n'
            '    set p to position of window 1\n'
            '    set s to size of window 1\n'
            '    return {item 1 of p, item 2 of p, item 1 of s, item 2 of s}\n'
            '  end tell\n'
            'end tell', 5.0,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(", ")
            if len(parts) == 4:
                try:
                    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
                except ValueError:
                    pass
        return None

    def _run_jxa(self, script: str, argv: Optional[List[str]] = None,
                 timeout_sec: float = 15.0) -> Tuple[int, str, str]:
        """Run a JXA script via osascript -l JavaScript. Returns (rc, stdout, stderr)."""
        cmd = ["osascript", "-l", "JavaScript", "-e", script]
        if argv:
            cmd.append("--")
            cmd.extend(argv)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            return proc.returncode, stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate()
            except Exception:
                pass
            return 124, "", "timeout"
        except OSError as exc:
            return 127, "", str(exc)
    def _get_kakao_window_id(self) -> Optional[str]:
        """Get CGWindowID of KakaoTalk's chat window (or main window) via Quartz."""
        try:
            r = subprocess.run(
                ["/usr/bin/python3", "-c",
                 "import Quartz\n"
                 "wins=[]\n"
                 "for w in Quartz.CGWindowListCopyWindowInfo("
                 "Quartz.kCGWindowListOptionOnScreenOnly,"
                 "Quartz.kCGNullWindowID):\n"
                 " if w.get('kCGWindowOwnerName') in ('KakaoTalk','\\uce74\\uce74\\uc624\\ud1a1')"
                 " and w.get('kCGWindowLayer',999)==0:\n"
                 "  wins.append(w['kCGWindowNumber'])\n"
                 "if wins:print(max(wins))"],
                capture_output=True, text=True, timeout=5,
            )
            wid = r.stdout.strip()
            return wid if wid and wid.isdigit() else None
        except Exception:
            return None

    def _screenshot_window(self, save_path: str, window_id: Optional[str] = None) -> bool:
        """Take a screenshot of KakaoTalk's front window."""
        self._osascript('tell application "KakaoTalk" to activate', 3.0)
        time.sleep(0.3)
        wid = window_id or self._get_kakao_window_id()
        if wid:
            r = subprocess.run(
                ["screencapture", "-o", "-x", "-l", wid, save_path],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0 and os.path.isfile(save_path):
                return True
        r = subprocess.run(
            ["screencapture", "-o", "-x", save_path],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0 and os.path.isfile(save_path)

    def _ax_press_download_button(self, filename: str) -> bool:
        # AX structure (confirmed via kmsg inspect):
        # AXScrollArea/AXTable/AXRow/AXCell contains:
        #   AXStaticText.value = filename
        #   AXButton(title="저장") = download button
        safe_name = filename.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        jxa = (
            'var se=Application("System Events");'
            'var kk=se.processes.byName("KakaoTalk");'
            f'var target="{safe_name}";'
            'function cellHasFilename(cell){try{var sts=cell.staticTexts();for(var i=0;i<sts.length;i++){try{if(sts[i].value().indexOf(target)!==-1)return true;}catch(x){}}}catch(x){} return false;}'
            'function pressSave(cell){try{var btns=cell.buttons();for(var i=0;i<btns.length;i++){try{var tt="";try{tt=btns[i].title()}catch(x){} if(tt=="저장"||tt=="다른 이름으로 저장"){try{btns[i].click();return "OK";}catch(x){} try{btns[i].performAction("AXPress");return "OK";}catch(y){}}}catch(x){}}}catch(x){} return "NO";}'
            'function scan(win){try{var sas=win.scrollAreas();for(var s=0;s<sas.length;s++){try{var tabs=sas[s].tables();for(var t=0;t<tabs.length;t++){try{var rows=tabs[t].rows();for(var r=rows.length-1;r>=0;r--){try{var cells=rows[r].cells();for(var c=0;c<cells.length;c++){if(cellHasFilename(cells[c])){var res=pressSave(cells[c]);if(res=="OK") return "OK";}}}catch(x){}}}catch(x){}}}catch(x){}}}catch(x){} return "NO";}'
            'try{return scan(kk.windows[0])}catch(x){return "NO"}'
        )
        try:
            proc = subprocess.Popen(["osascript", "-l", "JavaScript", "-e", jxa], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            stdout, _ = proc.communicate(timeout=15.0)
            return proc.returncode == 0 and "OK" in stdout
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _find_file_text_ax(self, filename: str) -> List[JSONDict]:
        safe_name = filename.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        jxa = (
            'var se=Application("System Events");'
            'var kk=se.processes.byName("KakaoTalk");'
            'var R=[];'
            f'var target="{safe_name}";'
            'function S(e,d){if(d>8)return;try{var c=e.uiElements();for(var i=0;i<c.length;i++){try{var ch=c[i];var v="";try{v=ch.value()}catch(x){} var tt="";try{tt=ch.title()}catch(x){} var ds="";try{ds=ch.description()}catch(x){} var txt=v+" "+tt+" "+ds; if(txt.indexOf(target)!==-1){var p=[0,0],sz=[0,0];try{p=ch.position()}catch(x){} try{sz=ch.size()}catch(x){} R.push({role:ch.role(),value:v,title:tt,desc:ds,x:p[0],y:p[1],w:sz[0],h:sz[1]})} S(ch,d+1)}catch(x){}}}catch(x){}} try{S(kk.windows[0],0)}catch(x){} JSON.stringify(R)'
        )
        try:
            proc = subprocess.Popen(["osascript", "-l", "JavaScript", "-e", jxa], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            stdout, _ = proc.communicate(timeout=15.0)
            if proc.returncode == 0 and stdout.strip():
                return json.loads(stdout.strip())
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _find_icons_template(self, screenshot_path: str, template_path: str) -> List[List[int]]:
        if not template_path or not os.path.isfile(template_path):
            return []
        script = ("import sys,json\ntry:\n from PIL import Image\nexcept ImportError:\n sys.exit(0)\nimg=Image.open(sys.argv[1]).convert('RGB')\ntpl=Image.open(sys.argv[2]).convert('RGB')\niw,ih=img.size;tw,th=tpl.size\nif tw>iw or th>ih:sys.exit(0)\ntd=list(tpl.getdata());ms=[]\nfor y in range(0,ih-th+1,3):\n for x in range(0,iw-tw+1,3):\n  d=0;c=0\n  for ty in range(0,th,4):\n   for tx in range(0,tw,4):\n    sp=img.getpixel((x+tx,y+ty));tp=td[ty*tw+tx]\n    d+=abs(sp[0]-tp[0])+abs(sp[1]-tp[1])+abs(sp[2]-tp[2]);c+=1\n  if c>0 and d/(c*3)<25:ms.append([x+tw//2,y+th//2])\nf=[]\nfor m in ms:\n if not any(abs(m[0]-e[0])<tw and abs(m[1]-e[1])<th for e in f):f.append(m)\nprint(json.dumps(f[:5]))\n")
        try:
            r = subprocess.run(["/usr/bin/python3", "-c", script, screenshot_path, template_path], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout.strip())
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return []

    def _click_at(self, x: int, y: int, button: str = "left") -> bool:
        down = "kCGEventLeftMouseDown" if button == "left" else "kCGEventRightMouseDown"
        up = "kCGEventLeftMouseUp" if button == "left" else "kCGEventRightMouseUp"
        btn = "kCGMouseButtonLeft" if button == "left" else "kCGMouseButtonRight"
        script = (
            "import Quartz,time\n"
            f"p=({x},{y})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,Quartz.CGEventCreateMouseEvent(None,"
            f"Quartz.{down},p,Quartz.{btn}))\n"
            "time.sleep(0.08)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,Quartz.CGEventCreateMouseEvent(None,"
            f"Quartz.{up},p,Quartz.{btn}))\n"
        )
        try:
            r = subprocess.run(["/usr/bin/python3", "-c", script], capture_output=True, timeout=5)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _move_mouse_to(self, x: int, y: int) -> bool:
        script = (
            "import Quartz\n"
            f"p=({x},{y})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,Quartz.CGEventCreateMouseEvent(None,Quartz.kCGEventMouseMoved,p,Quartz.kCGMouseButtonLeft))\n"
        )
        try:
            r = subprocess.run(["/usr/bin/python3", "-c", script], capture_output=True, timeout=5)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _scroll_in_window(self, direction: str = "up", amount: int = 5, window_id: Optional[str] = None) -> bool:
        key_code = 116 if direction == "up" else 121
        r = self._osascript('tell application "KakaoTalk" to activate\n' 'delay 0.2\n' 'tell application "System Events"\n' f"  key code {key_code}\n" 'end tell', 5.0)
        return r.returncode == 0

    def _FILE_BUBBLE_JXA(self) -> str:
        return ""

    def _ocr_find_text(self, screenshot_path: str, target: str, window_bounds: Optional[Tuple[int, int, int, int]] = None) -> List[JSONDict]:
        script = ("import sys,json,objc,Quartz\nfrom Foundation import NSURL\nV=objc.loadBundle('Vision',bundle_path='/System/Library/Frameworks/Vision.framework',module_globals=globals())\nimg_path=sys.argv[1];target=sys.argv[2]\nurl=NSURL.fileURLWithPath_(img_path)\nsrc=Quartz.CGImageSourceCreateWithURL(url,None)\nif not src:sys.exit(0)\nimg=Quartz.CGImageSourceCreateImageAtIndex(src,0,None)\nif not img:sys.exit(0)\nw=Quartz.CGImageGetWidth(img);h=Quartz.CGImageGetHeight(img)\nreq=VNRecognizeTextRequest.alloc().init()\nreq.setRecognitionLanguages_(['ko-KR','en-US'])\nreq.setRecognitionLevel_(0)\nhnd=VNImageRequestHandler.alloc().initWithCGImage_options_(img,None)\nhnd.performRequests_error_([req],None)\nR=[]\nfor obs in (req.results() or []):\n txt=obs.topCandidates_(1)[0].string()\n bb=obs.boundingBox()\n ox,oy,bw,bh=bb.origin.x,bb.origin.y,bb.size.width,bb.size.height\n px=int(ox*w);py=int((1-oy-bh)*h)\n pw=int(bw*w);ph=int(bh*h)\n if target.lower() in txt.lower():\n  R.append({'text':txt,'x':px,'y':py,'w':pw,'h':ph})\nprint(json.dumps(R))\n")
        try:
            proc = subprocess.Popen(["/usr/bin/python3", "-c", script, screenshot_path, target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            stdout, _ = proc.communicate(timeout=15.0)
            if proc.returncode == 0 and stdout.strip():
                return json.loads(stdout.strip())
        except subprocess.TimeoutExpired:
            pass
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _ocr_find_nearby(self, screenshot_path: str, anchor_y: int, keywords: List[str], window_bounds: Optional[Tuple[int, int, int, int]] = None) -> List[JSONDict]:
        script = ("import sys,json,objc,Quartz\nfrom Foundation import NSURL\nV=objc.loadBundle('Vision',bundle_path='/System/Library/Frameworks/Vision.framework',module_globals=globals())\nimg_path=sys.argv[1];anchor_y=int(sys.argv[2])\nkeywords=sys.argv[3:]\nurl=NSURL.fileURLWithPath_(img_path)\nsrc=Quartz.CGImageSourceCreateWithURL(url,None)\nif not src:sys.exit(0)\nimg=Quartz.CGImageSourceCreateImageAtIndex(src,0,None)\nif not img:sys.exit(0)\nw=Quartz.CGImageGetWidth(img);h=Quartz.CGImageGetHeight(img)\nreq=VNRecognizeTextRequest.alloc().init()\nreq.setRecognitionLanguages_(['ko-KR','en-US'])\nreq.setRecognitionLevel_(0)\nhnd=VNImageRequestHandler.alloc().initWithCGImage_options_(img,None)\nhnd.performRequests_error_([req],None)\nR=[]\nfor obs in (req.results() or []):\n txt=obs.topCandidates_(1)[0].string()\n bb=obs.boundingBox()\n ox,oy,bw,bh=bb.origin.x,bb.origin.y,bb.size.width,bb.size.height\n px=int(ox*w);py=int((1-oy-bh)*h)\n pw=int(bw*w);ph=int(bh*h)\n cy=py+ph//2\n if abs(cy-anchor_y)<=100:\n  for kw in keywords:\n   if kw in txt:\n    R.append({'text':txt,'x':px,'y':py,'w':pw,'h':ph})\n    break\nprint(json.dumps(R))\n")
        try:
            proc = subprocess.Popen(["/usr/bin/python3", "-c", script, screenshot_path, str(anchor_y)] + keywords, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            stdout, _ = proc.communicate(timeout=15.0)
            if proc.returncode == 0 and stdout.strip():
                return json.loads(stdout.strip())
        except subprocess.TimeoutExpired:
            pass
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _move_mouse_to(self, x: int, y: int) -> bool:
        """Move mouse cursor without clicking (so scroll events hit the chat area)."""
        script = (
            "import Quartz\n"
            f"p=({x},{y})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,"
            "Quartz.CGEventCreateMouseEvent(None,"
            "Quartz.kCGEventMouseMoved,p,Quartz.kCGMouseButtonLeft))\n"
        )
        try:
            r = subprocess.run(["python3", "-c", script], capture_output=True, timeout=5)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _scroll_chat_area(self, direction: str = "up", amount: int = 6) -> bool:
        """Scroll inside KakaoTalk's message area (right-side pane) via Quartz.

        Targets 70% width / 55% height so the event lands in the message
        pane rather than the chat-list sidebar on the left.
        """
        bounds = self._get_kakao_window_bounds()
        if not bounds:
            return False
        cx = bounds[0] + int(bounds[2] * 0.7)
        cy = bounds[1] + int(bounds[3] * 0.55)
        # Move cursor there first — macOS routes scroll to the window under
        # the pointer, so without this the chat list sidebar may eat the event.
        self._move_mouse_to(cx, cy)
        time.sleep(0.05)
        scroll_delta = amount if direction == "up" else -amount
        script = (
            "import Quartz\n"
            f"p=({cx},{cy})\n"
            f"delta={scroll_delta}\n"
            "e=Quartz.CGEventCreateScrollWheelEvent("
            "None,Quartz.kCGScrollEventUnitLine,1,delta)\n"
            "Quartz.CGEventSetLocation(e,p)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,e)\n"
        )
        try:
            r = subprocess.run(
                ["python3", "-c", script],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    # JXA library: shared walker that recurses the KakaoTalk main window AX
    # tree collecting either filename-matched elements or download/save
    # indicator elements. Returns [] on any JXA error.
    _FILE_BUBBLE_JXA = (
        'ObjC.import("Foundation");'
        'var args=ObjC.unwrap($.NSProcessInfo.processInfo.arguments);'
        'var target=ObjC.unwrap(args[args.length-1])||"";'
        'var se=Application("System Events");'
        'var kk=se.processes.byName("KakaoTalk");'
        'var R=[];'
        'var MARKERS=["\\uc800\\uc7a5","\\ub2e4\\uc6b4\\ub85c\\ub4dc",'
        '"download","Download"];'
        'function norm(s){return (s||"").toString();}'
        'function hasMarker(t){for(var i=0;i<MARKERS.length;i++){'
        'if(t.indexOf(MARKERS[i])!==-1)return true;}return false;}'
        'function S(e,d){if(d>10)return;try{var c=e.uiElements();'
        'for(var i=0;i<c.length;i++){try{var ch=c[i];'
        'var v=norm(ch.value()),t=norm(ch.title()),ds=norm(ch.description());'
        'var txt=v+" \\u241f "+t+" \\u241f "+ds;'
        'var matched=false;var reason="";'
        'if(target&&txt.indexOf(target)!==-1){matched=true;reason="filename";}'
        'else if(!target&&hasMarker(txt)){matched=true;reason="marker";}'
        'if(matched){var p=[0,0],sz=[0,0];'
        'try{p=ch.position()}catch(x){}'
        'try{sz=ch.size()}catch(x){}'
        'var rl="";try{rl=ch.role()}catch(x){}'
        'R.push({role:rl,value:v,title:t,desc:ds,reason:reason,'
        'x:p[0],y:p[1],w:sz[0],h:sz[1],depth:d});}'
        'S(ch,d+1)}catch(x){}}}catch(x){}}'
        'try{S(kk.windows[0],0)}catch(x){}'
        'JSON.stringify(R)'
    )

    def _find_file_elements_ax(self, filename: str) -> Tuple[List[JSONDict], str]:
        """Find AX elements matching a filename (or download markers if empty).

        Returns (elements, debug) — debug is non-empty when the JXA script
        fails so the caller can surface it instead of silently returning [].
        """
        key_code = 116 if direction == "up" else 121  # Page Up / Page Down
        r = self._osascript(
            'tell application "KakaoTalk" to activate\n'
            'delay 0.2\n'
            'tell application "System Events"\n'
            f"  key code {key_code}\n"
            "end tell", 5.0,
        )
        return r.returncode == 0

    def _ocr_find_text(
        self,
        screenshot_path: str,
        target: str,
        window_bounds: Optional[Tuple[int, int, int, int]] = None,
    ) -> List[JSONDict]:
        """Find text in a screenshot using Apple Vision OCR.

        Returns screen-coordinate matches. Uses /usr/bin/python3 (system Python)
        because Homebrew Python may lack PyObjC/Vision bindings.
        The target text is passed via sys.argv to prevent injection.
        """
        script = (
            "import sys,json,objc,Quartz\n"
            "from Foundation import NSURL\n"
            "V=objc.loadBundle('Vision',bundle_path="
            "'/System/Library/Frameworks/Vision.framework',"
            "module_globals=globals())\n"
            "img_path=sys.argv[1];target=sys.argv[2]\n"
            "url=NSURL.fileURLWithPath_(img_path)\n"
            "src=Quartz.CGImageSourceCreateWithURL(url,None)\n"
            "if not src:sys.exit(0)\n"
            "img=Quartz.CGImageSourceCreateImageAtIndex(src,0,None)\n"
            "if not img:sys.exit(0)\n"
            "w=Quartz.CGImageGetWidth(img);h=Quartz.CGImageGetHeight(img)\n"
            "req=VNRecognizeTextRequest.alloc().init()\n"
            "req.setRecognitionLanguages_(['ko-KR','en-US'])\n"
            "req.setRecognitionLevel_(0)\n"
            "hnd=VNImageRequestHandler.alloc()"
            ".initWithCGImage_options_(img,None)\n"
            "hnd.performRequests_error_([req],None)\n"
            "R=[]\n"
            "for obs in (req.results() or []):\n"
            " txt=obs.topCandidates_(1)[0].string()\n"
            " bb=obs.boundingBox()\n"
            " ox,oy,bw,bh=bb.origin.x,bb.origin.y,"
            "bb.size.width,bb.size.height\n"
            " px=int(ox*w);py=int((1-oy-bh)*h)\n"
            " pw=int(bw*w);ph=int(bh*h)\n"
            " if target.lower() in txt.lower():\n"
            "  R.append({'text':txt,'x':px,'y':py,'w':pw,'h':ph})\n"
            "print(json.dumps(R))\n"
        )
        if rc != 0:
            return [], f"jxa_rc={rc} err={err.strip()[:200]}"
        text = (out or "").strip()
        if not text:
            return [], ""
        try:
            proc = subprocess.Popen(
                ["/usr/bin/python3", "-c", script, screenshot_path, target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            stdout, _ = proc.communicate(timeout=15.0)
            if proc.returncode == 0 and stdout.strip():
                return json.loads(stdout.strip())
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _ocr_find_nearby(
        self,
        screenshot_path: str,
        anchor_y: int,
        keywords: List[str],
        window_bounds: Optional[Tuple[int, int, int, int]] = None,
    ) -> List[JSONDict]:
        """OCR the screenshot and return text blocks matching any keyword near anchor_y."""
        script = (
            "import sys,json,objc,Quartz\n"
            "from Foundation import NSURL\n"
            "V=objc.loadBundle('Vision',bundle_path="
            "'/System/Library/Frameworks/Vision.framework',"
            "module_globals=globals())\n"
            "img_path=sys.argv[1];anchor_y=int(sys.argv[2])\n"
            "keywords=sys.argv[3:]\n"
            "url=NSURL.fileURLWithPath_(img_path)\n"
            "src=Quartz.CGImageSourceCreateWithURL(url,None)\n"
            "if not src:sys.exit(0)\n"
            "img=Quartz.CGImageSourceCreateImageAtIndex(src,0,None)\n"
            "if not img:sys.exit(0)\n"
            "w=Quartz.CGImageGetWidth(img);h=Quartz.CGImageGetHeight(img)\n"
            "req=VNRecognizeTextRequest.alloc().init()\n"
            "req.setRecognitionLanguages_(['ko-KR','en-US'])\n"
            "req.setRecognitionLevel_(0)\n"
            "hnd=VNImageRequestHandler.alloc()"
            ".initWithCGImage_options_(img,None)\n"
            "hnd.performRequests_error_([req],None)\n"
            "R=[]\n"
            "for obs in (req.results() or []):\n"
            " txt=obs.topCandidates_(1)[0].string()\n"
            " bb=obs.boundingBox()\n"
            " ox,oy,bw,bh=bb.origin.x,bb.origin.y,"
            "bb.size.width,bb.size.height\n"
            " px=int(ox*w);py=int((1-oy-bh)*h)\n"
            " pw=int(bw*w);ph=int(bh*h)\n"
            " cy=py+ph//2\n"
            " if abs(cy-anchor_y)<=100:\n"
            "  for kw in keywords:\n"
            "   if kw in txt:\n"
            "    R.append({'text':txt,'x':px,'y':py,'w':pw,'h':ph})\n"
            "    break\n"
            "print(json.dumps(R))\n"
        )
        try:
            proc = subprocess.Popen(
                ["/usr/bin/python3", "-c", script, screenshot_path,
                 str(anchor_y)] + keywords,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            stdout, _ = proc.communicate(timeout=15.0)
            if proc.returncode == 0 and stdout.strip():
                return json.loads(stdout.strip())
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        except (json.JSONDecodeError, OSError):
            pass
        return []

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

    # ── File send / download handlers ────────────────────────────────

    def _call_kmsg_send_file(self, arguments: JSONDict) -> JSONDict:
        chat = str(arguments.get("chat", "")).strip()
        file_path = str(arguments.get("file_path", "")).strip()
        confirm = bool(arguments.get("confirm", False))

        if not chat or not file_path:
            return self._error_payload(
                "INVALID_ARGUMENT", "chat and file_path are required",
                "Provide both chat and file_path.", "", "", 0,
            )
        if confirm:
            return self._error_payload(
                "CONFIRMATION_REQUIRED",
                "kmsg_send_file blocked because confirm=true",
                "Ask user for approval, then call with confirm=false.",
                "", "", 0,
            )

        file_path = os.path.expanduser(file_path)
        if not os.path.isfile(file_path):
            return self._error_payload(
                "INVALID_ARGUMENT", f"File not found: {file_path}",
                "Provide a valid file path.", "", "", 0,
            )

        keep_window = bool(arguments.get("keep_window", False))
        start = time.time()

        # 1. Navigate to chat (opens the chat window)
        nav_cmd = [self.runner.kmsg_bin, "read", chat, "--limit", "1"]
        if keep_window:
            nav_cmd.append("--keep-window")
        nav = self.runner.run(nav_cmd, timeout_sec=20.0)
        if nav.returncode != 0:
            code = self._extract_error_code(f"{nav.stdout}\n{nav.stderr}")
            return self._error_payload(
                code, "Failed to navigate to chat", self._map_hint(code),
                nav.stdout, nav.stderr, nav.latency_ms,
            )

        # 2. Copy file to clipboard as POSIX file (KakaoTalk accepts this format).
        abs_path = os.path.abspath(file_path)
        clip_path = abs_path.replace("\\", "\\\\").replace('"', '\\"')
        copy_r = self._osascript(
            f'set the clipboard to (POSIX file "{clip_path}")', 5.0,
        )
        if copy_r.returncode != 0:
            return self._error_payload(
                "CLIPBOARD_FAILED", "Failed to copy file to clipboard",
                "Check file path and permissions.",
                copy_r.stdout, copy_r.stderr, int((time.time() - start) * 1000),
            )

        # 3. Raise chat window, locate input field, then Quartz-click it + Cmd+V + Return.
        #    System Events keystroke does NOT reliably deliver Cmd+V to KakaoTalk's
        #    text input — Quartz CGEvent posting is required.
        chat_name = chat
        cached_coords = self._get_cached_send_file_coords(chat_name)
        if cached_coords:
            cx, cy = cached_coords
            coord_r = CommandResult(0, f"{cx},{cy},0,0", "", 0, False)
        else:
            coord_r = self._lookup_send_file_coords(chat_name)
        if coord_r.returncode != 0 or not coord_r.stdout.strip() or coord_r.stdout.startswith("ERR"):
            return self._error_payload(
                "PASTE_FAILED", "Failed to locate chat input field",
                "Open the chat window in KakaoTalk and retry.",
                coord_r.stdout, coord_r.stderr, int((time.time() - start) * 1000),
            )
        try:
            px, py, pw, ph = (int(float(v)) for v in coord_r.stdout.strip().split(","))
            cx, cy = px + pw // 2, py + ph // 2
            self._set_cached_send_file_coords(chat_name, cx, cy)
        except (ValueError, IndexError):
            return self._error_payload(
                "PASTE_FAILED", "Could not parse input field coordinates",
                "Unexpected coord output.",
                coord_r.stdout, coord_r.stderr, int((time.time() - start) * 1000),
            )

        # 3b. Quartz click + Cmd+V + Return.
        quartz_script = (
            "import Quartz, time, sys\n"
            "x, y = int(sys.argv[1]), int(sys.argv[2])\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, "
            "Quartz.CGEventCreateMouseEvent(None, "
            "Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft))\n"
            "time.sleep(0.05)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, "
            "Quartz.CGEventCreateMouseEvent(None, "
            "Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft))\n"
            "time.sleep(0.4)\n"
            "src = Quartz.CGEventSourceCreate("
            "Quartz.kCGEventSourceStateHIDSystemState)\n"
            "vd = Quartz.CGEventCreateKeyboardEvent(src, 9, True)\n"
            "Quartz.CGEventSetFlags(vd, Quartz.kCGEventFlagMaskCommand)\n"
            "vu = Quartz.CGEventCreateKeyboardEvent(src, 9, False)\n"
            "Quartz.CGEventSetFlags(vu, Quartz.kCGEventFlagMaskCommand)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, vd)\n"
            "time.sleep(0.05)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, vu)\n"
            "time.sleep(1.5)\n"
            "rd = Quartz.CGEventCreateKeyboardEvent(src, 36, True)\n"
            "ru = Quartz.CGEventCreateKeyboardEvent(src, 36, False)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, rd)\n"
            "time.sleep(0.05)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, ru)\n"
            "print('OK')\n"
        )
        try:
            qproc = subprocess.run(
                ["/usr/bin/python3", "-c", quartz_script, str(cx), str(cy)],
                capture_output=True, text=True, timeout=10.0,
            )
        except subprocess.TimeoutExpired:
            qproc = subprocess.CompletedProcess(args=[], returncode=124, stdout="", stderr="timeout")
        latency_ms = int((time.time() - start) * 1000)
        if qproc.returncode != 0:
            fallback_script = (
                'tell application "KakaoTalk" to activate\n'
                'delay 0.4\n'
                'tell application "System Events"\n'
                '  key code 9 using {command down}\n'
                '  delay 0.05\n'
                '  key code 36\n'
                'end tell'
            )
            fallback_r = self._osascript(fallback_script, 8.0)
            if fallback_r.returncode != 0:
                return self._error_payload(
                    "PASTE_FAILED", "Quartz click/paste failed",
                    "Check Accessibility permissions for python3.",
                    qproc.stdout or fallback_r.stdout, qproc.stderr or fallback_r.stderr, latency_ms,
                )
            qproc = fallback_r

        time.sleep(1.5)

        return {
            "ok": True,
            "chat": chat,
            "sent": True,
            "file_path": abs_path,
            "input_coord": [cx, cy],
            "meta": {"latency_ms": latency_ms},
        }

    def _call_kmsg_download_file(self, arguments: JSONDict) -> JSONDict:
        """Download a file attachment via right-click → save menu → Save panel.

        Flow:
          1. Navigate to chat (kmsg read --keep-window)
          2. Scroll-and-search AX tree for the file element
          3. Right-click it, wait for the context menu
          4. Click the save menu item in the AX tree
          5. If a Save panel appears and save_dir differs from ~/Downloads,
             drive it with Cmd+Shift+G; otherwise press Return for default.
          6. Watch save_dir + ~/Downloads for a new stable file.
        """
        chat = str(arguments.get("chat", "")).strip()
        if not chat:
            return self._error_payload(
                "INVALID_ARGUMENT", "chat is required",
                "Provide a chat name.", "", "", 0,
            )

        default_downloads = os.path.expanduser("~/Downloads")
        save_dir = os.path.expanduser(str(arguments.get("save_dir", "~/Downloads")))
        save_dir = os.path.abspath(save_dir)
        raw_fn = arguments.get("filename")
        filename = str(raw_fn).strip() if raw_fn is not None else ""
        try:
            max_scroll = max(0, min(30, int(arguments.get("max_scroll", 8))))
        except (ValueError, TypeError):
            max_scroll = 8
        try:
            stable_timeout = float(arguments.get("stable_timeout_sec", 20.0))
        except (ValueError, TypeError):
            stable_timeout = 20.0
        stable_timeout = max(1.0, min(300.0, stable_timeout))
        start = time.time()

        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as exc:
            return self._error_payload(
                "INVALID_ARGUMENT",
                f"Cannot create save_dir: {exc}",
                "Check directory path and permissions.",
                "", str(exc), 0,
            )

        # 1. Navigate to chat (keep window open for interaction)
        nav_cmd = [self.runner.kmsg_bin, "read", chat, "--limit", "1", "--keep-window"]
        nav = self.runner.run(nav_cmd, timeout_sec=20.0)
        if nav.returncode != 0:
            code = self._extract_error_code(f"{nav.stdout}\n{nav.stderr}")
            return self._error_payload(
                code, "Failed to navigate to chat", self._map_hint(code),
                nav.stdout, nav.stderr, nav.latency_ms,
            )

        window_id = self._get_kakao_window_id()

        # 2. AX tree first: no screenshot required
        file_texts: List[JSONDict] = []
        download_buttons: List[JSONDict] = []
        target_btn: Optional[JSONDict] = None
        ax_clicked = False
        if filename:
            ax_clicked = self._ax_press_download_button(filename)
        if not ax_clicked:
            if filename:
                file_texts = self._find_file_text_ax(filename)
            download_buttons = self._find_download_buttons_ax()
            if filename and file_texts and download_buttons:
                ft = file_texts[0]
                ft_cy = ft.get("y", 0) + ft.get("h", 0) // 2
                nearby = [
                    b for b in download_buttons
                    if abs((b.get("y", 0) + b.get("h", 0) // 2) - ft_cy) <= 80
                ]
                if nearby:
                    target_btn = min(
                        nearby,
                        key=lambda b: abs((b.get("y", 0) + b.get("h", 0) // 2) - ft_cy),
                    )
            elif download_buttons:
                target_btn = download_buttons[-1]

        # 3. Screenshot + OCR fallback
        screenshot_path = ""
        has_screenshot = False
        save_links: List[JSONDict] = []
        already_downloaded = False
        template_matches: List[List[int]] = []
        scroll_count = 0

        for attempt in range(max_scroll + 1):
            if attempt > 0 or not has_screenshot:
                has_screenshot = self._screenshot_window(screenshot_path, window_id)
            if not has_screenshot:
                break

            if filename:
                file_texts = self._ocr_find_text(screenshot_path, filename)

            if filename and file_texts:
                ft = file_texts[0]
                ft_cy = ft.get("y", 0) + ft.get("h", 0) // 2
                save_links = self._ocr_find_nearby(
                    screenshot_path, ft_cy, ["저장"],
                )
                if save_links:
                    break
                open_links = self._ocr_find_nearby(
                    screenshot_path, ft_cy, ["열기", "Finder"],
                )
                if open_links:
                    already_downloaded = True
                    break
            elif not filename:
                save_links = self._ocr_find_text(screenshot_path, "저장")
                if save_links:
                    break

            if icon_template and has_screenshot:
                template_matches = self._find_icons_template(
                    screenshot_path, icon_template,
                )
                if template_matches:
                    break

            if attempt < max_scroll:
                scrolled = self._scroll_in_window("up", 5, window_id)
                if scrolled:
                    scroll_count += 1
                    time.sleep(0.8)
                else:
                    break

        # 4. Click target from AX/OCR/template
        downloaded_file: Optional[str] = None
        clicked = False
        try:
            before_files = set(os.listdir(save_dir))
        except OSError:
            before_files = set()

        if ax_clicked:
            clicked = True
            time.sleep(3.0)
        elif target_btn:
            cx = target_btn.get("x", 0) + target_btn.get("w", 0) // 2
            cy = target_btn.get("y", 0) + target_btn.get("h", 0) // 2
            if cx > 0 and cy > 0:
                clicked = self._click_at(cx, cy, window_id)
                time.sleep(3.0)
        elif save_links and not already_downloaded:
            bounds = self._get_kakao_window_bounds()
            if bounds:
                try:
                    from PIL import Image
                    with Image.open(screenshot_path) as screenshot_image:
                        retina_scale = max(
                            1.0,
                            float(screenshot_image.width) / float(bounds[2]),
                        ) if bounds[2] else 1.0
                except Exception:
                    retina_scale = 1.0
                sl = save_links[0]
                sx = bounds[0] + (sl["x"] + sl["w"] / 2.0) / retina_scale
                sy = bounds[1] + (sl["y"] + sl["h"] / 2.0) / retina_scale
                if sx > 0 and sy > 0:
                    clicked = self._click_at(
                        int(round(sx)),
                        int(round(sy)),
                        window_id,
                    )
                    time.sleep(3.0)
        elif template_matches:
            bounds = self._get_kakao_window_bounds()
            if bounds:
                wx, wy = bounds[0], bounds[1]
                sx = wx + template_matches[-1][0]
                sy = wy + template_matches[-1][1]
                clicked = self._click_at(sx, sy, window_id)
                time.sleep(3.0)

        # 6. Handle save dialog ONLY after a successful click
        if clicked:
            dialog_r = self._osascript(
                'tell application "System Events"\n'
                '  tell process "KakaoTalk"\n'
                '    if exists sheet 1 of window 1 then\n'
                '      return "DIALOG"\n'
                '    end if\n'
                '  end tell\n'
                'end tell\n'
                'return "NONE"', 3.0,
            )
            if "DIALOG" in dialog_r.stdout:
                self._osascript(
                    'tell application "System Events"\n'
                    "  keystroke return\n"
                    "end tell", 3.0,
                )
                time.sleep(2.0)

        # 7. Detect newly downloaded file
        try:
            after_files = set(os.listdir(save_dir))
        except OSError:
            after_files = set()
        new_files = after_files - before_files
        if new_files:
            newest = max(
                new_files,
                key=lambda f: os.path.getmtime(os.path.join(save_dir, f)),
            )
            downloaded_file = os.path.join(save_dir, newest)

        latency_ms = int((time.time() - start) * 1000)

        # Cleanup temp screenshot — avoid leaking chat contents via /tmp
        try:
            if os.path.exists(screenshot_path):
                os.unlink(screenshot_path)
        except OSError:
            pass

        # 8. Return structured result
        if not ax_clicked and not target_btn and not file_texts and not save_links and not template_matches:
            result: JSONDict = {
                "ok": False,
                "error": {
                    "code": "NO_DOWNLOAD_TARGET",
                    "message": "No file or save link found in chat via OCR",
                    "hint": "Ensure the chat window is visible and not obscured.",
                },
                "chat": chat,
                "scroll_attempts": scroll_count,
                "meta": {"latency_ms": latency_ms},
            }
            if filename:
                result["error"]["hint"] = (
                    f"File '{filename}' not found after {scroll_count} scroll(s). "
                    "The file may be further up in chat history or already expired."
                )
            return result

        if already_downloaded:
            return {
                "ok": False,
                "error": {
                    "code": "ALREADY_DOWNLOADED",
                    "message": f"File '{filename}' found but already downloaded",
                    "hint": "The file shows '열기 · Finder에서 보기' — already on disk.",
                },
                "chat": chat,
                "scroll_attempts": scroll_count,
                "candidates_seen": candidates_seen,
                "meta": {"latency_ms": latency_ms},
            }

        # 3. Right-click the target element to open its context menu.
        cx = int(target.get("x", 0) + target.get("w", 0) // 2)
        cy = int(target.get("y", 0) + target.get("h", 0) // 2)
        if cx <= 0 or cy <= 0:
            return self._error_payload(
                "INVALID_TARGET_POS",
                f"Target element has non-positive coordinates ({cx},{cy})",
                "The AX element has no screen position; try scrolling manually.",
                "", "", int((time.time() - start) * 1000),
            )

        # Snapshot both watched dirs BEFORE any clicking so we can diff later.
        watched_dirs: List[str] = [save_dir]
        if os.path.abspath(default_downloads) != save_dir and os.path.isdir(default_downloads):
            watched_dirs.append(default_downloads)
        baseline: Dict[str, Dict[str, Tuple[float, int]]] = {
            d: self._snapshot_dir(d) for d in watched_dirs
        }

        self._move_mouse_to(cx, cy)
        time.sleep(0.05)
        right_clicked = self._click_at(cx, cy, button="right")
        if not right_clicked:
            return self._error_payload(
                "RIGHT_CLICK_FAILED",
                "Failed to post right-click event",
                "Check Accessibility / Input Monitoring permission for this process.",
                "", "", int((time.time() - start) * 1000),
            )
        time.sleep(0.55)  # wait for context menu to appear

        # 4. Click the save menu item via AX
        menu_clicked, menu_info = self._click_save_menu_item()
        if not menu_clicked:
            # Dismiss any stray menu with Escape before reporting.
            self._osascript(
                'tell application "System Events" to key code 53', 2.0,
            )
            latency_ms = int((time.time() - start) * 1000)
            return {
                "ok": False,
                "error": {
                    "code": "NO_SAVE_MENU_ITEM",
                    "message": "Right-click context menu had no recognizable save/download item",
                    "hint": (
                        "KakaoTalk may have shown a menu for the wrong element "
                        "(e.g. background). Try providing a more specific filename."
                    ),
                    "menu_debug": menu_info,
                },
                "chat": chat,
                "target_filename": filename or None,
                "scroll_attempts": scroll_count,
                "meta": {"latency_ms": latency_ms},
            }

        # 5. Handle the Save panel if one appears
        panel_shown = self._wait_for_save_panel(timeout_sec=4.0)
        save_panel_overridden = False
        if panel_shown:
            if save_dir != os.path.abspath(default_downloads):
                ok_drive, drive_info = self._override_save_panel_path(save_dir)
                save_panel_overridden = ok_drive
                if not ok_drive:
                    # Fall back to accepting the default location so at
                    # least the file lands in ~/Downloads and we can move it.
                    self._osascript(
                        'tell application "System Events" to keystroke return', 3.0,
                    )
            else:
                # Same path — just accept the default.
                self._osascript(
                    'tell application "System Events" to keystroke return', 3.0,
                )

        # 6. Wait for a new stable file to appear in any watched directory.
        downloaded_file = self._wait_for_new_stable_file(
            watched_dirs, baseline, timeout_sec=stable_timeout,
        )

        # If the file landed in ~/Downloads but the user asked for save_dir,
        # and we couldn't override the Save panel, relocate it.
        if (
            downloaded_file
            and save_dir != os.path.abspath(default_downloads)
            and os.path.dirname(downloaded_file) != save_dir
            and not save_panel_overridden
        ):
            try:
                dest = os.path.join(save_dir, os.path.basename(downloaded_file))
                if not os.path.exists(dest):
                    shutil.move(downloaded_file, dest)
                    downloaded_file = dest
            except (OSError, shutil.Error):
                pass  # keep the original path

        latency_ms = int((time.time() - start) * 1000)

        result: JSONDict = {
            "ok": bool(downloaded_file),
            "chat": chat,
            "save_links_found": len(save_links),
            "downloaded_file": downloaded_file,
            "target": {
                "role": target.get("role"),
                "title": target.get("title"),
                "value": target.get("value"),
                "desc": target.get("desc"),
                "reason": target.get("reason"),
            },
            "scroll_attempts": scroll_count,
            "save_panel_shown": panel_shown,
            "save_panel_overridden": save_panel_overridden,
            "menu_info": menu_info,
            "watched_dirs": watched_dirs,
            "meta": {"latency_ms": latency_ms},
        }
        if filename:
            result["target_filename"] = filename
            result["file_texts_found"] = len(file_texts)
        if not downloaded_file:
            result["error"] = {
                "code": "DOWNLOAD_NOT_OBSERVED",
                "message": "Save menu was clicked but no new stable file appeared",
                "hint": (
                    "The file may still be downloading (raise stable_timeout_sec), "
                    "or KakaoTalk saved it to a different directory. "
                    f"Watched: {watched_dirs}"
                ),
            }
        return result

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
        elif name == "kmsg_send_file":
            result_obj = self._call_kmsg_send_file(call_args)
        elif name == "kmsg_download_file":
            result_obj = self._call_kmsg_download_file(call_args)
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
                        "Use confirm=true to intentionally require a confirmation step. "
                        "Use kmsg_send_file to send any file type (not just images). "
                        "Use kmsg_download_file to download file attachments from a chat."
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
