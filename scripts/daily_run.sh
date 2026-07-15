#!/usr/bin/env bash
# 每日数据更新一键脚本（支持中断续跑）
#
# 用法:
#   bash scripts/daily_run.sh                        # 正常运行，自动跳过今日已完成步骤
#   bash scripts/daily_run.sh --reset                # 清除今日进度，从头开始
#   bash scripts/daily_run.sh --skip-fundamental     # 跳过财务快照更新
#   bash scripts/daily_run.sh --skip-news-signals    # 跳过新闻信号维护
#   bash scripts/daily_run.sh --dry-run              # 只打印，不执行
#
# 执行顺序:
#   1. update_sequoia_candidates.py   (baostock, 更新 stock_daily 个股日线 + 上证指数)
#   2. update_fundamental_candidates.py (tushare, 更新财务快照, 可跳过)
#   3. maintain_news_signals.py       (opt-in, 新闻卡片/事件/outcome/Graphiti repair)
#
# 续跑机制:
#   每天在 .cache/ 下生成一个状态文件 daily_run_YYYYMMDD.state。
#   Sequoia 日线步骤按最新已完成交易日写入标记，避免盘中运行更新到上一交易日后
#   阻止收盘后补齐当日行情；其他步骤按自然日写入标记。
#   每步成功完成后写入标记；中断重跑时自动跳过已完成步骤。
#   --reset 可强制清除状态重头跑。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------- 参数解析 ----------
SKIP_FUNDAMENTAL=false
SKIP_NEWS_SIGNALS=false
DRY_RUN=false
RESET=false

for arg in "$@"; do
    case "$arg" in
        --skip-fundamental) SKIP_FUNDAMENTAL=true ;;
        --skip-news-signals) SKIP_NEWS_SIGNALS=true ;;
        --dry-run)          DRY_RUN=true ;;
        --reset)            RESET=true ;;
    esac
done

# ---------- 加载 .env ----------
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    # bash 3.2 (macOS default) does not load variables from process substitution via
    # `source <(...)`.  Using `source /dev/stdin <<<` works on both 3.2 and 4+.
    source /dev/stdin <<< "$(grep -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$REPO_ROOT/.env")"
    set +a
fi

# ---------- 工具函数 ----------
log()    { echo "[$(TZ='Asia/Shanghai' date '+%H:%M:%S')] $*"; }
die()    { echo "[ERROR] $*" >&2; exit 1; }
ok()     { log "✅ $*"; }
warn()   { log "⚠️  $*"; }
info()   { log "   $*"; }
sep()    { log "──────────────────────────────────────────"; }
masked() { local v="$1"; [ -n "$v" ] && echo "${v:0:4}****${v: -2}" || echo "(未配置)"; }

elapsed() {
    local s=$1
    if [ "$s" -ge 3600 ]; then printf "%dh%dm%ds" $((s/3600)) $(((s%3600)/60)) $((s%60))
    elif [ "$s" -ge 60 ]; then printf "%dm%ds" $((s/60)) $((s%60))
    else printf "%ds" "$s"
    fi
}

db_stat() {
    local db="$1"
    [ -f "$db" ] || { echo "DB 不存在"; return; }
    local out
    out=$(sqlite3 "$db" \
        "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM stock_daily;" \
        2>/dev/null) || { echo "读取失败"; return; }
    local rows symbols dmin dmax
    IFS='|' read -r rows symbols dmin dmax <<< "$out"
    printf "%s 行 / %s 个标的 / %s ~ %s" "$rows" "$symbols" "${dmin:-(空)}" "${dmax:-(空)}"
}

# ---------- 续跑状态管理 ----------
DATE_KEY=$(TZ='Asia/Shanghai' date '+%Y%m%d')
STATE_DIR="$REPO_ROOT/.cache"
STATE_FILE="$STATE_DIR/daily_run_${DATE_KEY}.state"

mkdir -p "$STATE_DIR"

