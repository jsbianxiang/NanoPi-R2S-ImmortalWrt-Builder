#!/bin/bash
# 构建期日志（注意：固件内的首启日志是 /etc/immortalwrt-builder/uci-defaults-log.txt，两者不是一回事）
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
# 第三方包统一由下方注入的 dllkids（kenzok8）源提供。

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
# ⚠ 下面三个 app 包**已内置简体中文**，不要再配同名的独立 i18n 包：
#     luci-app-diskman / luci-app-cpufreq / luci-app-argon-config
#   机制：这三个包在 **dllkids 第三方源**里有 27.150.x 版本，包内自带
#     usr/lib/lua/luci/i18n/<名>.zh-cn.lmo 与 etc/uci-defaults/luci-i18n-<名>-zh-cn
#   （官方源同名包是 26.236.x、diskman 为 0.2.13-r1，**不含**中文）；
#   而 apk **取版本高者** ⇒ 实际装上的是 dllkids 的 27.150.x 构建。
#   此时若再显式安装官方源的 luci-i18n-<名>-zh-cn（26.236.x），apk 会因文件冲突报
#     ERROR: luci-i18n-xxx-zh-cn-...: trying to overwrite ... owned by luci-app-xxx-...
#   直接中断 make image（package_install 返回 Error 3）。
#   （已实测：构建日志显示装入的正是 27.150.62553~79d9f36，且未装同名 i18n 包。）
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
#   ⚠ 维护提示：dllkids 源有 411 个 luci-app-*，黑名单不可能穷举（这也是那次
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
# 黑名单挡不住「源里新冒出来的代理前端」——dllkids 有 411 个 luci-app-*，
# 靠人工维护 _PROXY_STACKS 必然滞后（那次审计就发现漏了 8 套）。所以再扫一遍
# 已选包：凡 luci-app-* 命中代理关键词、又不在已知表里的，提示一下，由人判断
# 要不要补进 _PROXY_STACKS。刻意不 exit 1，避免误伤非代理包。
#
# ⚠ 已知盲区（有意保留，不做自动扫描）：
#   本启发式只扫 `luci-app-*` 前缀的包。代理栈里有若干是**纯名**包（不带
#   luci-app- 前缀）—— nikki / honk / daed / dae / v2raya / clashoo / mihomo …
#   它们**已在 _PROXY_STACKS 表里**，硬校验覆盖得到，所以漏不掉；但将来若源里
#   出现**新的纯名**代理前端，这里不会给出提示，需要人工发现并补表。
#   之所以不顺手对纯名包也扫一遍关键词：纯名空间里误报面大得多 ——
#   dnscrypt-proxy2 / https-dns-proxy / tinyproxy / ikoolproxy / dnsproxy /
#   haproxy-tcp 这些**非接管型**包名同样含 "proxy"，会被一并喊成「疑似代理类」，
#   反而把白名单拖进来说不清、降低这个提示的可信度。宁可少喊，不可乱喊。
_PROXY_KW="clash mihomo homo nikki passwall ssr shadowsocks redsocks xray xjay
v2ray dae homeproxy hysteria trojan brook naive singbox sing-box nekobox honk proxy"
# 白名单：命中关键词但**已知不属于接管型**的包，与 README「不算冲突」名单对应。
# 四类：状态页（xray-status）/ 服务端（*-server）/ DNS 与过滤层（dnsproxy、
# dnscrypt-proxy2、ikoolproxy、haproxy-tcp、https-dns-proxy）/ 非接管的显式
# 代理（tinyproxy 是 HTTP 正向代理，只监听端口供客户端显式指定，不写
# nftables/TPROXY 规则）—— 免得兜底提示自相矛盾、喊狼来了。
# ⚠ 后两项（https-dns-proxy / tinyproxy）来自 **ImmortalWrt 官方源**，不在
#   dllkids 里（实测 25.12.2 luci feed：26.236.50544~cb5d434 / 2026.05.06-r1）。
#   它们经 shell/custom-packages.sh 的短名清单进入 $PACKAGES，同样会被下面的
#   关键词启发式扫到，所以照样要留在白名单里。
_PROXY_OK="luci-app-xray-status
luci-app-ssrserver-python luci-app-ssr-mudb-server luci-app-v2ray-server
luci-app-dnscrypt-proxy2 luci-app-dnsproxy luci-app-ikoolproxy luci-app-haproxy-tcp
luci-app-https-dns-proxy luci-app-tinyproxy"
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
#   - luci-app-openclash 由官方源与注入的第三方源同时提供，但**两者版本会各自
#     演进，不要假定一致**（实测 2026-10-14：官方 luci feed 0.47.156 /
#     dllkids 0.48.002-r974，第三方源已超前一个版本）。所以**不要**再从
#     GitHub Release 下 apk 塞进 packages/，否则同一个包出现两个来源，
#     apk 解析会出问题。
#   - 官方源与 dllkids 存在大量**同名包**（实测 150 个，占 dllkids 的 24%）：
#     hysteria（官方 2.7.0-r1 / dllkids 2.13.0-r1）、naiveproxy（140.x / 154.x）、
#     sing-box（1.12.25-r1 / 1.14.2-r1）、luci-theme-argon、luci-app-passwall …
#     apk 解析时**取版本高者**，故这些同名包实际装的是 dllkids 的构建。
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
    META_TMP="files/etc/openclash/core/.clash_meta.tmp"

    # 下载到临时文件、三重校验通过才 mv 成正式文件 —— 管道直写成品时，
    # 「截断但 tar 恰好不报错」会把残缺二进制当成品发出去：
    #   ① pipefail 下 wget / tar 任一失败即失败；
    #   ② 体积 ≥ 4 MiB（clash_meta linux-arm64 实测 ~10 MiB，截断必远小于此）；
    #   ③ 文件头是 ELF 且 e_machine=0xB7（EM_AARCH64），防下错架构/错误页。
    if ! ( set -o pipefail; wget -qO- "$META_URL" | tar xOvz > "$META_TMP" 2>/dev/null ); then
        echo "ERROR: OpenClash meta 内核下载失败：$META_URL" >&2
        rm -f "$META_TMP"
        exit 1
    fi
    META_SIZE=$(wc -c < "$META_TMP" 2>/dev/null)
    if [ "${META_SIZE:-0}" -lt 4194304 ]; then
        echo "ERROR: meta 内核体积异常（${META_SIZE:-0} B < 4 MiB），疑似截断：$META_URL" >&2
        rm -f "$META_TMP"
        exit 1
    fi
    if [ "$(od -An -tx1 -N4 "$META_TMP" | tr -d ' \n')" != "7f454c46" ] \
       || [ "$(od -An -tx1 -j18 -N2 "$META_TMP" | tr -d ' \n')" != "b700" ]; then
        echo "ERROR: meta 内核不是 aarch64 ELF，疑似下错文件：$META_URL" >&2
        rm -f "$META_TMP"
        exit 1
    fi
    mv -f "$META_TMP" "$META_BIN"
    chmod +x "$META_BIN"

    # GeoIP / GeoSite（meta 内核使用）
    if ! wget -q https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat \
            -O files/etc/openclash/GeoIP.dat \
       || ! wget -q https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat \
            -O files/etc/openclash/GeoSite.dat; then
        echo "ERROR: GeoIP/GeoSite 下载失败" >&2
        exit 1
    fi
    # wget 退出码之外补 -s 非空校验：个别 CDN 故障返回 200 + 空 body 时
    # 退出码反而是 0，只有体积能发现。
    for _dat in files/etc/openclash/GeoIP.dat files/etc/openclash/GeoSite.dat; do
        if [ ! -s "$_dat" ]; then
            echo "ERROR: $_dat 为空（下载被静默截断）" >&2
            exit 1
        fi
    done
    unset _dat

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

