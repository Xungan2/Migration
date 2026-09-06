#!/usr/bin/env bash
# Linux device-mapper -> Asterinas: P0 -> P1 -> P2 -> P3-P5 -> P6 -> P7.
set -euo pipefail

porter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$porter_root"
porter_ws="${PORTER_OUTPUT_DIR:-$porter_root/migrations/dm-port}"
porter_intent="${PORTER_INTENT_FILE:-$porter_root/dm-intent.md}"

if ! command -v opencode >/dev/null 2>&1 && [[ -x "$HOME/.opencode/bin/opencode" ]]; then
  export PATH="$HOME/.opencode/bin:$PATH"
fi

# Keep any scope changes made by the user when resuming.
if [[ ! -e "$porter_intent" && -n "${PORTER_INTENT_FILE:-}" ]]; then
  printf '[dm-run-all] 指定的意图文件不存在：%s\n' "$porter_intent" >&2
  exit 2
fi
if [[ ! -e "$porter_intent" ]]; then
  cat > "$porter_intent" <<'EOF'
将 Linux device-mapper（DM）迁移为 Asterinas 原生安全 Rust 实现。
迁移范围包含 DM 核心、各 targets 及必需依赖。
drivers/md 中无关的 MD RAID、bcache 不作为迁移主体。
请明确列出范围、依赖和无法支持的功能，供人工审阅。
DM 是软件块设备框架，验证应基于目标 OS 支持的底层块设备。
EOF
fi

run_phase() {
  local phase="$1"
  local rc
  shift

  printf '\n[dm-run-all] %s\n' "$phase"
  if python3 "$porter_root/porter/main.py" "$@" --output-dir "$porter_ws"; then
    return 0
  else
    rc=$?
  fi

  printf '\n[dm-run-all] %s 已停止，退出码：%s\n' "$phase" "$rc" >&2
  if [[ "$rc" -eq 3 ]]; then
    printf '请查看 %s/human_questions.md，按要求填写 %s/answers.md。\n' \
      "$porter_ws" "$porter_ws" >&2
  else
    printf '请查看 %s 中的阶段日志，处理失败或缺失的前置条件。\n' \
      "$porter_ws" >&2
  fi
  printf '处理后重新运行 %s/scripts/dm-run-all.sh；已有产物会按 porter 的断点机制复用。\n' \
    "$porter_root" >&2
  exit "$rc"
}

# P0: environment checks.
run_phase 'P0：环境检查' p0 \
  --linux-driver "$porter_root/linux-5.10/drivers/md" \
  --target-os "$porter_root/asterinas" \
  --category block \
  --intent-file "$porter_intent" \
  --materials "$porter_intent" \
  --materials "$porter_root/examples/asterinas-materials/notes-build.md" \
  --materials "$porter_root/examples/asterinas-materials/ci-snippet.md" \
  --kb use asterinas

# P1 includes agent decisions for references left by functional pruning.
run_phase 'P1：拆分策略、模块划分与裁剪处置' p1

# P2: API mapping, scaffold, and probes.
run_phase 'P2：映射、骨架与探针' p2

# P3-P5: analyze, migrate, and verify every module.
run_phase 'P3-P5：逐模块迁移与验收' loop

# P6: draft, review, and execute system acceptance criteria.
run_phase 'P6：生成 L4 验收草案' p6 --draft-l4
run_phase 'P6：L4 验收标准定稿' p6 --finalize-l4
run_phase 'P6：执行系统验收' p6 --execute --l4

# P7: final report.
run_phase 'P7：最终报告' p7
printf '\n[dm-run-all] 完成。报告：%s/P7/reports/final_report.md\n' "$porter_ws"
