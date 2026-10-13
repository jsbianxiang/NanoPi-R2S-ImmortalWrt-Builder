#!/bin/sh
# 99-custom.sh —— ImmortalWrt 固件首次启动时执行的初始化脚本
# （固件内路径：/etc/uci-defaults/99-custom.sh）
#
# =========================================================
# 本脚本**只做一件事**：把工作流构建时传入的地址参数写进 uci 配置。
#
#   1) WAN 接入方式（wan_proto / wan_ip / wan_gateway / wan_dns）
#   2) LAN 管理地址（custom_router_ip）
#
# 除此之外**不做任何定制** —— 网络调优、DHCP/DNS、防火墙、主题、
# ttyd/dropbear 监听范围等一律保持 ImmortalWrt 官方镜像默认，行为与官方
# Image Builder 直接产出的镜像一致。需要额外调整请自行在 LuCI 里改，
# 或在本脚本末尾追加（记得 uci commit）。
#
# 本分支专用于 FriendlyARM NanoPi R2S，机型固定，因此这里**不做**任何
# 多机型 / 多网口的接口探测与映射。R2S 的网口是硬件固定的：
#
#   eth0 = 板载 GMAC（RK3328 原生，靠近 GPIO 排针）  → WAN
#   eth1 = USB3 RTL8153（靠近 USB 口）               → LAN
#
# 与官方 board.d 完全一致：
#   target/linux/rockchip/armv8/base-files/etc/board.d/02_network
#     friendlyarm,nanopi-r2s)  ucidef_set_interfaces_lan_wan 'eth1' 'eth0'
#
# 设备、br-lan 桥与端口、MAC 等都由官方 /bin/config_generate 生成
# （它在 **preinit_main** 阶段运行 —— /lib/preinit/82_config_generate，
#   早于本脚本，所以本脚本的 uci set 不会被覆盖）。
# MAC 来自 macaddr_generate_from_mmc_cid mmcblk0，即由 SD 卡 CID 派生，
# 同一张卡固定不变；LAN MAC = WAN MAC + 1。
# =========================================================

# ---------------------------------------------------------------------------
# 构建期注入的辅助文件统一放在 /etc/immortalwrt-builder/。
#
# ⚠ 不要把任何**非 uci 配置包**的文件放进 /etc/config/：
#   libuci 会 glob 整个配置目录（file.c: uci_list_config_files() 用
#   glob("<confdir>/*", GLOB_MARK)），只跳过「文件名含 `.`」的条目
#   （file.c: get_filename()，源码注释原文 "Filename with '.' are ignored
#   and not parsed"），其余一律按 uci 语法解析。无后缀文件（如 wan-settings）
#   会被当成配置包解析，首行 `wan_proto=dhcp` 立刻命中 file.c:553 的
#   uci_parse_error(ctx, "invalid command")，于是**任何** uci 调用
#   （含 LuCI「软件包」页的安装动作）都会打印：
#       uci: Parse error (invalid command) at line 1, byte 0
#   本脚本自身的日志也遵循该约定，放在 /etc/immortalwrt-builder/ 下。
# ---------------------------------------------------------------------------
SETTINGS_DIR='/etc/immortalwrt-builder'
mkdir -p "$SETTINGS_DIR" 2>/dev/null || :

# 迁移兼容：旧版固件把 wan-settings 误放在 /etc/config/ 下（无后缀 → 被 uci
# 当作配置包解析）。保留配置升级到本版时该文件会被 sysupgrade 带过来，因此
# 这里在**任何 uci 调用之前**先删掉它，否则本脚本自身的 uci 调用也会刷出
# "Parse error (invalid command)"。
# （/etc/config/ 下旧版遗留的 custom_router_ip.txt、uci-defaults-log.txt 带 `.`
#  后缀，uci 的 get_filename() 会跳过，无害，故不动它们。）
rm -f /etc/config/wan-settings 2>/dev/null || :

LOGFILE="$SETTINGS_DIR/uci-defaults-log.txt"
echo "Starting 99-custom.sh at $(date)" >>$LOGFILE