# ---------- nikki / SSR-Plus / Clashoo 内核：可选钉版到最新正式版（默认关闭） ----------
# 背景（2026-10-23 上游核实）：
#   · nikki 自身没有内核频道开关 —— nikki.init 直接执行 /usr/bin/mihomo，
#     nikki.conf 与 LuCI 视图（app/editor/log/mixin/profile/proxy）均无版本
#     设置；内核完全由 mihomo 包提供（实体 /usr/libexec/mihomo-core，
#     /usr/bin/mihomo 是 alternatives 链接）。
#   · 本构建的内核 = dllkids 源 mihomo apk，由 kenzok8/small 从 mihomo
#     **正式版 tag** 编译（Makefile 源地址 codeload.github.com/metacubex/
#     mihomo/tar.gz/v<版本>；提交历史 1.19.21→1.19.32 全是正式版），
#     **不是 Alpha**。Alpha 频道在上游是独立的 Prerelease-Alpha tag，
#     只有 nikki 官方 feed 的 mihomo-alpha 包跟它；本构建不引入该包，
#     天然与 Alpha 无关。
#   · 唯一差距是"滞后"：源内 1.19.31（2026-09-14）vs 上游最新正式版
#     v1.19.33（2026-10-21，kenzok8/small master 已跟到 1.19.32，
#     dllkids 二进制源尚未重建）。要严格用最新正式版时，取消下面两行
#     注释（版本与 sha256 必须成对填写，缺一即报错退出）：
#       sha256 取法：GitHub Release 页面 mihomo-linux-arm64-<版本>.gz
#       资产旁标注的哈希（expanded_assets 视图可见）。
#   生效方式：覆盖 files/usr/libexec/mihomo-core（FILES= 在 apk 安装
#     之后叠加，alternatives 链接与 nikki 的 DEPENDS:+mihomo 均不受
#     影响 —— 依赖仍由源内 mihomo apk 满足，只是二进制被换掉）。
#   ⚠ 设备端若 apk upgrade mihomo（固件自带 dllkids 源）会把文件恢复
#     成源内版本，需重刷固件或再替换一次。
#MIHOMO_CORE_VER="v1.19.33"
#MIHOMO_CORE_SHA256="ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187"

