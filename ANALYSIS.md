# kmsg-mcp 레포지토리 분석 결과

## 프로젝트 개요

| 항목 | 내용 |
|------|------|
| **이름** | kmsg-mcp |
| **버전** | 0.1.0 |
| **라이선스** | MIT (2026) |
| **언어** | Python 3.8+ (표준 라이브러리만 사용, 외부 pip 의존성 0) |
| **플랫폼** | macOS 전용 (KakaoTalk Desktop이 macOS only) |
| **프로토콜** | MCP 2024-11-05 / JSON-RPC 2.0 over stdio |

## 파일 구조

```
kmsg-mcp/
├── kmsg-mcp.py              # 메인 MCP 서버 (763줄)
├── install.sh               # 원클릭 설치 스크립트 (140줄)
├── README.md                # 한국어 문서 (202줄)
├── mcp-config-example.json  # Claude Code MCP 설정 예시
├── VERSION                  # 0.1.0
├── LICENSE                  # MIT
└── .gitignore
```

## 아키텍처 흐름

```
Claude Code (사용자 자연어)
  ↓ stdio (newline-delimited JSON)
kmsg-mcp.py (Python MCP 서버)
  ↓ subprocess
kmsg (Native macOS 바이너리, Homebrew로 설치)
  ↓ system calls
macOS Accessibility API
  ↓
KakaoTalk.app
```

## 핵심 클래스 (kmsg-mcp.py)

### `CommandResult` (line 23-29)
- subprocess 실행 결과를 감싸는 dataclass
- `returncode`, `stdout`, `stderr`, `latency_ms`, `timed_out`

### `MCPError` (line 32-37)
- MCP 프로토콜 에러 커스텀 예외

### `KmsgRunner` (line 40-123)
- kmsg 바이너리 실행 관리
- `_resolve_kmsg_bin()`: 환경변수 → PATH → 기본경로 순으로 kmsg 위치 탐색
- `run()`: subprocess 실행 + 타임아웃 처리
- `check_ready()`: kmsg 설치/KakaoTalk 연결 상태 확인

### `OpenClawKmsgMCPServer` (line 152-753)
- 메인 MCP 서버 구현체
- Thread-safe stdout 쓰기 (threading.Lock)
- LSP Content-Length 헤더 + newline-delimited JSON 둘 다 지원

## MCP 도구 3개

| 도구 | 기능 | 주요 파라미터 |
|------|------|---------------|
| `kmsg_read` | 채팅 메시지 읽기 | `chat`, `limit` (1-100), `deep_recovery`, `keep_window`, `trace_ax` |
| `kmsg_send` | 텍스트 메시지 전송 | `chat`, `message`, `confirm` (기본 false) |
| `kmsg_send_image` | 이미지 파일 전송 | `chat`, `image_path`, `confirm` |

## 에러 처리 전략

- 에러 코드 자동 감지: `KMSG_BIN_NOT_FOUND`, `KAKAO_WINDOW_UNAVAILABLE`, `CHAT_NOT_FOUND`, `ACCESSIBILITY_PERMISSION_DENIED` 등
- 채팅방 검색 실패 시 `--deep-recovery` 플래그로 자동 재시도
- 각 에러별 사용자 친화적 hint 메시지 제공
- 타임아웃: 읽기 20-40초, 전송 10-18초, 이미지 12-20초

## 설치 프로세스 (install.sh)

1. macOS 플랫폼 검증
2. Python 3 확인
3. Homebrew 확인/설치
4. `brew install channprj/tap/kmsg`
5. `~/.local/share/kmsg-mcp/`에 파일 복사
6. `~/.claude.json`에 MCP 설정 자동 병합 (Python JSON 조작, jq 불필요)

## 설계 특징

- **외부 의존성 제로**: pip 패키지 없이 Python 표준 라이브러리만 사용
- **안정적 자동화**: 웹 스크래핑이나 비공식 API 대신 macOS Accessibility API 사용
- **확인 모드**: `confirm=true`로 실수 발송 방지
- **Deep Recovery**: 어려운 윈도우 탐지 상황을 위한 복구 모드
- **AX Tracing**: 디버깅용 접근성 API 트레이스 로그

## 부재 항목 (의도적)

- 테스트 코드 없음 (test/, pytest 등)
- CI/CD 파이프라인 없음
- 빌드 시스템 없음 (순수 Python, 빌드 불필요)
- Docker 없음 (macOS 전용이므로)
- 타입 체크 설정 없음 (mypy, pyright 등)