step_done() {   # step_done step1 → true if already completed today
    grep -qx "$1" "$STATE_FILE" 2>/dev/null
}
mark_done() {   # mark_done step1
    echo "$1" >> "$STATE_FILE"
}

if [ "$RESET" = true ]; then
    rm -f "$STATE_FILE"
    log "🔄 已清除今日进度，将从头开始"
fi

# 显示当前进度
if [ -f "$STATE_FILE" ]; then
    DONE_STEPS=$(tr '\n' ' ' < "$STATE_FILE")
    log "📋 今日已完成步骤: ${DONE_STEPS}（续跑模式）"
else
    log "📋 今日无历史进度，全新运行"
fi

STEP_START=$SECONDS

# ---------- Python ----------
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
else
    PYTHON="${PYTHON:-python3}"
    command -v "$PYTHON" >/dev/null 2>&1 || die "找不到 python，请设置 PYTHON 或在项目根创建 .venv"
fi

# ---------- 启动摘要 ----------
log "=================================================="
log "  每日分析流程  $(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')"
log "=================================================="
log "Python  : $PYTHON ($("$PYTHON" --version 2>&1))"
log "工作目录: $REPO_ROOT"
log "状态文件: $STATE_FILE"
[ "$DRY_RUN" = true ]          && log "模式    : DRY-RUN（只打印，不执行）"
[ "$SKIP_FUNDAMENTAL" = true ] && log "模式    : 跳过财务快照更新"
[ "$SKIP_NEWS_SIGNALS" = true ] && log "模式    : 跳过新闻信号维护"

sep
log "关键配置检查"
info "TUSHARE_TOKEN        : $(masked "${TUSHARE_TOKEN:-}")"
info "SEQUOIA_DB           : ${SEQUOIA_CANDIDATE_DB_PATH:-Sequoia-X/data/sequoia_v2.db (默认)}"
info "FUNDAMENTAL_DB       : ${AGENT_FUNDAMENTAL_CANDIDATE_DB_PATH:-(未配置)}"
info "STOCK_LIST           : ${STOCK_LIST:-(未配置，使用默认)}"
info "CANDIDATE_MODE       : ${AGENT_CANDIDATE_DISCOVERY_MODE:-(未配置)}"
info "NEWS_SIGNAL_DAILY    : ${NEWS_SIGNAL_DAILY_ENABLED:-false}"
info "GRAPH_REPAIR_MODE    : ${NEWS_SIGNAL_GRAPH_REPAIR_MODE:-edges}"

SEQUOIA_DB="${SEQUOIA_CANDIDATE_DB_PATH:-Sequoia-X/data/sequoia_v2.db}"
STEP1_TARGET_DATE=$("$PYTHON" - <<'PY' 2>/dev/null
from scripts.update_sequoia_candidates import resolve_cn_resume_target_date

print(resolve_cn_resume_target_date().strftime("%Y%m%d"))
PY
) || STEP1_TARGET_DATE="$DATE_KEY"
if [ "$STEP1_TARGET_DATE" = "$DATE_KEY" ]; then
    info "SEQUOIA_TARGET_DATE  : ${STEP1_TARGET_DATE}"
else
    info "SEQUOIA_TARGET_DATE  : ${STEP1_TARGET_DATE}（最新已完成 A 股交易日）"
fi
STEP1_KEY="step1_stock_daily_with_index_${STEP1_TARGET_DATE}"

# ============================================================
# Step 1: 更新 stock_daily 日线
# ============================================================
sep
log ">>> [1/3] 更新 stock_daily 日线缓存 + 上证指数  (baostock)"

if step_done "$STEP1_KEY"; then
    ok "已完成（跳过）  —  使用上次结果: $(db_stat "$SEQUOIA_DB")"
