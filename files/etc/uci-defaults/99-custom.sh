#!/bin/sh
# 99-custom.sh —— ImmortalWrt 固件首次启动时执行的初始化脚本
# （固件内路径：/etc/uci-defaults/99-custom.sh）
#
# =========================================================
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
# 设备、br-lan 桥与端口、MAC 等都由官方 /bin/config_generate 在首次启动时生成
# （MAC 来自 macaddr_generate_from_mmc_cid mmcblk0，即由 SD 卡 CID 派生，
#   同一张卡固定不变；LAN MAC = WAN MAC + 1）。
#
# 因此本脚本负责：
#   1) WAN / WAN6 接入方式
#   2) LAN 管理地址
#   3) 网络全局与接口调优
#   4) DHCP / DNS（dnsmasq / odhcpd）
#   5) 防火墙
#   6) 默认主题（bootstrap）
#   7) ttyd / dropbear 的监听范围（保持上游默认，仅局域网可管理）
#
# 第 3～5 节的条目由「在 LuCI 里逐页点保存」的等价 uci 操作整理而来，
# 每条都标注了 [生效] / [等价] / [已移除] 三类之一：
#   [生效]   真正改变运行行为（README「首启初始化」小节有汇总）；
#   [等价]   只是 LuCI 的写法归一化 —— 或改记法，或删掉「已废弃 / 本来就等于
#            该选项默认值」的项，语义与改动前完全一致；
#   [已移除] 原实现写过、后经核实属无效或有害，现已删除（保留注释说明原因，
#            防止日后被误从上游合回）。
# 之所以把 [等价] 项也写进来，是为了让首启后的 `uci show` 与手工配置过的
# 设备逐行一致，便于比对排障。判断依据均来自上游源码（odhcpd config.c、
# dnsmasq.init、firewall4 fw4.uc、LuCI zones.js），注释里给了出处。
# =========================================================

LOGFILE="/etc/config/uci-defaults-log.txt"
echo "Starting 99-custom.sh at $(date)" >>$LOGFILE

# 原实现这里有一行 `uci set firewall.@zone[1].input='ACCEPT'`（放开 WAN 入站），
# 因 WAN 常处于上级设备的内网段、且 wan6 的 IPv6 公网可路由，暴露面过大，已移除。
# 现在 WAN 入站沿用 firewall4 官方默认（REJECT），详见 README「WAN 入站防火墙」。
# 如需临时从 WAN 侧访问：网络 → 防火墙 → wan 的入站数据 → 选择「接受」→ 保存并应用。

# 原实现这里有一段 `dhcp domain` 静态解析，把安卓的 NTP 域名 time.android.com
# 指向 203.107.6.88（阿里云 NTP），用于规避部分安卓 TV/盒子因对时失败而判定「无网络」。
# 该修复生效面很窄（仅对以本机作 DNS 的 LAN 客户端、且只覆盖这一个域名），已移除。

# =========================================================
# 1. WAN / WAN6 接入方式
# =========================================================
# wan-settings 由 rockchip/build.sh 生成，含 wan_proto / wan_ip / wan_gateway / wan_dns。
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
SETTINGS_FILE="/etc/config/wan-settings"
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

# WAN 固定为 eth0（板载 GMAC）。显式声明 section/device/proto，
# 不依赖 config_generate 的生成结果与先后顺序，保证一定生效。
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
        # [等价] sendclientid='auto'：IPv4 DHCP 请求里发送的客户端标识取「自动」。
        #   ⚠ netifd 的 dhcp.sh 用的是
        #     `case "$sendclientid" in global) … hardware) … none) … auto|*) … esac`
        #   —— **「未设」落进 `*)`，与 `auto` 走的是同一段代码**，
        #   所以这一行语义上等于不写（默认就是 auto）。写它只为让首启后的
        #   `uci show` 与在 LuCI 里保存过该页的设备逐行一致。
        uci set network.wan.sendclientid='auto'
        echo "WAN dhcp (official default)" >>$LOGFILE
        ;;
esac

