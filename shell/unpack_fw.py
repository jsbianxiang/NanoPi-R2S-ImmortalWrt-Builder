#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shell/unpack_fw.py —— 固件镜像解包 / 取证工具（纯 Python，零外部依赖）

本机**没有** 7-Zip / unsquashfs / debugfs，而这个仓库要反复做「产物级实证」
（ assertions 只在固件里才能被证实 ），于是有了本脚本。它只做三件事：

  1. **读 MBR 分区表**，找到 rootfs 分区；
  2. **纯 Python 解析 ext4**（超级块 → 组描述符 → inode → extent 树 → 线性目录项），
     支持 ls / cat / find / stat / walk；
  3. **解析 apk 的权威已装数据库** `/lib/apk/db/installed` 与 `/etc/apk/world`，
     并给出一份「代理栈核查」摘要。

`.apk` / `packages.adb` 的元数据解析走同目录的 `apkmeta.py`。

镜像布局（NanoPi R2S / rockchip-armv8，2026-10-14 实测）：
    MBR（**不是** GPT）
      part1  idbloader/uboot   16 MiB   @ LBA 65536
      part2  rootfs           1024 MiB  @ LBA 131072
    解压总长 1088 MiB = 64 MiB uboot + 1 GiB rootfs

用法：
    # 直接从工作流产物（.img.gz）读
    python3 shell/unpack_fw.py immortalwrt-...sysupgrade.img.gz part
    python3 shell/unpack_fw.py immortalwrt-...sysupgrade.img.gz ls /
    python3 shell/unpack_fw.py immortalwrt-...sysupgrade.img.gz cat /etc/config/firewall
    python3 shell/unpack_fw.py immortalwrt-...sysupgrade.img.gz packages
    python3 shell/unpack_fw.py immortalwrt-...sysupgrade.img.gz proxy

    # 已手工解出 rootfs 分区时可以跳过解压
    python3 shell/unpack_fw.py rootfs.img --offset 131072 ls /

    # 把 rootfs 分区落盘（后面反复查会快很多）
    python3 shell/unpack_fw.py xxx.img.gz extract rootfs.img

⚠ Git Bash 会把以 `/` 开头的参数改写成 Windows 路径 ⇒ 在本仓库的 bash 环境里
  跑之前先 `export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'`。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
import zlib
from collections import OrderedDict

# ---------------------------------------------------------------------------
# ext4 只读解析
# ---------------------------------------------------------------------------
EXT4_EXTENTS_FL = 0x00080000


