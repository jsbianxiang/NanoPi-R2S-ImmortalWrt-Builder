#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shell/audit.py —— 仓库一致性自检（把「人工审计」里反复过期的检查项固化下来）

背景：2026-10-14 第 3 轮全量审计发现 9 个问题，其中 5 个是「注释里的数字/版本过期」。
根因是没有自动校验 —— 改了一处、忘了同步另外两处，或源侧数据变了而文档没跟。
本脚本把那批检查固化，可在本地与 CI 里重复运行。

用法：
    python3 shell/audit.py              # 离线检查（不需要网络）
    python3 shell/audit.py --online     # 追加在线检查（拉官方源 + dllkids 目录）
    python3 shell/audit.py --online --version 25.12.2

退出码：0 = 无 FAIL；1 = 有 FAIL
（在线检查里「网络不可达」只记为 WARN，不判 FAIL —— 免得源站抖动就变红；
  但源可达而数字对不上，判 FAIL。）

检查项：
  离线
   1. 三处代理互斥清单同步：build.sh `_PROXY_STACKS` ⇄ README「代理插件互斥」表
      ⇄ custom-packages.sh「注意事项 2」清单
   2. 代理栈数量 / marker 数量与各处声明的数字一致
   3. `_PROXY_OK` 白名单 ⇄ README「不算冲突」⇄ custom-packages.sh「不算冲突」
   4. 短名清单格式合法、条数可统计、`I18N_BUILTIN` 豁免项确实在清单里
   5. 工作流输入安全：`${{ inputs.* }}` 只允许出现在 `env:` 赋值处，不得直插 shell
   6. 工作流 `IN_*` 变量：shell 里用到的必须在 job `env:` 里定义
   7. 构建期写入工作区的路径，必须「已被 git 跟踪」或「已被 .gitignore 忽略」
   8. 换行符：全树 0 处 CRLF
   9. 语法：`bash -n` 全部 shell 脚本
  在线（--online）
  10. dllkids 源可达性 + 计数 vs 文档声明
  11. 官方 5 个 arch feed + target + kmods 总数 vs 声明（11428）
  12. `_PROXY_STACKS` 全部 marker 是否存在于源（黑名单无死条目）
  13. `_PROXY_OK` 全部项是否存在于源
  14. 短名清单的 `luci-app-*` / `luci-i18n-*` 是否存在于源
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BUILD_SH = os.path.join(ROOT, "rockchip", "build.sh")
CUSTOM_SH = os.path.join(ROOT, "shell", "custom-packages.sh")
README = os.path.join(ROOT, "README.md")
PACKAGES_MD = os.path.join(ROOT, "PACKAGES.md")
WORKFLOW = os.path.join(
    ROOT, ".github", "workflows", "build-rockchip-immortalWrt-25.12.x.yml"
)
SHELL_SCRIPTS = [
    "rockchip/build.sh",
    "shell/custom-packages.sh",
    "files/etc/uci-defaults/99-custom.sh",
]

DLLKIDS_URL = "https://down.dllkids.xyz/openwrt-feed/25.12/aarch64_generic/"
OFFICIAL_RELEASES = "https://downloads.immortalwrt.org/releases/{ver}"
ARCH = "aarch64_generic"
TARGET_PATH = "targets/rockchip/armv8"
OFFICIAL_FEEDS = ["base", "luci", "packages", "routing", "telephony"]

RESULTS: list[tuple[str, str, str]] = []  # (level, name, detail)


def ok(name: str, detail: str = "") -> None:
    RESULTS.append(("PASS", name, detail))


def fail(name: str, detail: str = "") -> None:
    RESULTS.append(("FAIL", name, detail))


def warn(name: str, detail: str = "") -> None:
    RESULTS.append(("WARN", name, detail))


def info(name: str, detail: str = "") -> None:
    RESULTS.append(("INFO", name, detail))


def check(cond: bool, name: str, good: str = "", bad: str = "") -> bool:
    (ok if cond else fail)(name, good if cond else bad)
    return cond


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", ROOT, *args], capture_output=True, text=True
    )


