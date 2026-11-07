#!/usr/bin/env bash
# 数据库故障演练脚本（仅开发机 192.168.0.19 使用）
#
# 通过 iptables 阻断/恢复本机到 openGauss 的出站流量，用于验证
# “数据库宕机不影响 AI 转发”。脚本只操作 OUTPUT 链，不会 kill 任何进程。
#
# 用法：
#   sudo scripts/fault_injection_db.sh block <DB_IP>
#   sudo scripts/fault_injection_db.sh unblock <DB_IP>
set -euo pipefail

ACTION="${1:-}"
DB_IP="${2:-}"

usage() {
    echo "用法: $0 {block|unblock} <DB_IP>" >&2
    exit 2
}

[ -n "$ACTION" ] && [ -n "$DB_IP" ] || usage

# 安全校验 1：仅允许在开发机执行
if ! hostname -I 2>/dev/null | tr ' ' '\n' | grep -qx "192.168.0.19"; then
    echo "[ERROR] 仅允许在开发机 192.168.0.19 执行" >&2
    exit 1
fi

# 安全校验 2：禁止把本机回环/代理自身当作目标
if [ "$DB_IP" = "127.0.0.1" ] || [ "$DB_IP" = "localhost" ]; then
    echo "[ERROR] 不能对回环地址做故障注入" >&2
    exit 1
fi

# 安全校验 3：目标不能是 Claude Code 正在使用的代理端口
if echo "$DB_IP" | grep -q ":5566"; then
    echo "[ERROR] 目标不能包含 5566（代理端口），遵守项目自杀防护规则" >&2
    exit 1
fi

case "$ACTION" in
    block)
        if ! sudo iptables -C OUTPUT -d "$DB_IP" -j DROP 2>/dev/null; then
            sudo iptables -I OUTPUT -d "$DB_IP" -j DROP
        fi
        echo "[OK] 已阻断本机 -> $DB_IP（恢复用 unblock）"
        ;;
    unblock)
        while sudo iptables -C OUTPUT -d "$DB_IP" -j DROP 2>/dev/null; do
            sudo iptables -D OUTPUT -d "$DB_IP" -j DROP
        done
        echo "[OK] 已恢复本机 -> $DB_IP"
        ;;
    *)
        usage
        ;;
esac
