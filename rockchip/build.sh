#!/bin/bash
# 构建期日志（注意：固件内的首启日志是 /etc/config/uci-defaults-log.txt，两者不是一回事）
source shell/custom-packages.sh
echo "第三方APK软件包: $CUSTOM_PACKAGES"
LOGFILE="/tmp/build.log"
echo "Starting build.sh at $(date)" >> $LOGFILE
# yml 传入的路由器型号 PROFILE
echo "Building for profile: $PROFILE"
# yml 传入的固件大小 ROOTFS_PARTSIZE
echo "Building for ROOTFS_PARTSIZE: $ROOTFS_PARTSIZE"

# ---------------------------------------------------------------------------
# 构建期注入目录：/etc/immortalwrt-builder/
#
# ⚠ 这些辅助文件**绝不能**放在 /etc/config/ 下。
#   已核实 libuci 源码（openwrt/uci，file.c）：
#     - uci_list_config_files() 用 glob("<confdir>/*", GLOB_MARK) **扫描整个目录**；
#     - get_filename() 只跳过「文件名含 `.`」的条目，源码注释原文：
#         "Filename with '.' are ignored and not parsed. Uci doesn't
#          permit config files with '.' in the name and cause parsing problems."
#       其余条目一律经 uci_validate_package() 后当作 uci 配置包解析
#       （校验只放行可打印 ASCII，`-` 也允许，挡不住 wan-settings 这种名字）。
#   ⇒ 无后缀、内容又是 key=value 的 wan-settings 会被当 uci 语法解析，
#     首行 `wan_proto=dhcp` 不匹配 package/config/option/list 任一关键字，
#     直接命中 file.c:553 的 uci_parse_error(ctx, "invalid command")。
#     现象就是**任何** uci 调用都打印：
#         uci: Parse error (invalid command) at line 1, byte 0
#     LuCI 的「软件包」页安装软件同样经由 uci，因此也报同一行错误。
#
#   约定：/etc/config/ 只放 uci 配置包；构建期辅助文件统一放
#         /etc/immortalwrt-builder/（无后缀不会被任何组件扫描）。
#   （旧版曾把 wan-settings / custom_router_ip.txt 放进 /etc/config/，
#     files/etc/uci-defaults/99-custom.sh 里有对应的迁移清理。）
# ---------------------------------------------------------------------------
BUILDER_DIR="/home/build/immortalwrt"
SETTINGS_DIR="$BUILDER_DIR/files/etc/immortalwrt-builder"
echo "Create $SETTINGS_DIR"
mkdir -p "$SETTINGS_DIR"

# DNS 留空时回落到网关（光猫一般自带 DNS 转发）
WAN_DNS="${WAN_DNS:-$WAN_GATEWAY}"
# WAN 接入方式：默认 dhcp（与 ImmortalWrt 官方默认一致），可选 static
WAN_PROTO="${WAN_PROTO:-dhcp}"

# 创建 WAN 配置文件（yml 传入 WAN_PROTO / WAN_IP / WAN_GATEWAY / WAN_DNS）
# 该文件随固件发布，供 99-custom.sh 首次启动时读取
cat << EOF > "$SETTINGS_DIR/wan-settings"
wan_proto=${WAN_PROTO}
wan_ip=${WAN_IP}
wan_gateway=${WAN_GATEWAY}
wan_dns=${WAN_DNS}
EOF

echo "cat wan-settings"
cat "$SETTINGS_DIR/wan-settings"

if [ -z "$CUSTOM_PACKAGES" ]; then
  echo "⚪️ 未选择 任何第三方软件包"
fi
# 本分支不克隆任何第三方插件仓库（不集成 store / istore 商店）。
# 第三方包统一由下方注入的 Clashoo 源提供。

