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
  14b. 短名清单是否**按官方源**可解析（custom-packages.sh「注意事项 5」的承诺：
      官方源没有的前端一律剔除，不走第三方源兜底）
  15. **PACKAGES 依赖闭包**：用 bash 真实复现 build.sh 的 PACKAGES 拼接，BFS 解依赖
      闭包（读 `.apk` 前 32 KB 的 ADB 元数据），检查
        a) 显式包全部可解析；
        b) 闭包里是否冒出**第二个代理栈的前端**（FAIL）；
        c) 闭包里的虚拟名是否被**另一套栈**提供（WARN —— apk 择优安装风险）。
      ⚠ 仅在 `--online` 下运行，耗时约 1–3 分钟（有 500 包 / 420s 预算上限）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
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

# 检查 15（依赖闭包）的配额：抓元数据只用 .apk 的**前 32 KB**（元数据在流最前端），
# 但包数/时间仍要设上限，免得源站被扫爆或 CI 跑不完。
CLOSURE_PREFIX_B = 32 * 1024
CLOSURE_MAX_PKGS = 500
CLOSURE_BUDGET_S = 420
CLOSURE_WORKERS = 8

RESULTS: list[tuple[str, str, str]] = []  # (level, name, detail)

# shell/apkmeta.py 的运行时句柄（由 check_dependency_closure 惰性加载）
apkmeta = None


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


def check_existence(cond: bool, name: str, good: str, bad: str,
                    complete: bool) -> bool:
    """在线存在性检查：**两个源都取到了**才判 FAIL，否则只 WARN。

    依据文档约定「在线『源站不可达』只 WARN 不 FAIL」。若只取到一个源，
    缺项很可能住在**没取到的那个源**里 —— 例如 `tinyproxy` /
    `https-dns-proxy` 在官方 luci feed、不在 dllkids；此时判 FAIL 就是误报
    （2026-10-14 实测踩过：官方源一次 TLS EOF 就让这两个白名单项变红）。
    """
    if cond:
        ok(name, good)
    elif complete:
        fail(name, bad)
    else:
        warn(name, f"{bad}（⚠️ 只取到了一个源，可能是漏抓而非真的不存在）")
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


# 单次运行内按 URL 记忆抓取结果：同一目录列表在一次审计里会被多个检查用到
# （DLLKIDS_URL 被检查 10 的计数与 apk_catalog 的目录各抓一次；官方源的
# index.json / 目录页也被 official_names 与 apk_catalog 各抓一次）。源站抖动时
# 不缓存会让重试预算翻倍烧掉 —— 最坏情况（4 次重试 + http 回退 ≈ 8 分钟/URL）
# 两次串行就能顶爆 job 超时，「源站不可达只 WARN」的设计随之失效。
_FETCH_CACHE: dict[str, str] = {}


def fetch_retry(url: str, timeout: int = 60, tries: int = 4) -> str:
    """带重试的 fetch（同一次运行内同一 URL 只真抓一次，见 _FETCH_CACHE）。"""
    if url in _FETCH_CACHE:
        return _FETCH_CACHE[url]
    html = _fetch_retry(url, timeout, tries)
    _FETCH_CACHE[url] = html
    return html


