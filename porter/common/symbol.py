"""symbol.py — C 源码符号静态扫描（v2：语句分类版）。

设计目标：为依赖图提供准确的 定义/引用 数据。v1 的三类缺陷在本版由
结构修复（而非补丁）：
- 原型被算成引用 → 语句分类：`签名;` 为原型，名字入 protos（既非定义
  也非引用），仅参数中的类型名入引用
- 枚举值泄漏 → `enum X {` 块内标识符全部入定义集
- 字段名/局部名噪音 → 引用只来自：函数体、全局初始化表达式、宏体；
  struct 字段与局部声明不产生任何集合条目；引用过滤 <3 字符名

v2.1（corner case 加固）：
- extern 声明不入定义集（防所有权仲裁被偷）
- `struct X y = {...}`（无 static/const 前缀）归初始化分支，不再误判
  为类型块而丢失变量定义与初始化引用
- 预处理改单遍词法清洗：字符串/字符字面量内容置空——杜绝日志串幻影
  引用，并修复字符串内 `/*` 被当注释剥离的误伤
- 函数指针变量声明 `int (*cb)(...)` 找回定义名（类型名不再误配函数名）

v2.2（P2a 提取时发现）：
- 匿名 typedef 枚举 `typedef enum { ... } name;` 的枚举值此前落入
  "未识别块"进 refs（v2 的枚举修复只覆盖具名 `enum X {`）——现与
  具名分支同语义入 defs；匿名 typedef struct/union 取尾名入 defs

已知限制（真实驱动树未出现，暂不处理）：
- `#if 0` 死代码仍会被扫描（无预处理器）
- DEFINE_SPINLOCK/DECLARE_WORK 等宏的"定义语义"不识别（按引用处理）
- `#ifdef` 两个分支的代码都会被扫描

公开 API 与 v1 兼容：scan_file / scan_module_dir。
"""

from __future__ import annotations

import re
from pathlib import Path

_IDENT = re.compile(r"\b([A-Za-z_]\w*)\b")

_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default", "break",
    "continue", "return", "goto", "sizeof", "typedef", "struct", "union",
    "enum", "static", "const", "inline", "extern", "volatile", "unsigned",
    "signed", "int", "char", "long", "short", "void", "float", "double",
    "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64", "bool", "true",
    "false", "NULL", "define", "include", "ifdef", "ifndef", "endif", "elif",
    "pragma", "undef", "error", "warning", "line", "__le16", "__le32",
    "__le64", "__be16", "__be32", "__be64", "__attribute__", "packed",
    "aligned", "restrict", "auto", "register", "_Bool", "va_list",
}

# 声明名：行首起（允许修饰符/类型/指针/数组），最后一个标识符为名。
# 行尾允许 = / ; / 空（调用方可能已剥离 = 或处于单行块中）
_DECL_TAIL = re.compile(
    r"^(?:typedef\s+)?(?:static\s+|const\s+|volatile\s+|unsigned\s+|signed\s+|"
    r"struct\s+\w+\s+|union\s+\w+\s+|enum\s+\w+\s+|"
    r"(?:u8|u16|u32|u64|s8|s16|s32|s64|bool|int|char|long|short|void|float|double|"
    r"size_t|ssize_t|loff_t|__le16|__le32|__le64|__be16|__be32|__be64)\s+|"
    r"[A-Za-z_]\w*\s+)*[\s\*]*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*"
    r"\s*[=;]?\s*$")
# 函数名：签名中第一个跟 "(" 的非关键字标识符
_FUNC_HEAD = re.compile(r"([A-Za-z_]\w*)\s*\(")
_TYPE_TAG = re.compile(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)")
# 函数指针变量声明：int (*name)(...)
_FNPTR_DECL = re.compile(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)")


def _ids(text: str, *, exclude: set[str] = set()) -> set[str]:
    return {m for m in _IDENT.findall(text)
            if m not in _KEYWORDS and len(m) >= 3 and m not in exclude}