# 输出调试信息
echo "$(date '+%Y-%m-%d %H:%M:%S') - 开始构建固件..."
echo "查看repositories信息——————"
cat repositories
# 定义所需安装的包列表 下列插件你都可以自行删减
PACKAGES=""
PACKAGES="$PACKAGES curl"
PACKAGES="$PACKAGES bind-tools"
PACKAGES="$PACKAGES ca-certificates"
PACKAGES="$PACKAGES openssh-sftp-server"
# ---- 中文语言包 ----
# ⚠ ImmortalWrt 官方源里，下面三个 app 包（27.150.x）**已内置简体中文**
#   （自带 usr/lib/lua/luci/i18n/*.zh-cn.lmo 与 etc/uci-defaults/luci-i18n-*）。
#   若再显式安装同名的独立 i18n 包（26.236.x），apk 会因文件冲突报
#     ERROR: luci-i18n-xxx-zh-cn-...: trying to overwrite ... owned by luci-app-xxx-...
#   直接中断 make image（package_install 返回 Error 3）。故这三个只装 app 包，
#   中文由包本身提供：
#     luci-app-diskman / luci-app-cpufreq / luci-app-argon-config
#   其余 app 包（26.236.x，如 filemanager / ttyd / firewall / package-manager）
#   未内置中文，仍照常搭配独立的 luci-i18n-<名>-zh-cn 包，不会冲突。
PACKAGES="$PACKAGES luci-app-diskman"
PACKAGES="$PACKAGES luci-i18n-package-manager-zh-cn"
PACKAGES="$PACKAGES luci-i18n-firewall-zh-cn"
PACKAGES="$PACKAGES luci-app-cpufreq"
PACKAGES="$PACKAGES luci-theme-argon"
PACKAGES="$PACKAGES luci-app-argon-config"
PACKAGES="$PACKAGES luci-i18n-ttyd-zh-cn"
# 本分支不集成 Docker
# 文件管理器
PACKAGES="$PACKAGES luci-i18n-filemanager-zh-cn"
# ======== shell/custom-packages.sh =======
# 合并imm仓库以外的第三方插件
PACKAGES="$PACKAGES $CUSTOM_PACKAGES"