class Ext4:
    """极简 ext4 只读解析器。

    只用到：超级块 + 组描述符 + inode + extent 树 + 线性目录项。
    不支持：inline_data、加密、校验和（固件 rootfs 用不到这些）。
    `offset` 是文件系统在**镜像文件**里的起始字节偏移（分区场景）。
    """

    def __init__(self, path: str, offset: int = 0):
        self.f = open(path, "rb")
        self.offset = offset
        # 超级块固定在文件系统内字节偏移 1024 起，与块大小无关
        self.f.seek(self.offset + 1024)
        sb = self.f.read(1024)
        self.s_inodes_count = self._u32(sb, 0)
        self.blocks_count = self._u32(sb, 4) | (self._u32(sb, 0x150) << 32)
        self.first_data_block = self._u32(sb, 20)
        self.block_size = 1024 << self._u32(sb, 24)
        self.blocks_per_group = self._u32(sb, 32)
        self.inodes_per_group = self._u32(sb, 40)
        self.inode_size = self._u16(sb, 88)
        self.feature_incompat = self._u32(sb, 96)
        self.desc_size = self._u16(sb, 254) if (self.feature_incompat & 0x80) else 32
        self._bcache: OrderedDict[int, bytes] = OrderedDict()
        self._icache: dict[int, dict] = {}

    # ---------- 基础读 ----------
    @staticmethod
    def _u16(b, o):
        return struct.unpack_from("<H", b, o)[0]

    @staticmethod
    def _u32(b, o):
        return struct.unpack_from("<I", b, o)[0]

    def _read_block_raw(self, blk: int, length=None) -> bytes:
        self.f.seek(self.offset + blk * self.block_size)
        return self.f.read(length if length is not None else self.block_size)

    def block(self, blk: int) -> bytes:
        """带 LRU 缓存的块读取（4096 块 ≈ 16 MiB，目录遍历够用）。"""
        v = self._bcache.get(blk)
        if v is not None:
            self._bcache.move_to_end(blk)
            return v
        v = self._read_block_raw(blk)
        self._bcache[blk] = v
        if len(self._bcache) > 4096:
            self._bcache.popitem(last=False)
        return v

    # ---------- 组描述符 ----------
    def _group_desc(self, g: int) -> int:
        """返回第 g 块的 inode 表起始块号。

        ⚠ 描述符长度由超级块 `s_desc_size` 给（64 B 常见，老镜像是 32 B）；
          `bg_inode_table_hi` 在偏移 40 —— 必须**切够 44+ 字节**再读，
          否则 `struct.error: requires a buffer of at least 44 bytes`。
        """
        blk = self.first_data_block + 1 + (g * self.desc_size) // self.block_size
        off = (g * self.desc_size) % self.block_size
        b = self.block(blk)
        need = min(self.desc_size, len(b) - off, 64)
        d = b[off:off + need]
        hi = self._u32(d, 40) if need >= 44 else 0
        return self._u32(d, 8) | (hi << 32)

    # ---------- inode ----------
    def inode(self, n: int) -> dict:
        if n in self._icache:
            return self._icache[n]
        g, idx = divmod(n - 1, self.inodes_per_group)
        itable = self._group_desc(g)
        self.f.seek(self.offset + itable * self.block_size + idx * self.inode_size)
        raw = self.f.read(self.inode_size)
        mode = self._u16(raw, 0)
        size = self._u32(raw, 4) | (self._u32(raw, 108) << 32)
        ino = {
            "num": n, "mode": mode, "size": size, "iblock": raw[40:100],
            "is_dir": (mode & 0xF000) == 0x4000,
            "is_reg": (mode & 0xF000) == 0x8000,
            "is_symlink": (mode & 0xF000) == 0xA000,
        }
        self._icache[n] = ino
        return ino

    # ---------- extent 树 ----------
    def _extents(self, ino: dict, header_bytes: bytes):
        out = []
        if self._u16(header_bytes, 0) != 0xF30A:
            raise RuntimeError(f"inode {ino['num']} 不是 extent 格式（magic 不符）")

        def walk(buf, depth):
            for i in range(self._u16(buf, 2)):
                e = buf[12 + i * 12: 12 + i * 12 + 12]
                if depth == 0:
                    l_len = self._u16(e, 4) & 0x7FFF
                    p_blk = self._u32(e, 8) | (self._u16(e, 6) << 32)
                    if l_len:
                        out.append((self._u32(e, 0), p_blk, l_len))
                else:
                    child = self._u32(e, 4) | (self._u16(e, 8) << 32)
                    walk(self.block(child), depth - 1)

        walk(header_bytes, self._u16(header_bytes, 6))
        return out

    def read_file(self, ino: dict, limit=None) -> bytes:
        if not ino["is_reg"]:
            return b""
        exts = sorted(self._extents(ino, ino["iblock"]))
        total = ino["size"] if limit is None else min(limit, ino["size"])
        buf = bytearray()
        produced = 0
        for l_blk, p_blk, l_len in exts:
            if l_blk * self.block_size > produced:          # 逻辑空洞补零
                gap = l_blk * self.block_size - produced
                if produced + gap > total:
                    gap = total - produced
                buf += b"\x00" * gap
                produced += gap
            for k in range(l_len):
                if produced >= total:
                    break
                take = min(self.block_size, total - produced)
                buf += self.block(p_blk + k)[:take]
                produced += take
        if produced < total:                                # 尾部洞
            buf += b"\x00" * (total - produced)
        return bytes(buf)

    # ---------- 目录 ----------
    def listdir(self, ino: dict):
        out = []
        for l_blk, p_blk, l_len in sorted(self._extents(ino, ino["iblock"])):
            for k in range(l_len):
                b = self.block(p_blk + k)
                off = 0
                while off + 8 <= len(b):
                    inum = self._u32(b, off)
                    rec_len = self._u16(b, off + 4)
                    name_len = b[off + 6]
                    if rec_len < 8 or off + rec_len > len(b):
                        break
                    if inum and name_len:
                        out.append((inum, b[off + 8: off + 8 + name_len]
                                    .decode("utf-8", "replace"), b[off + 7]))
                    off += rec_len
        return out

    def walk(self, root: str = "/"):
        """全量路径迭代 → (path, inode_num, size)。遍历 1 GiB 根约 1.4 s。"""
        stack = [(2, "")]
        while stack:
            n, prefix = stack.pop()
            ino = self.inode(n)
            if not ino["is_dir"]:
                continue
            for inum, name, ftype in self.listdir(ino):
                if name in (".", ".."):
                    continue
                p = (prefix + "/" + name) if prefix else "/" + name
                yield p, inum, ftype
                if ftype == 2:
                    stack.append((inum, p))


