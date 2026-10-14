"""apk-tools v3 的 ADB 容器 / ADB.pckg 元数据解析（配合 audit.py 的依赖闭包检查）。

OpenWrt & ImmortalWrt 25.12 起，`.apk` 与 `packages.adb` 都改用 apk-tools v3 的
**ADB 容器**格式，而**不是** Alpine 传统的 gzip-tar：

    ┌─ 文件 ─────────────────────────────────────────────┐
    │ "ADBd" (4B) │ 裸 deflate 流（无 zlib/gzip 头）        │
    └─────────────────────────────────────────────────────┘
                        ↓ zlib.decompressobj(-15)（raw deflate）
    ┌─ 载荷 ─────────────────────────────────────────────┐
    │ "ADB.pckg" 或 "ADB.indx" … 二进制字段序列            │
    └─────────────────────────────────────────────────────┘

**关键性质（本模块的全部依据）**：包的元数据（P: 名称 / V: 版本 / D: 依赖 /
p: provides）位于 deflate 流的**最前端**。所以只需要请求对象的前几十 KB 就能拿到
全部元数据 —— 对 21.5 MB 的 clashoo 也只需 32 KB。`fetch_meta()` 即利用这点，
sec2=32 * 1024 只拉一小截。

ADB.pckg 的字段编码：标量字段是 `<u8 长度><字节>`；依赖/provides 区是一串**记录**，
每条 = `<u32 项数> <项数-1 个 u32 标记> <u8 名字长度> <名字>`（2026-10-14 hexdump 逆向）。
标记的最高字节 0x80 = 依赖、0xE0 = provides；**项数与标记值本身无法逐字节严格解析**
⇒ 只取「名字长度字节前 4 字节的最高字节」。

**已用镜像内 `/lib/apk/db/installed` 的权威 `D:`/`p:` 对拍 7 个包，结论：**
- `D` **从不漏真实依赖**（含 clashoo 的 13 项全中）；
- `names`（该区全部名字、不分类）**从不漏 provides**；
- 仅 `p` 的分类在多 provides 包（clashoo 的 `mihomo`/`clash-meta`）上会少几项。

⇒ **「X 是否提供虚拟名 Y」这类判断一律用 `Y in m["names"]`**（不依赖分类），
「依赖闭包」用 `m["D"]`。`fetch_meta()` 只拉前几十 KB 即可（元数据在 deflate 流最前端）。

用法：
    from apkmeta import fetch_meta, parse_apk
    m = fetch_meta("https://…/nikki-2026.10.10-r1.apk")
    m["P"], m["V"], m["D"], m["p"], m["names"]
"""
from __future__ import annotations

import re
import zlib
from typing import Iterable

ADB_MAGIC = b"ADBd"
PCKG_MAGIC = b"ADB.pckg"
#：贪婪扫描时，name 的合法字符集（apk 包名小写开头）
NAME_RE = re.compile(rb"[a-z0-9][a-z0-9._+!*-]{0,127}")


def adb_payload(raw: bytes) -> bytes:
    """把 ADB 容器的裸 deflate 部分解出来。raw 可以是**任意前缀**（流式可用）。"""
    if raw[:4] != ADB_MAGIC:
        raise ValueError(f"不是 ADB 容器（magic={raw[:4]!r}）")
    return zlib.decompressobj(-15).decompress(raw[4:])


def _scan_tokens(buf: bytes, start: int, end: int) -> list[bytes]:
    """在 [start, end) 内贪婪抽取 `<u8 len><name>` 形式的 name token。"""
    out: list[bytes] = []
    i = start
    while i < end:
        n = buf[i]
        # 长度为 0 的串在 ADB 里表示「空」，但约束描述符也常以 00 开头 ⇒ 只当噪声跳过
        if n == 0:
            i += 1
            continue
        j = i + 1
        if j + n > end:
            break
        tok = buf[j:j + n]
        if NAME_RE.fullmatch(tok):
            out.append(tok)
            i = j + n
        else:
            i += 1
    return out


def _find_magic(buf: bytes, magic: bytes) -> int:
    i = buf.find(magic)
    return i if i >= 0 else len(buf)


