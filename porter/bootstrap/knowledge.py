"""knowledge.py — P2 映射知识沉淀（仿 P1 三件套：自动草稿 → 价值判定 → 人工晋升）。

与 P1（splits/strategies）的差异，均源于**映射表是活文档**：
- 命名带目标 OS：`<驱动名>@<目标OS名>.md/.json`（映射离开目标 OS 无意义；
  P1 样例只涉及 Linux 侧故不带）
- 条目双文件：`.md`（人读渲染）+ `.json`（机器消费侧域过滤/检索）
- 晋升语义：同名条目 = **版本更新替换**（version+1、保留 hits）——循环期间
  映射表持续增量，快照并存会堆积 15 个版本；P1 样例是不可变快照故拒绝重复
- 草稿节奏（增量沉淀，§10 定案 6 修订）：P2 末自动写草稿；此后每轮
  P3(M) 末 run_map 重跑时刷新（幂等覆盖）；人工随时 p2-promote 晋升

目录模型（kb.py）：草稿入 knowledge/temp/maps/；晋升目标是**本次迁移
的知识库目录**（project.json["kb_dir"]，由 p0 --kb 指定）下的 maps/。

消费铁律（沉淀规范与 agent SKILL 同款）：条目是"经源码核实的主张"，
消费时必须重核实——"核实后抄入、不跨目标复用未验证结论"。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import kb
from .. import log as _log

# 域三级分类（价值判定用；启发式清单，未列出的域 = 驱动特异）
OS_GENERIC_DOMAINS = {
    "linux/pci.h", "linux/pci_ids.h", "linux/dma-mapping.h",
    "linux/interrupt.h", "linux/bitops.h", "linux/mm.h", "linux/slab.h",
    "linux/kernel.h", "linux/string.h", "linux/io.h", "linux/jiffies.h",
    "linux/delay.h", "linux/ioport.h", "linux/types.h", "linux/vmalloc.h",
    "linux/module.h", "linux/list.h", "linux/errno.h", "linux/capability.h",
    "linux/workqueue.h", "asm-generic/io.h",
}
NET_CATEGORY_DOMAINS = {
    "linux/netdevice.h", "linux/skbuff.h", "linux/etherdevice.h",
    "linux/if_vlan.h", "linux/mii.h", "linux/ethtool.h",
    "uapi/linux/ethtool.h", "linux/netdev_features.h", "linux/phy.h",
    "linux/tcp.h", "linux/ip.h", "linux/ipv6.h", "linux/in.h",
    "net/ip6_checksum.h",
}


def _bucket_of(domain: str) -> str:
    if domain in OS_GENERIC_DOMAINS:
        return "os-generic"
    if domain in NET_CATEGORY_DOMAINS:
        return "net-category"
    return "driver-specific"


def _value_judgment(mapping: dict) -> tuple[dict, list[str]]:
    """机器预检：域三级桶计数 + 高置信可复用统计。返回 (统计, 报告行)。"""
    ents = mapping.get("entries", [])
    buckets: dict[str, set[str]] = {"os-generic": set(), "net-category": set(),
                                    "driver-specific": set()}
    for e in ents:
        buckets[_bucket_of(e.get("domain", ""))].add(e["linux_api"])
    hi = {b: sorted(s) for b, s in buckets.items()}
    solid = sorted(e["linux_api"] for e in ents
                   if e["verdict"] in ("direct", "adapt")
                   and e.get("confidence") == "high")
    stats = {"os_generic": len(hi["os-generic"]),
             "net_category": len(hi["net-category"]),
             "driver_specific": len(hi["driver-specific"]),
             "high_confidence_direct_adapt": len(solid)}
    lines = [
        "## 沉淀价值判定（机器预检）", "",
        f"- 域三级桶：OS 通用 {stats['os_generic']} 条"
        f"（跨驱动全复用）/ net 类别 {stats['net_category']} 条"
        f"（net 驱动复用）/ 驱动特异 {stats['driver_specific']} 条",
        f"- 高置信 direct/adapt：{stats['high_confidence_direct_adapt']} 条"
        "（复用价值最高；消费侧仍须重核实）",
        f"- 换思路裁定 {len(mapping.get('redesigns', []))} 条 + 接线清单 "
        f"{len(mapping.get('wiring', []))} 条（目标 OS 侧通用，复用价值高）",
        "- 判定标准：桶分类为启发式清单；`not-migrated` 条目对同裁剪策略"
        "的后续迁移有参照价值；`gap` 条目在目标 OS 演进后需复核",
    ]
    return stats, lines


def draft_knowledge(ws: Path) -> int:
    """草稿：工作区映射表 → knowledge/temp/maps（幂等覆盖）+ 薄 INDEX 行。"""
    proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
    mapping_path = ws / "P2" / "mapping.json"
    md_path = ws / "P2" / "mapping.md"
    if not mapping_path.exists() or not md_path.exists():
        _log.console_line("[porter] P2 知识: 缺少 P2/mapping.json|md（先跑 p2-map）——跳过草稿")
        return 1
    driver = Path(proj["linux_driver"]).name
    target = Path(proj["target_os"]).name
    stem = f"{driver}@{target}"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    tdir = kb.domain_temp("maps", ws=ws)
    tdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(md_path, tdir / f"{stem}.md")
    shutil.copyfile(mapping_path, tdir / f"{stem}.json")

    vc = {v: sum(1 for e in mapping.get("entries", [])
                 if e.get("verdict") == v)
          for v in ("direct", "adapt", "gap", "not-migrated")}
    idx = kb.load_index(tdir) or []
    # 清同 stem 的旧行（含旧富格式 entry_file 行），保留其 hits
    fname = f"{stem}.md"
    old_hits = max([int(e.get("hits", 0) or 0)
                    for e in idx if isinstance(e, dict)
                    and (e.get("file") == fname or e.get("entry_file")
                         == fname)] or [0])
    idx = [e for e in idx if not (isinstance(e, dict)
                                  and (e.get("file") == fname
                                       or e.get("entry_file") == fname))]
    desc = (f"{stem} 完整映射表（"
            + "，".join(f"{k} {v}" for k, v in sorted(vc.items()))
            + f"；换思路 {len(mapping.get('redesigns', []))}）"
            f"——机器表 {stem}.json 同目录")
    row = {"file": fname, "desc": desc, "hits": old_hits}
    idx.append(row)
    kb.save_index(tdir, idx)

    _stats, lines = _value_judgment(mapping)
    rpt = ws / "P2" / "reports" / "mapping_report.md"
    if rpt.exists():
        text = rpt.read_text(encoding="utf-8")
        if "## 沉淀价值判定" not in text:
            rpt.write_text(text.rstrip() + "\n\n" + "\n".join(lines) + "\n",
                           encoding="utf-8")
    _log.console_line(f"[porter] P2 知识: 草稿已刷新 knowledge/temp/maps/{stem}.md/.json"
          f"（{len(mapping.get('entries', []))} 条）；"
          f"人工审阅后可 `p2-promote --output-dir <ws> "
          f"--driver {driver} --target {target}`")
    return 0


def promote_map(driver: str, kb_dir: Path,
                target: str | None = None) -> int:
    """晋升：knowledge/temp/maps → <知识库目录>/maps（薄 INDEX 行）。

    条目文件名 = `<驱动>@<目标>.md`（stem 携带身份；同名 = 活文档替换，
    hits 取两侧较高值）。kb_dir = 本次迁移的知识库目录。
    """
    tdir = kb.domain_temp("maps", kb_dir=kb_dir)
    tidx = kb.load_index(tdir) or []
    rows = [e for e in tidx if isinstance(e, dict)
            and (e.get("file") or e.get("entry_file"))]
    cands = [e for e in rows
             if str(e.get("file") or e.get("entry_file"))
             .split("@", 1)[0] == driver
             and (target is None
                  or str(e.get("file") or e.get("entry_file"))
                  == f"{driver}@{target}.md")]
    if not cands:
        _log.console_line(f"[porter] p2-promote: knowledge/temp/maps 无匹配 "
              f"{driver}@{target or '*'} 草稿")
        return 1
    if len(cands) > 1:
        _log.console_line(f"[porter] p2-promote: {len(cands)} 个匹配，请指定 --target：")
        for e in cands:
            print(f"  - {e.get('file') or e.get('entry_file')}")
        return 1
    entry = cands[0]
    fname = str(entry.get("file") or entry.get("entry_file"))
    stem = fname[:-3] if fname.endswith(".md") else fname
    src_md, src_json = tdir / fname, tdir / f"{stem}.json"
    if not src_md.exists():
        _log.console_line(f"[porter] p2-promote: 草稿缺失 {src_md}（temp INDEX 与磁盘不一致）")
        return 1

    hits = int(entry.get("hits", 0) or 0)
    desc = str(entry.get("desc")
               or f"{stem} 完整映射表——机器表 {stem}.json 同目录")
    kdir = kb.domain_kb("maps", kb_dir)
    kidx = kb.load_index(kdir) or []
    old_dst = next((e for e in kidx if isinstance(e, dict)
                    and (e.get("file") == fname
                         or e.get("entry_file") == fname)), None)
    hits = max(hits, int((old_dst or {}).get("hits", 0) or 0))
    kdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_md), kdir / fname)
    if src_json.exists():
        shutil.move(str(src_json), kdir / f"{stem}.json")
    kb.save_index(tdir, [e for e in tidx if not (
        isinstance(e, dict) and (e.get("file") == fname
                                 or e.get("entry_file") == fname))])
    kidx = [e for e in kidx if not (isinstance(e, dict)
                                    and (e.get("file") == fname
                                         or e.get("entry_file") == fname))]
    hits += int(kb.fold_sidecar_hits(kb_dir, "maps", [fname]).get(fname, 0))
    kidx.append({"file": fname, "desc": desc, "hits": hits})
    kb.save_index(kdir, kidx)
    _log.console_line(f"[porter] p2-promote: {stem} 已晋升 → {kdir}")
    return 0