def _clean_source(text: str) -> str:
    """单遍词法清洗：注释、字符串/字符字面量内容置空白（保留换行）。

    正确处理字符串与注释互相嵌套的边界：字符串里的 `/*` 不当注释起点
    （修 `pr_err("/****/")` 被注释剥离误伤），注释里的引号也不当字符串
    起点。字符串内容置空同时杜绝日志串产生幻影引用（如 "Link is Up"）。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif c == '"' or c == "'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if ch == quote:
                    out.append(quote)
                    i += 1
                    break
                out.append("\n" if ch == "\n" else " ")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _decl_name(head: str) -> str | None:
    m = _DECL_TAIL.match(head.strip())
    return m.group(1) if m else None


# 名字与 "=" 之间的内核注解词（如 copybreak __read_mostly = ...）
_TRAILING_ANNOS = re.compile(
    r"(?:\s+(?:__read_mostly|__ro_after_init|__cacheline_aligned|"
    r"__cacheline_aligned_in_smp|__section|__used|__force))+"
    r"\s*$")


def _decl_name_lhs(lhs: str) -> str | None:
    """声明左值取名：先剥 __attribute__((...)) 与尾部注解词。"""
    # 贪婪匹配：属性内部可含单层括号（如 aligned(8)）
    s = re.sub(r"__attribute__\s*\(\(.*\)\)", " ", lhs)
    s = _TRAILING_ANNOS.sub("", s)
    return _decl_name(s)


def _func_name(head: str) -> str | None:
    for m in _FUNC_HEAD.finditer(head):
        if m.group(1) not in _KEYWORDS and m.group(1) not in ("if", "while",
                                                              "for", "switch",
                                                              "sizeof"):
            return m.group(1)
    return None


def scan_file(path: Path) -> tuple[dict[str, int], set[str], set[str]]:
    """返回 (定义{符号: 1-based 行}, 引用集, 原型集)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    clean = _clean_source(text)
    lines = clean.splitlines()
    n = len(lines)
    defs: dict[str, int] = {}
    refs: set[str] = set()
    protos: set[str] = set()

    def add_def(name, line):
        if name and name not in _KEYWORDS:
            defs.setdefault(name, line)

    i = 0
    while i < n:
        line = lines[i]
        ls = line.strip()
        if not ls:
            i += 1
            continue

        if ls.startswith("#"):
            if ls.startswith("#include"):
                i += 1
                continue
            if ls.startswith("#define"):
                block = [ls]
                j = i + 1
                while j < n and block[-1].endswith("\\"):
                    block[-1] = block[-1][:-1]
                    block.append(lines[j].strip())
                    j += 1
                m = re.match(r"#define\s+([A-Za-z_]\w*)", block[0])
                if m:
                    name = m.group(1)
                    add_def(name, i + 1)
                    body = block[0][m.end():] + " " + " ".join(block[1:])
                    # 去掉函数式宏的形参表，避免参数名进引用
                    body = re.sub(r"\([^()]*\)", " ", body, count=1) \
                        if re.match(r"#define\s+[A-Za-z_]\w*\(", block[0]) else body
                    refs |= _ids(body, exclude={name})
                i = j
                continue
            i += 1
            continue

        # ---- 深度 0 语句：积累到 ';' 或 '{' ----
        parts: list[str] = []
        j = i
        kind = None            # 'semi' | 'brace'
        while j < n:
            parts.append(lines[j])
            s = lines[j].strip()
            if s.endswith("{") or (s.endswith(";") and "{" not in s):
                kind = "brace" if s.endswith("{") else "semi"
                j += 1
                break
            if "{" in s:      # 同行既开又可能的复杂行：按块处理
                kind = "brace"
                j += 1
                break
            j += 1
        head = "\n".join(parts)
        head_one = " ".join(p.strip() for p in parts)

        if kind == "semi":
            if head_one.startswith(("typedef",)):
                names = [t for t in _IDENT.findall(head_one)
                         if t not in _KEYWORDS]
                # typedef 末名（; 前最后一个）为定义名；struct 标签也入
                if names:
                    add_def(names[-1], i + 1)
                tm = _TYPE_TAG.search(head_one)
                if tm:
                    add_def(tm.group(1), i + 1)
            elif "(" in head_one:
                # 函数指针变量声明 `int (*cb)(...)` 的 `(*` 会让类型名
                # 误配 "标识符+(" —— 落在 (* 上的括号匹配不算函数名。
                fp_pos = head_one.find("(*")
                fname = None
                for m in _FUNC_HEAD.finditer(head_one):
                    if m.group(1) in _KEYWORDS:
                        continue
                    if fp_pos >= 0 and m.end() - 1 >= fp_pos:
                        continue
                    fname = m.group(1)
                    break
                if fname:
                    protos.add(fname)
                    # 原型参数中的类型名是真实引用（API 依赖）
                    refs |= _ids(head_one, exclude={fname})
                else:
                    fpm = _FNPTR_DECL.search(head_one)
                    if fpm:
                        add_def(fpm.group(1), i + 1)
                        if "=" in head_one:
                            rhs = head_one.split("=", 1)[1]
                            refs |= _ids(rhs, exclude={fpm.group(1)})
                    else:
                        dn = _decl_name(head_one)
                        add_def(dn, i + 1)
            else:
                # 裸前向声明（`struct X;`）不是定义；真定义在别处的块里。
                # 误记为定义会在跨模块所有权仲裁（.c 优先）中偷走归属。
                if re.match(r"(?:struct|union|enum)\s+[A-Za-z_]\w*\s*;$",
                            head_one):
                    i = j
                    continue
                # extern 是声明不是定义，同样防所有权偷走
                if head_one.startswith("extern ") or head_one == "extern":
                    i = j
                    continue
                if "=" in head_one:
                    # 变量初始化：左值取名（剥注解），右值产引用
                    lhs, _, rhs = head_one.partition("=")
                    dn = _decl_name_lhs(lhs)
                    add_def(dn, i + 1)
                    refs |= _ids(rhs, exclude={dn} if dn else set())
                else:
                    dn = _decl_name_lhs(head_one)
                    add_def(dn, i + 1)
            i = j
            continue

        if kind == "brace":
            # 块从开括号行（含）开始消费（单行/多行块统一处理）
            block_text, j2 = _consume_block(lines, i)
            # 分类块：初始化 / 类型块 / 函数定义
            # head 带 "=" 即变量初始化（含 `struct X y = {...}` 这种无
            # static/const 前缀的形态）；类型定义块的 head 不会带 "="。
            if "=" in head_one and not head_one.lstrip().startswith(
                    ("typedef",)):
                # 全局初始化（含 ops 表 designated initializer）
                lhs, _, rhs = head_one.partition("=")
                dn = _decl_name_lhs(lhs)
                add_def(dn, i + 1)
                refs |= _ids(rhs)
                refs |= _ids(block_text, exclude={dn} if dn else set())
                add_def(_block_tail_name(block_text), i + 1)
                i = j2
                continue
            tm = _TYPE_TAG.search(head_one)
            if tm and "(" not in head_one:
                tag = tm.group(1)
                add_def(tag, i + 1)
                is_enum = re.search(r"\benum\s+\w+", head_one) is not None
                if is_enum:
                    for s_ in _ids(block_text):
                        add_def(s_, i + 1)
                add_def(_block_tail_name(block_text), i + 1)
                i = j2
                continue
            # 匿名 typedef 类型块（`typedef enum {` / `typedef struct {`）：
            # 无标签，上面的 _TYPE_TAG 不命中。enum 值入 defs（与具名分支
            # 同语义——否则枚举值泄漏进 refs 污染外部符号面）；
            # struct/union 只取尾名（字段不产生任何集合条目）。
            if head_one.startswith(("typedef",)) and "(" not in head_one \
                    and re.match(r"typedef\s+(?:struct|union|enum)\s*\{?",
                                 head_one):
                if re.search(r"\benum\b", head_one):
                    for s_ in _ids(block_text):
                        add_def(s_, i + 1)
                add_def(_block_tail_name(block_text), i + 1)
                i = j2
                continue
            fname = _func_name(head_one)
            if fname:
                add_def(fname, i + 1)
                # 签名部分的类型名入引用（排除形参名近似：整个 head 减名字）
                refs |= _ids(head_one, exclude={fname})
                refs |= _ids(block_text, exclude={fname})
                add_def(_block_tail_name(block_text), i + 1)
                i = j2
                continue
            # 未识别的块（罕见）：内容按引用处理
            refs |= _ids(block_text)
            i = j2
            continue

        # 语句积累到文件尾未终止（异常容错）
        i = j

    return defs, refs, protos