if [ -n "${MIHOMO_CORE_VER:-}" ]; then
    if [ -z "${MIHOMO_CORE_SHA256:-}" ]; then
        echo "ERROR: MIHOMO_CORE_VER=${MIHOMO_CORE_VER} 已设置但 MIHOMO_CORE_SHA256 为空，拒绝下载未校验内核" >&2
        exit 1
    fi
    if echo " $PACKAGES " | grep -qE " (nikki|luci-app-nikki|luci-app-ssr-plus|clashoo|luci-app-clashoo) "; then
        MIHOMO_CORE_URL="https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_CORE_VER}/mihomo-linux-arm64-${MIHOMO_CORE_VER}.gz"
        MIHOMO_CORE_GZ="$BUILDER_DIR/files/usr/libexec/.mihomo-core.gz"
        MIHOMO_CORE_BIN="$BUILDER_DIR/files/usr/libexec/.mihomo-core"
        mkdir -p "$BUILDER_DIR/files/usr/libexec"
        echo "==> 下载 mihomo ${MIHOMO_CORE_VER}（linux-arm64 正式版资产）..."
        if ! curl -fL --retry 3 --connect-timeout 30 -o "$MIHOMO_CORE_GZ" "$MIHOMO_CORE_URL"; then
            echo "ERROR: mihomo 内核下载失败：$MIHOMO_CORE_URL" >&2
            rm -f "$MIHOMO_CORE_GZ"; exit 1
        fi
        if ! echo "${MIHOMO_CORE_SHA256}  ${MIHOMO_CORE_GZ}" | sha256sum -c -; then
            echo "ERROR: mihomo 内核 sha256 校验失败（版本与资产不匹配？）" >&2
            rm -f "$MIHOMO_CORE_GZ"; exit 1
        fi
        if ! gunzip -c "$MIHOMO_CORE_GZ" > "$MIHOMO_CORE_BIN"; then
            echo "ERROR: mihomo 内核解压失败" >&2
            rm -f "$MIHOMO_CORE_GZ" "$MIHOMO_CORE_BIN"; exit 1
        fi
        rm -f "$MIHOMO_CORE_GZ"
        # 三重校验（与 OpenClash meta 内核同口径）：体积 / ELF magic / e_machine=EM_AARCH64
        MIHOMO_CORE_SIZE=$(wc -c < "$MIHOMO_CORE_BIN" 2>/dev/null)
        if [ "${MIHOMO_CORE_SIZE:-0}" -lt 4194304 ]; then
            echo "ERROR: mihomo 内核体积异常（${MIHOMO_CORE_SIZE} 字节 < 4 MiB）" >&2
            rm -f "$MIHOMO_CORE_BIN"; exit 1
        fi
        if [ "$(od -An -tx1 -N4 "$MIHOMO_CORE_BIN" | tr -d ' \n')" != "7f454c46" ] \
           || [ "$(od -An -tx1 -j18 -N2 "$MIHOMO_CORE_BIN" | tr -d ' \n')" != "b700" ]; then
            echo "ERROR: mihomo 内核文件头不是 aarch64 ELF" >&2
            rm -f "$MIHOMO_CORE_BIN"; exit 1
        fi
        mv -f "$MIHOMO_CORE_BIN" "$BUILDER_DIR/files/usr/libexec/mihomo-core"
        chmod 0755 "$BUILDER_DIR/files/usr/libexec/mihomo-core"
        echo "✅ mihomo 内核已钉版 ${MIHOMO_CORE_VER}（覆盖源内 apk 同名文件，$(du -h "$BUILDER_DIR/files/usr/libexec/mihomo-core" | cut -f1)）"
    else
        echo "⚠️ MIHOMO_CORE_VER 已设置，但未启用使用 mihomo 内核的代理栈（nikki / ssr-plus / clashoo），跳过钉版"
    fi
