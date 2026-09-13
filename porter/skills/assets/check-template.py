#!/usr/bin/env python3
"""check-template v2 —— 验收节消费脚本参考实现（exp-accept 样例）。

用法：python <N>-<slug>.check.py <N>-<slug>.json
调用环境（编排器 invoke 注入）：cwd=目标树根；env 携带
`PORTER_TARGET_OS_ROOT`（绝对）/ `PORTER_DRIVER_HOME`（相对）/
`PORTER_EVIDENCE_DIR`（证据目录；缺席则回退 <json 同目录>/.evidence/）。

私有 JSON 约定（节作者可自定格式，本约定仅供参考）：
  commands: [{"cmd": "...", "timeout_sec": 900,
              "snapshots": ["qemu.log", "qemu-serial.log"]}, ...]  按序执行
  expect:   {"rc": 0,                          末条命令退出码
             "log_contains": ["正则", ...],    全部须命中（合并输出）
             "log_not_contains": ["正则", ...], 全部须缺席
             "min_matches": [{"expr": "正则", "count": 8}]}  命中计数下限
exit 0 = 通过；非零 = 不过。stdout = 证据叙述。

v2 相对 v1（spinor-mini/final 两轮试跑所用）的四项改进——证据可达性
硬要求（红绿对称保留；错误信息必须完整可考）：
1. 完整证据落盘：每条命令的完整输出**无条件**写
   `<EVIDENCE_DIR>/cmd-<i>.log`——绿轮的历史同样是证据；
2. 共享文件快照：命令条目可选 `snapshots`（树内相对路径列表），
   命令结束后拷入证据目录（命名 `cmd-<i>.<basename>`）；缺席打
   note 不失败。boot 类节用它快照 qemu.log / qemu-serial.log——
   前者逐次覆写、后者承载 panic 文本，不快照则事后无可考现场；
3. 有标记的瘦回显：命令输出超 1000 字符时回显 头 400 +
   `…[truncated N chars, full evidence: <路径>]…` + 尾 600——
   旧模板无标记截断且尾部系统性被 stderr 占据（stdout/stderr
   拼接顺序），关键结论行常被挤掉；
4. 占位符安全替换：仅替换独立 `{VAR}`（`(?<!\$)` 前瞻），
   `${VAR}` 留给 shell 展开——旧模板子串替换会把 shell 形式
   打碎成 `$...}`（docker 参数非法）。

判定语义与 v1 一致：expect 合取跑在进程内捕获的**完整**合并文本上，
不受回显截断影响。结尾打印 `evidence: <文件清单>` 行，供修环者从
invoke.log 直接发现证据文件。
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ECHO_FULL_LIMIT = 1000     # 回显全量上限（超过则头尾+标记）
_ECHO_HEAD = 400
_ECHO_TAIL = 600


def _evidence_dir(json_path: Path) -> Path:
    d = os.environ.get("PORTER_EVIDENCE_DIR")
    p = Path(d) if d else json_path.resolve().parent / ".evidence"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _subst(cmd: str) -> str:
    """独立 {VAR} 占位符替换（${VAR} 不动，留给 shell）。"""
    for k in ("PORTER_TARGET_OS_ROOT", "PORTER_DRIVER_HOME"):
        v = os.environ.get(k, "")
        cmd = re.sub(r"(?<!\$)\{" + k + r"\}", lambda _m: v, cmd)
    return cmd


def _echo(out: str, ev_path: Path) -> None:
    if not out:
        return
    if len(out) <= _ECHO_FULL_LIMIT:
        print(out)
        return
    n_cut = len(out) - _ECHO_FULL_LIMIT
    print(out[:_ECHO_HEAD])
    print(f"…[truncated {n_cut} chars, full evidence: {ev_path}]…")
    print(out[-_ECHO_TAIL:])


def main() -> int:
    json_path = Path(sys.argv[1])
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    env = dict(os.environ)
    root = env.get("PORTER_TARGET_OS_ROOT", os.getcwd())
    ev = _evidence_dir(json_path)
    out_all, rc_last, ev_files = [], 0, []
    cmds = doc.get("commands") or []
    if not cmds:
        print("not-applicable：无命令（依据见 JSON 叙述）")
        return 0
    for i, c in enumerate(cmds, 1):
        cmd = _subst(c["cmd"] if isinstance(c, dict) else str(c))
        tmo = int(c.get("timeout_sec", 3600)) if isinstance(c, dict) else 3600
        snaps = (c.get("snapshots") or []) if isinstance(c, dict) else []
        print(f"[cmd {i}/{len(cmds)}] {cmd[:160]}")
        try:
            p = subprocess.run(["bash", "-c", cmd], env=env, cwd=root,
                               capture_output=True, text=True, timeout=tmo)
            out = (p.stdout or "") + (p.stderr or "")
            rc_last = p.returncode
        except subprocess.TimeoutExpired:
            out = f"TIMEOUT after {tmo}s"
            rc_last = 124
        out_all.append(out)
        ce = ev / f"cmd-{i}.log"
        ce.write_text(out, encoding="utf-8", errors="replace")
        ev_files.append(ce.name)
        for s in snaps:
            sp = Path(root) / s
            if sp.exists():
                dst = ev / f"cmd-{i}.{Path(s).name}"
                shutil.copy2(sp, dst)
                ev_files.append(dst.name)
            else:
                print(f"note: snapshot 缺席（未拷贝）：{s}")
        _echo(out, ce)
    text = "\n".join(out_all)
    exp = doc.get("expect") or {}
    checks = []
    if "rc" in exp:
        checks.append((f"rc={exp['rc']}", rc_last == int(exp["rc"])))
    for pat in exp.get("log_contains") or []:
        checks.append((f"contains:{pat[:40]}",
                       re.search(pat, text) is not None))
    for pat in exp.get("log_not_contains") or []:
        checks.append((f"not_contains:{pat[:40]}",
                       re.search(pat, text) is None))
    for mm in exp.get("min_matches") or []:
        n = len(re.findall(mm["expr"], text, re.M))
        checks.append((f"matches({mm['expr'][:30]}…)={n}>={mm['count']}",
                       n >= int(mm["count"])))
    ok = bool(checks) and all(v for _, v in checks)
    for name, v in checks:
        print(f"{'PASS' if v else 'FAIL'}  {name}")
    if ev_files:
        print("evidence: " + ", ".join(ev_files))
    print("VERDICT:", "pass" if ok else "fail")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
