"""Resolve driver references left by functional pruning before P2.

The scanner supplies candidates, not reachability claims. The agent supplies
evidence and a disposition for every candidate in one task. Potential restore
dependencies are supplied up front. A machine-only check accepts the complete
proposal or stops; rewrites travel to P3 criteria and P4 production.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .. import log as _log
from ..common import agent, scope
from ..common.symbol import _clean_source, scan_file, scan_module_dir
from . import fragments, index, resolve

REPORT = "P1/reports/pruning.json"
CONTEXT = "P1/reports/pruning_context.json"


def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _fingerprint(ws: Path, driver_root: Path) -> str:
    h = hashlib.sha256(scope.input_fingerprint(ws, driver_root).encode())
    # Include excluded files: references to a definition outside scope are still
    # candidates, e.g. a retained initialization array naming another plugin.
    for p in scope.driver_files(driver_root):
        h.update(p.relative_to(driver_root).as_posix().encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    for p in sorted((ws / "P1" / "modules").rglob("*")):
        if p.is_file() and p.suffix in (".c", ".h", ".json"):
            h.update(p.relative_to(ws).as_posix().encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    h.update((ws / "P1" / "reports" / "P1D_plan.json").read_bytes())
    return h.hexdigest()


def _validate_source_stamp(ws: Path, driver_root: Path) -> None:
    stamp = _read(ws / "P1/reports/P1D_inputs.json")
    if stamp is None and scope.load_scope(ws, driver_root) is None:
        return  # Legacy unscoped plans retain their original flow.
    if (not isinstance(stamp, dict)
            or stamp.get("fingerprint") != scope.input_fingerprint(ws, driver_root)):
        raise scope.ScopeError("拆分计划缺少有效源码指纹或输入已变化；"
                               "请重新生成 P1，或通过 p1-import 校验导入")


def _definitions(driver_root: Path, selected: set[str] | None) -> dict:
    definitions: dict[str, list[dict]] = {}
    for p in scope.driver_files(driver_root):
        rel = p.relative_to(driver_root).as_posix()
        defs, _refs, _protos = scan_file(p)
        for symbol, line in defs.items():
            definitions.setdefault(symbol, []).append({
                "file": rel, "line": line,
                "in_scope": selected is None or rel in selected})
    return definitions


def _identifiers(line: str):
    for match in re.finditer(r"\b[A-Za-z_]\w*\b", line):
        if not line[:match.start()].rstrip().endswith((".", "->")):
            yield match.group()


def discover(ws: Path, driver_root: Path) -> list[dict]:
    """Find unresolved driver definitions, including outside the file whitelist."""
    definitions = _definitions(driver_root, scope.load_scope(ws, driver_root))
    modules = sorted(p.parent for p in (ws / "P1" / "modules").glob("*/module.json"))
    scans = {m.name: scan_module_dir(m) for m in modules}
    retained = {s for defs, _refs in scans.values() for s in defs}
    candidates = []
    for m in modules:
        _defs, refs = scans[m.name]
        # The dependency scanner deliberately omits struct fields to avoid
        # name noise. Their explicit type tags can still reference cut types.
        for p in scope.driver_files(m):
            clean = _clean_source(p.read_text(encoding="utf-8"))
            refs |= set(re.findall(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)", clean))
        unresolved = (refs & set(definitions)) - retained
        uses = {s: [] for s in unresolved}
        for p in scope.driver_files(m):
            lines = _clean_source(p.read_text(encoding="utf-8")).splitlines()
            for line_no, line in enumerate(lines, 1):
                for s in _identifiers(line):
                    if s in unresolved:
                        uses[s].append(f"{p.relative_to(ws).as_posix()}:{line_no}")
        for s in sorted(unresolved):
            if uses[s]:
                candidates.append({
                    "id": f"{m.name}:{s}", "module": m.name, "symbol": s,
                    "definitions": definitions[s],
                    "uses": list(dict.fromkeys(uses[s]))})
    return candidates


def _dependency_context(ws: Path, driver_root: Path, candidates: list[dict]) -> dict:
    """Conservative source dependencies of all potentially restored definitions.

    This is a read-only worklist, not an agent retry loop. Excluded definitions
    are terminal references: their source cannot be restored into this scope.
    """
    selected = scope.load_scope(ws, driver_root)
    definitions = _definitions(driver_root, selected)
    entries = index.build_index(driver_root, selected)
    lines = {src: _clean_source((driver_root / src).read_text(
        encoding="utf-8")).splitlines() for src in entries}
    modules = sorted(p.parent for p in (ws / "P1/modules").glob("*/module.json"))
    retained = {s for m in modules for s in scan_module_dir(m)[0]}
    pending = sorted({c["symbol"] for c in candidates}, reverse=True)
    dependencies = {}
    while pending:
        symbol = pending.pop()
        if symbol in dependencies:
            continue
        references: dict[str, set[str]] = {}
        spans = []
        for definition in definitions[symbol]:
            src = definition["file"]
            own = [e for e in entries.get(src, []) if e.symbol == symbol
                   and e.kind not in ("reg", "fwd", "chunk")]
            if not own:
                continue
            own += [e for e in entries[src] if e.kind == "reg" and symbol in e.refs]
            for e in own:
                spans.append(f"{src}:{e.start}-{e.end}")
                for line_no in range(e.start, e.end + 1):
                    for ref in _identifiers(lines[src][line_no - 1]):
                        if ref in definitions and ref not in retained and ref != symbol:
                            references.setdefault(ref, set()).add(f"{src}:{line_no}")
        dependencies[symbol] = {
            "definitions": definitions[symbol], "restore_spans": spans,
            "references": {s: sorted(uses) for s, uses in sorted(references.items())}}
        pending.extend(sorted(set(references) - dependencies.keys(), reverse=True))
    return {"candidates": candidates, "modules": [m.name for m in modules],
            "dependencies": dict(sorted(dependencies.items()))}


def _proposal_candidates(rows, context: dict) -> list[dict]:
    """Allow decisions for dependencies introduced by this same proposal."""
    candidates = {c["id"]: c for c in context["candidates"]}
    for row in rows if isinstance(rows, list) else []:
        key = row.get("id") if isinstance(row, dict) else None
        if not isinstance(key, str) or key in candidates:
            continue
        module, sep, symbol = key.partition(":")
        node = context["dependencies"].get(symbol)
        if sep and module in context["modules"] and node is not None:
            candidates[key] = {"id": key, "module": module, "symbol": symbol,
                               "definitions": node["definitions"], "uses": []}
    return list(candidates.values())


def _evidence_valid(value: str, ws: Path, driver_root: Path) -> bool:
    match = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", value)
    if not match:
        return False
    name, start, end = match.groups()
    base = ws if name.startswith("P1/") else driver_root
    p = (base / name).resolve()
    if not p.is_relative_to(base.resolve()) or not p.is_file():
        return False
    total = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return 1 <= int(start) <= int(end or start) <= total


def _validate(rows, candidates: list[dict], ws: Path, driver_root: Path) -> list[str]:
    if not isinstance(rows, list):
        return ["decisions 须为列表"]
    by_id = {c["id"]: c for c in candidates}
    seen: set[str] = set()
    errors = []
    modules = {p.parent.name for p in (ws / "P1" / "modules").glob("*/module.json")}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or row["id"] not in by_id):
            errors.append("decision 含未知 id 或不是对象")
            continue
        key = row["id"]
        if key in seen:
            errors.append(f"{key}: 重复决定")
        seen.add(key)
        action = row.get("action")
        if action not in ("restore", "rewrite", "remove_path", "not_applicable"):
            errors.append(f"{key}: action 非法")
        for field in ("reason", "implementation", "verification"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                errors.append(f"{key}: 缺少 {field}")
        evidence = row.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(e, str) or not _evidence_valid(e, ws, driver_root)
                       for e in evidence)):
            errors.append(f"{key}: evidence 必须是可读取的相对路径:行号")
        if action == "restore":
            allowed = {d["file"] for d in by_id[key]["definitions"] if d["in_scope"]}
            if (not isinstance(row.get("source"), str) or row["source"] not in allowed
                    or not isinstance(row.get("module"), str) or row["module"] not in modules):
                errors.append(f"{key}: restore 须选已审范围内定义文件与现有模块；"
                              "范围外依赖可用 rewrite 明确等价实现，或重新审定范围")
    if seen != set(by_id):
        errors.append("缺少决定：" + ", ".join(sorted(set(by_id) - seen)))
    return errors


def _restore(plan: dict, rows: list[dict], candidates: list[dict],
             driver_root: Path) -> dict:
    """Restore indexed definitions and their registration macros, without overlap."""
    result = copy.deepcopy(plan)
    by_id = {c["id"]: c for c in candidates}
    occupied: dict[str, set[int]] = {}
    restored: set[tuple[str, str]] = set()
    for mod in result["modules"]:
        for f in mod["files"]:
            for fr in f["fragments"]:
                start, end = resolve._parse_range(fr["lines"])
                occupied.setdefault(f["src"], set()).update(range(start, end + 1))
    for row in rows:
        if row["action"] != "restore":
            continue
        src = row["source"]
        symbol = by_id[row["id"]]["symbol"]
        if (src, symbol) in restored:
            # Multiple consumers can request the same definition. The first
            # selected owner provides it to all consumers through resolve.
            continue
        entries = index.build_index(driver_root, {src})[src]
        definitions = [e for e in entries if e.symbol == symbol
                       and e.kind not in ("reg", "fwd", "chunk")]
        if not definitions:
            raise ValueError(f"{symbol}: 定义无法由索引完整定位，请选择明确的 rewrite")
        selected = definitions + [e for e in entries if e.kind == "reg" and symbol in e.refs]
        wanted = {n for e in selected for n in range(e.start, e.end + 1)}
        missing = sorted(wanted - occupied.setdefault(src, set()))
        if not missing:
            raise ValueError(f"{symbol}: restore 无新增源码，不能解决引用；请重新裁定")
        mod = next(m for m in result["modules"] if m["name"] == row["module"])
        dest = next((f for f in mod["files"] if f["src"] == src), None)
        if dest is None:
            dest = {"src": src, "dest": src.replace("/", "__"), "fragments": []}
            mod["files"].append(dest)
        start = end = missing[0]
        for n in missing[1:] + [missing[-1] + 2]:
            if n == end + 1:
                end = n
            else:
                dest["fragments"].append({"lines": f"{start}-{end}", "symbol": symbol})
                start = end = n
        dest["fragments"].sort(key=lambda f: int(f["lines"].split("-")[0]))
        occupied[src].update(missing)
        restored.add((src, symbol))
    return result


def _source_evidence(rows: list[dict], ws: Path, driver_root: Path) -> list[dict]:
    """Anchor module evidence to original source before restoration moves lines."""
    result = copy.deepcopy(rows)
    for row in result:
        evidence = []
        for value in row["evidence"]:
            if not value.startswith("P1/modules/"):
                evidence.append(value)
                continue
            match = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", value)
            name, start, end = match.groups()
            parts = Path(name).parts
            meta = _read(ws / "P1/modules" / parts[2] / "module.json", {})
            source = meta.get("source_map", {}).get("/".join(parts[3:]))
            if source is None:
                raise ValueError(f"{value}: 证据须引用模块源码或原始源码")
            src = source["src"]
            lines = (driver_root / src).read_text(encoding="utf-8").splitlines()
            _, includes = fragments.split_include_block(lines)
            mapping = list(range(1, len(includes) + 1)) + [None] if includes else []
            for spec in source["fragments"]:
                a, b = resolve._parse_range(spec)
                mapping.extend(range(a, b + 1))
            original = [n for n in mapping[int(start) - 1:int(end or start)]
                        if n is not None]
            if not original:
                raise ValueError(f"{value}: 证据未定位到原始源码")
            a = b = original[0]
            for n in original[1:] + [None]:
                if n == b + 1:
                    b = n
                else:
                    evidence.append(f"{src}:{a}" + (f"-{b}" if b != a else ""))
                    a = b = n
        row["evidence"] = list(dict.fromkeys(evidence))
    return result


def _prompt(ws: Path, driver_root: Path, context: dict) -> str:
    return (agent.load_skill("P1-pruning") + "\n\n## 任务数据\n"
            f"- Linux 驱动目录（只读）：{driver_root}\n"
            f"- 迁移意图：{ws / 'goals.md'}（如存在）\n"
            f"- 已审功能与文件范围：{ws / 'P1/scope.json'}（如存在）\n"
            f"- 已审策略：{ws / 'P1/strategy.md'}\n"
            f"- 原始计划：{ws / 'P1/reports/P1D_plan.json'}\n"
            f"- 当前模块：{ws / 'P1/modules'}\n"
            f"- 当前依赖图：{ws / 'P1/modules/deps.json'}\n"
            f"- 潜在补回依赖（按所选 restore 沿 references 查阅）：{ws / CONTEXT}\n"
            "- 本次全部候选（在同一任务内完成，不再启动下一轮）：\n"
            + json.dumps(context["candidates"], ensure_ascii=False, indent=2))


def _handoff_candidates(initial: list[dict], decisions: list[dict],
                        remaining: list[dict]) -> list[dict]:
    # Restoring a shared symbol must not erase another consumer's explicit
    # remove/rewrite obligation, even though its reference now resolves.
    obligations = {d["id"] for d in decisions
                   if d["action"] in ("rewrite", "remove_path")}
    candidates = {c["id"]: c for c in initial if c["id"] in obligations}
    candidates.update((c["id"], c) for c in remaining)
    return [candidates[key] for key in sorted(candidates)]


def _ready(report: dict, ws: Path, driver_root: Path, fingerprint: str) -> bool:
    if (not isinstance(report, dict) or report.get("status") != "ready"
            or report.get("fingerprint") != fingerprint):
        return False
    candidates = discover(ws, driver_root)
    if report.get("mode") == "single_pass":
        if report.get("checked_candidates") != candidates:
            return False
        initial, proposal = report.get("input_candidates"), report.get("proposal")
        for rows in (initial, proposal):
            if not isinstance(rows, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    for row in rows):
                return False
        if any(row.get("action") not in ("restore", "rewrite", "remove_path", "not_applicable")
               for row in proposal):
            return False
        candidates = _handoff_candidates(initial, proposal, candidates)
    return (report.get("candidates") == candidates
            and not _validate(report.get("decisions"), candidates, ws, driver_root))


def run_pruning(ws: Path, driver_root: Path) -> int:
    """One agent proposal, followed by machine-only validation and publication."""
    report_path = ws / REPORT
    plan_path = ws / "P1/reports/P1D_plan.json"
    report = None
    if not plan_path.is_file() or not (ws / "P1/modules/deps.json").is_file():
        _log.console_line("[porter] P1C: 先完成 p1-divide 和 p1-resolve")
        return 2
    try:
        scope.validate_plan_scope(ws, driver_root, _read(plan_path))
        _validate_source_stamp(ws, driver_root)
        fingerprint = _fingerprint(ws, driver_root)
        old = _read(report_path, {})
        if _ready(old, ws, driver_root, fingerprint):
            _log.console_line("[porter] P1C: 裁剪处置已就绪，输入指纹一致")
            return 0
        candidates = discover(ws, driver_root)
        context = _dependency_context(ws, driver_root, candidates)
        _write(ws / CONTEXT, context)
        _write(ws / "P1/reports/pruning_candidates.json", candidates)
        report = {"status": "pending", "mode": "single_pass",
                  "input_fingerprint": fingerprint, "input_candidates": candidates,
                  "candidates": candidates}
        _write(report_path, report)

        def blocked(errors: list[str]) -> int:
            report.update(status="blocked", errors=errors)
            _write(report_path, report)
            _log.console_line("[porter] P1C: 单次处置未通过校验，停止；详见 pruning.json")
            return 1

        decisions = []
        if candidates:
            _log.console_line(f"[porter] P1C: 一次分析 {len(candidates)} 个驱动引用，"
                              f"已展开 {len(context['dependencies'])} 个潜在依赖符号")
            if os.environ.get("PORTER_NO_AGENT") == "1":
                _log.console_line("[porter] P1C: PORTER_NO_AGENT=1，保留待裁定候选")
                return 2
            rc, _out, parsed = agent.run_agent_structured(
                _prompt(ws, driver_root, context), workdir=ws,
                log_stem=str(ws / "P1/logs/P1C_single"),
                gen_schema={"decisions": "list"}, max_tries=1,
                timeout_sec=1800, task={"phase": "p1", "step": "pruning",
                                       "task_id": "p1.prune.proposal"})
            decisions = parsed.get("decisions") if rc == 0 and parsed else None
            proposal_candidates = _proposal_candidates(decisions, context)
            errors = _validate(decisions, proposal_candidates, ws, driver_root)
            if parsed and parsed.get("status") == "blocked":
                errors = ["agent 报告无法完成处置：" + str(parsed.get("reason") or "blocked")]
            if errors:
                report["proposal"] = parsed
                return blocked(errors)
            report["proposal"] = decisions
            _write(report_path, report)
            decisions = _source_evidence(decisions, ws, driver_root)
        else:
            proposal_candidates = candidates
        report["proposal"] = decisions
        # Save the proposal before checking it. Failed proposals never replace
        # the accepted source plan or modules, and never trigger another agent.
        _write(report_path, report)
        if fingerprint != _fingerprint(ws, driver_root):
            return blocked(["分析期间输入发生变化，方案未应用"])
        restores = [d for d in decisions if d["action"] == "restore"]
        new_plan = _restore(_read(plan_path), restores, proposal_candidates, driver_root)
        scope.validate_plan_scope(ws, driver_root, new_plan)
        with tempfile.TemporaryDirectory(prefix="porter-pruning-") as tmp:
            staged = Path(tmp)
            if restores:
                fragments.extract_modules(staged, driver_root, new_plan)
            else:
                shutil.copytree(ws / "P1/modules", staged / "P1/modules")
            if (ws / "P1/scope.json").exists():
                shutil.copyfile(ws / "P1/scope.json", staged / "P1/scope.json")
            final_candidates = discover(staged, driver_root)
            handoff_candidates = _handoff_candidates(candidates, decisions, final_candidates)
            by_id = {d["id"]: d for d in decisions if d["action"] != "restore"}
            final_decisions = [by_id[c["id"]] for c in handoff_candidates if c["id"] in by_id]
            errors = _validate(final_decisions, handoff_candidates, ws, driver_root)
            graph = resolve._build_graph(staged)
            cycles = resolve._find_cycles(graph["edges"])
            if cycles:
                errors.append("补回方案产生依赖环：" + "; ".join(" → ".join(c) for c in cycles))
            report["checked_candidates"] = final_candidates
            if errors:
                return blocked(errors)
            deps = resolve._dependency_report(graph, cycles)
        if fingerprint != _fingerprint(ws, driver_root):
            return blocked(["校验期间输入发生变化，方案未应用"])
        if restores:
            fragments.extract_modules(ws, driver_root, new_plan)
            _write(plan_path, new_plan)
        _write(ws / "P1/modules/deps.json", deps)
        report.update(status="ready", fingerprint=_fingerprint(ws, driver_root),
                      candidates=handoff_candidates, decisions=final_decisions)
        _write(report_path, report)
        lines = ["# P1 裁剪处置", "", "单次 agent 方案已通过机器校验；P3/P4 继续落实与验证。",
                 "所有修改与验收针对目标 OS 产物；P1/modules 只作只读参考。",
                 "证据路径定位原始源码；残留检查应在目标 OS 对应代码执行。", ""]
        for d in decisions:
            lines.extend([f"## {d['id']} — {d['action']}", "", d["reason"],
                          "", f"实施：{d['implementation']}",
                          f"验证：{d['verification']}", ""])
        (ws / "P1/reports/pruning.md").write_text("\n".join(lines), encoding="utf-8")
        _log.console_line("[porter] P1C: 单次处置通过，供 P3 判据与 P4 生产消费")
        return 0
    except (scope.ScopeError, ValueError, OSError, fragments.DivideError) as e:
        if report is not None and report.get("status") != "ready":
            report.update(status="blocked", errors=[str(e)])
            _write(report_path, report)
        _log.console_line(f"[porter] P1C: {e}")
        return 2


def require_ready(ws: Path, driver_root: Path) -> int:
    """Prevent scoped production from silently dropping unreviewed cut references."""
    try:
        selected = scope.load_scope(ws, driver_root)
        if selected is None and not (ws / REPORT).exists():
            return 0  # Legacy workspaces keep their original flow.
        _validate_source_stamp(ws, driver_root)
        report = _read(ws / REPORT, {})
        if _ready(report, ws, driver_root, _fingerprint(ws, driver_root)):
            return 0
        _log.console_line("[porter] P1C: 裁剪处置缺失、未完成或已过期；先运行 p1-prune")
    except (ValueError, OSError) as e:
        _log.console_line(f"[porter] P1C: {e}")
    return 2


def _module_decisions(ws: Path, module: str) -> list[dict]:
    report = _read(ws / REPORT, {})
    if report.get("status") != "ready":
        return []
    ids = {c["id"] for c in report.get("candidates", []) if c["module"] == module}
    return [d for d in report.get("decisions", []) if d["id"] in ids]


def criteria_errors(ws: Path, module: str, criteria: list[dict]) -> list[str]:
    """Require behavioural criteria for every actual pruning rewrite."""
    required = {d["id"] for d in _module_decisions(ws, module)
                if d["action"] in ("rewrite", "remove_path")}
    covered = {key for c in criteria
               if c.get("kind") in ("unit_test", "log_pattern", "counter", "e2e")
               for key in c.get("pruning_ids", [])}
    missing = required - covered
    return ["裁剪处置缺少行为判据：" + ", ".join(sorted(missing))] if missing else []


def module_context(ws: Path, module: str) -> str:
    """Functional scope and pruning obligations both reach P3 and P4."""
    context = ""
    features = _read(ws / "P1/scope.json", {}).get("features")
    if features is not None:
        context = ("\n## 已审功能范围（本模块的实现与验证均须遵循）\n"
                   + json.dumps(features, ensure_ascii=False, indent=2)
                   + "\n仅在本模块职责内落实；跨模块的实例化和端到端验证由"
                     "相应消费者完成。保留原生等价入口，不能重新引入已排除的设施。\n")
    rows = _module_decisions(ws, module)
    if not rows:
        return context
    return (context + "\n## P1 功能裁剪处置（逐项实施并加入验收，保留错误与资源语义）\n"
            + json.dumps(rows, ensure_ascii=False, indent=2)
            + "\n以上路径/行号定位迁移前语义来源。P1/modules 是只读参考，"
              "不得通过修改它满足验收；实施、静态残留检查和行为测试均针对"
              "目标 OS 产物。若处置原文把 grep 指向 P1/modules，须转为"
              "检查目标 OS 对应实现。\n"
            + "Linux 机制名仅说明源端契约；目标侧使用已核实的原生机制，"
              "不能仅凭名字假定同名 API 存在，也不得重建已排除接口。"
              "引用释放、设备登记和等待语义须保留；若范围已排除 Linux "
              "sysfs，则不以创建其节点作为验收要求。\n"
            + "\nP3 须为每个 rewrite/remove_path 生成至少一条行为判据（unit_test、"
              "log_pattern、counter 或 e2e）。在判据的 pruning_ids 字符串列表"
              "标明处置 id；允许一条判据覆盖多项。compile/boot 不替代行为验证。"
              "P4 按给定判据落实代码与测试。\n")