fi

# =========================================================
# 注入 dllkids（kenzok8）第三方 APK 源
# =========================================================
#
# 官方 Image Builder 的 repositories 只包含 ImmortalWrt 官方源，
# 不含 dllkids。这里在 make image 之前把 dllkids 的 APK 源与
# 签名公钥注入 Image Builder 根目录。
#
# 重要：
#   imm.config 启用了 CONFIG_SIGNATURE_CHECK=y，
#   因此 apk 会校验源签名 —— 必须提供公钥，
#   否则安装 dllkids 源里的包时会报 "UNTRUSTED signature"。
#
# 说明：
#   - 内核为官方原版（路线 C），dllkids 源里的包所依赖的 kmod
#     (kmod-tun / kmod-nft-tproxy 等) 由官方 kmods 源解析；
#   - 代理类包本体为纯用户态，不受内核 ABI 影响。
# =========================================================

DLLKIDS_FEED="https://down.dllkids.xyz/openwrt-feed/25.12/aarch64_generic/packages.adb"
DLLKIDS_KEY_URL="https://down.dllkids.xyz/openwrt-feed/keys/dllkids-feed.pub.pem"

echo "==> 注入 dllkids APK 源..."

grep -qxF "$DLLKIDS_FEED" repositories 2>/dev/null || echo "$DLLKIDS_FEED" >> repositories

mkdir -p keys
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$DLLKIDS_KEY_URL" -o keys/dllkids-feed.pub.pem
elif command -v wget >/dev/null 2>&1; then
    wget -q -O keys/dllkids-feed.pub.pem "$DLLKIDS_KEY_URL"
else
    echo "ERROR: 缺少 curl/wget，无法下载 dllkids 签名公钥" >&2
    exit 1
fi

echo "---- repositories ----"
cat repositories
echo "---- keys ----"
ls -l keys

# =========================================================
# 让固件也带上该源与公钥，使设备端可以直接 apk add / apk upgrade dllkids 源里的包
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
$DLLKIDS_FEED
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
