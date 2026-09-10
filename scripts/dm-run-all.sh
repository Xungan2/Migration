#!/usr/bin/env bash
# Linux device-mapper -> Asterinas: unified preparation entry.
set -euo pipefail

porter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$porter_root"
porter_ws="${PORTER_OUTPUT_DIR:-$porter_root/migrations/dm-unified}"
porter_intent="${PORTER_INTENT_FILE:-$porter_root/dm-intent.md}"

if ! command -v opencode >/dev/null 2>&1 && [[ -x "$HOME/.opencode/bin/opencode" ]]; then
  export PATH="$HOME/.opencode/bin:$PATH"
fi

# An explicit override updates goals.md; ordinary resume keeps workspace edits.
porter_intent_args=()
if [[ -n "${PORTER_INTENT_FILE:-}" || ! -f "$porter_ws/project.json" ]]; then
  porter_intent_args=(--intent-file "$porter_intent")
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
    printf '请查看 %s/prepare/state.json 和 prepare/handoffs/，按要求填写 %s/answers.md。\n' \
      "$porter_ws" "$porter_ws" >&2
  else
    printf '请查看 %s 中的阶段日志，处理失败或缺失的前置条件。\n' \
      "$porter_ws" >&2
  fi
  printf '处理后重新运行 %s/scripts/dm-run-all.sh；已有产物会按 porter 的断点机制复用。\n' \
    "$porter_root" >&2
  exit "$rc"
}

# Unified skeleton, planning and knowledge.
run_phase '统一准备：骨架构建/载入与迁移规划' prepare \
  --linux-driver "$porter_root/linux-5.10/drivers/md" \
  --target-os "$porter_root/asterinas" \
  "${porter_intent_args[@]}"

printf '\n[dm-run-all] 统一准备完成；后续迁移不在本入口执行。知识：%s/knowledgebase/\n' "$porter_ws"