def _consume_block(lines: list[str], opener_idx: int) -> tuple[str, int]:
    """从语句首行消费到块闭合（深度归零），返回 (块文本, 下一行索引)。

    从 opener_idx 起以 depth=0 计数：天然支持单行块（`enum X { .. };`
    同行开合）与多行块。必须等见过开括号后才允许 depth<=0 退出——
    多行函数签名的 `{` 不在首行，若按 depth<=0 立即退出会把签名续行
    （如 `struct e1000_hw *hw)`）泄漏回外层被重解析成假定义。
    """
    depth = 0
    seen_open = False
    j = opener_idx
    texts: list[str] = []
    while j < len(lines):
        l = lines[j]
        opened = l.count("{")
        depth += opened - l.count("}")
        if opened:
            seen_open = True
        texts.append(l)
        j += 1
        if seen_open and depth <= 0:
            break
    return "\n".join(texts), j


def _block_tail_name(block_text: str) -> str | None:
    """块末行若是 `} name;`（typedef 收尾），返回 name。"""
    last = block_text.strip().splitlines()[-1] if block_text.strip() else ""
    m = re.match(r"^\}\s*([A-Za-z_]\w*)\s*(?:[^\s;][^;]*)?;\s*$", last.strip())
    return m.group(1) if m else None


def scan_module_dir(mdir: Path) -> tuple[dict[str, list[str]], set[str]]:
    """模块目录聚合：({符号: [文件:行]}, 引用集)。原型不参与依赖。"""
    all_defs: dict[str, list[str]] = {}
    all_refs: set[str] = set()
    for f in sorted(mdir.glob("*")):
        if f.suffix not in (".c", ".h"):
            continue
        defs, refs, _protos = scan_file(f)
        for sym, ln in defs.items():
            all_defs.setdefault(sym, []).append(f"{f.name}:{ln}")
        all_refs |= refs
    return all_defs, all_refs