def parse_apk(raw_prefix: bytes) -> dict:
    """解析 `.apk` 的 ADB 容器前缀，返回 {'P','V','D','p','arch','origin'}。

    只依赖流的最前端，因此 raw_prefix 给 32 KB 就能解析大包的元数据。

    字段顺序（由 ADB.pckg 实测得出）：
        name → version → description → arch → license → origin → maintainer
        → D: 依赖名表 → p: provides 名表 → install_if → 文件清单
    依赖名/provides 名都是 `<u8 长度><可打印名>`，两者之间以及各项之间夹着
    **变长二进制约束描述符**（长度随版本比较运算而变，无法逐字节严格解析）。
    因此这里用两项稳健技巧：
      1) 包名用「magic 后第一个合法 `<len><name>` 令牌」定位，不硬编码偏移；
      2) D: 与 p: 的分界取**区域内最大的无令牌空隙**（实测该空隙远大于
         单项描述符，见 tests/ 的对拍）。
    """
    out = adb_payload(raw_prefix)
    if not out.startswith(PCKG_MAGIC):
        raise ValueError("载荷不是 ADB.pckg")
    body = out

    # 1) 定位包名：magic(8) + 长度字段若干字节后，第一个合法 `<len><name>`
    name_at = -1
    for i in range(8, min(len(body), 96)):
        n = body[i]
        if 2 <= n <= 96 and i + 1 + n <= len(body) \
                and NAME_RE.fullmatch(body[i + 1:i + 1 + n]):
            name_at = i
            break
    if name_at < 0:
        raise ValueError("找不到包名字段")

    # 2) 顺序读 7 个标量字段
    scalars: list[bytes] = []
    i = name_at
    for _ in range(7):
        n = body[i]
        i += 1
        val = b"" if n == 0 else body[i:i + n]
        i += len(val)
        scalars.append(val)

    def s(k: int) -> str:
        return scalars[k].decode("utf-8", "replace") if len(scalars) > k else ""

    # 3) 令牌扫描：只收 `<u8 长度><可打印名>`，并取「记录标记」。
    #    **ADB.pckg 的依赖/provides 区**（2026-10-14 由 hexdump 逆向得出）：
    #        7 个标量字段之后是一串**记录**，每条 =
    #            `<u32 项数> <项数-1 个 u32 标记> <u8 名字长度> <名字>`
    #        记录的**最后一个标记**紧贴名字长度字节，其最高字节：
    #            0x80 → 该名字是**依赖**（D:）
    #            0xE0 → 该名字是 **provides**（p:）
    #        项数在 2…40 之间（provides 越多项数越多），标记值本身是 ADB 内部的
    #        偏移/引用，无法逐字节严格解析 ⇒ 只取「长度字节前 4 字节的最高字节」。
    #    ⚠ 已知局限（对拍 6/7 完全一致）：**多 provides** 包（如 clashoo 的
    #      `mihomo`/`clash-meta`）会被误判成依赖 ⇒ `p:` 少几项。
    #      因此本模块额外导出 `names`（该区**全部**名字，不分类）：
    #      「X 是否提供虚拟名 Y」这类判断请用 `Y in m["names"]`，它**不依赖分类**。
    region_start = i
    region_end = min(len(body), region_start + 65536)
    toks: list[tuple[int, str, int]] = []      # (长度字节偏移, 名字, 标记|0)
    j = region_start
    while j < region_end:
        n = body[j]
        if n == 0:
            j += 1
            continue
        k = j + 1
        if k + n > region_end:
            j += 1
            continue
        tok = body[k:k + n]
        if NAME_RE.fullmatch(tok):
            tag = body[j - 1] if j >= 4 else 0
            if tag not in (0x80, 0xE0):
                tag = 0
            toks.append((j, tok.decode("utf-8", "replace"), tag))
            j = k + n
        else:
            j += 1

    # 4) 右界：第一个像路径 / install_if 用户的 token（其后是文件清单）
    PATHISH = re.compile(
        r"^(?:etc$|usr$|bin$|sbin$|lib$|lib(?:64)?$|var$|www$|tmp$|overlay$|rom$|root$"
        r"|dev$|proc$|/|etc/|usr/|bin/|lib/)")
    PATHISH_PROBE = re.compile(r"^(?:root$|etc$|usr$|bin$|lib$|var$|www$|tmp$|overlay$|rom$)")
    cut = len(toks)
    for idx, (_, t, _) in enumerate(toks):
        if PATHISH.match(t):
            cut = idx
            break
    toks = toks[:cut]

    # 5) 拆 D: / p: —— **第一个标记为 0xE0 的 token 就是 provides 段的起点**。
    #    依据（2026-10-14 hexdump 逆向 + 7 个包权威对拍）：
    #      依赖/provides 区按固定顺序排布：D: 段 → p: 段 → install_if（`root`）。
    #      - D: 段每条记录只有 1 个标记，最高字节 0x80（首条 `libc` 连头都省了 ⇒ 0）；
    #      - p: 段每条记录有多个标记，**第一个就是 0xE0**；
    #      - 之后即便又出现 0x80（clashoo 的 `mihomo`/`clash-meta`）也在 p: 段里。
    #    ⚠ 因此「按 tag 逐条分类」会把多 provides 包误判；改用「段首」判定。
    #      若整段都没有 0xE0（无 provides 的极少数包）⇒ 全部算依赖（宁可多解）。
    arch = s(3)
    first_prov = next((i for i, (_, _, tg) in enumerate(toks) if tg == 0xE0),
                      len(toks))
    deps = [t for _, t, _ in toks[:first_prov]]
    provides = [t for _, t, _ in toks[first_prov:]]
    self_provide = f"{s(0)}-{arch}" if arch else ""
    if self_provide and self_provide in deps:
        deps.remove(self_provide)
        provides.insert(0, self_provide)
    # install_if（`root` 等）理论上已被 PATHISH 截掉；兜底再剔一次
    provides = [x for x in provides if not PATHISH_PROBE.match(x)]

    return {
        "P": s(0), "V": s(1), "desc": s(2), "arch": s(3), "license": s(4),
        "origin": s(5), "maint": s(6),
        "D": deps, "p": provides, "names": [t for _, t, _ in toks],
    }


