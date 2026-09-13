"""accept_exec.py — accept 节对 supervisor。

形态（2026-09-12 定案；同日归属修正）：
- 每节验收标准 = 一对文件：exp-accept/acceptance/N-slug.json（数据面，
  须含按顺序排列的命令 + 成功判定标准，其余字段自由）+
  N-slug.check.py（消费者脚本）。
- 消费脚本契约：python check.py <json路径>；exit 0 = 过 / 非零 = 不过；
  stdout = 证据叙述（人审与修环消费）。调用环境：cwd=目标树根；env 注入
  PORTER_TARGET_OS_ROOT（绝对）/ PORTER_DRIVER_HOME（相对）/
  PORTER_EVIDENCE_DIR（脚本自产工件归档目录）；PYTHONPATH 含工具根。
- 工具零格式假设：不解析 JSON 语义、不硬编码节格式——只发现、调用、
  归档。格式纪律写在 skill；诚实性由人审把关。
- 节归属（三 tier）：t1 = §1-§4（自 mono 执行事实提取，目标树零变更）；
  inject = §5/§6（agent 探索制定）；
  e2e = §7（agent 设计）。frozen 机器（§2/§3 自 runner.json 机械生成）
  已随归属修正退场（§1-§4 单一属主 = Tier1）；最小格式样例内嵌于
  EXP-accept-inject skill。
- 兜底超时 INVOKE_FALLBACK_SEC：单次 invoke 墙钟上限，防脚本挂死；
  超时杀进程组（连带 QEMU/docker 子进程——dmzero-t3 #21 孤儿坑）。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import log as _log

TOOL_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_DIRNAME = "acceptance"
INVOKE_FALLBACK_SEC = 7200      # 兜底防挂死（命令级超时由脚本自管）

SECTION_TIER = {"t1": (1, 2, 3, 4), "inject": (5, 6), "e2e": (7,)}
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
