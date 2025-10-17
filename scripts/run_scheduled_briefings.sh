#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log_section() {
    local title="$1"
    printf '\n======================================\n%s\n======================================\n' "$title"
}

run_target() {
    local target="$1"
    log_section "开始执行 ${target} 任务 (MULTI_STAGE=1)"
    if MULTI_STAGE=1 make "$target"; then
        log_section "${target} 任务完成"
    else
        log_section "${target} 任务失败"
        return 1
    fi
}

log_section "启动自动化简报任务"

# 确保 Docker 服务就绪
log_section "等待 Docker 服务启动..."
cd "$REPO_ROOT" || exit 1
make start >/dev/null 2>&1 || {
    echo "警告: make start 失败，尝试继续..."
}

# 等待服务健康检查
max_wait=60
waited=0
while [ $waited -lt $max_wait ]; do
    if curl -sf http://localhost:1200/healthz >/dev/null 2>&1 && \
       curl -sf http://localhost:8080/health >/dev/null 2>&1; then
        log_section "服务就绪，开始收集任务"
        break
    fi
    echo "等待服务启动... ($waited/$max_wait 秒)"
    sleep 5
    waited=$((waited + 5))
done

if [ $waited -ge $max_wait ]; then
    log_section "警告: 服务启动超时，仍然尝试运行任务"
fi

status=0
if ! run_target "twitter"; then
    status=1
fi
if ! run_target "hn"; then
    status=1
fi

log_section "全部任务结束 (exit_code=${status})"
exit "$status"