# ===========================================================================
# 代理类插件互斥 —— 构建期硬校验
# ---------------------------------------------------------------------------
# 本段 2026-10-13 自 NanoPi-R2S-OpenWrt-Builder 平移而来，与该仓库的校验保持
# 一致（那边默认 Clashoo，这边默认 nikki，仅默认值不同，判定逻辑完全相同）。
#
# 本项目默认启用 nikki（见 shell/custom-packages.sh「一」）。若想换成
# dllkids（kenzok8）源里的其他代理（clashoo / openclash / passwall(2) /
# ssr-plus / homeproxy / dae(d) / shadowsocks / redsocks / xray / v2raya …），
# **必须先停旧的再开新的**，不能双开。
#
# 为什么不能双开 —— 这些插件每一套都会**独自接管整个透明代理**：
#   - 各自写 nftables/iptables 的 REDIRECT / TPROXY 规则（链名与优先级不同，
#     互相覆盖后流量走向不确定）；
#   - 各自劫持 DNS（dnsmasq 的 server/ipset/nftset 与 53 端口占用）；
#   - 抢占同一批端口（7890-7895 / 9090 / 1080 / 53 …）；
#   - 各自注册 /etc/init.d 脚本与 UCI 配置包，LuCI 页面选项重名。
#   后果是规则打架、LuCI 配置错乱、DNS 解析冲突：轻则无法科学上网，
#   重则刷机后直接断网。而且这类问题往往刷完才暴露 —— 所以拦在构建期。
#
# 判定：$PACKAGES 里出现 ≥2 个「代理栈」即失败。
#   ⚠ 必须**整词**匹配，不能用 grep 子串，否则会误判：
#       luci-app-passwall2   是与 luci-app-passwall 不同的包；
#       luci-app-xray-status 是 luci-app-xray 的**配套状态页**（可共存，不算独立栈）；
#       luci-app-daed-next / luci-app-daede 与 luci-app-daed 也是不同包。
#     因此这里用 case 的 " $PACKAGES " 整词匹配，而不是 grep。
#
# 放行：确需同时安装（仅实验用途、不指望能上网）时设 ALLOW_PROXY_CONFLICT=1。
#
# —— 2026-10-12「联动审计」补充的 9 套（原表 18 套漏掉了它们）——
#   证据均可复现（包名/体积取自 dllkids 源目录列表，描述取自上游仓库）：
#     · luci-app-nekobox   —— Thaolga/openwrt-nekobox，自述 "Unlock the Web with
#                             Sing-box"；apk 10.0 MiB（内置 sing-box 内核，
#                             普通 LuCI 前端仅几十 KiB）
#     · luci-app-honk      —— QiuSimons/luci-app-honk，README 自述「基于 eBPF 的
#                             高性能透明代理引擎，dae 兼容」；apk 9.6 MiB
#     · luci-app-jederproxy—— ttimasdf/luci-app-jederproxy，自述 "LuCI interface
#                             and nftables/iptables generator for multiple proxy
#                             backends, supporting mihomo/clash"
#     · luci-app-xjay      —— rafmilecki/luci-app-xjay，自述 "Yet another
#                             xray-core luci app"
#     · luci-app-ss-redir  —— ss-redir 即 shadowsocks 的透明重定向组件；
#                             apk 仅 3.5 KiB（极简配置页，与已确认的
#                             luci-app-redsocks 2.5 KiB 同级）
#     · luci-app-mproxy / -miniproxy / -fchomo —— 按包名（proxy / homo=mihomo）
#                             与体积推断，置信度略低；若确认不是接管型，删行即可。
#     · luci-app-domain-proxy —— mukaiu/luci-app-domain-proxy，Makefile 里
#                             LUCI_TITLE:="Domain Proxy for redsocks."，README
#                             「配合 dnsmasq-full ipset redsocks 使用」⇒ 它是
#                             redsocks 的**另一种前端**（走 dnsmasq+ipset 做域名
#                             分流），与 luci-app-redsocks 共用同一个 redsocks
#                             守护进程。两个前端同装会生成两套 ipset/规则、
#                             两个 init 抢同一进程，故独立成一栈拦下。
#
#   ⚠ 维护提示：dllkids 源有 406 个 luci-app-*，黑名单不可能穷举（这也是那次
#     审计的起因）。所以本文件在校验之后还有一段「关键词启发式提示」兜底
#     （见下方 _PROXY_KW）。**新增/发现新栈时，请三处同步**：
#       1) 本表 _PROXY_STACKS
#       2) README「代理插件互斥」的对照表
#       3) shell/custom-packages.sh 文件头「注意事项 2」的清单
# ===========================================================================
_PROXY_STACKS="
clashoo|luci-app-clashoo clashoo
nikki|luci-app-nikki nikki
openclash|luci-app-openclash
passwall|luci-app-passwall
passwall2|luci-app-passwall2
ssr-plus|luci-app-ssr-plus
homeproxy|luci-app-homeproxy
dae|luci-app-dae dae
daed|luci-app-daed daed
daed-next|luci-app-daed-next
daede|luci-app-daede
shadowsocks|luci-app-shadowsocks
shadowsocks-libev|luci-app-shadowsocks-libev
shadowsocks-rust|luci-app-shadowsocks-rust
redsocks|luci-app-redsocks
redsocks2|luci-app-redsocks2
domain-proxy|luci-app-domain-proxy
xray|luci-app-xray
v2raya|v2raya
ss-redir|luci-app-ss-redir
nekobox|luci-app-nekobox
honk|luci-app-honk honk
jederproxy|luci-app-jederproxy
xjay|luci-app-xjay
mproxy|luci-app-mproxy
miniproxy|luci-app-miniproxy
fchomo|luci-app-fchomo
"
_PROXY_HIT=""
_PROXY_KNOWN=""          # 所有已知 marker 的并集，供下方启发式兜底排除
while IFS='|' read -r _stack _markers; do
    [ -n "$_stack" ] || continue
    for _m in $_markers; do
        _PROXY_KNOWN="$_PROXY_KNOWN $_m"
        case " $PACKAGES " in
            *" $_m "*) _PROXY_HIT="$_PROXY_HIT $_stack"; break ;;
        esac
    done
