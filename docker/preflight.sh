#!/bin/bash
# CC-Proxy 部署前宿主机检查
#
# 用途：在 docker-compose up / docker restart 之前运行，
#       提前发现会让容器"看起来起来了但出网全断"的宿主机内核参数问题。
#
# 背景：2026-11-25 生产 18 的 net.ipv4.ip_forward 被改为 0，
#       所有容器出站连接（含连 39:5432 数据库）全部超时，
#       cc-proxy 主容器反复重启、writer 无法消费 spool。
#       Docker 启动时会把 ip_forward 设为 1，但只是内存值，
#       若之后被其它工具/加固脚本改回 0，容器立刻失去出网能力。
#
# 用法：
#   bash docker/preflight.sh          # 仅检查，失败返回非零
#   bash docker/preflight.sh --fix    # 检查并尝试自动修复（需要 sudo）

set -u

FIX=0
if [ "${1:-}" = "--fix" ]; then
    FIX=1
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAIL=0

check_ip_forward() {
    local v
    v=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo "missing")
    if [ "$v" = "1" ]; then
        echo -e "${GREEN}✓${NC} net.ipv4.ip_forward = 1"
        return 0
    fi

    echo -e "${RED}✗${NC} net.ipv4.ip_forward = $v"
    echo "  影响：所有 docker 容器出站连接超时（连不上数据库、连不上任何上游 API）"
    echo "  修复：sudo sysctl -w net.ipv4.ip_forward=1"
    echo "        并写入 /etc/sysctl.conf 持久化"

    if [ "$FIX" = "1" ]; then
        echo -e "  ${YELLOW}尝试自动修复...${NC}"
        if sudo -n sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1; then
            if ! grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf 2>/dev/null; then
                echo 'net.ipv4.ip_forward = 1' | sudo -n tee -a /etc/sysctl.conf >/dev/null
            else
                sudo -n sed -i 's|^net.ipv4.ip_forward.*|net.ipv4.ip_forward = 1|' /etc/sysctl.conf
            fi
            echo -e "  ${GREEN}已修复并持久化${NC}"
            return 0
        else
            echo -e "  ${RED}自动修复失败（需要 sudo 权限）${NC}"
        fi
    fi
    FAIL=1
}

check_docker_running() {
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "${YELLOW}-${NC} docker 命令不存在（在容器内运行属正常，跳过）"
        return 0
    fi
    if docker info >/dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} docker daemon 运行中"
    else
        echo -e "${RED}✗${NC} docker daemon 未运行或当前用户无权限"
        FAIL=1
    fi
}

check_port_free() {
    local port=$1
    if ss -tln 2>/dev/null | grep -q ":${port} "; then
        echo -e "${YELLOW}-${NC} 端口 ${port} 已被占用（若是已有 cc-proxy 在跑，属正常）"
    else
        echo -e "${GREEN}✓${NC} 端口 ${port} 空闲"
    fi
}

echo "== CC-Proxy 部署前检查 =="
check_ip_forward
check_docker_running
check_port_free 5566

if [ "$FAIL" = "1" ]; then
    echo
    echo -e "${RED}检查未通过${NC}，请先修复上述问题再部署。"
    exit 1
fi
echo
echo -e "${GREEN}检查通过${NC}"
exit 0
