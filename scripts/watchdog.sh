#!/usr/bin/env bash
# ============================================================================
# Berry Melody 查分 Bot 看门狗（Linux 服务器用）
#
# 功能：
#   - 每分钟检查 bot 的 screen 会话与进程是否存活
#   - 下线 → 自动重启 → 通过 NapCat HTTP API 发 QQ 消息通知你
#   - 连续重启超过上限自动冷却，避免 bot 反复崩溃时刷屏
#   - NapCat 自身不可达时也会记日志（可配置附加重启命令）
#
# 安装步骤：
#   1. 把本脚本放到服务器上（例如 ~/qwwshs/scripts/watchdog.sh）
#   2. 修改下方【配置区】
#   3. 确认 NapCat 已开启 HTTP 服务（NapCat WebUI → 网络配置 → HTTP 服务器，
#      端口默认 3000；若设置了访问令牌请填到 NAPCAT_TOKEN）
#   4. crontab -e 加入一行（每分钟执行）：
#        * * * * * /bin/bash /home/<用户名>/qwwshs/scripts/watchdog.sh
#   5. 手动停 bot（如更新代码）前先执行：touch /home/<用户名>/qwwshs/manual_stop
#      开服完成后执行：                   rm  /home/<用户名>/qwwshs/manual_stop
#
# 常用手工检查命令：
#   screen -ls                  # 查看 screen 会话
#   pgrep -af "nb run"          # 查看 bot 进程
#   tail -f watchdog.log        # 查看看门狗日志（在脚本同目录）
# ============================================================================

set -u

# ------------------------------ 配置区 ------------------------------
BOT_DIR="/home/<用户名>/qwwshs"     # 服务器上项目目录
SCREEN_NAME="nb"                    # screen 会话名
START_CMD="nb run"                  # 启动命令（在 BOT_DIR 下执行；建议写绝对路径，如 /root/.local/bin/nb run）
NAPCAT_URL="http://127.0.0.1:3000"  # NapCat HTTP 服务地址
NAPCAT_TOKEN=""                     # NapCat HTTP 访问令牌（没开鉴权就留空）
NOTIFY_QQ=""                          # 接收下线通知的 QQ 号（必填，如 "1234567890"）
MAX_RESTARTS=3                      # 冷却周期内最多自动重启次数
BACKOFF=600                         # 冷却时间（秒）
NAPCAT_RESTART_CMD=""               # NapCat 下线时执行的重启命令（可选，例如 "systemctl restart napcat"）
# --------------------------------------------------------------------

LOG_FILE="$(cd "$(dirname "$0")" && pwd)/watchdog.log"
STOP_FLAG="$BOT_DIR/manual_stop"
STATE_FILE="$BOT_DIR/.watchdog_state"

# cron 环境 PATH 很精简，把常用目录补上（保留原有 PATH，方便排错）
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${HOME:-/root}/.local/bin"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG_FILE"; }

# 通过 NapCat HTTP API 发送 QQ 私聊消息
notify_qq() {
    local msg="$1" curl_args=()
    [ -n "$NAPCAT_TOKEN" ] && curl_args+=(-H "Authorization: Bearer $NAPCAT_TOKEN")
    for path in send_private_msg api/send_private_msg; do
        if curl -s -m 5 -X POST "$NAPCAT_URL/$path" "${curl_args[@]}" \
            -H "Content-Type: application/json" \
            -d "{\"user_id\":$NOTIFY_QQ,\"message\":\"$msg\"}" | grep -q '"status":"ok"'; then
            return 0
        fi
    done
    log "QQ 通知失败：NapCat HTTP 不可达（$NAPCAT_URL）"
    return 1
}

# bot 是否存活：screen 会话存在 且 进程存在
bot_alive() {
    screen -ls "$SCREEN_NAME" 2>/dev/null | grep -q "\.$SCREEN_NAME" \
        && pgrep -f "$START_CMD" >/dev/null 2>&1
}

# 重启 bot（先清残留会话，再开新 screen）
restart_bot() {
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1
    sleep 1
    screen -dmS "$SCREEN_NAME" bash -c "cd '$BOT_DIR' && $START_CMD"
}

# 重启后轮询等待进程出现（最多 30 秒）
wait_bot_up() {
    local i
    for i in 1 2 3 4 5 6; do
        sleep 5
        bot_alive && return 0
    done
    return 1
}

main() {
    # NapCat 自身健康检查（可选告警，不阻塞后续流程）
    if ! curl -s -m 3 "$NAPCAT_URL/get_version" | grep -q '"status":"ok"'; then
        log "⚠️ NapCat HTTP 不可达，可能 NapCat 也下线了"
        if [ -n "$NAPCAT_RESTART_CMD" ]; then
            log "尝试执行 NAPCAT_RESTART_CMD：$NAPCAT_RESTART_CMD"
            bash -c "$NAPCAT_RESTART_CMD" >> "$LOG_FILE" 2>&1
        fi
    fi

    [ -f "$STOP_FLAG" ] && exit 0   # 手动停止中，不打扰
    bot_alive && exit 0             # 一切正常

    # 冷却控制：连挂多次就歇一会，避免刷屏
    local now last count=0
    now=$(date +%s)
    if [ -f "$STATE_FILE" ]; then
        read -r last count < "$STATE_FILE" 2>/dev/null || true
    fi
    last=${last:-0}
    count=${count:-0}
    [ $((now - last)) -ge $BACKOFF ] && count=0   # 冷却期已过，重置计数

    if [ "$count" -ge "$MAX_RESTARTS" ]; then
        log "已连续重启 ${count} 次，进入 ${BACKOFF}s 冷却，本次跳过（请人工检查）"
        exit 0
    fi

    log "检测到 bot 下线，尝试自动重启..."
    restart_bot
    if wait_bot_up; then
        log "重启成功"
        notify_qq "⚠️ Bot 掉线，已自动重启 ✅"
    else
        log "重启失败！"
        notify_qq "❌ Bot 掉线且重启失败，请登录服务器检查！"
    fi
    echo "$now $((count + 1))" > "$STATE_FILE"
}

main