else
    info "目标 DB : $SEQUOIA_DB"
    info "当前状态: $(db_stat "$SEQUOIA_DB")"
    T1=$SECONDS

    if [ "$DRY_RUN" = true ]; then
        warn "dry-run: 跳过（不写入状态，重跑仍会执行）"
    else
        if "$PYTHON" scripts/update_sequoia_candidates.py --trading-days 260; then
            ok "stock_daily 更新完成  (耗时: $(elapsed $((SECONDS - T1))))"
            info "更新后: $(db_stat "$SEQUOIA_DB")"
            mark_done "$STEP1_KEY"
        else
            warn "stock_daily 更新脚本失败，已有 $(db_stat "$SEQUOIA_DB") 仍可用"
            warn "step1 未标记完成，下次续跑会重试"
        fi
    fi
fi

# ============================================================
# Step 2: 更新财务快照
# ============================================================
sep
log ">>> [2/3] 更新财务候选快照  (tushare)"

if step_done "step2"; then
    ok "已完成（跳过）"
elif [ "$SKIP_FUNDAMENTAL" = true ]; then
    warn "已指定 --skip-fundamental，跳过"
    mark_done "step2"
elif [ -z "${TUSHARE_TOKEN:-}" ]; then
    warn "TUSHARE_TOKEN 未配置，跳过（不影响其他种子来源）"
    mark_done "step2"
else
    FUND_DB="${AGENT_FUNDAMENTAL_CANDIDATE_DB_PATH:-}"
    if [ -n "$FUND_DB" ] && [ -f "$FUND_DB" ]; then
        FUND_ROWS=$(sqlite3 "$FUND_DB" \
            "SELECT COUNT(DISTINCT ts_code) FROM fundamental_candidate_snapshot;" 2>/dev/null || echo "?")
        info "当前快照: $FUND_ROWS 只股票"
    fi
    T2=$SECONDS

    if [ "$DRY_RUN" = true ]; then
        warn "dry-run: 跳过（不写入状态，重跑仍会执行）"
    else
        if "$PYTHON" scripts/update_fundamental_candidates.py --resume; then
            ok "财务快照更新完成  (耗时: $(elapsed $((SECONDS - T2))))"
            mark_done "step2"
        else
            warn "财务快照更新失败，继续执行主分析"
            warn "step2 未标记完成，下次续跑会重试"
        fi
    fi
fi

# ============================================================
# Step 3: 新闻信号卡片、事件与 Graphiti 投影维护
# ============================================================
sep
log ">>> [3/3] 维护新闻信号卡片、事件、outcome 与 Graphiti 投影"

NEWS_TARGET_DATE="${STEP1_TARGET_DATE:0:4}-${STEP1_TARGET_DATE:4:2}-${STEP1_TARGET_DATE:6:2}"
STEP3_KEY="step3_news_signals_${STEP1_TARGET_DATE}"
NEWS_SIGNAL_DAILY_VALUE=$(printf '%s' "${NEWS_SIGNAL_DAILY_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')

if step_done "$STEP3_KEY"; then
    ok "已完成（跳过）"
elif [ "$SKIP_NEWS_SIGNALS" = true ]; then
    warn "已指定 --skip-news-signals，跳过"
    mark_done "$STEP3_KEY"
elif [ "$NEWS_SIGNAL_DAILY_VALUE" != "true" ]; then
    warn "NEWS_SIGNAL_DAILY_ENABLED 未启用，跳过（opt-in，不写入完成标记）"
elif [ "$DRY_RUN" = true ]; then
    warn "dry-run: 将执行 maintain_news_signals.py --target-date $NEWS_TARGET_DATE"
else
    T3=$SECONDS
    if "$PYTHON" scripts/maintain_news_signals.py --target-date "$NEWS_TARGET_DATE"; then
        ok "新闻信号维护完成  (耗时: $(elapsed $((SECONDS - T3))))"
        mark_done "$STEP3_KEY"
    else
        warn "新闻信号维护失败，关系型旧数据仍可用"
        warn "step3 未标记完成，下次续跑会重试"
    fi
fi

sep
log "全部步骤完成 ✅  总耗时: $(elapsed $((SECONDS - STEP_START)))"
log "状态文件: $STATE_FILE"
log "=================================================="
