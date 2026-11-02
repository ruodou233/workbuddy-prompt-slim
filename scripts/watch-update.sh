#!/bin/bash
# WorkBuddy 升级后自动重放精简配置：apply -> verify -> 发一条 macOS 通知。
# 由 launchd 的 WatchPaths（监视 Info.plist）触发，也可以手动运行。
#
# 环境变量（都有默认值）：
#   WB_SLIM_PROFILE  配置目录，默认 <skill>/profile
#   WB_SLIM_STATE    日志和备份的上级目录，默认 ~/.workbuddy-slim
#   WB_SLIM_DELAY    触发后先等几秒再动手（等安装器写完），默认 45
#   WB_SLIM_ARGS     额外传给 slim.py 的全局参数，例如 "--bundle /x --home /y"（测试用）
#   WB_SLIM_NOTIFY   设为 0 则不发通知，只写日志（测试用）
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SLIM="$HERE/slim.py"
PROFILE="${WB_SLIM_PROFILE:-$HERE/../profile}"
STATE="${WB_SLIM_STATE:-$HOME/.workbuddy-slim}"
DELAY="${WB_SLIM_DELAY:-45}"
PY=/usr/bin/python3
read -r -a EXTRA <<< "${WB_SLIM_ARGS:-}"

mkdir -p "$STATE/logs"
LOG="$STATE/logs/watch-$(date +%Y%m%d).log"
exec >>"$LOG" 2>&1

notify() {
  echo "通知: $1"
  [ "${WB_SLIM_NOTIFY:-1}" = "0" ] && return 0
  /usr/bin/osascript -e "display notification \"$1\" with title \"WorkBuddy 提示词精简\"" >/dev/null 2>&1 || true
}

echo "== $(date '+%F %T') 触发"

# 同一时刻只跑一个实例（安装器会连续改好几次 Info.plist）
LOCK="$STATE/watch.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "已有实例在运行，退出"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

if [ ! -d "$PROFILE" ]; then
  notify "找不到配置目录 $PROFILE，看日志 $LOG"
  exit 1
fi

sleep "$DELAY"

APPLY_OUT="$("$PY" -I "$SLIM" ${EXTRA[@]+"${EXTRA[@]}"} apply --profile "$PROFILE" 2>&1)"
APPLY_RC=$?
echo "$APPLY_OUT"
CHANGED="$(printf '%s\n' "$APPLY_OUT" | sed -n 's/^SUMMARY changed=\([0-9]*\).*/\1/p' | tail -1)"
CHANGED="${CHANGED:-0}"
DENIED="$(printf '%s\n' "$APPLY_OUT" | sed -n 's/.*denied=\([0-9]*\).*/\1/p' | tail -1)"
DENIED="${DENIED:-0}"

"$PY" -I "$SLIM" ${EXTRA[@]+"${EXTRA[@]}"} verify --profile "$PROFILE"
VERIFY_RC=$?

if [ "$DENIED" -gt 0 ]; then
  # 后台进程没有「App 管理」权限：用户目录那部分已重放，应用包内的要在前台补一次
  notify "WorkBuddy 已升级：$CHANGED 项已重放，应用包内 $DENIED 项需在终端或 Agent 里重跑 apply（macOS 权限），详见日志"
  exit 1
fi
if [ "$APPLY_RC" -ne 0 ] || [ "$VERIFY_RC" -ne 0 ]; then
  notify "验证失败，看日志 $LOG"
  exit 1
fi
if [ "$CHANGED" -gt 0 ]; then
  notify "已重放 $CHANGED 项，请重启 WorkBuddy 生效"
else
  echo "没有需要重放的改动"
fi
exit 0