def resolve(fs: Ext4, path: str):
    if path == "/":
        return fs.inode(2)
    n = 2
    for part in [p for p in path.split("/") if p]:
        for inum, name, _ in fs.listdir(fs.inode(n)):
            if name == part:
                n = inum
                break
        else:
            return None
    return fs.inode(n)


# ---------------------------------------------------------------------------
# MBR / gzip
# ---------------------------------------------------------------------------
def parse_mbr(mbr: bytes) -> list[tuple[int, int, int, int]]:
    """解析 512 字节 MBR → [(序号, 类型, 起始 LBA, 扇区数)]。

    OpenWrt/ImmortalWrt 的 sysupgrade.img 用 **MBR**，不是 GPT —— 别按 GPT 解析。
    """
    if mbr[510:512] != b"\x55\xaa":
        raise RuntimeError("没有 MBR 签名 55AA（不是 MBR 镜像？）")
    out = []
    for i in range(4):
        e = mbr[446 + i * 16: 446 + (i + 1) * 16]
        ptype = e[4]
        lba = struct.unpack_from("<I", e, 8)[0]
        nsec = struct.unpack_from("<I", e, 12)[0]
        if ptype and nsec:
            out.append((i + 1, ptype, lba, nsec))
    return out


def decompress_prefix(src_path: str, n: int) -> bytes:
    """解出 src_path（gzip）的前 n 个字节。

    ⚠ 本机 `gzip.decompress()` 会莫名其妙报 `BadGzipFile: Not a gzipped file`
      （即便 magic 是 1f 8b）⇒ 统一用 `zlib.decompressobj(31)` 流式解压。
    """
    dec = zlib.decompressobj(31)
    out = bytearray()
    with open(src_path, "rb") as fi:
        while len(out) < n:
            chunk = fi.read(1 << 20)
            if not chunk:
                break
            out += dec.decompress(chunk)
            if dec.eof:
                break
    return bytes(out[:n])


def gunzip_to(src_path: str, dst_path: str, skip_bytes: int, want_bytes: int) -> None:
    """把 src_path（gzip）解压，跳过前 skip_bytes，只写后面 want_bytes 到 dst_path。

    ⚠ 本机 `gzip.decompress()` 会莫名其妙报 `BadGzipFile: Not a gzipped file`
      （即便 magic 是 1f 8b）⇒ 统一用 `zlib.decompressobj(31)` 流式解压。
    ⚠ 源站多不支持 Range ⇒ 只能顺序流过去；skip 部分边解边丢。
    """
    dec = zlib.decompressobj(31)
    done = 0            # 已消费的解压字节数
    written = 0         # 已写出的字节数
    with open(src_path, "rb") as fi, open(dst_path, "wb") as fo:
        while written < want_bytes:
            chunk = fi.read(1 << 20)
            if not chunk:
                break
            data = dec.decompress(chunk)
            while data:
                if done < skip_bytes:                        # 前段：丢弃
                    drop = min(len(data), skip_bytes - done)
                    data, done = data[drop:], done + drop
                    continue
                take = min(len(data), want_bytes - written)  # 目标段：写出
                fo.write(data[:take])
                written, data = written + take, data[take:]
            if dec.eof:
                break
        if written < want_bytes:
            raise RuntimeError(
                f"解压流比预期短：只要 {want_bytes} 字节，实际 {written} 字节")


def find_rootfs(image: str, quiet: bool = False, _parts=None):
    """返回 (rootfs 在解压流里的起始偏移, rootfs 字节数)。

    MBR 在 **gzip 流里面**，所以要先解出前 64 KiB 读分区表。
    """
    if _parts is None:
        head = decompress_prefix(image, 64 * 1024)
        _parts = parse_mbr(head)
    if not quiet:
        print("MBR 分区表：")
        for idx, ptype, lba, nsec in _parts:
            print(f"  part{idx}  type=0x{ptype:02x}  LBA={lba}  "
                  f"起={lba * 512 / 1048576:.0f} MiB  "
                  f"长={nsec * 512 / 1048576:.0f} MiB")
    # rootfs：不在最前面（前面是 idbloader/uboot）且远大于 64 MiB
    root = [p for p in _parts
            if p[2] * 512 > 16 * 1048576 and p[3] * 512 > 64 * 1048576]
    if not root:
        raise RuntimeError("没找到像 rootfs 的分区（>64 MiB 且不在最前面）")
    _, _, lba, nsec = root[-1]
    return lba * 512, nsec * 512


