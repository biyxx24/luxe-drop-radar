#!/bin/bash
# Mac：设置登录后自动运行上新监控（崩溃自动重启，运行期间不让电脑自动睡眠）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then echo "找不到 python3，请先安装 Python 3"; exit 1; fi
case "$DIR" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "⚠️  文件夹在 桌面/文稿/下载 里，Mac 会阻止后台程序读取。请先把整个文件夹移到 $HOME 下再运行本脚本。"
    exit 1;;
esac
"$PY" -c "import curl_cffi, yaml" 2>/dev/null || { echo "缺少依赖，请先运行：$PY -m pip install -r \"$DIR/requirements.txt\""; exit 1; }

PLIST="$HOME/Library/LaunchAgents/com.luxmonitor.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.luxmonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>$DIR/monitor.py</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/launchd.err.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ 已设置：现在已在后台运行，以后登录会自动启动。"
echo "   运行日志：$DIR/monitor.log"
echo "   改完 config.yaml 后重启：launchctl unload \"$PLIST\" && launchctl load \"$PLIST\""
echo "   停止并取消自动运行：launchctl unload \"$PLIST\" && rm \"$PLIST\""
