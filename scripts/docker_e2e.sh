#!/usr/bin/env bash
# ===================================
# Docker E2E 冒烟测试
# ===================================
#
# 目标：在真实 Docker 镜像中验证「构建 -> 启动 -> 健康检查 -> 关键只读接口」
# 全链路可用，作为 CI docker-build 之外的端到端补充。
#
# 设计原则：
#   - 不依赖任何外部 LLM / 数据源 / 通知配置，全部使用只读或本地接口
#   - 不写入仓库工作区，使用临时目录挂载 data/logs/reports
#   - 失败即非零退出，便于 CI 直接作为阻断项
#
# 使用方法：
#   ./scripts/docker_e2e.sh
#
# 可选环境变量：
#   DOCKER_E2E_IMAGE   镜像名（默认 stock-analysis:e2e）
#   DOCKER_E2E_PORT    宿主机端口（默认 18000）
#   DOCKER_E2E_KEEP    设为 1 时保留容器与命名卷，便于排障
#
# 数据目录使用 Docker 命名卷（而非宿主机 bind mount）并在启动前显式把属主
# 初始化为镜像运行时用户 (UID/GID 1000)，因此不依赖调用者宿主机的 UID，
# Linux CI runner 上同样可写。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${DOCKER_E2E_IMAGE:-stock-analysis:e2e}"
HOST_PORT="${DOCKER_E2E_PORT:-18000}"
CONTAINER_NAME="dsa-e2e-$$"
KEEP="${DOCKER_E2E_KEEP:-0}"

# 运行时用户（见 docker/Dockerfile: useradd -u 1000 -g dsa）
RUNTIME_UID=1000
RUNTIME_GID=1000
VOLUME_PREFIX="dsa-e2e-vol-$$"
DATA_VOLUME="${VOLUME_PREFIX}-data"
LOGS_VOLUME="${VOLUME_PREFIX}-logs"
REPORTS_VOLUME="${VOLUME_PREFIX}-reports"
VOLUMES_CREATED=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[PASS]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[FAIL]${NC} $1"; }

cleanup() {
    local exit_code=$?
    if [ "$KEEP" = "1" ]; then
        warn "DOCKER_E2E_KEEP=1，保留容器 ${CONTAINER_NAME} 与命名卷 ${VOLUME_PREFIX}-*"
        return
    fi
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    if [ -n "$VOLUMES_CREATED" ]; then
        # shellcheck disable=SC2086
        docker volume rm $VOLUMES_CREATED >/dev/null 2>&1 || true
    fi
    exit "$exit_code"
}
trap cleanup EXIT

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        error "未找到 docker 命令"
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        error "Docker daemon 不可用"
        exit 1
    fi
}

# 断言 HTTP 状态码与可选响应体关键字
# 用法: assert_http <描述> <url> <期望状态码> [期望包含的字符串]
assert_http() {
    local desc="$1" url="$2" expected_code="$3" expect_body="${4:-}"
    local body_file
    body_file="$(mktemp)"
    local code
    code="$(curl -sS -o "$body_file" -w '%{http_code}' --max-time 15 "$url" || echo "000")"

    if [ "$code" != "$expected_code" ]; then
        error "${desc}: 期望 HTTP ${expected_code}，实际 ${code} (${url})"
        echo "----- 响应体 -----"
        cat "$body_file" || true
        echo "------------------"
        rm -f "$body_file"
        return 1
    fi

    if [ -n "$expect_body" ] && ! grep -q "$expect_body" "$body_file"; then
        error "${desc}: 响应体未包含 '${expect_body}' (${url})"
        echo "----- 响应体 -----"
        cat "$body_file" || true
        echo "------------------"
        rm -f "$body_file"
        return 1
    fi

    rm -f "$body_file"
    success "${desc} (HTTP ${code})"
}

wait_for_health() {
    local url="http://127.0.0.1:${HOST_PORT}/api/health"
    local attempts=60
    info "等待服务就绪: ${url}"
    for i in $(seq 1 "$attempts"); do
        if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
            success "服务已就绪（第 ${i} 次探测）"
            return 0
        fi
        # 容器提前退出则立即失败，避免空等
        if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            error "容器已退出，无法就绪"
            docker logs "$CONTAINER_NAME" 2>&1 | tail -n 60 || true
            return 1
        fi
        sleep 2
    done
    error "等待服务就绪超时（${attempts} 次探测）"
    docker logs "$CONTAINER_NAME" 2>&1 | tail -n 60 || true
    return 1
}

