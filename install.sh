#!/bin/bash
set -euo pipefail

# kmsg-mcp installer
# KakaoTalk MCP server for Claude Code

INSTALL_DIR="$HOME/.local/share/kmsg-mcp"
CLAUDE_CONFIG="$HOME/.claude.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}  kmsg-mcp installer${NC}"
echo -e "${GREEN}  KakaoTalk MCP for Claude Code${NC}"
echo -e "${GREEN}================================${NC}"
echo ""

# 1. macOS check
if [[ "$(uname -s)" != "Darwin" ]]; then
    error "This tool only works on macOS (KakaoTalk desktop is macOS-only)."
fi
ok "macOS detected"

# 2. Python 3 check
if ! command -v python3 &>/dev/null; then
    error "Python 3 is required. Install via: xcode-select --install"
fi
ok "Python 3 found: $(python3 --version)"

# 3. Homebrew check & install kmsg
if ! command -v brew &>/dev/null; then
    error "Homebrew is required. Install from https://brew.sh"
fi
ok "Homebrew found"

if command -v kmsg &>/dev/null; then
    ok "kmsg already installed: $(kmsg --version 2>/dev/null || echo 'unknown version')"
else
    info "Installing kmsg via Homebrew..."
    brew install channprj/tap/kmsg
    ok "kmsg installed: $(kmsg --version 2>/dev/null || echo 'installed')"
fi

# 4. Copy MCP server files
info "Installing MCP server to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/kmsg-mcp.py" "$INSTALL_DIR/kmsg-mcp.py"
cp "$SCRIPT_DIR/VERSION" "$INSTALL_DIR/VERSION"
chmod +x "$INSTALL_DIR/kmsg-mcp.py"
ok "MCP server installed"

# 5. Find kmsg binary path
KMSG_BIN="$(command -v kmsg)"
ok "kmsg binary: $KMSG_BIN"

# 6. Configure Claude Code MCP
info "Configuring Claude Code MCP..."

MCP_CONFIG=$(cat <<ENDJSON
{
  "type": "stdio",
  "command": "python3",
  "args": ["-u", "$INSTALL_DIR/kmsg-mcp.py"],
  "env": {
    "KMSG_BIN": "$KMSG_BIN",
    "PYTHONUNBUFFERED": "1"
  }
}
ENDJSON
)

if [[ -f "$CLAUDE_CONFIG" ]]; then
    # Merge into existing config using Python (no jq dependency)
    python3 -c "
import json, sys

config_path = '$CLAUDE_CONFIG'
with open(config_path, 'r') as f:
    config = json.load(f)

mcp_config = json.loads('''$MCP_CONFIG''')

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['kmsg'] = mcp_config

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write('\n')

print('Merged into existing config')
"
else
    # Create new config
    python3 -c "
import json

config = {
    'mcpServers': {
        'kmsg': json.loads('''$MCP_CONFIG''')
    }
}

with open('$CLAUDE_CONFIG', 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write('\n')

print('Created new config')
"
fi
ok "Claude Code MCP configured"

# 7. Done
echo ""
echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}================================${NC}"
echo ""
echo -e "Next steps:"
echo -e "  1. Open ${YELLOW}KakaoTalk${NC} on your Mac"
echo -e "  2. Grant Accessibility permission:"
echo -e "     ${BLUE}System Settings > Privacy & Security > Accessibility${NC}"
echo -e "     Add your terminal app (Terminal, iTerm2, etc.)"
echo -e "  3. Restart ${YELLOW}Claude Code${NC}"
echo -e "  4. Try: \"Read my KakaoTalk messages from [chat name]\""
echo ""