# ---------------------------------------------------------------------------
# apk 已装数据库
# ---------------------------------------------------------------------------
def parse_installed(text: str) -> list[dict]:
    """解析 `/lib/apk/db/installed`（`RS="";FS="\\n"` 风格）。

    记录之间用空行分隔；每条是 `KEY:值` 行。同一个键可出现多次（`D:`/`p:`/`F:`）。
    这是**镜像里最权威**的已装包清单 —— `/etc/apk/world` 只是显式顶层清单。
    """
    pkgs = []
    for rec in text.split("\n\n"):
        if not rec.strip():
            continue
        d: dict[str, list[str]] = {}
        for line in rec.split("\n"):
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            d.setdefault(k, []).append(v)
        if "P" in d:
            pkgs.append(d)
    return pkgs


def read_installed(fs: Ext4) -> list[dict]:
    ino = resolve(fs, "/lib/apk/db/installed")
    if ino is None:
        raise RuntimeError("镜像里没有 /lib/apk/db/installed")
    return parse_installed(fs.read_file(ino).decode("utf-8", "replace"))


def read_world(fs: Ext4) -> list[str]:
    ino = resolve(fs, "/etc/apk/world")
    if ino is None:
        return []
    return [x for x in fs.read_file(ino).decode("utf-8", "replace").split("\n") if x]


# ---------------------------------------------------------------------------
# 专用查询
# ---------------------------------------------------------------------------
def cmd_packages(fs: Ext4, args) -> None:
    pkgs = read_installed(fs)
    world = set(read_world(fs))
    if args.json:
        import json
        print(json.dumps([
            {"P": p["P"][0], "V": p.get("V", [""])[0], "S": p.get("S", [""])[0],
             "I": p.get("I", [""])[0], "D": p.get("D", []), "p": p.get("p", []),
             "explicit": p["P"][0] in world}
            for p in pkgs], ensure_ascii=False, indent=1))
        return
    print(f"已装包 {len(pkgs)} 个（/etc/apk/world 显式 {len(world)} 个）\n")
    print(f"  {'P':<34} {'V':<28} {'S':>9}  I")
    for p in pkgs:
        name = p["P"][0]
        ver = (p.get("V") or [""])[0]
        sz = (p.get("S") or ["0"])[0]
        mark = "*" if name in world else " "
        print(f"  {mark} {name:<32} {ver:<28} {sz:>9}  {(p.get('I') or [''])[0]}")
    print("\n  (* = /etc/apk/world 里的显式顶层包；其余全是依赖闭包带进来的)")


PROXY_HINTS = (
    "nikki", "clashoo", "openclash", "passwall", "ssr-plus", "homeproxy",
    "daed", "dae", "shadowsocks", "redsocks", "xray", "v2raya", "mihomo",
    "sing-box", "hysteria", "honk", "nekobox", "jederproxy", "xjay",
)


