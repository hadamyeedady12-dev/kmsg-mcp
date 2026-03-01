# kmsg-mcp

Claude Code에서 카카오톡 메시지를 읽고 보낼 수 있는 MCP 서버입니다.

[kmsg](https://github.com/channprj/kmsg) CLI를 MCP(Model Context Protocol) 도구로 래핑하여, Claude Code가 자연어로 카카오톡을 제어할 수 있게 합니다.

## 주요 기능

| 도구 | 설명 |
|------|------|
| `kmsg_read` | 카카오톡 채팅방의 최근 메시지 읽기 |
| `kmsg_send` | 카카오톡 채팅방에 텍스트 메시지 보내기 |
| `kmsg_send_image` | 카카오톡 채팅방에 이미지 보내기 |

## 전제 조건

- **macOS** (카카오톡 데스크톱은 macOS 전용)
- **KakaoTalk** 데스크톱 앱 설치 및 로그인
- **Python 3.8+** (macOS 기본 포함)
- **Homebrew** ([설치](https://brew.sh))
- **Claude Code** ([설치](https://docs.anthropic.com/en/docs/claude-code/overview))

## 설치

### 원클릭 설치

```bash
git clone https://github.com/hadamyeedady12-dev/kmsg-mcp.git
cd kmsg-mcp
bash install.sh
```

### 수동 설치

1. **kmsg 설치**

```bash
brew install channprj/tap/kmsg
```

2. **MCP 서버 복사**

```bash
mkdir -p ~/.local/share/kmsg-mcp
cp kmsg-mcp.py ~/.local/share/kmsg-mcp/
cp VERSION ~/.local/share/kmsg-mcp/
```

3. **Claude Code MCP 설정**

`~/.claude.json` 파일에 아래 내용을 추가하세요:

```json
{
  "mcpServers": {
    "kmsg": {
      "type": "stdio",
      "command": "python3",
      "args": ["-u", "~/.local/share/kmsg-mcp/kmsg-mcp.py"],
      "env": {
        "KMSG_BIN": "/opt/homebrew/bin/kmsg",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

> `KMSG_BIN` 경로는 `which kmsg`로 확인할 수 있습니다.

4. **Claude Code 재시작**

## Accessibility 권한 설정

kmsg는 macOS Accessibility API를 사용하여 카카오톡을 제어합니다. 터미널 앱에 권한을 부여해야 합니다:

1. **시스템 설정** > **개인정보 보호 및 보안** > **손쉬운 사용**
2. 사용 중인 터미널 앱 추가 (Terminal, iTerm2, Warp 등)
3. Claude Code를 IDE에서 사용하는 경우, 해당 IDE도 추가 (VS Code, Cursor 등)

## 사용법

Claude Code에서 자연어로 사용하면 됩니다:

```
# 메시지 읽기
"홍길동 카카오톡 메시지 읽어줘"
"개발팀 단톡방 최근 메시지 50개 보여줘"

# 메시지 보내기
"홍길동한테 카카오톡으로 '회의 시간 변경됐어' 보내줘"

# 이미지 보내기
"홍길동한테 ./screenshot.png 이미지 카카오톡으로 보내줘"
```

## MCP 도구 상세

### kmsg_read

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `chat` | string | O | 채팅방 또는 사용자 이름 |
| `limit` | integer | X | 읽을 메시지 수 (1-100, 기본값: 20) |
| `deep_recovery` | boolean | X | 윈도우 복구 모드 (기본값: false) |
| `keep_window` | boolean | X | 자동 열린 창 유지 (기본값: false) |
| `trace_ax` | boolean | X | AX 디버깅 로그 포함 (기본값: false) |

### kmsg_send

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `chat` | string | O | 채팅방 또는 사용자 이름 |
| `message` | string | O | 보낼 메시지 |
| `confirm` | boolean | X | true면 전송 전 확인 요청 (기본값: false) |

### kmsg_send_image

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `chat` | string | O | 채팅방 또는 사용자 이름 |
| `image_path` | string | O | 이미지 파일 경로 |
| `confirm` | boolean | X | true면 전송 전 확인 요청 (기본값: false) |

## 트러블슈팅

### "kmsg binary not executable"

kmsg가 설치되지 않았거나 PATH에 없습니다.

```bash
brew install channprj/tap/kmsg
which kmsg  # 경로 확인
```

### "Accessibility permission denied"

터미널 앱에 Accessibility 권한이 없습니다.

**시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용**에서 터미널 앱을 추가하세요.

### "KakaoTalk window was not ready"

카카오톡이 실행되지 않았거나 최소화 상태입니다.

1. 카카오톡을 열고 로그인하세요
2. `deep_recovery: true` 옵션을 사용해보세요

### "Chat was not found"

채팅방 이름이 정확하지 않습니다.

- 카카오톡에서 표시되는 정확한 이름을 사용하세요
- 띄어쓰기에 주의하세요

## 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `KMSG_BIN` | kmsg 바이너리 경로 | `which kmsg` |
| `KMSG_DEFAULT_DEEP_RECOVERY` | 기본 deep recovery 활성화 | `false` |
| `KMSG_TRACE_DEFAULT` | 기본 AX 트레이스 활성화 | `false` |
| `KMSG_MCP_VERSION` | 서버 버전 오버라이드 | VERSION 파일 |

## 아키텍처

```
Claude Code
  -> stdio -> kmsg-mcp.py (Python MCP 서버)
    -> subprocess -> kmsg (네이티브 바이너리)
      -> macOS Accessibility API
        -> KakaoTalk.app
```

- **순수 Python 표준 라이브러리** - 외부 패키지 설치 불필요
- **MCP Protocol 2024-11-05** 준수
- **JSON-RPC 2.0** over stdio

## 라이선스

MIT License - [LICENSE](LICENSE) 참조

## 크레딧

- [kmsg](https://github.com/channprj/kmsg) - KakaoTalk CLI by [@channprj](https://github.com/channprj)