# WAN6：DHCPv6 客户端，向光猫请求 IPv6 地址与 PD 前缀（供下游 LAN 下发 IPv6）
uci set network.wan6=interface
uci set network.wan6.device='eth0'
uci set network.wan6.proto='dhcpv6'
uci set network.wan6.reqaddress='try'
uci set network.wan6.reqprefix='auto'
# [生效] norelease：重启/断开时不发 DHCPv6 RELEASE，减少上级设备立刻回收地址、
#   导致重启后前缀变化的情况（LuCI 该复选框的说明即「minimise the chance of
#   prefix change after a restart」）。
uci set network.wan6.norelease='1'
# [等价] sendclientid='auto'：DHCPv6 请求里发送的客户端标识取「自动」
#   （优先用本机 DUID，回退到接口 MAC 派生的 DUID-LL）。
#   ⚠ odhcp6c 的 dhcpv6.sh 同样是 `case "$sendclientid" in global) … hardware) …
#     auto|*) … esac` —— **「未设」落进 `*)`，与 `auto` 同一分支**，故这行等于不写。
uci set network.wan6.sendclientid='auto'

# =========================================================
# 2. LAN 管理地址
# =========================================================
# LAN 固定为 eth1（USB RTL8153）；br-lan 桥及其端口由官方 config_generate 生成，
# 这里只改「管理地址」，不动 device / ports。
# 地址由工作流的 custom_router_ip 传入（文件挂在 /etc/config/）。
# 同样采用 CIDR 列表记法，与 LuCI 切换后的形态一致（netifd 会从前缀长度推出掩码）。
uci set network.lan.proto='static'
uci -q delete network.lan.netmask
uci -q delete network.lan.ipaddr

CUSTOM_IP='192.168.2.1'
IP_VALUE_FILE="/etc/config/custom_router_ip.txt"
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