done <<EOF
$_PROXY_STACKS
EOF
_PROXY_HIT="${_PROXY_HIT# }"
_PROXY_N=0
for _h in $_PROXY_HIT; do _PROXY_N=$((_PROXY_N + 1)); done

if [ "$_PROXY_N" -gt 1 ]; then
    {
        echo ""
        echo "=============================================================="
        echo "ERROR: 检测到 $_PROXY_N 套代理插件同时启用：$_PROXY_HIT"
        echo "--------------------------------------------------------------"
        echo "它们都会各自接管透明代理（防火墙规则 / DNS / TUN / 端口），"
        echo "双开会互相覆盖规则、LuCI 配置错乱、DNS 劫持冲突，"
        echo "轻则无法科学上网，重则刷机后直接断网。"
        echo ""
        echo "改法（shell/custom-packages.sh）—— 先停旧的，再开新的："
        echo "  1) 注释掉「一」里当前启用的那行（默认是 nikki）；"
        echo "  2) 取消你要用的那行注释；"
        echo "  3) 重跑构建。"
        echo ""
        echo "确需同时安装（仅实验用途、不指望能上网）："
        echo "  ALLOW_PROXY_CONFLICT=1 bash rockchip/build.sh"
        echo "=============================================================="
        echo ""
    } >&2
    if [ "${ALLOW_PROXY_CONFLICT:-0}" != "1" ]; then
        exit 1
    fi
    echo "⚠ ALLOW_PROXY_CONFLICT=1：已显式放行代理插件互斥，继续构建（风险自负）。" >&2
else
    echo "✅ 代理插件互斥检查通过：启用 ${_PROXY_N} 套（${_PROXY_HIT:-无}）"
fi

# ---------------------------------------------------------------------------
# 兜底：关键词启发式提示（只提示，不阻断构建）
# ---------------------------------------------------------------------------
# 黑名单挡不住「源里新冒出来的代理前端」——dllkids 有 406 个 luci-app-*，
# 靠人工维护 _PROXY_STACKS 必然滞后（那次审计就发现漏了 8 套）。所以再扫一遍
# 已选包：凡 luci-app-* 命中代理关键词、又不在已知表里的，提示一下，由人判断
# 要不要补进 _PROXY_STACKS。刻意不 exit 1，避免误伤非代理包。
_PROXY_KW="clash mihomo homo nikki passwall ssr shadowsocks redsocks xray xjay
v2ray dae homeproxy hysteria trojan brook naive singbox sing-box nekobox honk proxy"
# 白名单：命中关键词但**已知不属于接管型**的包，与 README「不算冲突」名单对应。
# 三类：状态页（xray-status）/ 服务端（*-server）/ DNS 与过滤层（dnsproxy、
# dnscrypt-proxy2、ikoolproxy、haproxy-tcp）—— 免得兜底提示自相矛盾、喊狼来了。
_PROXY_OK="luci-app-xray-status
luci-app-ssrserver-python luci-app-ssr-mudb-server luci-app-v2ray-server
luci-app-dnscrypt-proxy2 luci-app-dnsproxy luci-app-ikoolproxy luci-app-haproxy-tcp"
_PROXY_SUS=""
for _p in $PACKAGES; do
    case "$_p" in luci-app-*) ;; *) continue ;; esac
    case " $_PROXY_KNOWN " in *" $_p "*) continue ;; esac
    # 白名单用 for 逐项比对，不用 case 整串匹配：_PROXY_OK 是多行变量，
    # case 的 *" $_p "* 需要前导空格，遇到换行会漏判（实测踩过）。
    _skip=""
    for _o in $_PROXY_OK; do
        [ "$_o" = "$_p" ] && { _skip=1; break; }
    done
    [ -n "$_skip" ] && continue
    for _k in $_PROXY_KW; do
        case "$_p" in *"$_k"*) _PROXY_SUS="$_PROXY_SUS $_p"; break ;; esac
    done