def tracked_or_ignored(path: str) -> tuple[bool, bool]:
    """返回 (是否被 git 跟踪, 是否被 .gitignore 忽略)。

    ⚠ `git check-ignore` 对**不存在**的路径会把它当成文件，
    于是 `.gitignore` 里 `/files/etc/apk/` 这种**尾斜杠（只匹配目录）**的模式匹配不上。
    这里改判 `path + "/__probe__"`（子路径），让 git 按目录语义匹配。
    """
    tracked = bool(git("ls-files", "--", path).stdout.strip())
    ignored = git("check-ignore", "-q", "--", path + "/__probe__").returncode == 0
    return tracked, ignored


def fetch(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "r2s-audit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
def parse_proxy_stacks(build_sh: str) -> list[tuple[str, list[str]]]:
    m = re.search(r'_PROXY_STACKS="\n(.*?)\n"', build_sh, re.S)
    if not m:
        return []
    stacks = []
    for line in m.group(1).strip().splitlines():
        name, _, markers = line.partition("|")
        stacks.append((name.strip(), markers.split()))
    return stacks


def parse_shell_var(text: str, var: str) -> list[str]:
    m = re.search(re.escape(var) + r'="(.*?)"\n', text, re.S)
    return m.group(1).split() if m else []


def parse_custom_proxy_list(cs: str) -> tuple[int | None, list[str]]:
    """custom-packages.sh 文件头「注意事项 2」的清单。"""
    lines = cs.splitlines()
    start = None
    declared = None
    for i, l in enumerate(lines):
        m = re.search(r"只能启用其中一个（共\s*(\d+)\s*套）", l)
        if m:
            declared = int(m.group(1))
            start = i + 1
            break
    if start is None:
        return None, []
    out: list[str] = []
    for l in lines[start:]:
        if "⚠ **判定真源是" in l:
            break
        if not l.strip().startswith("#"):
            continue
        for tok in re.split(r"[/\s]+", l.strip().lstrip("#").strip()):
            if re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", tok):
                out.append(tok)
    return declared, out


def parse_readme_proxy_table(rd: str) -> list[str]:
    """README「代理插件互斥」下那张表的标志包（含反引号的 token）。"""
    lines = rd.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.strip().startswith("|") and "代理栈" in l:
            start = i
            break
    if start is None:
        return []
    rows = []
    for l in lines[start:]:
        s = l.strip()
        if not s.startswith("|"):
            break
        if set(s) <= set("|-: "):
            continue
        rows.append(s)
    body = "\n".join(rows[1:])  # 去掉表头
    return re.findall(r"`([^`]+)`", body)


def parse_readme_ok_list(rd: str) -> list[str]:
    """README「不算冲突」小节里的 luci-app-* 全名。"""
    lines = rd.splitlines()
    start = None
    for i, l in enumerate(lines):
        if "不算冲突" in l and l.strip().startswith("**"):
            start = i  # 从标记行本身开始（列表可能就接在同一行）
            break
    if start is None:
        return []
    out: list[str] = []
    for l in lines[start:]:
        s = l.strip()
        if s.startswith(">") or s.startswith("---"):
            break
        out += re.findall(r"`(luci-app-[a-z0-9][a-z0-9.-]*)`", l)
    return out


def parse_custom_ok_list(cs: str) -> list[str]:
    """custom-packages.sh「✅ 不算冲突（可共存）」里的 luci-app-* 全名。"""
    lines = cs.splitlines()
    start = None
    for i, l in enumerate(lines):
        if "不算冲突（可共存）" in l:
            start = i  # 首个名字就接在标记行末尾（`...（可共存）：luci-app-xray-status / ...`）
            break
    if start is None:
        return []
    out: list[str] = []
    for l in lines[start:]:
        if "（LuCI 应用一律写" in l:
            break
        out += re.findall(r"(luci-app-[a-z0-9][a-z0-9.-]*)", l)
    return out


def parse_short_names(cs: str) -> list[str]:
    """「三、可用短名清单」里的全部短名（去重、保持顺序）。

    只接受「整行 token 全部形如包名」的行 —— 这样分类标题
    （`# --- 代理 / VPN / DNS 分流 ---`）和区块标题（含中文与 `25.12`）会被跳过。
    ⚠ 首字符允许数字（`3cat` / `3ginfo-lite` 是真实包名），但排除纯数字/版本号
    （`25.12` 这类），否则标题行会被误收。
    """
    lines = cs.splitlines()
    start = None
    for i, l in enumerate(lines):
        if "三、可用短名清单" in l:
            start = i + 1
            break
    if start is None:
        return []

    def is_name(tok: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", tok)) and not re.fullmatch(
            r"[0-9.]+", tok
        )

    names: list[str] = []
    for l in lines[start:]:
        s = l.strip()
        if not s.startswith("#"):
            continue
        body = s.lstrip("#").strip()
        if "★" in body:
            body = body.split("★")[0]
        toks = body.split()
        if not toks or not all(is_name(t) for t in toks):
            continue
        for t in toks:
            if t not in names:
                names.append(t)
    return names


def declared_number(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 离线检查
# ---------------------------------------------------------------------------
def offline_checks(version: str) -> None:
    build_sh = read(BUILD_SH)
    custom_sh = read(CUSTOM_SH)
    rd = read(README)
    pk_md = read(PACKAGES_MD)
    wf = read(WORKFLOW)

    stacks = parse_proxy_stacks(build_sh)
    markers = [m for _, mk in stacks for m in mk]
    custom_declared, custom_list = parse_custom_proxy_list(custom_sh)
    readme_markers = parse_readme_proxy_table(rd)

    # --- 1/2. 三处代理清单同步 -------------------------------------------
    if not stacks:
        fail("三处代理清单同步", "build.sh 里解析不到 _PROXY_STACKS")
    else:
        s1, s2 = set(n for n, _ in stacks), set(custom_list)
        same = s1 == s2
        detail = f"{len(stacks)} 套 / {len(markers)} marker"
        if same:
            ok("三处清单：build.sh ⇄ custom-packages.sh", f"{detail}，零差集")
        else:
            fail(
                "三处清单：build.sh ⇄ custom-packages.sh",
                f"{detail}；差集 A-B={sorted(s1 - s2)} B-A={sorted(s2 - s1)}",
            )

        if custom_declared is None:
            fail("custom-packages.sh 声明的套数", "解析不到「共 N 套」")
        else:
            check(
                custom_declared == len(stacks),
                "custom-packages.sh 声明的套数",
                f"声明 {custom_declared} 套 = 实测 {len(stacks)} 套",
                f"声明 {custom_declared} 套 ≠ 实测 {len(stacks)} 套",
            )

        missing = [m for m in markers if m not in " ".join(readme_markers)]
        check(
            not missing,
            "README 表覆盖全部 marker",
            f"{len(markers)}/{len(markers)} marker 已覆盖（表 {len(readme_markers)} 项）",
            f"README 表缺 marker：{missing}",
        )

        # README 表里出现的、但不在 build.sh 表内的 marker（可能是过期条目）
        extra = [
            m
            for m in readme_markers
            if m not in markers and re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", m)
        ]
        if extra:
            warn("README 表有多余条目", f"不在 _PROXY_STACKS 里：{extra}")
        else:
            ok("README 表无多余条目", "表中每个标志包都在 _PROXY_STACKS 内")

    # --- 3. 白名单三处一致 ----------------------------------------------
    proxy_ok = parse_shell_var(build_sh, "_PROXY_OK")
    rd_ok = sorted(set(parse_readme_ok_list(rd)))
    cs_ok = sorted(set(parse_custom_ok_list(custom_sh)))
    a, b, c = set(proxy_ok), set(rd_ok), set(cs_ok)
    if a == b == c:
        ok(
            "_PROXY_OK 白名单三处一致",
            f"{len(proxy_ok)} 项：build.sh ⇄ README「不算冲突」⇄ custom-packages.sh",
        )
    else:
        fail(
            "_PROXY_OK 白名单三处一致",
            f"build.sh={len(a)} / README={len(b)} / custom={len(c)}；"
            f"差集 build-README={sorted(a - b)} README-build={sorted(b - a)} "
            f"build-custom={sorted(a - c)}",
        )

    # --- 4. 短名清单 ------------------------------------------------------
    shorts = parse_short_names(custom_sh)
    i18n_builtin = parse_shell_var(custom_sh, "I18N_BUILTIN")
    check(
        len(shorts) > 0,
        "短名清单可解析",
        f"{len(shorts)} 个短名",
        "解析不到「三、可用短名清单」里的短名",
    )
    bad_fmt = [n for n in shorts if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", n)]
    check(not bad_fmt, "短名格式合法", "全部合法", f"非法短名：{bad_fmt}")
    # I18N_BUILTIN 的语义是「这些 app 包自带中文，别再装同名 i18n」。
    # 若某个名字不在短名清单里，这条豁免就无从触发（属前向兼容，不是错误）⇒ 只提示。
    not_in = [n for n in i18n_builtin if n not in shorts]
    if not_in:
        info(
            "I18N_BUILTIN 有清单外的项",
            f"{not_in} 不在「三」的短名清单里 ⇒ 这两条豁免当前不生效"
            f"（清单内的是 {[n for n in i18n_builtin if n in shorts]}）",
        )
    else:
        ok("I18N_BUILTIN 豁免项均在短名清单内", f"{i18n_builtin}")

    # --- 5/6. 工作流输入安全与变量联动 ------------------------------------
    bad_inject = []
    for n, l in enumerate(wf.splitlines(), 1):
        if "${{ inputs." not in l:
            continue
        s = l.strip()
        if s.startswith("#"):
            continue
        if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*:\s*\$\{\{\s*inputs\.[A-Za-z0-9_.]+\s*\}\}", s
        ):
            continue  # 合法：env: KEY: ${{ inputs.x }}
        bad_inject.append(f"L{n}: {s}")
    check(
        not bad_inject,
        "工作流输入不直插 shell",
        "全部 ${{ inputs.* }} 均在 env 赋值处",
        "发现直插 shell 的输入：" + " | ".join(bad_inject),
    )

    defined = set(re.findall(r"^\s*(IN_[A-Z0-9_]+):\s*\$\{\{\s*inputs\.", wf, re.M))
    used = set(re.findall(r"\$\{?(IN_[A-Z0-9_]+)", wf))
    undefined = sorted(used - defined)
    check(
        not undefined,
        "工作流 IN_* 变量已定义",
        f"定义 {len(defined)} 个、使用 {len(used)} 个",
        f"shell 里用到但未在 env 定义：{undefined}",
    )
    unused = sorted(defined - used)
    if unused:
        warn("工作流 IN_* 有未使用项", f"{unused}")

    # --- 7. 构建期写入路径必须「已跟踪」或「已忽略」 ----------------------
    candidates: set[str] = set()
    for txt in (build_sh, wf):
        for m in re.finditer(
            r"(?<![\w/$-])files/((?:[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+)", txt
        ):
            candidates.add("files/" + m.group(1))
        for m in re.finditer(r"github\.workspace \}/([A-Za-z0-9._-]+)", txt):
            candidates.add(m.group(1))
    unhandled = []
    for p in sorted(candidates):
        tracked, ignored = tracked_or_ignored(p)
        if not (tracked or ignored):
            unhandled.append(p)
    check(
        not unhandled,
        "构建期写入路径已跟踪或已忽略",
        f"{len(candidates)} 条路径全部有归宿",
        f"既未跟踪也未忽略（会脏工作区）：{unhandled}",
    )

    # --- 8. 换行符 --------------------------------------------------------
    tracked_files = [f for f in git("ls-files").stdout.splitlines() if f]
    crlf = []
    for f in tracked_files:
        p = os.path.join(ROOT, f)
        try:
            with open(p, "rb") as fh:
                if b"\r\n" in fh.read():
                    crlf.append(f)
        except OSError:
            continue
    check(not crlf, "换行符全为 LF", f"{len(tracked_files)} 个受控文件 0 处 CRLF", f"含 CRLF：{crlf}")

    # --- 9. 语法 ----------------------------------------------------------
    syntax_bad = []
    for s in SHELL_SCRIPTS:
        r = subprocess.run(["bash", "-n", os.path.join(ROOT, s)], capture_output=True, text=True)
        if r.returncode != 0:
            syntax_bad.append(f"{s}: {r.stderr.strip().splitlines()[:1]}")
    check(not syntax_bad, "shell 脚本语法", f"bash -n ×{len(SHELL_SCRIPTS)} 全 OK", f"{syntax_bad}")

    # --- 附带：文档数字声明（离线只能列出，供 --online 比对） -------------
    decl = {
        "dllkids_luci_app": declared_number(
            rd, r"(\d+)\s*个\s*`?luci-app-\*"
        ),
        "dllkids_total": declared_number(pk_md, r"源内\s*(\d+)\s*个包"),
        "official_total": declared_number(custom_sh, r"共\s*(\d+)\s*个包"),
    }
    info(
        "文档声明的数字",
        f"dllkids luci-app-*={decl['dllkids_luci_app']}、dllkids 总包={decl['dllkids_total']}、"
        f"官方源合计={decl['official_total']}（--online 时逐项实测比对）",
    )
    return decl


# ---------------------------------------------------------------------------
# 在线检查
# ---------------------------------------------------------------------------
def dllkids_names() -> tuple[list[str], set[str]]:
    html = fetch(DLLKIDS_URL)
    apks = sorted(set(re.findall(r'href="[^"]*?/([^/"?#]+\.apk)"', html)))
    names: set[str] = set()
    for s in apks:
        b = s[:-4]
        b = re.sub(r"-r\d+$", "", b)
        b = re.sub(r"-\d[^-]*$", "", b)  # 版本号可能含 ~（27.284.54144~206653b）
        names.add(b)
    return apks, names


def official_names(version: str) -> tuple[set[str], dict[str, int]]:
    base = OFFICIAL_RELEASES.format(ver=version)
    names: set[str] = set()
    counts: dict[str, int] = {}
    for f in OFFICIAL_FEEDS:
        data = json.loads(fetch(f"{base}/packages/{ARCH}/{f}/index.json"))
        pk = set(data.get("packages", {}).keys())
        counts[f] = len(pk)
        names |= pk
    html = fetch(f"{base}/{TARGET_PATH}/packages/")
    tgt = set(x[:-4] for x in re.findall(r'href="([^"]+\.apk)"', html))
    counts["target"] = len(tgt)
    names |= tgt
    html = fetch(f"{base}/{TARGET_PATH}/kmods/")
    subs = [x for x in re.findall(r'href="([^"/]+)/"', html)]
    if subs:
        data = json.loads(fetch(f"{base}/{TARGET_PATH}/kmods/{subs[0]}/index.json"))
        km = set(data.get("packages", {}).keys())
        counts["kmods"] = len(km)
        names |= km
    return names, counts


def online_checks(version: str, decl: dict) -> None:
    build_sh = read(BUILD_SH)
    custom_sh = read(CUSTOM_SH)
    stacks = parse_proxy_stacks(build_sh)
    markers = [m for _, mk in stacks for m in mk]
    proxy_ok = parse_shell_var(build_sh, "_PROXY_OK")
    shorts = parse_short_names(custom_sh)
    i18n_builtin = parse_shell_var(custom_sh, "I18N_BUILTIN")

    # --- 10. dllkids ------------------------------------------------------
    dll: set[str] = set()
    try:
        apks, dll = dllkids_names()
        n_luci = len([n for n in dll if n.startswith("luci-app-")])
        n_i18n = len([n for n in dll if n.startswith("luci-i18n-")])
        ok(
            "dllkids 源可达",
            f"{len(apks)} 个 .apk / {len(dll)} 唯一名 / {n_luci} 个 luci-app-* / {n_i18n} 个 luci-i18n-*",
        )
        d1 = decl.get("dllkids_luci_app")
        if d1 is None:
            warn("dllkids luci-app-* 数字声明", "文档里找不到声明，无法比对")
        else:
            check(
                d1 == n_luci,
                "dllkids luci-app-* 数字声明",
                f"文档 {d1} = 实测 {n_luci}",
                f"文档写 {d1}，实测 {n_luci} ⇒ 文档过期",
            )
        d2 = decl.get("dllkids_total")
        if d2 is None:
            warn("dllkids 总包数声明", "文档里找不到声明，无法比对")
        else:
            check(
                d2 == len(dll),
                "dllkids 总包数声明",
                f"文档 {d2} = 实测 {len(dll)}",
                f"文档写 {d2}，实测 {len(dll)} ⇒ 文档过期",
            )
    except Exception as e:  # noqa: BLE001
        warn("dllkids 源可达", f"网络不可达或不稳定，跳过相关检查：{type(e).__name__}: {e}")

    # --- 11. 官方源 -------------------------------------------------------
    off: set[str] = set()
    try:
        off, counts = official_names(version)
        total = sum(counts.values())
        ok(
            f"官方源可达（{version}）",
            " + ".join(f"{k} {v}" for k, v in counts.items()) + f" = {total}",
        )
        d3 = decl.get("official_total")
        if d3 is None:
            warn("官方源包总数声明", "文档里找不到声明，无法比对")
        else:
            check(
                d3 == total,
                "官方源包总数声明",
                f"文档 {d3} = 实测 {total}",
                f"文档写 {d3}，实测 {total} ⇒ 文档过期（或该版本源结构变了）",
            )
    except Exception as e:  # noqa: BLE001
        warn(f"官方源可达（{version}）", f"网络不可达或不稳定，跳过：{type(e).__name__}: {e}")

    if not (dll or off):
        warn("源侧存在性检查", "两个源都没取到，跳过 marker / 短名核验")
        return

    allpkgs = dll | off

    # --- 12/13. marker 与白名单存在性 ------------------------------------
    miss_m = [(n, m) for n, mk in stacks for m in mk if m not in allpkgs]
    check(
        not miss_m,
        "_PROXY_STACKS marker 均存在",
        f"{len(markers)}/{len(markers)} marker 存在于源（黑名单无死条目）",
        f"源里找不到：{miss_m}",
    )
    miss_ok = [x for x in proxy_ok if x not in allpkgs]
    check(
        not miss_ok,
        "_PROXY_OK 白名单项均存在",
        f"{len(proxy_ok)}/{len(proxy_ok)} 项存在于源",
        f"源里找不到：{miss_ok}",
    )

    # --- 14. 短名可用性 ---------------------------------------------------
    miss_app, miss_i18n = [], []
    for n in shorts:
        if f"luci-app-{n}" not in allpkgs:
            miss_app.append(n)
        if n not in i18n_builtin and f"luci-i18n-{n}-zh-cn" not in allpkgs:
            miss_i18n.append(n)
    check(
        not miss_app,
        "短名清单：luci-app-* 均可用",
        f"{len(shorts)}/{len(shorts)} 个 luci-app-<短名> 存在于源",
        f"源里没有 luci-app- 前端：{miss_app}",
    )
    check(
        not miss_i18n,
        "短名清单：luci-i18n-* 均可用",
        f"除 I18N_BUILTIN（{i18n_builtin}）外全部存在独立中文包",
        f"缺 luci-i18n-<短名>-zh-cn：{miss_i18n}",
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def read_luci_version() -> str:
    wf = read(WORKFLOW)
    m = re.search(r"luci_version:.*?default:\s*\"?([0-9.]+)\"?", wf, re.S)
    return m.group(1) if m else "25.12.2"


def main() -> int:
    ap = argparse.ArgumentParser(description="NanoPi-R2S-ImmortalWrt-Builder 一致性自检")
    ap.add_argument("--online", action="store_true", help="追加在线检查（需网络）")
    ap.add_argument("--version", default=None, help="官方源版本，默认取 workflow 里 luci_version 的 default")
    args = ap.parse_args()

    version = args.version or read_luci_version()

    print("=" * 72)
    print("R2S ImmortalWrt 构建仓库 · 一致性自检")
    print(f"仓库：{ROOT}")
    print(f"在线检查：{'开' if args.online else '关'}   官方源版本：{version}")
    print("=" * 72)

    print("\n--- 离线检查 ---")
    decl = offline_checks(version)

    if args.online:
        print("\n--- 在线检查 ---")
        online_checks(version, decl)

    print("\n--- 汇总 ---")
    order = {"FAIL": 0, "WARN": 1, "INFO": 2, "PASS": 3}
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "INFO": "ℹ️ "}
    for level, name, detail in sorted(RESULTS, key=lambda r: order[r[0]]):
        print(f"{icon[level]} {name}" + (f"  —— {detail}" if detail else ""))

    n_fail = sum(1 for r in RESULTS if r[0] == "FAIL")
    n_warn = sum(1 for r in RESULTS if r[0] == "WARN")
    n_pass = sum(1 for r in RESULTS if r[0] == "PASS")
    print(f"\nPASS {n_pass} / WARN {n_warn} / FAIL {n_fail}")
    if n_fail:
        print("❌ 存在 FAIL，请按上面的提示修正（多为文档/注释与实测不符）。")
        return 1
    print("✅ 全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
