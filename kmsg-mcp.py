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
import tempfile
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
                    "Screenshots the chat window, searches for download/save buttons "
                    "via accessibility tree inspection and optional icon template matching, "
                    "then clicks to download. Scrolls up automatically to find files "
                    "not visible in the current viewport."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "string", "description": "Chat room or user name"},
                        "filename": {
                            "type": "string",
                            "description": "Target filename to download (e.g. '회의록.txt'). If omitted, downloads the most recent file found.",
                        },
                        "save_dir": {
                            "type": "string",
                            "default": "~/Downloads",
                            "description": "Directory to save downloaded file",
                        },
                        "icon_template_path": {
                            "type": "string",
                            "description": "Path to download icon template image for matching (optional)",
                        },
                        "keep_window": {
                            "type": "boolean",
                            "default": False,
                            "description": "Keep auto-opened KakaoTalk window",
                        },
                        "max_scroll": {
                            "type": "integer",
                            "default": 5,
                            "minimum": 0,
                            "maximum": 20,
                            "description": "Max scroll-up attempts to find file (0 = no scroll, default 5)",
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

    def _find_download_buttons_ax(self) -> List[JSONDict]:
        """Search KakaoTalk accessibility tree for download/save buttons via JXA."""
        jxa = (
            'var se=Application("System Events");'
            'var kk=se.processes.byName("KakaoTalk");'
            'var R=[];'
            'function S(e,d){if(d>8)return;try{var c=e.uiElements();'
            'for(var i=0;i<c.length;i++){try{var ch=c[i];'
            'var ds="";try{ds=ch.description()}catch(x){}'
            'var tt="";try{tt=ch.title()}catch(x){}'
            'var cm=(ds+" "+tt);'
            'if(cm.indexOf("\uc800\uc7a5")!==-1||cm.indexOf("\ub2e4\uc6b4\ub85c\ub4dc")!==-1'
            '||cm.indexOf("download")!==-1||cm.indexOf("Download")!==-1){'
            'var p=[0,0],sz=[0,0];'
            'try{p=ch.position()}catch(x){}'
            'try{sz=ch.size()}catch(x){}'
            'R.push({role:ch.role(),desc:ds,title:tt,'
            'x:p[0],y:p[1],w:sz[0],h:sz[1]})}'
            'S(ch,d+1)}catch(x){}}}catch(x){}}'
            'try{S(kk.windows[0],0)}catch(x){}'
            'JSON.stringify(R)'
        )
        try:
            proc = subprocess.Popen(
                ["osascript", "-l", "JavaScript", "-e", jxa],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            stdout, _ = proc.communicate(timeout=15.0)
            if proc.returncode == 0 and stdout.strip():
                return json.loads(stdout.strip())
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
        return []

    def _find_icons_template(self, screenshot_path: str, template_path: str) -> List[List[int]]:
        """Find download icon positions via PIL template matching (optional)."""
        if not template_path or not os.path.isfile(template_path):
            return []
        # Paths passed as sys.argv to avoid code injection.
        script = (
            "import sys,json\n"
            "try:\n"
            " from PIL import Image\n"
            "except ImportError:\n"
            " sys.exit(0)\n"
            "img=Image.open(sys.argv[1]).convert('RGB')\n"
            "tpl=Image.open(sys.argv[2]).convert('RGB')\n"
            "iw,ih=img.size;tw,th=tpl.size\n"
            "if tw>iw or th>ih:sys.exit(0)\n"
            "td=list(tpl.getdata());ms=[]\n"
            "for y in range(0,ih-th+1,3):\n"
            " for x in range(0,iw-tw+1,3):\n"
            "  d=0;c=0\n"
            "  for ty in range(0,th,4):\n"
            "   for tx in range(0,tw,4):\n"
            "    sp=img.getpixel((x+tx,y+ty));tp=td[ty*tw+tx]\n"
            "    d+=abs(sp[0]-tp[0])+abs(sp[1]-tp[1])+abs(sp[2]-tp[2]);c+=1\n"
            "  if c>0 and d/(c*3)<25:ms.append([x+tw//2,y+th//2])\n"
            "f=[]\n"
            "for m in ms:\n"
            " if not any(abs(m[0]-e[0])<tw and abs(m[1]-e[1])<th for e in f):f.append(m)\n"
            "print(json.dumps(f[:5]))\n"
        )
        try:
            r = subprocess.run(
                ["python3", "-c", script, screenshot_path, template_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout.strip())
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return []

    def _click_at(self, x: int, y: int) -> bool:
        """Click at screen coordinates using Quartz CGEvent."""
        script = (
            "import Quartz,time\n"
            f"p=({x},{y})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,"
            "Quartz.CGEventCreateMouseEvent(None,"
            "Quartz.kCGEventLeftMouseDown,p,Quartz.kCGMouseButtonLeft))\n"
            "time.sleep(0.1)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap,"
            "Quartz.CGEventCreateMouseEvent(None,"
            "Quartz.kCGEventLeftMouseUp,p,Quartz.kCGMouseButtonLeft))\n"
        )
        try:
            r = subprocess.run(["python3", "-c", script], capture_output=True, timeout=5)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _scroll_in_window(self, direction: str = "up", amount: int = 5) -> bool:
        """Scroll inside the KakaoTalk chat window using Quartz CGEvent."""
        bounds = self._get_kakao_window_bounds()
        if not bounds:
            return False
        cx = bounds[0] + bounds[2] // 2
        cy = bounds[1] + bounds[3] // 2
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

    def _find_file_text_ax(self, filename: str) -> List[JSONDict]:
        """Search KakaoTalk AX tree for static text matching a filename.

        The filename is passed via argv (not string interpolation) to prevent
        JXA injection — consistent with _find_icons_template's approach.
        """
        jxa = (
            'ObjC.import("Foundation");'
            'var args=ObjC.unwrap($.NSProcessInfo.processInfo.arguments);'
            'var target=ObjC.unwrap(args[args.length-1]);'
            'var se=Application("System Events");'
            'var kk=se.processes.byName("KakaoTalk");'
            'var R=[];'
            'function S(e,d){if(d>8)return;try{var c=e.uiElements();'
            'for(var i=0;i<c.length;i++){try{var ch=c[i];'
            'var v="";try{v=ch.value()}catch(x){}'
            'var tt="";try{tt=ch.title()}catch(x){}'
            'var ds="";try{ds=ch.description()}catch(x){}'
            'var txt=v+" "+tt+" "+ds;'
            'if(txt.indexOf(target)!==-1){'
            'var p=[0,0],sz=[0,0];'
            'try{p=ch.position()}catch(x){}'
            'try{sz=ch.size()}catch(x){}'
            'R.push({role:ch.role(),value:v,title:tt,desc:ds,'
            'x:p[0],y:p[1],w:sz[0],h:sz[1]})}'
            'S(ch,d+1)}catch(x){}}}catch(x){}}'
            'try{S(kk.windows[0],0)}catch(x){}'
            'JSON.stringify(R)'
        )
        try:
            proc = subprocess.Popen(
                ["osascript", "-l", "JavaScript", "-e", jxa, "--", filename],
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

        # 2. Copy file to macOS clipboard via NSPasteboard
        #    Path passed as argv to avoid AppleScript injection.
        abs_path = os.path.abspath(file_path)
        copy_script = (
            "on run argv\n"
            '  use framework "AppKit"\n'
            "  set pb to current application's NSPasteboard's generalPasteboard()\n"
            "  pb's clearContents()\n"
            "  set fileURL to current application's |NSURL|'s "
            "fileURLWithPath:(item 1 of argv)\n"
            "  pb's writeObjects:{fileURL}\n"
            '  return "OK"\n'
            "end run"
        )
        copy_start = time.time()
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", copy_script, abs_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            c_out, c_err = proc.communicate(timeout=5.0)
            copy_r = CommandResult(proc.returncode, c_out, c_err,
                                   int((time.time() - copy_start) * 1000), False)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            copy_r = CommandResult(124, "", "timeout",
                                   int((time.time() - copy_start) * 1000), True)
        if copy_r.returncode != 0:
            return self._error_payload(
                "CLIPBOARD_FAILED", "Failed to copy file to clipboard",
                "Check file path and permissions.",
                copy_r.stdout, copy_r.stderr, int((time.time() - start) * 1000),
            )

        # 3. Activate KakaoTalk, paste (Cmd+V), send (Enter)
        paste_r = self._osascript(
            'tell application "KakaoTalk" to activate\n'
            "delay 0.5\n"
            'tell application "System Events"\n'
            '  tell process "KakaoTalk"\n'
            '    keystroke "v" using command down\n'
            "    delay 1.5\n"
            "    keystroke return\n"
            "  end tell\n"
            "end tell\n"
            'return "OK"',
            timeout_sec=15.0,
        )
        latency_ms = int((time.time() - start) * 1000)

        if paste_r.returncode != 0:
            return self._error_payload(
                "PASTE_FAILED", "Failed to paste file into chat",
                "Check Accessibility permissions for System Events.",
                paste_r.stdout, paste_r.stderr, latency_ms,
            )

        return {
            "ok": True,
            "chat": chat,
            "sent": True,
            "file_path": abs_path,
            "meta": {"latency_ms": latency_ms},
        }

    def _call_kmsg_download_file(self, arguments: JSONDict) -> JSONDict:
        chat = str(arguments.get("chat", "")).strip()
        if not chat:
            return self._error_payload(
                "INVALID_ARGUMENT", "chat is required",
                "Provide a chat name.", "", "", 0,
            )

        save_dir = os.path.expanduser(str(arguments.get("save_dir", "~/Downloads")))
        icon_template = str(arguments.get("icon_template_path", "")).strip()
        keep_window = bool(arguments.get("keep_window", False))
        raw_fn = arguments.get("filename")
        filename = str(raw_fn).strip() if raw_fn is not None else ""
        try:
            max_scroll = max(0, min(20, int(arguments.get("max_scroll", 5))))
        except (ValueError, TypeError):
            max_scroll = 5
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

        # 2. Screenshot KakaoTalk window (unpredictable path)
        fd, screenshot_path = tempfile.mkstemp(suffix=".png", prefix="kmsg_dl_")
        os.close(fd)
        has_screenshot = self._screenshot_window(screenshot_path)

        # 3. Scroll-and-search loop: scan AX tree, scroll up if not found
        download_buttons: List[JSONDict] = []
        template_matches: List[List[int]] = []
        file_texts: List[JSONDict] = []
        scroll_count = 0

        for attempt in range(max_scroll + 1):
            # 3a. Search for download buttons via accessibility tree
            download_buttons = self._find_download_buttons_ax()

            # 3b. If filename specified, also search for file text
            if filename:
                file_texts = self._find_file_text_ax(filename)

            # 3c. Template matching fallback (if icon template provided)
            if icon_template and has_screenshot:
                template_matches = self._find_icons_template(screenshot_path, icon_template)

            # 3d. Determine if we found a usable target
            found_target = False
            _MAX_ROW_DISTANCE = 80  # px — file text and its button must be on the same row
            if filename:
                if file_texts and download_buttons:
                    # Verify a download button is actually near the target file
                    ft_cy = file_texts[0].get("y", 0) + file_texts[0].get("h", 0) // 2
                    near_btn = any(
                        abs((b.get("y", 0) + b.get("h", 0) // 2) - ft_cy) <= _MAX_ROW_DISTANCE
                        for b in download_buttons
                    )
                    if near_btn:
                        found_target = True
                    # else: buttons exist but belong to other files — keep scrolling
                elif file_texts:
                    # File visible but no download button = already downloaded
                    found_target = True
            else:
                # No filename filter: any download button is a target
                if download_buttons or template_matches:
                    found_target = True

            if found_target:
                break

            # 3e. Not found yet — scroll up and retry
            if attempt < max_scroll:
                scrolled = self._scroll_in_window("up", 5)
                if scrolled:
                    scroll_count += 1
                    time.sleep(0.8)
                    # Re-screenshot after scroll for template matching
                    if icon_template:
                        has_screenshot = self._screenshot_window(screenshot_path)
                else:
                    break  # scroll failed, stop trying

        # 4. Pick the best download button to click
        _MAX_ROW_DISTANCE = 80
        target_btn: Optional[JSONDict] = None
        if filename and file_texts and download_buttons:
            # Find the download button closest to the filename text, within row distance
            ft = file_texts[0]
            ft_cy = ft.get("y", 0) + ft.get("h", 0) // 2
            nearby = [
                b for b in download_buttons
                if abs((b.get("y", 0) + b.get("h", 0) // 2) - ft_cy) <= _MAX_ROW_DISTANCE
            ]
            if nearby:
                target_btn = min(
                    nearby,
                    key=lambda b: abs((b.get("y", 0) + b.get("h", 0) // 2) - ft_cy),
                )
        elif download_buttons:
            target_btn = download_buttons[-1]  # last = most recent in chat

        # 5. Attempt click-to-download
        downloaded_file: Optional[str] = None
        clicked = False
        try:
            before_files = set(os.listdir(save_dir))
        except OSError:
            before_files = set()

        if target_btn:
            cx = target_btn.get("x", 0) + target_btn.get("w", 0) // 2
            cy = target_btn.get("y", 0) + target_btn.get("h", 0) // 2
            if cx > 0 and cy > 0:
                clicked = self._click_at(cx, cy)
                time.sleep(3.0)
        elif template_matches:
            bounds = self._get_kakao_window_bounds()
            if bounds:
                wx, wy = bounds[0], bounds[1]
                sx = wx + template_matches[-1][0]
                sy = wy + template_matches[-1][1]
                clicked = self._click_at(sx, sy)
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
        if not download_buttons and not template_matches and not file_texts:
            result: JSONDict = {
                "ok": False,
                "error": {
                    "code": "NO_DOWNLOAD_TARGET",
                    "message": "No download button or icon found in chat",
                    "hint": (
                        "Provide icon_template_path for template matching."
                    ),
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

        # File text found but no download button = already downloaded
        if filename and file_texts and not download_buttons and not template_matches:
            return {
                "ok": False,
                "error": {
                    "code": "ALREADY_DOWNLOADED",
                    "message": f"File '{filename}' found but no download button available",
                    "hint": "The file may already be downloaded (shows '열기 · Finder에서 보기').",
                },
                "chat": chat,
                "file_texts_found": len(file_texts),
                "scroll_attempts": scroll_count,
                "meta": {"latency_ms": latency_ms},
            }

        result = {
            "ok": bool(downloaded_file),
            "chat": chat,
            "download_buttons_found": len(download_buttons),
            "template_matches_found": len(template_matches),
            "downloaded_file": downloaded_file,
            "clicked": clicked,
            "scroll_attempts": scroll_count,
            "meta": {"latency_ms": latency_ms},
        }
        if filename:
            result["target_filename"] = filename
            result["file_texts_found"] = len(file_texts)
        if download_buttons:
            result["buttons"] = download_buttons
        if not downloaded_file:
            result["hint"] = (
                "Click attempted but no new file detected in save_dir. "
                "The file may have been saved elsewhere or download may still be in progress."
            )
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
