# kmsg-mcp

> **카카오톡 공식 API?** 없습니다. **벽타기?** 이제 안 해도 됩니다.
>
> `bash install.sh` 한 방이면, Claude Code에서 카톡 읽고 보내기 끝.

---

## 왜 만들었나

AI 시대인데 나만 뒤처지는 것 같은 느낌, 다들 한 번쯤 받아보셨죠?

MCP니 에이전트니 하는 얘기가 쏟아지는데, 막상 카카오톡 하나 자동화하려면 공식 API는 없고, 셀레니움 돌리고, ADB 붙이고, 별의별 벽타기를 해야 합니다. 그 과정에서 포기하는 분들이 많습니다.

**그래서 만들었습니다.** AI 지식 격차에 FOMO를 느끼며, 벽타기하느라 고생하는 분들이 한 방에 해결할 수 있도록.

이 MCP 서버 하나면 Claude Code가 카카오톡을 자연어로 제어합니다. "홍길동한테 카톡 보내줘" 하면 **진짜 보냅니다.**

[kmsg](https://github.com/channprj/kmsg) CLI를 MCP(Model Context Protocol)로 래핑했고, macOS Accessibility API를 사용하기 때문에 **비공식 API 크롤링 없이** 안정적으로 동작합니다.

> **면책 조항**: 이 도구는 개인 생산성 향상 목적으로 제작되었습니다. 카카오톡 이용약관을 준수하며 사용해 주세요. 스팸, 대량 발송, 자동화 도배 등 남용으로 인한 계정 제재는 **본인 책임**입니다. 우리 다 어른이니까, 알아서 잘 쓰시리라 믿습니다. 강제로 쓰라고 한 적 없어요!

## 주요 기능

| 도구 | 설명 |
|------|------|
| `kmsg_read` | 카카오톡 채팅방의 최근 메시지 읽기 |
| `kmsg_send` | 카카오톡 채팅방에 텍스트 메시지 보내기 |
| `kmsg_send_image` | 카카오톡 채팅방에 이미지 보내기 |
| `kmsg_send_file` | 카카오톡 채팅방에 파일 보내기 (문서, 압축파일 등) |
| `kmsg_download_file` | 카카오톡 채팅방에서 파일 첨부 다운로드 (자동 스크롤 탐색) |

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

# 파일 보내기
"개발팀 단톡방에 ./report.pdf 파일 보내줘"

# 파일 다운로드
"홍길동 채팅방에서 파일 다운로드해줘"
"회장님 채팅방에서 '회의록.hwpx' 파일 다운로드해줘"
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

### kmsg_send_file

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `chat` | string | O | 채팅방 또는 사용자 이름 |
| `file_path` | string | O | 보낼 파일의 절대 경로 |
| `confirm` | boolean | X | true면 전송 전 확인 요청 (기본값: false) |
| `keep_window` | boolean | X | 자동 열린 창 유지 (기본값: false) |

### kmsg_download_file

| 파라미터 | 타입 | 필수 | 설명 |
|----------|------|------|------|
| `chat` | string | O | 채팅방 또는 사용자 이름 |
| `filename` | string | X | 다운로드할 파일명 (예: `회의록.txt`). 생략 시 가장 최근 파일 |
| `save_dir` | string | X | 저장 디렉토리 (기본값: `~/Downloads`) |
| `max_scroll` | integer | X | 파일 탐색을 위한 최대 스크롤 횟수 (0-20, 기본값: 5) |
| `icon_template_path` | string | X | 다운로드 아이콘 템플릿 이미지 경로 |
| `keep_window` | boolean | X | 자동 열린 창 유지 (기본값: false) |

> `filename`을 지정하면 채팅방을 위로 스크롤하며 해당 파일을 찾습니다. 화면에 바로 보이지 않는 파일도 자동 탐색합니다.

## 변경 사항

### v0.3.0 (2026-04-23)

`kmsg_send_file`을 실제로 안정적으로 동작하게 만든 릴리스입니다.

**Fixed**
- **`kmsg_send_file` 클립보드 paste 안정화**: macOS AppleScript `keystroke "v" using command down`은 카카오톡 입력 필드에 안정적으로 도달하지 않습니다. 클립보드 + Quartz CGEvent 마우스 클릭 + Quartz 키보드 이벤트 조합으로 재구성. (`POSIX file` 클립보드 → 입력 필드 좌표 AX lookup → Quartz click → Cmd+V → Return)
- **AppleScript `use` 절 위치 오류 수정**: `use framework "AppKit"` 등이 `on run argv` 다음에 와서 osascript 파싱 실패하던 버그 (Korean macOS error: `"end"을(를) 예상했지만 "use"을(를) 발견했습니다`) 수정.
- **CGWindowID vs AppleScript window index 혼용 버그 수정**: `_get_kakao_window_id()`가 반환하는 Quartz window number를 AppleScript `window N` ordinal로 잘못 사용하던 문제 (-10006 에러) 수정.
- **윈도우 락**: 다운로드 흐름에서 screenshot/scroll/click이 모두 동일한 채팅 윈도우(채팅 리스트가 아닌 대화 창)를 타게팅하도록 window_id 캐시.
- **다크모드 한글 OCR 정합화**: 좌표 변환을 `retina_scale = image_width / window_bounds_width`로 정규화하여 Retina 디스플레이에서 클릭 좌표가 빗나가는 문제 수정.

**Added**
- **send_file 입력 좌표 LRU 캐시**: 같은 채팅방에 30초 이내 연속 send_file 시 좌표 lookup 스킵 (체감 1초 단축).
- **paste 실패 시 fallback**: Quartz 경로 실패 시 osascript keystroke으로 한 번 자동 재시도.
- **download_file AX 1차 + OCR fallback**: 화면 녹화 권한이 없는 환경에서도 AX 트리로 첨부 파일 row + 저장 버튼 탐지 시도.
- **다중 카카오톡 윈도우 지원**: 영문/한글 로케일(`KakaoTalk` / `카카오톡`) 모두 인식.

**Changed**
- send_file 응답에서 `verified` 필드 제거. 카카오톡의 파일 메시지가 텍스트 read API에 본문 없이 들어와 검증 로직이 항상 false를 리턴하던 문제 (실제로는 정상 도착하는데 false로 보고). 검증을 옵션화하지 않고 신뢰성 높이는 방향(좌표 정확도 + retry)으로 정리.

**Known Issues**
- `kmsg_download_file`은 다운로드 버튼이 화면에 보이는 row만 AX로 잡힙니다. 화면에 보이지 않는 첨부는 macOS 화면 녹화 권한(System Settings → Privacy & Security → Screen Recording → Claude/터미널 활성화) 부여 후 OCR 경로로만 가능합니다.
- 카카오톡 1:1 채팅방은 한 번이라도 메시지를 주고받아 채팅 목록에 등록돼있어야 검색됩니다 (친구 목록만으로는 검색 안 됨).

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
- **1:1 채팅방의 경우 한 번이라도 대화를 주고받아 채팅 목록에 떠있어야** 검색됩니다 (친구 등록만으로는 안 됨)

### `kmsg_send_file` 응답은 ok:true인데 도착 안 함 (v0.2.0 이하)

v0.3.0에서 해결되었습니다. 업그레이드하세요.

### `kmsg_download_file` "No file or save link found"

다운로드 버튼이 현재 채팅창 화면에 보여야 AX로 잡힙니다. 카카오톡에서 해당 첨부가 보이는 위치까지 스크롤해두고 다시 호출하세요.

화면 밖 첨부 자동 스크롤 탐색은 macOS **화면 녹화 권한**이 필요합니다 (시스템 설정 → 개인정보 보호 및 보안 → 화면 기록 → Claude/터미널 추가 후 재시작).

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