# =========================================================
# 1. WAN 接入方式
# =========================================================
# wan-settings 由 rockchip/build.sh 生成（内容取自工作流输入），含
# wan_proto / wan_ip / wan_gateway / wan_dns。
# 位置：/etc/immortalwrt-builder/wan-settings（**不是** /etc/config/，见文件开头说明）。
# 默认 dhcp（与 ImmortalWrt 官方默认一致）；构建时选 static 才用后三项。
#
# ⚠ 这里**逐行解析**，不要写成 `. "$SETTINGS_FILE"`（source）：
#   source 会把文件内容当 shell 代码执行，于是
#     - 含空格的值（如 wan_dns='223.5.5.5 1.1.1.1'）会被拆成
#       「一次性赋值 + 执行命令 1.1.1.1」，前置赋值不持久 → 整个值丢失，
#       并静默回落到网关（曾实测复现）；
#     - 值里的 $( ) / ; / && 会在首启时被当命令执行。
#   `while IFS='=' read -r _k _v` 只按第一个 `=` 切分，把其后的**整行剩余内容**
#   原样取为值 —— 既支持空格分隔的多值 DNS，也不执行任何内容。
SETTINGS_FILE="$SETTINGS_DIR/wan-settings"
if [ -f "$SETTINGS_FILE" ]; then
    while IFS='=' read -r _k _v; do
        case "$_k" in
            wan_proto)   wan_proto="$_v" ;;
            wan_ip)      wan_ip="$_v" ;;
            wan_gateway) wan_gateway="$_v" ;;
            wan_dns)     wan_dns="$_v" ;;
        esac
    done < "$SETTINGS_FILE"
    unset _k _v
else
    echo "WAN settings file not found. Using built-in defaults." >>$LOGFILE
fi

# 兜底默认值（wan-settings 缺失或字段为空时生效）
wan_proto="${wan_proto:-dhcp}"
wan_ip="${wan_ip:-192.168.1.2}"
wan_gateway="${wan_gateway:-192.168.1.1}"
wan_dns="${wan_dns:-$wan_gateway}"