def cmd_proxy(fs: Ext4, args) -> None:
    """代理栈核查：镜像里到底装了哪个前端、哪个内核、有没有第二个前端。"""
    pkgs = read_installed(fs)
    by_name = {p["P"][0]: p for p in pkgs}
    world = set(read_world(fs))

    fronts = sorted(n for n in by_name
                    if n.startswith("luci-app-") and any(h in n for h in PROXY_HINTS))
    cores = sorted(n for n in by_name
                   if not n.startswith("luci-app-")
                   and any(h == n or h in n.split("-") for h in PROXY_HINTS))
    print(f"已装 {len(pkgs)} 包；/etc/apk/world 显式 {len(world)} 个\n")
    print("代理类 LuCI 前端（luci-app-*）：")
    if fronts:
        for n in fronts:
            print(f"    {n}{'  ← world 显式' if n in world else ''}")
    else:
        print("    （无）")
    print("\n代理类内核 / 纯名包：")
    if cores:
        for n in cores:
            print(f"    {n:<24} {(by_name[n].get('V') or [''])[0]}"
                  f"{'  ← world 显式' if n in world else ''}")
    else:
        print("    （无）")

    # 提供者核查：某个前端声明了 provide 谁（用来判断「内核是被谁拉进来的」）
    print("\n前端声明的 provides（p:）：")
    for n in fronts:
        prov = by_name[n].get("p", [])
        print(f"    {n:<26} {prov}")

    print("\n⚠ 判读提示：透明代理接管型插件只能启用**一个**。若上面出现两个不同体系的"
          "\n  前端（如 nikki 与 clashoo），即互斥失守；若只出现一个前端但多出别的体系的"
          "\n  **内核**，多半是虚拟名（mihomo / clash-meta …）被 apk 择优解析拉进来的"
          "\n  —— 此时应看该前端的 `D:` 里是否声明了那个虚拟名，并确认那份代价可接受。")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def open_fs(args) -> Ext4:
    """打开镜像得到 Ext4 句柄。

    - 给了 `--offset`（含 `--offset 0`）⇒ 把 image 当成**裸文件系统**直接读；
    - 否则按 `sysupgrade.img.gz` 处理：读 MBR → 解出 rootfs 分区到临时文件再读。
      临时文件每次重解（1 GiB）比较浪费，反复查同一个固件时建议先
      `unpack_fw.py xxx.img.gz extract rootfs.img`，之后
      `unpack_fw.py rootfs.img --offset 0 <子命令>`。
    """
    if args.offset is not None:
        return Ext4(args.image, args.offset)
    off, size = find_rootfs(args.image)
    fd, tmp = tempfile.mkstemp(prefix="r2s-rootfs-", suffix=".img")
    os.close(fd)
    print(f"解出 rootfs 分区 → {tmp}（{size / 1048576:.0f} MiB，稍慢）")
    gunzip_to(args.image, tmp, off, size)
    return Ext4(tmp)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="固件镜像解包/取证（纯 Python：MBR + ext4 + apk 已装数据库）")
    ap.add_argument("image", help="sysupgrade.img.gz，或已解出的 rootfs 分区镜像")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=None,
                    help="文件系统在镜像里的起始字节偏移；给了（含 0）就把 image "
                         "当裸文件系统读，跳过 MBR/gzip 解析")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("part", help="打印 MBR 分区表并定位 rootfs")
    sub.add_parser("extract", help="把 rootfs 分区解压落盘").add_argument("out")
    p_ls = sub.add_parser("ls", help="列目录")
    p_ls.add_argument("path", nargs="?", default="/")
    p_cat = sub.add_parser("cat", help="输出文件内容")
    p_cat.add_argument("path")
    p_cat.add_argument("-n", "--max-bytes", type=int, default=None)
    p_find = sub.add_parser("find", help="按路径子串列举")
    p_find.add_argument("needle")
    sub.add_parser("walk", help="全量文件清单")
    p_pkg = sub.add_parser("packages", help="解析 /lib/apk/db/installed")
    p_pkg.add_argument("--json", action="store_true", help="输出 JSON（便于脚本消费）")
    sub.add_parser("proxy", help="代理栈核查（前端 / 内核 / provides）")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "part":
        head = decompress_prefix(args.image, 64 * 1024)
        parts = parse_mbr(head)
        for idx, ptype, lba, nsec in parts:
            print(f"part{idx}  type=0x{ptype:02x}  LBA={lba}  "
                  f"起={lba * 512 / 1048576:.0f} MiB  "
                  f"长={nsec * 512 / 1048576:.0f} MiB")
        off, size = find_rootfs(args.image, quiet=True, _parts=parts)
        print(f"\nrootfs：偏移 {off}（= LBA {off // 512}），{size / 1048576:.0f} MiB")
        print(f"解压后总长约 {(max(p[2] + p[3] for p in parts) * 512) / 1048576:.0f} MiB")
        return 0

    if args.cmd == "extract":
        off, size = find_rootfs(args.image)
        gunzip_to(args.image, args.out, off, size)
        print(f"已写出 {args.out}（{os.path.getsize(args.out) / 1048576:.0f} MiB）")
        return 0

    fs = open_fs(args)

    if args.cmd == "ls":
        ino = resolve(fs, args.path)
        if ino is None:
            print("  不存在:", args.path)
            return 1
        if not ino["is_dir"]:
            print("  非目录:", args.path)
            return 1
        tstr = {1: "f", 2: "d", 7: "l"}
        for inum, name, t in sorted(fs.listdir(ino), key=lambda x: x[1]):
            if name in (".", ".."):
                continue
            print(f"  {tstr.get(t, '?')}  {name}")
        return 0
    if args.cmd == "cat":
        ino = resolve(fs, args.path)
        if ino is None:
            print("  不存在:", args.path, file=sys.stderr)
            return 1
        data = fs.read_file(ino, limit=args.max_bytes)
        (sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout).write(data)
        return 0
    if args.cmd == "find":
        cnt = 0
        for p, _inum, _t in fs.walk():
            if args.needle in p:
                cnt += 1
                if cnt <= 400:
                    print(" ", p)
        print(f"  共 {cnt} 条")
        return 0
    if args.cmd == "walk":
        total = 0
        for p, _inum, _t in fs.walk():
            print(p)
            total += 1
        print(f"# 合计 {total} 个条目", file=sys.stderr)
        return 0
    if args.cmd == "packages":
        cmd_packages(fs, args)
        return 0
    if args.cmd == "proxy":
        cmd_proxy(fs, args)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())