def _read_prefix(url: str, prefix_bytes: int, timeout: int) -> bytes:
    """拉取 URL 的前 prefix_bytes 字节（读满即主动断开）。"""
    import http.client  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    u = urllib.parse.urlparse(url)
    https = (u.scheme or "https") == "https"
    cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    port = u.port or (443 if https else 80)
    conn = cls(u.hostname, port, timeout=timeout)
    try:
        path = u.path + (("?" + u.query) if u.query else "")
        conn.request("GET", path, headers={"Host": u.hostname,
                                          "User-Agent": "r2s-audit/1.0"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        buf = bytearray()
        while len(buf) < prefix_bytes:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    return bytes(buf)


def fetch_meta(url: str, prefix_bytes: int = 32 * 1024, timeout: int = 60,
               retries: int = 3) -> dict:
    """只拉取 URL 的前 prefix_bytes 字节并解析元数据。

    多数源不支持 HTTP Range（dllkids 就返回 200 + 全量），所以这里读满
    prefix_bytes 就主动断开 —— 元数据在流最前端，足够了。

    ⚠ 本机代理会偶发 TLS EOF（`UNEXPECTED_EOF_WHILE_READING`），故内置重试；
      重试仍失败时退回 **http://** 同路径（实测 dllkids/immortalwrt 80 端口
      也能给到同样的内容）。
    """
    import time  # noqa: PLC0415

    last: Exception | None = None
    for i in range(max(1, retries)):
        try:
            return parse_apk(_read_prefix(url, prefix_bytes, timeout))
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < retries:
                time.sleep(0.8 * (i + 1))
    if url.startswith("https://"):
        try:
            return parse_apk(_read_prefix("http://" + url[8:], prefix_bytes, timeout))
        except Exception:  # noqa: BLE001
            pass
    raise last if last else OSError("fetch_meta 失败")


def dedup(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def provides_of(meta: dict, virtual: str) -> bool:
    """包 `meta` 是否提供虚拟名 `virtual`（tag 无关，最可靠）。

    直接查 `names`（依赖/provides 区全部名字），不经过 D/p 分类 —— 已知
    `p:` 在多 provides 包上会少几项，而 `names` 不漏。
    """
    return virtual in meta.get("names", [])