# WAN 固定为 eth0（板载 GMAC）。官方 config_generate 已按 board.d 生成
# network.wan（device=eth0 / proto=dhcp），这里只按工作流输入调整 proto 与地址；
# 显式写 device 是为了不依赖 config_generate 的生成结果，保证一定生效。
uci set network.wan=interface
uci set network.wan.device='eth0'
case "$wan_proto" in
    static)
        uci set network.wan.proto='static'
        # 地址用 CIDR 列表记法（`list ipaddr '192.168.1.2/24'`），不写 netmask ——
        # 与 LuCI「接口 → WAN → 常规设置 → IPv4 地址：切换到 CIDR 列表记法」一致。
        uci -q delete network.wan.netmask
        uci -q delete network.wan.ipaddr
        case "$wan_ip" in
            */*) uci add_list network.wan.ipaddr="$wan_ip" ;;
            *)   uci add_list network.wan.ipaddr="$wan_ip/24" ;;
        esac
        uci set network.wan.gateway="$wan_gateway"
        # DNS 同样用列表记法（可填多个，空格分隔）。
        # ⚠ `for _dns in $wan_dns` 在分词之外还会做**路径名展开**：若值里含
        #   `*` / `?` / `[`，会被 glob 成当前目录下的文件名（例如 wan_dns='*'
        #   会写入一堆文件名），且不报错。故此处临时 `set -f` 关闭 glob；
        #   子 shell 包裹保证退出后不影响后续代码。
        uci -q delete network.wan.dns
        (
            set -f
            for _dns in $wan_dns; do
                uci add_list network.wan.dns="$_dns"
            done
        )
        unset _dns
        echo "WAN static: ip=$wan_ip gw=$wan_gateway dns=$wan_dns" >>$LOGFILE
        ;;
    *)
        uci set network.wan.proto='dhcp'
        # 清掉可能残留的静态地址（例如从 static 改为 dhcp、且升级时保留配置）
        for _opt in ipaddr netmask gateway dns; do
            uci -q delete "network.wan.$_opt"
        done
        unset _opt
        echo "WAN dhcp (official default)" >>$LOGFILE
        ;;
esac

# WAN6（IPv6）**不在本脚本设置**，保持官方默认：
# 官方 /bin/config_generate 在 proto=dhcp 时已自动生成
#   network.wan6 = interface, device=eth0, proto=dhcpv6
# 向光猫请求 IPv6 地址与 PD 前缀（供下游 LAN 下发 IPv6），故 IPv6 双栈不受影响。
# 如需调整（例如加 norelease 以减少重启后前缀变化），请自行在 LuCI 或此处追加。

# =========================================================
# 2. LAN 管理地址
# =========================================================
# LAN 固定为 eth1（USB RTL8153）；br-lan 桥及其端口由官方 config_generate 生成，
# 这里只改「管理地址」，不动 device / ports。
# 地址由工作流的 custom_router_ip 传入
# （位置：/etc/immortalwrt-builder/custom_router_ip.txt，见文件开头说明）。
# 同样采用 CIDR 列表记法，与 LuCI 切换后的形态一致（netifd 会从前缀长度推出掩码）。
CUSTOM_IP='192.168.2.1'
IP_VALUE_FILE="$SETTINGS_DIR/custom_router_ip.txt"
if [ -f "$IP_VALUE_FILE" ]; then
    # 逐行读首个非空行 —— 既可裁掉可能的结尾换行，也能剔除首尾空白：
    #   `_tmp=$(cat file)` 会**保留**首尾空格，若文件是 "192.168.2.1  " 就会
    #   拼成 "192.168.2.1  /24" 导致 netifd 解析失败；纯空白文件还会被
    #   `[ -n ]` 误判为「非空」。这里用 read 天然跳过前导空白，再手动裁尾部。
    _tmp=''
    if read -r _tmp < "$IP_VALUE_FILE" 2>/dev/null; then :; fi
    # 裁掉尾部空白（前导空白已由 read 的 IFS 机制吃掉）
    while [ "${_tmp%[[:space:]]}" != "$_tmp" ]; do
        _tmp="${_tmp%[[:space:]]}"
    done
    if [ -n "$_tmp" ]; then
        CUSTOM_IP="$_tmp"
        echo "custom router ip is $CUSTOM_IP" >>$LOGFILE
    else
        echo "custom_router_ip.txt is empty, using default 192.168.2.1" >>$LOGFILE
    fi
else
    echo "default router ip is 192.168.2.1" >>$LOGFILE
fi
unset _tmp

uci set network.lan.proto='static'
uci -q delete network.lan.netmask
uci -q delete network.lan.ipaddr
case "$CUSTOM_IP" in
    */*) uci add_list network.lan.ipaddr="$CUSTOM_IP" ;;
    *)   uci add_list network.lan.ipaddr="$CUSTOM_IP/24" ;;
esac

uci commit network

# 仅记录实际接口映射，便于排查问题（不修改任何配置）
echo "iface map: lan=$(uci -q get network.lan.device) wan=$(uci -q get network.wan.device) wan6=$(uci -q get network.wan6.device)" >>$LOGFILE

# 本分支不做品牌改写，固件保留 ImmortalWrt 官方 DISTRIB_DESCRIPTION

# =========================================================
# 以下各项**一律保持 ImmortalWrt 官方镜像默认**，本脚本不再干预
# （如需修改，请自行在 LuCI 里改，或在此处追加 uci 命令 + uci commit）：
#
#   - 网络全局 packet_steering、接口 multipath
#   - DHCP / DNS：dnsmasq（含 dns_redirect 等）、odhcpd 各项
#   - 防火墙：fullcone / syn_flood / flow_offloading 等
#   - 默认主题（官方默认 Argon）
#   - ttyd / dropbear 的监听范围（上游默认：仅局域网可管理）
#
# ⚠ 提示：ImmortalWrt 的防火墙出厂配置（由 firewall4 补丁写入）为
#   flow_offloading 1 / flow_offloading_hw 1 / fullcone 1 / fullcone6 0；
#   若你启用了 nikki 等 TPROXY 透明代理，flow offload 会让首包之后的流量
#   绕过 netfilter 钩子而使代理失效，需自行在
#   LuCI「网络 → 防火墙 → 常规设置」里关闭「软件流量分载 / 硬件流量分载」。
# =========================================================

exit 0