done
if [ -n "$_PROXY_SUS" ]; then
    {
        echo ""
        echo "⚠ 提示：以下包名疑似代理类，但不在已知互斥表（_PROXY_STACKS）里："
        echo "   $_PROXY_SUS"
        echo "   若它确实是「透明代理接管型」插件，请补进 rockchip/build.sh 的"
        echo "   _PROXY_STACKS，并同步 README「代理插件互斥」表与"
        echo "   shell/custom-packages.sh 文件头「注意事项 2」的清单。"
        echo ""
    } >&2
fi

unset _PROXY_STACKS _PROXY_HIT _PROXY_N _PROXY_KNOWN _stack _markers _m _h
unset _PROXY_KW _PROXY_OK _PROXY_SUS _p _k _skip _o

# 构建镜像
echo "$(date '+%Y-%m-%d %H:%M:%S') - Building image with the following packages:"
echo "$PACKAGES"

# =========================================================
# 可选内核：OpenClash / SSR-Plus
# =========================================================
# 仅在 $PACKAGES 里出现对应插件时才处理，未启用则整段跳过。
#
# ⚠ 25.12 注意事项：
#   - luci-app-openclash 由官方源与注入的第三方源同时提供，且版本一致
#     （官方 0.47.156 = GitHub Release v0.47.156 = 第三方源 0.47.156-r936），
#     所以**不要**再从 GitHub Release 下 apk 塞进 packages/，
#     否则同一个包出现两个来源，apk 解析会出问题。
#   - 内核优先用源里的 apk 包（如 mihomo），能随源自动更新，
#     不必手动下载并固定某个旧版本二进制。
#   - 只有 OpenClash 的 meta 内核（clash_meta）源里没有，仍需单独下载。
# =========================================================

# ---------- OpenClash：只补 meta 内核 + GeoIP/GeoSite ----------
if echo "$PACKAGES" | grep -q "luci-app-openclash"; then
    echo "✅ 已选择 luci-app-openclash，下载 meta 内核与 GeoIP/GeoSite"

    mkdir -p files/etc/openclash/core
    # meta 内核：只用 OpenClash 官方 core 分支
    # （aarch64_generic → clash-linux-arm64.tar.gz）。
    # 由 OpenClash 自身分发、与之最匹配，含 Smart 策略组支持。
    META_URL="https://raw.githubusercontent.com/vernesong/OpenClash/core/master/meta/clash-linux-arm64.tar.gz"
    META_BIN="files/etc/openclash/core/clash_meta"

    if ! wget -qO- "$META_URL" | tar xOvz > "$META_BIN" 2>/dev/null || [ ! -s "$META_BIN" ]; then
        echo "ERROR: OpenClash meta 内核下载失败：$META_URL" >&2
        exit 1
    fi
    chmod +x "$META_BIN"

    # GeoIP / GeoSite（meta 内核使用）
    if ! wget -q https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat \
            -O files/etc/openclash/GeoIP.dat \
       || ! wget -q https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat \
            -O files/etc/openclash/GeoSite.dat; then
        echo "ERROR: GeoIP/GeoSite 下载失败" >&2
        exit 1
    fi

    ls -lh "$META_BIN" files/etc/openclash/GeoIP.dat files/etc/openclash/GeoSite.dat
else
    echo "⚪️ 未选择 luci-app-openclash"
fi

# ---------- SSR-Plus：内核改用源内 mihomo apk，不手动下二进制 ----------
if echo "$PACKAGES" | grep -q "luci-app-ssr-plus"; then
    echo "✅ 已选择 luci-app-ssr-plus，内核使用源内 mihomo 包"
    # mihomo 由注入的第三方源以 apk 形式提供（当前 1.19.31），
    # 随源更新、可被 apk 正常升级/卸载，比手动 wget 二进制可靠。
    # 原实现固定 v1.19.24 二进制且直接写 /usr/bin，已过时且绕过包管理，故移除。
    if ! echo " $PACKAGES " | grep -q " mihomo "; then
        PACKAGES="$PACKAGES mihomo"
    fi
