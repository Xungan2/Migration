"""accept_exec.py — accept 节对 supervisor 与 frozen 节生成器。

形态（2026-09-12 定案，本轮简化定稿）：
- 每节验收标准 = 一对文件：exp-accept/acceptance/N-slug.json（数据面，
  须含按顺序排列的命令 + 成功判定标准，其余字段自由）+
  N-slug.check.py（消费者脚本）。
- 消费脚本契约：python check.py <json路径>；exit 0 = 过 / 非零 = 不过；
  stdout = 证据叙述（人审与修环消费）。调用环境：cwd=目标树根；env 注入
  PORTER_TARGET_OS_ROOT（绝对）/ PORTER_DRIVER_HOME（相对）/
  PORTER_EVIDENCE_DIR（脚本自产工件归档目录）；PYTHONPATH 含工具根。
- 工具零格式假设：不解析 JSON 语义、不硬编码节格式——只发现、调用、
  归档。格式纪律写在 skill；诚实性由人审把关。
- §2/§3 frozen：从 runner.json 机械生成（通用 check 模板，兼作 agent
  可参考的样例形态）；幂等（runner_sha16 不变则不重生成）。
- 兜底超时 INVOKE_FALLBACK_SEC：单次 invoke 墙钟上限，防脚本挂死；
  超时杀进程组（连带 QEMU/docker 子进程——dmzero-t3 #21 孤儿坑）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import log as _log

TOOL_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_DIRNAME = "acceptance"
INVOKE_FALLBACK_SEC = 7200      # 兜底防挂死（命令级超时由脚本自管）

SECTION_TIER = {"inject": (1, 4, 5, 6), "e2e": (7,)}
ALL_SECTIONS = tuple(range(1, 8))


def acceptance_dir(ws: Path) -> Path:
    return Path(ws) / "exp-accept" / ACCEPTANCE_DIRNAME


def _sha16_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _sha16_file(path: Path) -> str:
    try:
        return _sha16_bytes(Path(path).read_bytes())
    except OSError:
        return ""


def _esc(s) -> str:
    """冻结 pattern（runner.json 的子串语义）→ 正则字面量。"""
    return re.escape(str(s))


# ---------- 通用 check 模板（frozen 生成物；agent 可参考的样例形态） ----------

CHECK_TEMPLATE = '''#!/usr/bin/env python3
"""通用验收消费脚本（工具 frozen 生成物；agent 自由节可参考此形态）。

用法：python <N>-<slug>.check.py <N>-<slug>.json
本模板私有 JSON 约定（自由节可自定格式）：
  commands: [{"cmd": "...", "timeout_sec": 900}, ...]     按序执行
  expect:   {"rc": 0,                                     末条命令退出码
             "log_contains": ["正则", ...],               全部须命中（合并输出）
             "log_not_contains": ["正则", ...],           全部须缺席
             "min_matches": [{"expr": "正则", "count": 8}]}  命中计数下限
