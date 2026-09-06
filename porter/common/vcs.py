"""vcs.py — git 管理模块（两个 repo：目标 OS 树 + 迁移工作区）。

职责（全部 best-effort：任何 git 失败记 warning，永不阻塞流水线）：
- 目标 OS repo：用户已有仓（支持并行多仓，嵌套只管最外层）。P0 登记
  baseline + 建 porter 分支；P2 骨架 / P4 每模块 / P6 execute / 求解
  修码后按触碰路径或 status 捕获 commit。
- 工作区 repo：P0 时 git init（与目标 OS 统一分支名）。阶段末 / loop
  模块 done / 每次 agent 调用前后（pre-agent/agent 成对隔离，
  diff 即该次调用的 ws 产物）/ exit 3 停车 / answers 消费后 commit；
  知识库（<ws>/knowledge/）随工作区统一入库。两仓 commit 流互相独立
  ——目标 OS 只按既定点提交（P2 骨架 / P4 模块末 / P6 execute /
  求解修码后），不为 agent 调用加目标树 commit 点。
- 台账：<ws>/vcs_commits.json 记录每次成功 commit（repo/phase/hash/
  msg/time），P7 据此输出 commit 链，导出据此定位范围。
- 跨机器：git bundle（保 commit hash；目标 OS 出 baseline..HEAD 增量包，
  工作区出全量包；导入端要求同一起点 commit）。

配置（porter/config.json 的 vcs 节；PORTER_VCS=0/1 环境变量强制覆盖）：
  enabled            总开关（false = 全部跳过，等价无 vcs 行为）
  identity           git 身份兜底（容器常无全局 user.name/email）
  target_os/workspace 每 repo 开关
  export.format      导出格式（bundle）

分支管理（project.json["vcs"]，工作区级；resume 依据）：
  {"branch": "porter/e1000-20260903-a1b2",
   "repos": [{"root": "/abs/path", "baseline": "<sha>"}]}
  commit 前惰性校验：HEAD 已在记录分支 → 继续；不在 → 切回（会冲突则
  跳过该次 commit 并告警）。旧工作区无此节 → 不管理分支，当前分支直提。
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .. import log as _log

TOOL_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = TOOL_ROOT / "porter" / "config.json"

# 目标树接线面（P2b 骨架 + P4 会话接线产生的已知修改；driver crate 之外）。
# p7.py 的分组也以此为准（单一事实源）。
TARGET_WIRING_FILES = (
    "Cargo.toml", "Cargo.lock", "Components.toml",
    "kernel/core/Cargo.toml",
    "kernel/core/src/driver/mod.rs",
    "kernel/core/src/net/iface/init.rs",
)

# 目标树仓扫描：跳过的构建/依赖目录名
_SKIP_NAMES = {"target", "target2", "build", "dist", "out", "node_modules"}

_LEDGER_NAME = "vcs_commits.json"

# 配置缓存（mtime 失效；避免每次 git 调用重读 config.json）
_CFG_CACHE: dict = {"mtime": None, "cfg": {}}


# ---------- 配置 ----------

def _load_cfg() -> dict:
    try:
        mtime = CONFIG_PATH.stat().st_mtime
        if _CFG_CACHE["mtime"] == mtime:
            return _CFG_CACHE["cfg"]
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) \
            .get("vcs", {})
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    _CFG_CACHE.update({"mtime": mtime, "cfg": cfg})
    return cfg


def enabled(kind: str | None = None) -> bool:
    """vcs 开关（PORTER_VCS=0/1 强制覆盖；kind=目标 repo 细分开关）。"""
    env = os.environ.get("PORTER_VCS")
    if env in ("0", "false"):
        return False
    if env in ("1", "true"):
        return kind is None or bool((_load_cfg().get(kind) or {}).get("enabled", True))
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return False
    if kind is not None:
        return bool((cfg.get(kind) or {}).get("enabled", True))
    return True


# ---------- 底层 git 封装 ----------

def _git(repo: Path, *args: str, timeout: int = 300) -> tuple[int, str]:
    """git -C <repo> ...（identity 兜底 + 关 gpgsign/用户 hook）。永不上抛。

    空/`.` 路径一律拒绝——`git -C .` 会操作进程 CWD，历史上曾因此把
    测试进程所在的工具仓误提交（2026-09-04 事故，见 commit_target）。
    """
    if str(repo).strip() in ("", "."):
        return 1, ""
    ident = _load_cfg().get("identity") or {}
    cmd = ["git", "-C", str(repo)]
    if ident.get("name"):
        cmd += ["-c", f"user.name={ident['name']}"]
    if ident.get("email"):
        cmd += ["-c", f"user.email={ident['email']}"]
    cmd += ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
            *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or ""))
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def head(repo: Path) -> str:
    rc, out = _git(repo, "rev-parse", "HEAD")
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""


def is_dirty(repo: Path) -> bool:
    rc, out = _git(repo, "status", "--porcelain")
    return rc == 0 and bool(out.strip())


def current_branch(repo: Path) -> str:
    rc, out = _git(repo, "branch", "--show-current")
    return out.strip() if rc == 0 else ""


def branch_exists(repo: Path, branch: str) -> bool:
    rc, _ = _git(repo, "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{branch}")
    return rc == 0


def ensure_branch(repo: Path, branch: str) -> bool:
    """HEAD 在记录分支 → True；不在 → 切回/新建（失败告警 False）。"""
    if not branch:
        return True
    if current_branch(repo) == branch:
        return True
    args = (["checkout", branch] if branch_exists(repo, branch)
            else ["checkout", "-b", branch])
    rc, out = _git(repo, *args)
    if rc != 0:
        _log.console_line(f"[porter] vcs: ⚠️ {repo} 切回分支 {branch} 失败"
                          f"（未提交改动冲突？本次 commit 跳过）："
                          f"{out.strip()[:160]}")
        return False
    return True


def init_repo(path: Path, branch: str | None = None) -> bool:
    """git init（已存在则 skip）+ 可选分支。幂等。"""
    path = Path(path)
    if not (path / ".git").exists():
        rc, out = _git(path, "init")
        if rc != 0:
            _log.console_line(f"[porter] vcs: ⚠️ git init 失败 {path}："
                              f"{out.strip()[:160]}")
            return False
    if branch:
        ensure_branch(path, branch)
    return True


def commit(repo: Path, msg: str, paths: list[str] | None = None,
           phase: str | None = None,
           exclude: list[str] | None = None) -> str | None:
    """commit 指定路径（相对 repo 根；None=status 捕获全部）。

    exclude = add -A 时排除的路径（工作区仓排除台账/exports，防自引用
    提交噪音与 bundle 递归膨胀）。返回 commit hash；无变更/失败返回
    None（幂等：重复 commit 无副作用）。
    """
    repo = Path(repo)
    if paths is None:
        args = ["add", "-A", "--", "."]
        for e in (exclude or []):
            args.append(f":(exclude){e}")
        rc, out = _git(repo, *args)
        if rc != 0:
            _log.console_line(f"[porter] vcs: ⚠️ {repo} git add -A 失败："
                              f"{out.strip()[:160]}")
            return None
    else:
        rel = [p for p in paths if (repo / p).exists()]
        if not rel:
            return None
        rc, out = _git(repo, "add", "--", *rel)
        if rc != 0:
            _log.console_line(f"[porter] vcs: ⚠️ {repo} git add 失败："
                              f"{out.strip()[:160]}")
            return None
    rc, _ = _git(repo, "diff", "--cached", "--quiet")
    if rc == 0:
        return None                     # 暂存区无变更（幂等重入）
    full = msg + (f"\n\nPorter-Phase: {phase}" if phase else "")
    rc, out = _git(repo, "commit", "-m", full)
    if rc != 0:
        _log.console_line(f"[porter] vcs: ⚠️ {repo} git commit 失败："
                          f"{out.strip()[:160]}")
        return None
    return head(repo)


# ---------- 目标树并行仓发现 ----------

def register_repos(target_os: Path, max_depth: int = 6) -> list[dict]:
    """扫描目标树下的 git 仓（嵌套只留最外层；跳过构建目录）。

    返回 [{"root": abs, "baseline": HEAD}]；非 git 仓（head 为空）剔除。
    """
    root = Path(target_os)
    found: list[str] = []
    if (root / ".git").exists() and root != TOOL_ROOT:
        found.append(str(root))
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            if e.name in _SKIP_NAMES or e.name == ".git":
                continue
            p = Path(e.path)
            if (p / ".git").exists():
                found.append(str(p))
            stack.append((p, depth + 1))
    kept: list[str] = []
    for r in sorted(set(found), key=len):
        if not any(r == k or r.startswith(k + os.sep) for k in kept):
            kept.append(r)
    out: list[dict] = []
    for r in kept:
        h = head(Path(r))
        if h:
            out.append({"root": r, "baseline": h})
    return out


def gen_branch_name(driver: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", driver or "drv").strip("-") or "drv"
    day = datetime.now().strftime("%Y%m%d")
    rand = f"{random.randrange(16 ** 4):04x}"
    return f"porter/{safe}-{day}-{rand}"


# ---------- project.json["vcs"] ----------

def _load_proj(ws: Path) -> dict | None:
    try:
        return json.loads((Path(ws) / "project.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_proj_vcs(ws: Path, vcs: dict) -> None:
    proj = _load_proj(ws) or {}
    proj["vcs"] = vcs
    (Path(ws) / "project.json").write_text(
        json.dumps(proj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def hook_p0(ws: Path, target_os: Path, branch: str | None = None) -> int:
    """P0 的 vcs 登记（幂等）。返回 0=继续；2=输入错（分支名已存在）。

    - 扫描目标树并行 git 仓，逐仓记 baseline；
    - 分支：用户指定（须全新）或自动生成 porter/<driver>-<日期>-<rand4>；
      各目标仓 checkout -b；工作区仓 git init 同分支名；
    - 写 project.json["vcs"]（resume 与惰性分支校验的依据）。
    """
    if not enabled():
        return 0
    ws, target_os = Path(ws), Path(target_os)
    proj = _load_proj(ws)
    if proj is None:
        return 0                        # 无 project.json（T1 异常由上层处理）
    vv = proj.get("vcs") or {}
    if vv:
        # resume：只补齐分支（目标仓 + 工作区仓）
        name = vv.get("branch")
        for r in (vv.get("repos") or []):
            if name:
                ensure_branch(Path(r["root"]), name)
        init_repo(ws, name)
        return 0
    repos = register_repos(target_os)
    name = None
    if repos:
        driver = proj.get("driver_name") or Path(
            proj.get("linux_driver") or "drv").name
        if branch:
            clash = [r["root"] for r in repos
                     if branch_exists(Path(r["root"]), branch)]
            if clash:
                _log.console_line(
                    f"[porter] vcs: --os-branch {branch!r} 已存在于 "
                    f"{clash}——须用全新分支名")
                return 2
            name = branch
        else:
            name = gen_branch_name(driver)
            while any(branch_exists(Path(r["root"]), name) for r in repos):
                name = gen_branch_name(driver)
        for r in repos:
            if not ensure_branch(Path(r["root"]), name):
                _log.console_line(f"[porter] vcs: ⚠️ {r['root']} 分支建立"
                                  "失败（该仓后续 commit 将跳过）")
    init_repo(ws, name)
    write_ws_gitignore(ws)
    _save_proj_vcs(ws, {"branch": name, "repos": repos})
    if repos:
        _log.console_line(f"[porter] vcs: 目标树登记 {len(repos)} 个 git 仓，"
                          f"porter 分支 {name}（baseline 已记 "
                          "project.json['vcs']）")
    else:
        _log.console_line("[porter] vcs: 目标树未发现 git 仓——目标 OS 不做"
                          " commit 管理（工作区照常）")
    return 0


# ---------- agent 调用隔离（仅工作区仓；目标 OS 按既定点提交） ----------

def agent_pre(ws: Path, stem: str) -> str | None:
    """agent 调用前的工作区隔离点：pre-agent commit。

    pre 与 post 之间的 ws 变更（prompt/log/报告）即该次调用的产物——
    git diff <pre>..<post> = 该次非交互调用在 ws 侧做的全部事情。
    目标 OS 树不在此列（两仓 commit 流独立，目标树按既定点提交）。
    """
    return commit_workspace(ws, f"pre-agent: {stem}", phase="agent")


def agent_post(ws: Path, stem: str, rc: int) -> str | None:
    """agent 调用后的工作区隔离点（与 agent_pre 成对，台账相邻）。"""
    return commit_workspace(ws, f"agent: {stem} rc={rc}", phase="agent")


# ---------- 工作区仓 .gitignore ----------

_WS_IGNORE_ENTRIES = ("/vcs_commits.json", "/exports/")


def write_ws_gitignore(ws: Path) -> bool:
    """工作区仓 .gitignore：排除台账与导出物（幂等：缺文件写入/缺条目追加）。

    与 commit() 的 :(exclude) pathspec 双保险——本文件防手动 git add -A
    误入（bundle 二进制入库会让下次导出递归膨胀）。
    """
    p = Path(ws) / ".gitignore"
    try:
        cur = p.read_text(encoding="utf-8") if p.exists() else ""
        missing = [ln for ln in _WS_IGNORE_ENTRIES
                   if ln not in cur.splitlines()]
        if not missing:
            return True
        block = ("# porter vcs 运行时产物（台账可从 git log 重建；"
                 "exports 为 bundle 二进制，入库会递归膨胀）\n"
                 + "\n".join(missing) + "\n")
        p.write_text((cur.rstrip("\n") + "\n\n" if cur.strip() else "")
                     + block, encoding="utf-8")
        return True
    except OSError:
        return False


# ---------- 台账 ----------

def _ledger_append(ws: Path, entry: dict) -> None:
    p = Path(ws) / _LEDGER_NAME
    doc: dict = {"commits": []}
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) or doc
    except (OSError, json.JSONDecodeError):
        pass
    doc.setdefault("commits", []).append(
        {"time": datetime.now().isoformat(timespec="seconds"), **entry})
    try:
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    except OSError:
        pass


def load_ledger(ws: Path) -> list[dict]:
    try:
        doc = json.loads((Path(ws) / _LEDGER_NAME).read_text(
            encoding="utf-8"))
        return doc.get("commits") or []
    except (OSError, json.JSONDecodeError):
        return []


# ---------- 语义封装（接线点用） ----------

def commit_workspace(ws: Path, msg: str, phase: str | None = None) -> str | None:
    """工作区 commit（未 init / 未启用 → no-op）。登记台账。"""
    ws = Path(ws)
    if not enabled("workspace") or not (ws / ".git").is_dir():
        return None
    vv = (_load_proj(ws) or {}).get("vcs") or {}
    branch = vv.get("branch")
    if branch and not ensure_branch(ws, branch):
        return None
    # 排除台账与 exports（在仓内部但不应自引用提交/bundle 递归）
    h = commit(ws, msg, phase=phase,
               exclude=[_LEDGER_NAME, "exports"])
    if h:
        _ledger_append(ws, {"repo_kind": "workspace", "repo": str(ws),
                            "phase": phase, "msg": msg, "hash": h})
    return h


def commit_target(ws: Path, msg: str, paths: list[str] | None = None,
                  phase: str | None = None) -> list[str]:
    """目标 OS commit（各登记仓）。paths=目标树相对路径（None=status 捕获）。

    返回成功 commit 的 hash 列表（逐仓登记台账）。
    """
    ws = Path(ws)
    if not enabled("target_os"):
        return []
    proj = _load_proj(ws) or {}
    vv = proj.get("vcs") or {}
    repos = [dict(r) for r in (vv.get("repos") or [])]
    raw = proj.get("target_os")
    tos = Path(raw) if isinstance(raw, str) and raw.strip() else None
    if not repos:
        # 旧工作区（无 vcs 节）兜底：仅当 target_os 为显式绝对路径且自身
        # 是 git 仓时单仓管理。空/相对路径/工具仓自身一律拒绝——
        # Path("") 会归一化为 CWD，dirty-CWD 下会把进程所在仓误提交
        # （2026-09-04 事故：工具仓被测试误提交 8 条 solve[d1] commit）。
        if (tos is None or not tos.is_absolute() or tos == TOOL_ROOT
                or not (tos / ".git").exists()):
            return []
        repos = [{"root": str(tos), "baseline": None}]
    branch = vv.get("branch")
    hashes: list[str] = []
    for r in repos:
        root = Path(r["root"])
        if root == TOOL_ROOT or not (root / ".git").exists():
            continue
        if branch and not ensure_branch(root, branch):
            continue
        rels = None
        if paths is not None:
            if tos is None:
                continue
            rels = []
            for p in paths:
                ap = Path(p)
                if not ap.is_absolute():
                    ap = tos / p
                try:
                    if ap.is_relative_to(root):
                        rels.append(str(ap.relative_to(root)))
                except (ValueError, OSError):
                    pass
            rels = [p for p in rels if (root / p).exists()]
            if not rels:
                continue                # 本仓无涉及路径
        h = commit(root, msg, paths=rels, phase=phase)
        if h:
            _ledger_append(ws, {"repo_kind": "target_os", "repo": str(root),
                                "phase": phase, "msg": msg, "hash": h})
            hashes.append(h)
    return hashes


# ---------- P7 commit 链 ----------

def _chain_files(entry: dict) -> dict:
    """台账/git-log 条目 → 附上该 commit 的文件变更清单。"""
    files: list[dict] = []
    root, h = entry.get("repo"), entry.get("hash")
    if root and h:
        rc, txt = _git(Path(root), "show", "--name-status",
                       "--format=", h)
        if rc == 0:
            for ln in txt.splitlines():
                parts = ln.split("\t")
                if len(parts) >= 2 and parts[0][:1] in "MDARC":
                    files.append({"status": parts[0][:1],
                                  "path": parts[-1]})
    return {**entry, "files": files}


def commit_chain(ws: Path) -> list[dict]:
    """porter commit 链（台账优先；台账空 = 导入的工作区 → git log 重建）。

    逐条附 git show --name-status 的文件清单（"哪次 commit 改了什么"）。
    """
    led = load_ledger(ws)
    if led:
        return [_chain_files(e) for e in led]
    proj = _load_proj(ws) or {}
    vv = proj.get("vcs") or {}
    out: list[dict] = []
    targets = [(Path(r["root"]), "target_os", r.get("baseline"))
               for r in (vv.get("repos") or [])]
    targets.append((Path(ws), "workspace", None))
    for root, kind, baseline in targets:
        if not (root / ".git").exists():
            continue
        args = ["log", "-z", "--reverse", "--format=%H%x1f%cI%x1f%B"]
        if baseline:
            args.append(f"{baseline}..HEAD")
        rc, txt = _git(root, *args)
        if rc != 0:
            continue
        for rec in txt.split("\x00"):
            rec = rec.strip("\n")
            if not rec:
                continue
            h, _, rest = rec.partition("\x1f")
            when, _, body = rest.partition("\x1f")
            m = re.search(r"Porter-Phase: (\S+)", body)
            subj = body.strip().splitlines()[0] if body.strip() else ""
            out.append(_chain_files({
                "time": when, "repo_kind": kind, "repo": str(root),
                "phase": m.group(1) if m else None,
                "msg": subj, "hash": h}))
    out.sort(key=lambda e: str(e.get("time") or ""))
    return out


# ---------- 跨机器可移植（git bundle） ----------

def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", s).strip("-") or "repo"


def export_all(ws: Path, out_dir: Path | None = None) -> dict:
    """导出可移植 bundle 集 → <ws>/exports/（+ manifest.json）。

    目标 OS 各仓：baseline..HEAD 增量包（无 baseline 则全量）；
    工作区仓：HEAD 全量包。禁用/无登记 → {}。
    """
    ws = Path(ws)
    if not enabled():
        return {}
    proj = _load_proj(ws)
    if proj is None:
        return {}
    vv = proj.get("vcs") or {}
    out = Path(out_dir) if out_dir else ws / "exports"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    files: list[dict] = []
    for i, r in enumerate(vv.get("repos") or []):
        root = Path(r["root"])
        if not (root / ".git").exists():
            continue
        fname = f"target-os-{i}-{_safe_name(root.name)}.bundle"
        baseline = r.get("baseline")
        range_arg = (f"{baseline}..HEAD" if baseline else "--all")
        rc, txt = _git(root, "bundle", "create", str(out / fname),
                       range_arg)
        if rc == 0:
            files.append({"repo": str(root), "kind": "target_os",
                          "baseline": baseline, "bundle": fname})
        else:
            _log.console_line(f"[porter] vcs: ⚠️ bundle 导出失败 {root}："
                              f"{txt.strip()[:160]}")
    if (ws / ".git").is_dir():
        rc, txt = _git(ws, "bundle", "create", str(out / "workspace.bundle"),
                       "HEAD")
        if rc == 0:
            files.append({"repo": str(ws), "kind": "workspace",
                          "baseline": None, "bundle": "workspace.bundle"})
        else:
            _log.console_line(f"[porter] vcs: ⚠️ 工作区 bundle 导出失败："
                              f"{txt.strip()[:160]}")
    manifest = {"time": datetime.now().isoformat(timespec="seconds"),
                "branch": vv.get("branch"), "dir": str(out), "files": files}
    try:
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        pass
    return manifest


def import_bundle(bundle: Path, repo: Path,
                  branch: str | None = None) -> tuple[bool, str]:
    """把 bundle 的 commit 链接回 git 仓（保 hash；要求同一起点 commit）。

    git fetch <bundle> HEAD → 可选建分支并切换。失败返回 (False, 原因)。
    """
    bundle, repo = Path(bundle), Path(repo)
    if not bundle.is_file():
        return False, f"bundle 不存在: {bundle}"
    if not (repo / ".git").exists():
        return False, f"目标不是 git 仓: {repo}"
    rc, out = _git(repo, "fetch", str(bundle), "HEAD")
    if rc != 0:
        return False, (out.strip()[:400]
                       or "fetch 失败（起点 commit 不匹配？bundle 前置"
                          "提交在目标仓缺失）")
    if branch:
        if branch_exists(repo, branch):
            rc, out2 = _git(repo, "checkout", branch)
            if rc != 0:
                return True, f"已 fetch；切回 {branch} 失败：{out2.strip()[:200]}"
            return True, f"已 fetch 并切回 {branch}"
        rc, out2 = _git(repo, "branch", branch, "FETCH_HEAD")
        if rc != 0:
            return True, f"已 fetch；建分支 {branch} 失败：{out2.strip()[:200]}"
        _git(repo, "checkout", branch)
        return True, f"已 fetch，建分支 {branch} 并切换"
    return True, "已 fetch（FETCH_HEAD）"