else
    echo "⚪️ 未选择 luci-app-ssr-plus"
fi

# =========================================================
# 注入 Clashoo（kenzok8）第三方 APK 源
# =========================================================
#
# 官方 Image Builder 的 repositories 只包含 ImmortalWrt 官方源，
# 不含 Clashoo。这里在 make image 之前把 Clashoo 的 APK 源与
# 签名公钥注入 Image Builder 根目录。
#
# 重要：
#   imm.config 启用了 CONFIG_SIGNATURE_CHECK=y，
#   因此 apk 会校验源签名 —— 必须提供公钥，
#   否则安装 Clashoo 时会报 "UNTRUSTED signature"。
#
# 说明：
#   - 内核为官方原版（路线 C），Clashoo 依赖的 kmod
#     (kmod-tun / kmod-nft-tproxy 等) 由官方 kmods 源解析；
#   - Clashoo 包本体为纯用户态，不受内核 ABI 影响。
# =========================================================

CLASHOO_FEED="https://down.dllkids.xyz/openwrt-feed/25.12/aarch64_generic/packages.adb"
CLASHOO_KEY_URL="https://down.dllkids.xyz/openwrt-feed/keys/dllkids-feed.pub.pem"

echo "==> 注入 Clashoo APK 源..."

grep -qxF "$CLASHOO_FEED" repositories 2>/dev/null || echo "$CLASHOO_FEED" >> repositories

mkdir -p keys
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$CLASHOO_KEY_URL" -o keys/dllkids-feed.pub.pem
elif command -v wget >/dev/null 2>&1; then
    wget -q -O keys/dllkids-feed.pub.pem "$CLASHOO_KEY_URL"
else
    echo "ERROR: 缺少 curl/wget，无法下载 Clashoo 签名公钥" >&2
    exit 1
fi

echo "---- repositories ----"
cat repositories
echo "---- keys ----"
ls -l keys

# =========================================================
# 让固件也带上该源与公钥，使设备端可以直接 apk add / apk upgrade Clashoo 系包
# =========================================================
# 上面注入的 repositories / keys 只用于【构建期】：Image Builder 不会把它们
# 拷贝进固件根文件系统（固件的 distfeeds.list 来自官方预编译的 base-files）。
# 所以这里显式写入 files/，随 make image 的 FILES= 一起进入固件。
#
#   /etc/apk/repositories.d/customfeeds.list   第三方源地址
#   /etc/apk/keys/dllkids-feed.pub.pem         公钥（CONFIG_SIGNATURE_CHECK=y，缺它会被拒签）
#
# customfeeds.list 是 apk 包为「用户自加第三方源」预留的 conffile，
# 上游默认内容就是三行注释占位（package/system/apk/files/customfeeds.list）。
# 这里原样保留那三行，并在末尾追加本项目的源 —— 与手工添加的做法一致。
# =========================================================
mkdir -p files/etc/apk/repositories.d files/etc/apk/keys

cat > files/etc/apk/repositories.d/customfeeds.list <<EOF
# add your custom package feeds here
#
# http://www.example.com/path/to/files/packages.adb
$CLASHOO_FEED
EOF

cp -f keys/dllkids-feed.pub.pem files/etc/apk/keys/dllkids-feed.pub.pem

echo "---- 随固件发布的第三方源 ----"
cat files/etc/apk/repositories.d/customfeeds.list
echo "---- 随固件发布的公钥 ----"
ls -l files/etc/apk/keys/

make image PROFILE=$PROFILE PACKAGES="$PACKAGES" FILES="$BUILDER_DIR/files" ROOTFS_PARTSIZE=$ROOTFS_PARTSIZE

if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Error: Build failed!"
    exit 1
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') - Build completed successfully."
