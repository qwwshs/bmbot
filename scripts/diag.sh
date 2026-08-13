#!/usr/bin/env bash
# ============================================================================
# 服务器资源一键诊断：CPU / 内存 / 磁盘 / OOM / 重启循环
#
# 用法：在服务器上执行  bash ~/qwwshs/scripts/diag.sh
# 把输出发给维护者即可定位「服务器跑满」的原因。
# ============================================================================

BOT_DIR="${1:-$HOME/qwwshs}"     # 项目目录，可在命令行覆盖

echo "========== 1. 系统负载 & 运行时间 =========="
uptime

echo; echo "========== 2. 内存 =========="
free -h

echo; echo "========== 3. 磁盘 =========="
df -h / | tail -1
du -sh "$BOT_DIR/data" "$BOT_DIR/data/bm" 2>/dev/null

echo; echo "========== 4. CPU 占用 TOP 10 =========="
ps aux --sort=-%cpu | head -11

echo; echo "========== 5. 内存占用 TOP 10 =========="
ps aux --sort=-%mem | head -11

echo; echo "========== 6. OOM 记录（内存不足杀进程的证据）=========="
if command -v dmesg >/dev/null && dmesg 2>/dev/null | grep -qiE "oom|killed process"; then
    dmesg -T 2>/dev/null | grep -iE "oom|killed process" | tail -5
else
    echo "（无 dmesg 权限或没有 OOM 记录；可用 sudo dmesg 再查一次）"
fi

echo; echo "========== 7. 相关进程与启动时间（看是否反复重启）=========="
ps -eo pid,lstart,etime,cmd | grep -iE "python|nb |napcat|node" | grep -v grep

echo; echo "========== 8. screen 会话 =========="
screen -ls 2>/dev/null || echo "（无 screen 会话）"

echo; echo "========== 9. bot 屏幕日志中 reload/崩溃痕迹 =========="
if screen -S nb -X hardcopy /tmp/nb_screen.txt 2>/dev/null; then
    echo "「Succeeded to load plugin」出现次数（多次=反复重启）:"
    grep -c "Succeeded to load plugin" /tmp/nb_screen.txt
    echo "「Restarted process」出现次数（--reload 重载次数）:"
    grep -c "Restarted process" /tmp/nb_screen.txt
    echo "--- 最近的错误 ---"
    grep -iE "error|exception|traceback" /tmp/nb_screen.txt | tail -8
else
    echo "（screen 会话 nb 不存在，可能 bot 没在跑或会话名不同）"
fi

echo; echo "========== 10. 看门狗日志（若有） =========="
tail -20 "$BOT_DIR/scripts/watchdog.log" 2>/dev/null || echo "（无看门狗日志）"

echo; echo "========== 诊断完成 =========="