def _fetch_retry(url: str, timeout: int = 60, tries: int = 4) -> str:
    """带重试的 fetch。

    ⚠ 本机代理会偶发 `UNEXPECTED_EOF_WHILE_READING` / 读超时（不是站点挂了）⇒
      重试；仍失败则退回 `http://` 同路径（dllkids / immortalwrt 的 80 端口
      实测能给到同样内容）。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            return fetch(url, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    if url.startswith("https://"):
        try:
            return fetch("http://" + url[8:], timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
    raise last if last else OSError(f"fetch 失败：{url}")


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
    html = fetch_retry(DLLKIDS_URL)
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
        data = json.loads(fetch_retry(f"{base}/packages/{ARCH}/{f}/index.json"))
        pk = set(data.get("packages", {}).keys())
        counts[f] = len(pk)
        names |= pk
    html = fetch_retry(f"{base}/{TARGET_PATH}/packages/")
    tgt = set(x[:-4] for x in re.findall(r'href="([^"]+\.apk)"', html))
    counts["target"] = len(tgt)
    names |= tgt
    html = fetch_retry(f"{base}/{TARGET_PATH}/kmods/")
    subs = [x for x in re.findall(r'href="([^"/]+)/"', html)]
    if subs:
        data = json.loads(fetch_retry(f"{base}/{TARGET_PATH}/kmods/{subs[0]}/index.json"))
        km = set(data.get("packages", {}).keys())
        counts["kmods"] = len(km)
        names |= km
    return names, counts


# ---------------------------------------------------------------------------
# 检查 15：PACKAGES 依赖闭包 & 第二个代理前端
#
# 背景：2026-10-14 产物级解包发现一个 **构建期硬校验看不见的盲区** ——
# `nikki` 的 `D:` 里有虚拟名 `mihomo`，而 `clashoo` 的 `p:` 声明提供
# `mihomo=<更高版本>` ⇒ apk 择优装上了 clashoo（虽然 `luci-app-clashoo`
# 前端并未启用，显式 PACKAGES 里也没有它）。显式清单的互斥校验看不出来，
# 只有把**依赖闭包**解出来才看得见。
#
# 因此本检查做三件事：
#   15a 用 bash 真实复现 build.sh 的 PACKAGES 拼接，BFS 解依赖闭包
#   15b 闭包里是否出现**第二个代理栈的前端**（⇒ FAIL）
#   15c 闭包里出现的虚拟名是否被**另一套栈**提供（⇒ WARN，择优安装风险）
# ---------------------------------------------------------------------------
def _dedup_keep_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def explicit_packages() -> list[str]:
    """用 bash 真实复现 build.sh 的 `PACKAGES` 拼接（不自己解析 shell）。

    build.sh 里 `PACKAGES="$PACKAGES …"` 的静态行 + `source
    shell/custom-packages.sh` 展开出的 `$CUSTOM_PACKAGES`（含短名 → luci-app-*
    + luci-i18n-*-zh-cn 的展开、I18N_BUILTIN 豁免）—— 逻辑全在 shell 里，
    最可靠的办法是真跑一遍，只取结果。
    """
    script = (
        'set -e\n'
        'cd "$1"\n'
        'CUSTOM_PACKAGES=""\n'
        '. ./shell/custom-packages.sh\n'
        'PACKAGES=""\n'
        'eval "$(grep -h \'^PACKAGES=\' rockchip/build.sh | grep -v CUSTOM_PACKAGES)"\n'
        'printf \'%s\\n\' "$PACKAGES $CUSTOM_PACKAGES"\n'
    )
    out = subprocess.run(["bash", "-c", script, "bash", ROOT],
                         capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError("bash 求 PACKAGES 失败：" + out.stderr.strip()[:200])
    return _dedup_keep_order(out.stdout.split())


def _apk_files(html: str) -> list[str]:
    """目录列表页里的 .apk 文件名（有的站 href 带路径，有的不带）。"""
    return (re.findall(r'href="[^"]*?/([^/"?#]+\.apk)"', html)
            or re.findall(r'href="([^"]+\.apk)"', html))


def _strip_apk_version(fn: str) -> str:
    """`luci-app-x-26.236.50544~cb5d434.apk` → `luci-app-x`。

    ⚠ 版本号可能含 `~`（27.284.54144~206653b），也可能以 `-r<n>` 收尾
      （kmod-tun-6.12.103-r1）⇒ 必须先剥 `-r\\d+$` 再剥最后一个 `-\\d…`。
    """
    b = fn[:-4] if fn.endswith(".apk") else fn
    b = re.sub(r"-r\d+$", "", b)
    b = re.sub(r"-\d[^-]*$", "", b)
    return b


def apk_catalog(version: str) -> dict[str, str]:
    """8 个源（5 个 arch feed + target + kmods + dllkids）的 name → .apk URL。

    同名包**让 dllkids 覆盖官方**：apk 取版本高者，实测 150 个同名包里装的
    多是 dllkids 构建（README「150 同名包」）。
    """
    base = OFFICIAL_RELEASES.format(ver=version)
    cat: dict[str, str] = {}
    for f in OFFICIAL_FEEDS:
        for fn in _apk_files(fetch_retry(f"{base}/packages/{ARCH}/{f}/")):
            cat.setdefault(_strip_apk_version(fn),
                           f"{base}/packages/{ARCH}/{f}/{fn}")
    for fn in _apk_files(fetch_retry(f"{base}/{TARGET_PATH}/packages/")):
        cat.setdefault(_strip_apk_version(fn),
                       f"{base}/{TARGET_PATH}/packages/{fn}")
    for sub in re.findall(r'href="([^"/]+)/"',
                          fetch_retry(f"{base}/{TARGET_PATH}/kmods/")):
        for fn in _apk_files(fetch_retry(f"{base}/{TARGET_PATH}/kmods/{sub}/")):
            cat.setdefault(_strip_apk_version(fn),
                           f"{base}/{TARGET_PATH}/kmods/{sub}/{fn}")
    for fn in _apk_files(fetch_retry(DLLKIDS_URL)):
        cat[_strip_apk_version(fn)] = f"{DLLKIDS_URL}{fn}"
    return cat


def resolve_closure(explicit: list[str], cat: dict[str, str],
                    deadline: float) -> tuple[dict[str, dict], dict[str, str], bool]:
    """BFS 解依赖闭包。

    只沿 `D:`（依赖）展开 —— 那才是「装这个包还会装什么」。`p:`（provides）
    留到 15c 用来判断「另一个包是否也提供这个虚拟名」。
    返回 (已取到元数据的包, 未解析的名字及原因, 是否被上限/预算截断)。
    """
    meta: dict[str, dict] = {}
    unresolved: dict[str, str] = {}
    queue = list(explicit)
    seen = set(queue)
    truncated = False
    while queue and len(meta) < CLOSURE_MAX_PKGS:
        if time.monotonic() > deadline:
            truncated = True
            break
        batch: dict[str, str] = {}
        for name in queue:
            if name in meta or name in unresolved:
                continue
            url = cat.get(name)
            if url is None:
                unresolved[name] = "8 个源里都没有同名包，也没找到 provides 者"
                continue
            batch[name] = url
        if batch:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=CLOSURE_WORKERS) as ex:
                futs = {ex.submit(apkmeta.fetch_meta, u, CLOSURE_PREFIX_B): n
                        for n, u in batch.items()}
                for fu in concurrent.futures.as_completed(futs):
                    n = futs[fu]
                    try:
                        meta[n] = fu.result()
                    except Exception as e:  # noqa: BLE001
                        unresolved[n] = f"{type(e).__name__}: {e}"
        nxt: list[str] = []
        for name in queue:
            m = meta.get(name)
            if m is None:
                continue
            for child in m["D"]:
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        queue = nxt
    return meta, unresolved, truncated


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
    both = bool(dll and off)          # 两个源都取到了，「不存在」才是可信结论
    if not both:
        warn("源侧数据不完整",
             f"dllkids {'✓' if dll else '✗（网络抖动）'} / "
             f"官方源 {'✓' if off else '✗（网络抖动）'} "
             "⇒ 下面的存在性检查只在源不完整时降级为 WARN")

    # --- 12/13. marker 与白名单存在性 ------------------------------------
    miss_m = [(n, m) for n, mk in stacks for m in mk if m not in allpkgs]
    check_existence(
        not miss_m,
        "_PROXY_STACKS marker 均存在",
        f"{len(markers)}/{len(markers)} marker 存在于源（黑名单无死条目）",
        f"源里找不到：{miss_m}", both,
    )
    miss_ok = [x for x in proxy_ok if x not in allpkgs]
    check_existence(
        not miss_ok,
        "_PROXY_OK 白名单项均存在",
        f"{len(proxy_ok)}/{len(proxy_ok)} 项存在于源",
        f"源里找不到：{miss_ok}", both,
    )

    # --- 14. 短名可用性 ---------------------------------------------------
    miss_app, miss_i18n = [], []
    for n in shorts:
        if f"luci-app-{n}" not in allpkgs:
            miss_app.append(n)
        if n not in i18n_builtin and f"luci-i18n-{n}-zh-cn" not in allpkgs:
            miss_i18n.append(n)
    check_existence(
        not miss_app,
        "短名清单：luci-app-* 均可用",
        f"{len(shorts)}/{len(shorts)} 个 luci-app-<短名> 存在于源",
        f"源里没有 luci-app- 前端：{miss_app}", both,
    )
    check_existence(
        not miss_i18n,
        "短名清单：luci-i18n-* 均可用",
        f"除 I18N_BUILTIN（{i18n_builtin}）外全部存在独立中文包",
        f"缺 luci-i18n-<短名>-zh-cn：{miss_i18n}", both,
    )

    # --- 14b. 短名清单的「官方源承诺」 ------------------------------------
    # custom-packages.sh「注意事项 5」承诺：短名清单**按官方源**逐一校验，官方源
    # 没有的前端全部剔除（haproxy-tcp / nft-qos / nikki 在 dllkids 有 luci-app-
    # 前端，故不列入，要走「一、第三方插件」受代理互斥约束）。上面的 14 用的是
    # 8 源合并目录，dllkids 会兜底放过「只在第三方源有」的短名 —— 这里单独按
    # 官方源再核一遍，守住承诺。
    # ⚠ 本检查只依赖官方源数据：off 取到了即可判 FAIL，dllkids 在否不影响结论
    #   （承诺的对象就是官方源，不存在「住在没取到的源里」的辩解）。
    if off:
        miss_off_app = [n for n in shorts if f"luci-app-{n}" not in off]
        miss_off_i18n = [
            n for n in shorts
            if n not in i18n_builtin and f"luci-i18n-{n}-zh-cn" not in off
        ]
        if not miss_off_app and not miss_off_i18n:
            ok(
                "短名清单：官方源承诺核验",
                f"{len(shorts)}/{len(shorts)} 个短名在官方源（{len(off)} 包）"
                "可解析 app / i18n ⇒「按官方源校验」承诺成立",
            )
        else:
            fail(
                "短名清单：官方源承诺核验",
                f"仅第三方源可解析 ⇒ 违背承诺：app 缺 {miss_off_app}；"
                f"i18n 缺 {miss_off_i18n}",
            )

    # --- 15. 依赖闭包 & 第二个代理前端 -----------------------------------
    check_dependency_closure(version, stacks, explicit_hint=None)


def _load_apkmeta():
    """把 shell/ 加进 sys.path 后导入 apkmeta（与 audit.py 同目录）。"""
    import importlib.util  # noqa: PLC0415

    path = os.path.join(ROOT, "shell", "apkmeta.py")
    spec = importlib.util.spec_from_file_location("apkmeta", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_dependency_closure(version: str, stacks, explicit_hint) -> None:
    """解 PACKAGES 依赖闭包，检查有没有第二个代理前端/跨栈提供者。"""
    global apkmeta  # noqa: PLW0603
    try:
        apkmeta = _load_apkmeta()
    except Exception as e:  # noqa: BLE001
        warn("依赖闭包解析", f"导入 shell/apkmeta.py 失败：{type(e).__name__}: {e}")
        return

    # --- 15a 显式 PACKAGES +依赖闭包 -----------------------------------
    try:
        explicit = explicit_packages()
    except Exception as e:  # noqa: BLE001
        warn("显式 PACKAGES 求值", f"{type(e).__name__}: {e}")
        return
    info("显式 PACKAGES", f"{len(explicit)} 个：{' '.join(explicit)}")

    try:
        cat = apk_catalog(version)
    except Exception as e:  # noqa: BLE001
        warn("源目录（8 个 feed）", f"网络不可达或不稳定，跳过闭包检查：{e}")
        return
    ok("源目录（8 个 feed）", f"{len(cat)} 个包名可解析到 .apk URL")

    t0 = time.monotonic()
    try:
        meta, unresolved, truncated = resolve_closure(
            explicit, cat, time.monotonic() + CLOSURE_BUDGET_S)
    except Exception as e:  # noqa: BLE001
        warn("依赖闭包解析", f"{type(e).__name__}: {e}")
        return
    took = time.monotonic() - t0

    miss = [p for p in explicit if p not in meta]
    check(
        not miss,
        "依赖闭包：显式 PACKAGES 全部可解析",
        f"闭包 {len(meta)} 个包 / {len(unresolved)} 个未解析虚拟名"
        f"（{took:.0f}s，只用 .apk 前 {CLOSURE_PREFIX_B // 1024} KB）",
        f"这些显式包取不到元数据：{miss}",
    )
    if truncated:
        warn("依赖闭包被截断",
             f"达到 {CLOSURE_MAX_PKGS} 包或 {CLOSURE_BUDGET_S}s 预算，闭包可能不完整")
    if unresolved:
        info("依赖闭包里未解析的虚拟名",
             f"{len(unresolved)} 个（正常：多为 `<包>-<arch>` 形式的自提供）"
             + ("；其中含显式包：" + str(miss) if miss else ""))

    # --- 15b 第二个代理前端 ----------------------------------------------
    markers: dict[str, tuple[str, list[str]]] = {}
    for nm, mk in stacks:
        for p in mk:
            markers[p] = (nm, mk)
    intended = {nm for nm, mk in stacks if any(p in explicit for p in mk)}
    found: dict[str, list[str]] = {}
    for p in meta:
        if p in markers:
            found.setdefault(markers[p][0], []).append(p)

    extra_front: dict[str, list[str]] = {}
    extra_core: dict[str, list[str]] = {}
    for nm, pkgs in sorted(found.items()):
        if nm in intended:
            continue
        front = sorted(x for x in pkgs if x.startswith("luci-app-"))
        core = sorted(x for x in pkgs if not x.startswith("luci-app-"))
        if front:
            extra_front[nm] = front
        if core:
            extra_core[nm] = core

    if not intended:
        warn("依赖闭包里的代理栈", "显式 PACKAGES 一个代理栈都没启用"
                                  "（`ALLOW_PROXY_CONFLICT` 场景？），跳过栈归属判定")
    check(
        not extra_front,
        "依赖闭包里没有第二个代理前端",
        "闭包含 " + "/".join(f"{nm}:{','.join(sorted(p))}" for nm, p in sorted(found.items()))
        + f"；显式启用 {sorted(intended) or '（无）'}",
        "闭包里冒出非显式启用的代理前端 ⇒ 说明某条依赖把整个另一套栈拉进来了："
        + "; ".join(f"{nm} → {v}" for nm, v in sorted(extra_front.items())),
    )
    if extra_core and not extra_front:
        warn("依赖闭包里出现别的代理栈的**内核**",
             "; ".join(f"{nm} → {v}" for nm, v in sorted(extra_core.items()))
             + " —— 前端没启用，但内核被装上了；通常是某个前端显式依赖它")

    # --- 15c 跨栈提供者（apk 择优安装风险）------------------------------
    closure_names = set(meta)
    for p in meta:
        closure_names |= set(meta[p]["D"])
    prov_hits: list[tuple[str, str, str, list[str]]] = []
    for p, (nm, _mk) in sorted(markers.items()):
        if p in meta or p not in cat:
            continue
        try:
            m2 = apkmeta.fetch_meta(cat[p], CLOSURE_PREFIX_B, retries=2)
        except Exception:  # noqa: BLE001
            continue
        offer = [x for x in m2["p"] if x in closure_names]
        if offer:
            prov_hits.append((p, nm, m2["V"], offer))
    if prov_hits:
        warn(
            "跨栈提供者（apk 择优可能装上另一套栈的内核）",
            "; ".join(f"{p}（{nm} 栈 {v}）provides {o}" for p, nm, v, o in prov_hits)
            + " —— 这些虚拟名在闭包里出现，而另一套栈的版本更高 ⇒ apk 会选它。"
              "2026-10-14 产物级解包已证实过一例：nikki 的 D: mihomo 让 "
              "clashoo 被装了进来（前端未启用、`/etc/config/clashoo` enable 0）。"
              "**不是互斥失守**，但要确认这份空间/复杂度代价可接受。",
        )
    else:
        ok("跨栈提供者", "没有其他代理栈提供闭包里出现的虚拟名")


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