exit 0 = 通过；非零 = 不过。stdout = 证据叙述。
"""
import json
import os
import re
import subprocess
import sys


def main() -> int:
    doc = json.loads(open(sys.argv[1], encoding="utf-8").read())
    env = dict(os.environ)
    root = env.get("PORTER_TARGET_OS_ROOT", os.getcwd())
    out_all, rc_last = [], 0
    cmds = doc.get("commands") or []
    if not cmds:
        print("not-applicable：无命令（依据见 JSON 叙述）")
        return 0
    for i, c in enumerate(cmds):
        cmd = c["cmd"] if isinstance(c, dict) else str(c)
        for k in ("PORTER_TARGET_OS_ROOT", "PORTER_DRIVER_HOME"):
            cmd = cmd.replace("{" + k + "}", env.get(k, ""))
        tmo = int(c.get("timeout_sec", 3600)) if isinstance(c, dict) else 3600
        print(f"[cmd {i + 1}/{len(cmds)}] {cmd[:160]}")
        try:
            p = subprocess.run(["bash", "-c", cmd], env=env, cwd=root,
                               capture_output=True, text=True, timeout=tmo)
            out = (p.stdout or "") + (p.stderr or "")
            rc_last = p.returncode
        except subprocess.TimeoutExpired:
            out = f"TIMEOUT after {tmo}s"
            rc_last = 124
        out_all.append(out)
        if out:
            print(out[-2000:])
    text = "\\n".join(out_all)
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
    print("VERDICT:", "pass" if ok else "fail")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------- 发现与结构校验 ----------

def pair_json(ws: Path, num: int) -> Path | None:
    """节号 → 节 JSON 路径（不唯一/不存在 → None）。"""
    ms = sorted(acceptance_dir(ws).glob(f"{num}-*.json"))
    if len(ms) != 1:
        return None
    return ms[0]


def structural_problems(ws: Path, nums) -> list[str]:
    """节文件对的结构校验（不解析语义）：在场、唯一、JSON 对象、脚本非空。"""
    d = acceptance_dir(ws)
    probs: list[str] = []
    if not d.exists():
        return [f"acceptance 目录不存在：{d}"]
    for n in nums:
        ms = sorted(d.glob(f"{n}-*.json"))
        if not ms:
            probs.append(f"§{n} 缺节文件（{d}/{n}-<slug>.json）")
            continue
        if len(ms) > 1:
            probs.append(f"§{n} 节文件不唯一：{[m.name for m in ms]}")
            continue
        j = ms[0]
        c = j.with_name(j.stem + ".check.py")
        if not c.exists():
            probs.append(f"§{n} 缺消费脚本：{c.name}（JSON 与脚本成对交付）")
            continue
        try:
            doc = json.loads(j.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("非 JSON 对象")
        except (OSError, ValueError, json.JSONDecodeError) as ex:
            probs.append(f"§{n} 节 JSON 不可解析：{j.name}（{ex}）")
            continue
        try:
            if not c.read_text(encoding="utf-8").strip():
                probs.append(f"§{n} 消费脚本为空文件：{c.name}")
        except OSError:
            probs.append(f"§{n} 消费脚本不可读：{c.name}")
    return probs


def touched_paths(ws: Path, nums) -> list[str]:
    """各节 JSON 顶层 "paths" 列表的并集（范围守卫机器白名单；缺席即无）。"""
    out: list[str] = []
    for n in nums:
        j = pair_json(ws, int(n))
        if j is None:
            continue
        try:
            doc = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        v = doc.get("paths")
        if isinstance(v, list):
            out += [str(x) for x in v if isinstance(x, str) and x.strip()]
    return out


# ---------- invoke（唯一执行通道） ----------

def invoke(ws: Path, target_os: Path, driver_home_rel: str,
           json_path: Path, tag: str = "") -> dict:
    """调用节消费脚本：python <check.py> <json>。归档 stdout（+stderr 合并）。

    返回 {rc, timed_out, status, output, duration_sec}。
    兜底超时 → 杀进程组，rc=124。
    """
    ws, target_os = Path(ws), Path(target_os)
    slug = json_path.stem
    check = json_path.with_name(slug + ".check.py")
    log_dir = ws / "exp-accept" / "logs" / (f"{slug}.{tag}" if tag else slug)
    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PORTER_TARGET_OS_ROOT"] = str(target_os.resolve())
    env["PORTER_DRIVER_HOME"] = str(driver_home_rel)
    env["PORTER_EVIDENCE_DIR"] = str(log_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(TOOL_ROOT)] + ([env["PYTHONPATH"]]
                            if env.get("PYTHONPATH") else []))
    out_log = log_dir / "invoke.log"
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(check), str(json_path)],
            cwd=str(target_os), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
    except OSError as ex:
        return {"rc": 127, "timed_out": False, "status": "fail",
                "output": "", "error": repr(ex), "duration_sec": 0.0}
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=INVOKE_FALLBACK_SEC)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        rc = 124
    out = out or ""
    out_log.write_text(out, encoding="utf-8", errors="replace")
    return {"rc": rc, "timed_out": timed_out,
            "status": "pass" if (rc == 0 and not timed_out) else "fail",
            "output": str(out_log), "duration_sec": round(time.time() - t0, 1)}


def _tail_of(log_path: str, lines: int = 12) -> str:
    try:
        text = Path(log_path).read_text(encoding="utf-8",
                                        errors="replace").splitlines()
    except OSError:
        return "(输出不可读)"
    return "\n".join(text[-lines:])


def invoke_sections(ws: Path, target_os: Path, driver_home_rel: str,
                    nums, tag: str) -> tuple[dict, list[str]]:
    """逐节真实 invoke。返回 (records, problems)；problems 空 = 全绿。"""
    records: dict[int, dict] = {}
    problems: list[str] = []
    for n in nums:
        j = pair_json(ws, int(n))
        if j is None:
            problems.append(f"§{n} 缺节文件对")
            continue
        rec = invoke(ws, target_os, driver_home_rel, j, tag)
        records[int(n)] = rec
        if rec["status"] != "pass":
            why = "兜底超时（脚本挂死被杀）" if rec["timed_out"] else \
                f"exit={rec['rc']}"
            problems.append(f"§{n} 判定未过（{why}）——输出尾部：\n"
                            + _tail_of(rec["output"]))
    return records, problems


def invoke_ladder(ws: Path, target_os: Path, driver_home_rel: str,
                  tag: str) -> tuple[dict, list[str]]:
    """执行相位阶梯：§1→§7 按序 invoke，**首个非 pass 即停**。

    七节天然是成本阶梯（编译→单测/构建→启动→端到端），前置红时后续
    节的启动费不烧。未达节显式记 {"status": "skipped", "reason": …}。
    返回 (records, problems)；problems 空 = 全绿。
    """
    records: dict[int, dict] = {}
    problems: list[str] = []
    stopped_at: int | None = None
    for n in ALL_SECTIONS:
        j = pair_json(ws, n)
        if j is None:
            records[n] = {"status": "fail", "rc": None, "timed_out": False,
                          "output": "", "duration_sec": 0.0,
                          "error": "缺节文件对"}
            problems.append(f"§{n} 缺节文件对")
            stopped_at = n
            break
        _log.console_line(f"[porter] accept: 阶梯 §{n}（{j.stem}）执行中…")
        rec = invoke(ws, target_os, driver_home_rel, j, tag)
        records[n] = rec
        if rec["status"] != "pass":
            why = "兜底超时（脚本挂死被杀）" if rec["timed_out"] else \
                f"exit={rec['rc']}"
            problems.append(f"§{n} 首个未过（{why}）——输出尾部：\n"
                            + _tail_of(rec["output"]))
            stopped_at = n
            break
    if stopped_at is not None:
        for m in ALL_SECTIONS:
            if m > stopped_at and m not in records:
                records[m] = {"status": "skipped",
                              "reason": f"前置 §{stopped_at} 失败"}
    return records, problems


def fingerprint_problems(ws: Path, index: dict) -> list[str]:
    """执行期标准冻结：各节文件对当前 sha16 == 七节索引登记值。"""
    probs: list[str] = []
    for s in (index or {}).get("sections") or []:
        n = s.get("section")
        if s.get("status") != "bound":
            probs.append(f"§{n} 索引未绑定（先完成标准制定）")
            continue
        j = Path(ws) / str(s.get("json") or "")
        c = Path(ws) / str(s.get("check") or "")
        if _sha16_file(j) != s.get("sha16"):
            probs.append(f"§{n} 标准 JSON 指纹不符：{s.get('json')}")
        if _sha16_file(c) != s.get("check_sha16"):
            probs.append(f"§{n} 消费脚本指纹不符：{s.get('check')}")
    return probs


# ---------- frozen 生成（§2/§3 自 runner.json 机械绑定，幂等） ----------

def _spec_unit_test(runner: dict) -> dict | None:
    ut = runner.get("unit_test") or {}
    cmd = ut.get("driver_scope_cmd") or ut.get("cmd")
    if not cmd:
        return None
    expect: dict = {"rc": 0}
    if ut.get("success_pattern"):
        expect["log_contains"] = [_esc(ut["success_pattern"])]
    if ut.get("fail_pattern"):
        expect["log_not_contains"] = [_esc(ut["fail_pattern"])]
    return {"num": 2, "slug": "unit-test", "title": "单元测试",
            "source": ("runner.unit_test.driver_scope_cmd"
                       if ut.get("driver_scope_cmd") else "runner.unit_test.cmd"),
            "commands": [{"cmd": cmd,
                          "timeout_sec": int(ut.get("timeout_sec") or 3600)}],
            "expect": expect}


def _spec_image_build(runner: dict) -> dict | None:
    b = runner.get("build") or {}
    if not b.get("cmd"):
        return None
    expect: dict = {"rc": 0}
    if b.get("success_pattern"):
        expect["log_contains"] = [_esc(b["success_pattern"])]
    return {"num": 3, "slug": "image-build", "title": "镜像级编译",
            "source": "runner.build.cmd",
            "commands": [{"cmd": b["cmd"],
                          "timeout_sec": int(b.get("timeout_full_sec")
                                             or 3000)}],
            "expect": expect}


def frozen_generate(ws: Path, runner: dict) -> list[str]:
    """生成/刷新 §2/§3 节对。幂等：在场且 runner_sha16 未变 → 跳过。
    返回日志消息列表（空 = 无事发生）。"""
    ws = Path(ws)
    if not runner:
        return ["runner.json 缺失——§2/§3 frozen 节未生成（后续节仍可进行）"]
    runner_sha = _sha16_file(ws / "runner.json")
    msgs: list[str] = []
    d = acceptance_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    for spec in (_spec_unit_test(runner), _spec_image_build(runner)):
        if spec is None:
            continue                      # runner 键缺失：不生成该节
        num, slug = spec["num"], spec["slug"]
        jpath = d / f"{num}-{slug}.json"
        cpath = d / f"{num}-{slug}.check.py"
        doc = {"section": num, "title": spec["title"], "origin": "frozen",
               "source": spec["source"],
               "commands": spec["commands"], "expect": spec["expect"],
               "paths": [],
               "notes": ("工具自 runner.json 机械绑定；重新生成条件 = "
                         "runner.json 指纹变化。语义：按序执行 commands，"
                         "expect 合取判定（rc/log_contains/log_not_contains"
                         "/min_matches 词汇见 check 脚本头注）。"),
               "runner_sha16": runner_sha}
        if jpath.exists() and cpath.exists():
            try:
                old = json.loads(jpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old = None
            if isinstance(old, dict) and \
                    old.get("runner_sha16") == runner_sha:
                continue                      # 幂等：指纹未变不重生成
            _log.console_line(f"[porter] accept: runner.json 已变——"
                              f"刷新 frozen §{num}")
        jpath.write_text(json.dumps(doc, ensure_ascii=False, indent=2)
                         + "\n", encoding="utf-8")
        cpath.write_text(CHECK_TEMPLATE, encoding="utf-8")
        msgs.append(f"§{num} frozen 节对已生成：{jpath.name} + {cpath.name}")
    return msgs