main() {
    require_docker

    echo ""
    echo "=============================================="
    echo -e "${GREEN}Docker E2E 冒烟测试${NC}"
    echo "=============================================="
    echo ""

    # ---------- 1. 构建镜像 ----------
    info "构建镜像 ${IMAGE} ..."
    docker build -t "$IMAGE" -f docker/Dockerfile .
    success "镜像构建完成"

    # ---------- 2. 准备命名卷并显式初始化属主 ----------
    # 命名卷不受调用者宿主机 UID 影响；再以 root 显式 chown 一次，保证
    # Linux runner（UID != 1000）下 /app/data|logs|reports 依然可写。
    docker volume create "$DATA_VOLUME" >/dev/null
    docker volume create "$LOGS_VOLUME" >/dev/null
    docker volume create "$REPORTS_VOLUME" >/dev/null
    VOLUMES_CREATED="${DATA_VOLUME} ${LOGS_VOLUME} ${REPORTS_VOLUME}"
    info "命名卷: ${VOLUMES_CREATED}"

    docker run --rm --user 0 \
        -v "${DATA_VOLUME}:/vol/data" \
        -v "${LOGS_VOLUME}:/vol/logs" \
        -v "${REPORTS_VOLUME}:/vol/reports" \
        --entrypoint sh "$IMAGE" -c \
        "chown -R ${RUNTIME_UID}:${RUNTIME_GID} /vol/data /vol/logs /vol/reports && chmod 755 /vol/data /vol/logs /vol/reports"
    success "数据卷属主已初始化为 ${RUNTIME_UID}:${RUNTIME_GID}"

    # ---------- 3. 启动容器（serve-only，无外部依赖） ----------
    info "启动容器 ${CONTAINER_NAME} ..."
    docker run -d \
        --name "$CONTAINER_NAME" \
        -p "127.0.0.1:${HOST_PORT}:8000" \
        -e WEBUI_HOST=0.0.0.0 \
        -e API_PORT=8000 \
        -e ADMIN_AUTH_ENABLED=false \
        -e SCHEDULE_ENABLED=false \
        -v "${DATA_VOLUME}:/app/data" \
        -v "${LOGS_VOLUME}:/app/logs" \
        -v "${REPORTS_VOLUME}:/app/reports" \
        "$IMAGE" \
        python main.py --serve-only --host 0.0.0.0 --port 8000 >/dev/null

    # ---------- 4. 健康检查 ----------
    wait_for_health

    local base="http://127.0.0.1:${HOST_PORT}"

    # ---------- 5. 关键只读接口 ----------
    # 注意：健康检查仅注册在 /api/health（见 api/app.py），v1 路由未挂载 health。
    assert_http "根健康检查 /api/health"            "${base}/api/health" "200" '"status":"ok"'
    assert_http "认证状态 /api/v1/auth/status"      "${base}/api/v1/auth/status" "200"
    assert_http "配置 setup 状态"                   "${base}/api/v1/system/config/setup/status" "200"
    assert_http "Agent 技能列表 /api/v1/agent/skills" "${base}/api/v1/agent/skills" "200"
    assert_http "OpenAPI 文档 /docs"                "${base}/docs" "200"
    assert_http "未知 API 返回 JSON 404"            "${base}/api/does-not-exist" "404" '"error"'

    # ---------- 6. 前端静态资源已随镜像构建 ----------
    # index.html 使用小写 <!doctype html>，grep 需忽略大小写。
    assert_http "前端首页 /"                        "${base}/" "200" "<!doctype html>"

    # 校验 index.html 引用的首个 /assets/* 资源真实存在（防止空白页回归）
    local asset_ref
    asset_ref="$(curl -fsS --max-time 15 "${base}/" | grep -oE '/assets/[^"]+\.(js|css)' | head -n 1 || true)"
    if [ -z "$asset_ref" ]; then
        error "前端首页未引用任何 /assets/* 资源"
        return 1
    fi
    assert_http "前端资源 ${asset_ref}"             "${base}${asset_ref}" "200"

    # ---------- 7. 非 root 用户与数据目录可写 ----------
    local whoami
    whoami="$(docker exec "$CONTAINER_NAME" whoami)"
    if [ "$whoami" != "dsa" ]; then
        error "容器内用户应为 dsa，实际为 ${whoami}"
        return 1
    fi
    success "容器以非 root 用户运行 (${whoami})"

    if ! docker exec "$CONTAINER_NAME" sh -c '
        for d in /app/data /app/logs /app/reports; do
            touch "$d/.e2e_write_test" && rm -f "$d/.e2e_write_test" || exit 1
        done'; then
        error "数据目录 /app/data|/app/logs|/app/reports 不可写"
        return 1
    fi
    success "数据目录 /app/data、/app/logs、/app/reports 均可写（UID/GID ${RUNTIME_UID}）"

    # ---------- 8. 容器仍在运行 ----------
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        error "容器在测试过程中退出"
        docker logs "$CONTAINER_NAME" 2>&1 | tail -n 60 || true
        return 1
    fi
    success "容器保持运行"

    echo ""
    echo "=============================================="
    echo -e "${GREEN}✅ Docker E2E 冒烟测试全部通过${NC}"
    echo "=============================================="
    echo ""
}

main "$@"