case "$CUSTOM_IP" in
    */*) uci add_list network.lan.ipaddr="$CUSTOM_IP" ;;
    *)   uci add_list network.lan.ipaddr="$CUSTOM_IP/24" ;;
esac

# =========================================================
# 3. 网络全局与接口调优
# =========================================================
# [等价] 数据包引导（packet steering）显式写 '1'。
#   该值由 /usr/libexec/network/packet-steering.uc 消费，而它只特判
#   '0'（关闭）与 '2'（用全部 CPU）—— **'1' 与「未设」都不带任何开关，
#   走同一套默认分摊算法**，所以这一行语义上等于不写。
#   官方 99-default-settings 只为 bcm4908 / bcm53xx / ramips-mt7621 / x86 写入该项
#   （rockchip 不在其列，故本机默认就是「未设」）；这里显式写 '1' 只为让首启后的
#   `uci show` 与在 LuCI 里保存过「全局网络选项」的设备一致。
uci -q get network.globals >/dev/null 2>&1 || uci set network.globals=globals
uci set network.globals.packet_steering='1'

# [等价] 三个接口显式写 multipath='off'（关闭多路径）。
#   说明：这些行**当前不改变任何行为**，原因有二（均源码级核实）：
#     1) `multipath` 不是 netifd 的选项——openwrt/netifd 全库搜 `multipath` / `mptcp`
#        均为 0 命中，它只被 LuCI 的 interfaces.js 消费，且仅在
#        `network.globals.multipath=enable` 时才去生成多路由表脚本；
#     2) 本固件**不设** `network.globals.multipath`（保持官方默认＝MPTCP 全局未启用）。
#   ⇒ 全局 multipath 未开时，这三个 `off` 只是躺在配置里的孤儿选项，与「不写」完全等价。
#   写它们只为：日后若有人手动开启全局 MPTCP，接口也不会自动 multipath
#   （单 WAN 的 R2S 上 multipath 本就无意义）。
#   ⚠ 写法：`uci set network.lan.multipath='off'` 的**外层单引号是 shell 引用符**，会被
#      剥掉，落盘值就是干净的 `off`（不含引号）。若写成
#      `uci set "network.lan.multipath='off'"`（双引号包单引号）才会把引号存进值——那是旧版 bug。
#   ⚠ 旁注：`off` 这个字面量已被上游废弃（LuCI 提交 317ff9dd76，2026-04-25，把「关闭」改
#      为空串 `''` 并 `o.optional = true`）；这里用 `off` 仅为与旧设备 `uci show` 形态一致，
#      等价现代写法可改为 `uci set network.X.multipath=''`。
uci set network.lan.multipath='off'
uci set network.wan.multipath='off'
uci set network.wan6.multipath='off'

uci commit network

# 仅记录实际接口映射，便于排查问题（不修改任何配置）
echo "iface map: lan=$(uci -q get network.lan.device) wan=$(uci -q get network.wan.device) wan6=$(uci -q get network.wan6.device)" >>$LOGFILE

# =========================================================
# 4. DHCP / DNS（dnsmasq / odhcpd）
# =========================================================
# 注意：这里一律用 `@类型[序号]` 寻址（如 dhcp.@dnsmasq[0]），不用 `cfgXXXXXX`
# 这种自动生成的匿名段名 —— 后者随配置文件内容变化，写死不可靠。

# [等价] LAN 的 IPv6 服务两项，删掉后语义不变：
#   - dhcp.lan.ra_slaac：odhcpd 的默认值就是 true
#     （odhcpd src/config.c: `iface->ra_slaac = true;`），删掉后 SLAAC 仍开启。
#     ⚠ 若本意是「关闭 LAN 的 SLAAC」，**删选项达不到目的**，必须显式写
#       uci set dhcp.lan.ra_slaac='0'。本脚本按设备现状保持「删除」。
#   - dhcp.lan.dhcpv6：原本就是 'disabled'，而 odhcpd 的默认值也是 disabled
#     （src/config.c: `iface->dhcpv6 = MODE_DISABLED;`）。
uci -q delete dhcp.lan.ra_slaac
uci -q delete dhcp.lan.dhcpv6

# [等价] ra_preference='medium'：odhcpd 未设该项时 route_preference 为 0，
#   而 0 就是 medium（config.c 中 low→-1、high→1、其余→0），故这也等于默认值。
uci set dhcp.lan.ra_preference='medium'

# [等价] WAN 侧的 DHCP 服务参数。dhcp.wan.ignore='1' 是官方默认，本机不在 WAN 侧
#   提供 DHCP 服务，所以这三项不产生实际效果；写上只为与设备现状一致。
uci set dhcp.wan.leasetime='12h'
uci set dhcp.wan.start='100'
uci set dhcp.wan.limit='150'

# [等价] dnsmasq / odhcpd 的若干「已废弃 / 本就取默认值」选项：
#   nonwildcard —— dnsmasq.init 里是
#                  `append_bool "$cfg" nonwildcard "--bind-dynamic" 1`
#                  第 4 个参数 1 就是默认值：删掉后 --bind-dynamic 照样加。
#   boguspriv   —— `config_get_bool boguspriv "$cfg" boguspriv 1`，默认 1。
#   filterwin2k / filter_aaaa / filter_a —— append_bool 无默认值（默认 0），原本也是 0。
#   odhcpd.maindhcp —— 默认 0，原本也是 0。
uci -q delete dhcp.@dnsmasq[0].nonwildcard
uci -q delete dhcp.@dnsmasq[0].boguspriv
uci -q delete dhcp.@dnsmasq[0].filterwin2k
uci -q delete dhcp.@dnsmasq[0].filter_aaaa
uci -q delete dhcp.@dnsmasq[0].filter_a
uci -q delete dhcp.odhcpd.maindhcp

# [生效] 关闭 DNS 劫持 —— 这是 **ImmortalWrt 特有**的选项（上游 OpenWrt 没有）。
#   ImmortalWrt 的 dnsmasq.init 末尾：
#     config_get_bool dns_redirect "$cfg" dns_redirect 0
#     if [ "$dns_redirect" = 1 ]; then
#         nft add table inet dnsmasq
#         nft add chain inet dnsmasq prerouting "{ type nat hook prerouting priority -95; ... }"
#         nft add rule inet dnsmasq prerouting "... udp dport 53 counter \
#           redirect to :53 comment \"DNSMASQ HIJACK\""
#     fi
#   即置 1 时会把**所有过路的 UDP/53**（含 LAN 客户端发往外部 DNS 的查询）
#   强行 NAT 重定向到本机 dnsmasq。不用代理插件时这是「防 DNS 泄漏」，
#   但与 Clashoo 这类自己接管 DNS 的插件会互相打架，故关闭。
#   ⚠ 注意：上面的 `config_get_bool ... 0` 只是**脚本兜底默认**（选项缺失时取 0），
#   而 ImmortalWrt 发布的 /etc/config/dhcp 里**显式写了 `option dns_redirect 1`**，
#   所以设备上该项出厂就是**开的** —— 这行 delete 确实改变了运行行为（故标 [生效]）。
uci -q delete dhcp.@dnsmasq[0].dns_redirect

uci commit dhcp

# 设备上还手工加过一条静态 DHCP 租约（dhcp.@host[0]，
# MAC 34:2e:b6:61:9b:c9 → 192.168.2.201）。那是单台设备的绑定、不属于固件默认，
# 故未写入。需要时可在 LuCI「网络 → DHCP/DNS → 静态地址分配」里添加，或取消注释：
# uci add dhcp host
# uci set dhcp.@host[-1].name='my-pc'
# uci set dhcp.@host[-1].mac='34:2e:b6:61:9b:c9'
# uci set dhcp.@host[-1].ip='192.168.2.201'
# uci commit dhcp

# =========================================================
# 5. 防火墙
# =========================================================

# 区域（zone）策略**按需修改**：其余沿用 firewall4 官方默认

uci -q delete firewall.@defaults[0].syn_flood
uci set firewall.@defaults[0].synflood_protect
uci -q delete firewall.@defaults[0].fullcone
uci -q delete firewall.@defaults[0].fullcone6

uci -q delete firewall.@defaults[0].flow_offloading
uci -q delete firewall.@defaults[0].flow_offloading_hw

#  - wan 区域 input=REJECT / forward=REJECT / output=ACCEPT
# 这是防止 WAN 侧入站的纵深防御措施，保持上游默认即可。
# 防止代理降速 必要操作
uci set firewall.@zone[1].input='REJECT'
uci set firewall.@zone[1].output='ACCEPT'
uci set firewall.@zone[1].forward='REJECT'

uci commit firewall

# =========================================================
# 6. 默认主题
# =========================================================
# 固件同时装了 luci-theme-bootstrap 与 luci-theme-argon 两个主题，各自带一个
# /etc/uci-defaults/30_luci-theme-* 脚本，会争抢 luci.main.mediaurlbase：
#   - 30_luci-theme-argon     ：【无条件】把 mediaurlbase 改成 /luci-static/argon
#   - 30_luci-theme-bootstrap ：只在「该选项尚不存在」时才设
#                               （luci-base 自带 /etc/config/luci 的默认值已是 bootstrap）
# 而 uci-defaults 按文件名字母序执行，argon 在前 —— 它先改掉，bootstrap 便不再改，
# 结果默认主题会变成 Argon。本脚本 99- 前缀保证在所有 30_ 之后执行，
# 显式设回 Bootstrap，取消下方命令前的注释即可。
# （用户之后仍可在 LuCI「系统 → 系统 → 语言和界面」里切回 Argon）
# uci set luci.main.mediaurlbase='/luci-static/bootstrap'
# uci commit luci

# =========================================================
# 7. ttyd / dropbear 的监听范围
# =========================================================
# 原实现这里有两行，把网页终端与 SSH 的监听范围**放开到所有接口**：
#     uci -q deleteete ttyd.@ttyd[0].interface            # 删掉 ttyd 的 interface 限制
#     uci set dropbear.@dropbear[0].Interface=''    # 清空 dropbear 的 Interface
# 现已**注释掉**：本机只做局域网管理，不需要从其他接口访问，保持上游默认更严格。
#
# 上游默认值（`ttyd` / `dropbear` 包随固件发布的配置，未改动前即为此）：
#   /etc/config/ttyd      → option interface '@lan'
#       ttyd.init 会把 '@' 前缀解析成「网络 lan 对应的设备」（即 br-lan），
#       启动参数为 `-i br-lan` → 只监听 LAN 桥。
#   /etc/config/dropbear  → option Interface 'lan'
#       dropbear.init 会把 'lan' 解析成 LAN 的地址（network_get_ipaddrs_all），
#       启动参数为 `-p <LAN地址>:22` → 只监听 LAN 地址。
# 两处本身就是「仅局域网可管理」的严格形态，因此无需再额外设置。
#
# 另外，防火墙也拦住了 WAN 入站（wan 区域 input=REJECT），所以这一节属
# 「纵深防御」：即便将来误放开防火墙，这两个服务也不会监听在 WAN 侧。
#
# 如需临时从其他接口访问（例如从 WAN 侧调试），取消下面两行注释即可：
# uci -q deleteete ttyd.@ttyd[0].interface
# uci set dropbear.@dropbear[0].Interface=''

# 兜底提交（本节已无待提交项，保留以防日后在此处新增设置时漏 commit）
uci commit

# 本分支不做品牌改写，固件保留 ImmortalWrt 官方 DISTRIB_DESCRIPTION

# 原实现这里还有两段第三方插件的兼容补丁，因本项目不提供这些插件而移除：
#   - luci-app-advancedplus（进阶设置）的 zsh 调用清理：
#     该包在官方源与注入的 Clashoo 源中**都不存在**，根本无法安装。
#   - luci-app-quickfile 的 nginx 配置改写：
#     该包仅在 Clashoo 第三方源中有，本项目不提供（该源也没有对应 i18n 包）。
# 如需自行启用，请先按 README「如何自行增加第三方源」加源，再把所需配置写回本脚本。

# 本分支不集成 Docker，无需配置 docker 防火墙规则

exit 0
