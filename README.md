# NanoPi-R2S-ImmortalWrt-Builder

NanoPi R2S 专用：基于 GitHub Actions + ImmortalWrt 官方 Image Builder 的自动化固件构建工作流。
本分支（`main`）**专用于 FriendlyARM NanoPi R2S**。

**⚠️ 重要声明**

> 本项目为个人独立维护的第三方项目（脚本），与 ImmortalWrt 官方没有关联。
> 固件由 ImmortalWrt 官方 Image Builder 工具打包生成；用户自行定制产生的任何问题，
> 均不代表 ImmortalWrt 官方固件的 bug。
> 为不给上游维护者增加额外负担，相关问题请勿在 ImmortalWrt 社区反馈。

---

## 目标机型

| 项目 | 值 |
| --- | --- |
| 机型 | FriendlyARM NanoPi R2S |
| 平台 | Rockchip RK3328（ARM64 / aarch64_generic） |
| Image Builder profile | `friendlyarm_nanopi-r2s` |
| 内核 | 官方原版内核（不做自定义内核） |

机型已固定在工作流的 `env.PROFILE` 中，**不再提供机型下拉选择**。

> 本仓库**只支持 ImmortalWrt 25.12.x（apk 包管理）**，
> 已移除全部 24.10 / opkg 相关代码与配置。

---

## 本分支的设计取舍

本分支刻意保持精简，**明确不集成**以下内容：

| 项目 | 说明 |
| --- | --- |
| Docker | 不预装 Docker / Dockerman，`99-custom.sh` 中也没有 docker 防火墙逻辑 |
| store / istore 商店 | 不集成 `luci-app-store`，工作流中也没有相关开关 |
| 第三方插件仓库 | 不克隆任何第三方插件仓库（不依赖外部 run/apk 仓库） |
| PPPoE 拨号 | WAN 默认 **DHCP** 接入（与 ImmortalWrt 官方默认一致），也可选静态 IP，`99-custom.sh` 中已无任何 PPPoE 逻辑 |
| 多机型 / 多网口适配 | 不做。本分支专用于 R2S，网口映射（`eth0`=WAN / `eth1`=LAN）在 `99-custom.sh` 中**硬编码**，不再探测接口数量 |

第三方软件只注入 **Clashoo（kenzok8）** 一个源（默认只启用 Clashoo），
由 `rockchip/build.sh` 在构建时注入，详见下文「预置软件与第三方源」。

---

## 可用工作流

| 工作流文件 | 名称 | 包管理 | 说明 |
| --- | --- | --- | --- |
| `.github/workflows/build-rockchip-immortalWrt-25.12.x.yml` | Build R2S ImmortalWrt 25.12.x | apk | 构建固件，默认 `25.12.2` |
| `.github/workflows/clean-workflow.yml` | Cleanup Old Workflow Runs | — | 维护用：手动清理旧的 Actions 运行记录（默认只保留最近 1 天） |

### 工作流输入项

| 输入 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `luci_version` | 否 | `25.12.2` | Image Builder 版本（25.12.0 / .1 / .2） |
| `custom_router_ip` | 是 | `192.168.2.1` | LAN 管理地址，格式 `192.168.x.1` 或 `10.x.x.1` |
| `rootfs_partsize` | 是 | `1G` | 软件包空间大小（1G / 2G / 3G / 4G） |
| `wan_proto` | 是 | `dhcp` | WAN 接入方式（`dhcp` 官方默认 / `static` 静态 IP） |
| `wan_ip` | 否 | `192.168.1.2` | WAN 静态 IP（**仅 `wan_proto=static` 时生效**，须在上级光猫网段内） |
| `wan_gateway` | 否 | `192.168.1.1` | WAN 网关（**仅 `wan_proto=static` 时生效**，上级光猫地址） |
| `wan_dns` | 否 | `192.168.1.1` | WAN DNS（**仅 `wan_proto=static` 时生效**，留空则用网关；可用空格分隔填多个，如 `223.5.5.5 1.1.1.1`） |

### 使用方法

1. 把本项目 fork 到你自己的账号（或克隆后推送到自己的仓库）。
2. 进入 **Actions**，选择需要的工作流 → **Run workflow**，填写上述输入。
3. 构建完成后，固件会作为 Release 资源（tag `Autobuild`，文件 `*.img.gz`）发布。

> **提示**：`workflow_dispatch` 类型的工作流，**其文件必须存在于仓库的默认分支上**，
> GitHub 才会在 Actions 页面显示 "Run workflow" 按钮（这是 GitHub 的硬性要求）。
> 本仓库的默认分支为 **`main`**，R2S 工作流就在 `main` 上，因此可直接触发。

---

## 固件默认属性（必读）

- 用户名 `root`，**无密码**。
- **WAN 接入方式：默认 DHCP**（与 ImmortalWrt 官方默认一致，不使用 PPPoE）。
  WAN 口接上级设备（光猫 / 主路由），地址由上级自动分配。
  如需固定地址，把工作流的 `wan_proto` 选为 `static`，再填写
  `wan_ip` / `wan_gateway` / `wan_dns`（默认 `192.168.1.2` / `192.168.1.1` / `192.168.1.1`）；
  此时请确保 `wan_ip` 与光猫处于同一网段（默认 `192.168.1.0/24`）且未被其他设备占用。
- **WAN6：DHCPv6 客户端**，向光猫请求 IPv6 地址与 **PD 前缀**（`reqprefix=auto`），
  供下游 LAN 分配 IPv6 使用。
- **WAN 入站防火墙：采用 ImmortalWrt 25.12.x 官方镜像的策略（入站拒绝）**，
  不允许从 WAN 侧访问后台；刷机后请从 **LAN 口**访问。详见下文「WAN 入站防火墙」章节。
- **网口（R2S 本体）**：`eth0` 为 WAN（默认 DHCP），`eth1` 为 LAN，
  LAN 地址为工作流中填写的 `custom_router_ip`。对应关系见下节。
- **默认主题：Bootstrap**。固件同时装了 Argon（可在 LuCI「系统 → 系统 → 语言和界面」
  里切换），默认值由 `99-custom.sh` **显式指定** —— 两个主题各带一个
  `/etc/uci-defaults/30_luci-theme-*` 脚本会争抢 `luci.main.mediaurlbase`，
  其中 Argon 那个是**无条件**改写、且按文件名字母序先于 Bootstrap 执行，
  若不显式指定，默认主题会变成 Argon。
- **网络 / DHCP / 防火墙的调优**（`wan6` 的 DHCPv6 行为、关闭 DNS 劫持、
  关闭流量卸载等）同样由 `99-custom.sh` 在首启写入，
  逐项说明见下文「首启初始化（`99-custom.sh`）」。
- **网页终端 / SSH 只监听局域网**：`ttyd` 监听 `br-lan`、`dropbear` 监听 LAN 地址，
  均保持上游默认 —— `99-custom.sh` **不再**把它们放开到所有接口。
- 上述行为全部由 `files/etc/uci-defaults/99-custom.sh` 定义，可按需修改。

### R2S 网口对应关系（重要）

本分支专用于 R2S，机型固定，`99-custom.sh` 中**不做任何接口探测**，网口映射是硬编码的，
与 ImmortalWrt 官方 `02_network` 的 `ucidef_set_interfaces_lan_wan 'eth1' 'eth0'` 一致：

| 内核接口 | 硬件 | 角色 | 外壳丝印 |
| --- | --- | --- | --- |
| `eth0` | 板载 GMAC（RK3328 原生，靠近 GPIO 排针） | **WAN** | WAN |
| `eth1` | USB3 RTL8153（靠近 USB 口） | **LAN** | LAN |

即 `kmod-usb-net-rtl8152` 是 **LAN 口**需要的驱动，**不是** WAN 口的 —— WAN 走的是板载
GMAC，由内核直接支持。

`br-lan` 桥及其端口、以及两个网口的 MAC 都由官方 `/bin/config_generate` 在首次启动时生成
（MAC 取自 `macaddr_generate_from_mmc_cid mmcblk0`，即由 **SD 卡 CID 派生**，
同一张卡固定不变；`LAN MAC = WAN MAC + 1`）。所以 MAC 是「本地管理地址」形式
（首字节的 bit1 置位，例如 `9E:E2:...`），这是官方设计，不是异常。

### 首启初始化（`99-custom.sh`）

固件**首次启动**时由 `/etc/uci-defaults/99-custom.sh` 一次性写入下列配置
（执行后该脚本自动删除）。标注 **`[生效]`** 的才是真正改变运行行为的项；
**`[等价]`** 的只是 LuCI 的写法归一化（改记法，或删掉「已废弃 / 本来就等于
该选项默认值」的项），语义与官方默认**完全一致** —— 写进来只为让首启后的
`uci show` 与手工配置过的设备逐行一致，便于比对排障。

| 分类 | 项 | 类型 | 说明 |
| --- | --- | --- | --- |
| 网络 | `network.globals.packet_steering='1'` | `[等价]` | 数据包引导。消费该值的 `packet-steering.uc` **只特判 `'0'`（关闭）与 `'2'`（全部 CPU）**，`'1'` 与「未设」走同一套默认算法，故等于不写。官方 `99-default-settings` 只为 bcm4908 / bcm53xx / ramips-mt7621 / x86 写入该项（rockchip 默认即「未设」） |
| 网络 | LAN / WAN 地址改用 CIDR 列表记法 | `[等价]` | 用 `list ipaddr '192.168.2.1/24'` 取代 `ipaddr` + `netmask`，netifd 从前缀长度推掩码 |
| 网络 | `lan` / `wan` / `wan6` 写 `multipath='off'` | `[等价]` | MPTCP 开关，netifd 未设该项时即为关闭 |
| 网络 | `wan6.norelease='1'` | `[生效]` | 重启时不发 DHCPv6 RELEASE（odhcp6c `-k`），降低上级回收地址导致**前缀变化**的概率。未设时 odhcp6c 会带 `-R`（退出时发 RELEASE），故这是真变更 |
| 网络 | `wan` / `wan6` 的 `sendclientid='auto'` | `[等价]` | DHCP 客户端标识取「自动」。`dhcp.sh` 与 `dhcpv6.sh` 的 `case` 都是 `auto\|*)` **合并分支** —— 「未设」落进 `*)`，与 `auto` 走同一段代码，等于不写 |
| DHCP | 删除 `dhcp.lan.ra_slaac`、`dhcp.lan.dhcpv6` | `[等价]` | odhcpd 的默认值本就是 `ra_slaac=true`、`dhcpv6=disabled`，删除后行为不变。**⚠ 删 `ra_slaac` 并不能关闭 SLAAC**，要关必须显式写 `ra_slaac='0'` |
| DHCP | `dhcp.lan.ra_preference='medium'` | `[等价]` | odhcpd 默认 `route_preference=0`，0 即 medium |
| DHCP | `dhcp.wan.leasetime='12h'` / `start='100'` / `limit='150'` | `[等价]` | WAN 侧本就不提供 DHCP 服务（`dhcp.wan.ignore='1'` 是官方默认），这三项不产生实际效果；写上只为与设备现状一致 |
| DHCP | 删除 `dnsmasq` 的 `nonwildcard` / `boguspriv` / `filterwin2k` / `filter_aaaa` / `filter_a`，以及 `odhcpd.maindhcp` | `[等价]` | 每一项都等于其默认值（`nonwildcard` 默认 1、`boguspriv` 默认 1，其余默认 0） |
| DHCP | 删除 `dnsmasq.dns_redirect` | `[生效]` | **ImmortalWrt 特有**选项（上游 OpenWrt 没有）。置 1 时 dnsmasq 会插入 nft 规则把**所有过路 UDP/53** 劫持到本机（规则注释 `DNSMASQ HIJACK`），与 Clashoo 自己接管 DNS 的行为冲突，故关闭 |
| 防火墙 | `syn_flood` → `synflood_protect='1'` | `[等价]` | 选项改名迁移（LuCI 保存该页时即如此），SYN-flood 保护保持**开启** |
| 防火墙 | 删除 `fullcone6` | `[等价]` | 原本就是关闭（未设即关闭） |
| 防火墙 | 清理 9 条默认通信规则上的**非法 `enabled` 值** | `[生效]` | **修历史脏数据**。旧版脚本用 `uci -q set "…enabled='1'"` 把**含字面单引号的 `'1'`** 写进了配置（`uci set` 不解析引号），fw4 解析该布尔值失败 → 报 `skipped due to invalid options` → **整条规则段被丢弃**，`Allow-Ping` / `Allow-DHCPv6` / `Allow-ICMPv6-*` 等**全部失效**。现按 `name` 匹配、只清非法值（删掉即回落 fw4 默认「启用」），不再写入 `enabled` |
| 防火墙 | 删除 `flow_offloading` / `flow_offloading_hw` | `[生效]` | **关闭流量卸载**。fw4 的默认值是 0（官方配置里写的 `'1'` 才是开启），删除即关闭；flow offload 会让首包之后的流量走 fast path **绕过 netfilter 钩子**，与 Clashoo 这类 TPROXY 透明代理冲突 |

> **小结：首启真正改变行为的只有 4 项** —— `wan6.norelease='1'`、
> 关闭 DNS 劫持（删 `dns_redirect`）、关闭流量卸载（删 `flow_offloading*`）、
> 以及**清理 9 条默认通信规则上的非法 `enabled` 值**（修复旧版本写入的脏数据）；
> 其余全部是写法归一化。
>
> 各项的判断依据均取自上游源码（odhcpd `src/config.c`、`dnsmasq.init`、
> netifd `proto/dhcp.sh`、odhcp6c `dhcpv6.sh`、
> `usr/libexec/network/packet-steering.uc`、firewall4 `root/usr/share/ucode/fw4.uc`、
> LuCI `view/firewall/zones.js` 与 `view/network/interfaces.js`），
> 脚本注释里逐条给了出处。

> **有意不写入的**：`ttyd` / `dropbear` 的监听范围。脚本里原本那两行
> 「放开到所有接口」的设置已**注释掉**，两个服务保持上游默认、只监听局域网。
> 详见「WAN 入站防火墙」一节。

---

## 预置软件与第三方源

### 官方源

使用 ImmortalWrt 官方 Image Builder 与官方软件源，因此：

- 官方软件源中的**全部 kmod** 都可直接安装（kmods 源由 Image Builder 自动纳入，
  其依赖精确的内核版本，官方内核天然匹配）。
- Clashoo 依赖的 `kmod-tun`、`kmod-nft-tproxy`、`kmod-nft-socket`、`kmod-inet-diag`
  等内核模块均由官方 kmods 源解析。

> **注意：首启后官方源地址会被改写为 ImmortalWrt 官方镜像。**
> 固件里的 `default-settings-chn` 带一个 `/etc/uci-defaults/99-default-settings-chinese`，
> 它会把 `/etc/apk/repositories.d/distfeeds.list` 中的
> `https://downloads.immortalwrt.org` 替换为 `https://mirrors.vsean.net/openwrt`
> （该地址会重定向到中科大镜像）。这是 **ImmortalWrt 官方行为**，不是本分支的定制，
> 无需处理；因此看到设备端源地址与官方域名不一致属正常现象。
> 该操作会留下一个 `distfeeds.list.bak`，但 apk 只读取 `.list` 后缀的文件，
> 这个备份不会被加载，无副作用。

### 第三方源：Clashoo

`rockchip/build.sh` 在 `make image` 之前注入 Clashoo 的 APK 源与签名公钥：

```sh
CLASHOO_FEED="https://down.dllkids.xyz/openwrt-feed/25.12/aarch64_generic/packages.adb"
CLASHOO_KEY_URL="https://down.dllkids.xyz/openwrt-feed/keys/dllkids-feed.pub.pem"
```

> - `aarch64_generic` 必须与目标的 `ARCH_PACKAGES` 一致，否则 apk 会以架构不符拒绝。
> - 25.12 的配置启用了 `CONFIG_SIGNATURE_CHECK`，**必须**提供公钥，
>   否则安装时会报 `UNTRUSTED signature`。
> - Clashoo 与 nikki 配置冲突，**不要同时集成**。
> - **该源与公钥也会随固件发布**，因此设备端可直接 `apk update` / `apk add` /
>   `apk upgrade` Clashoo 系包。做法：`rockchip/build.sh` 除注入构建期用的
>   `repositories` / `keys/`（`make image` 通过 `--repositories-file` / `--keys-dir`
>   读取，这两个本身不会被拷进固件）之外，还把源与公钥写进 `files/`，随固件落到
>   `/etc/apk/repositories.d/customfeeds.list` 与 `/etc/apk/keys/dllkids-feed.pub.pem`。
>   （`customfeeds.list` 是 `apk` 包为「用户自加第三方源」预留的 conffile，
>   上游默认就是三行注释占位；`CONFIG_SIGNATURE_CHECK=y`，设备端缺公钥会报
>   `UNTRUSTED signature`。）

### 预置软件包清单

默认包列表定义在 `rockchip/build.sh` 的 `PACKAGES` 变量中；
可选软件包定义在 `shell/custom-packages.sh`：

- 把要装的**短名**填进 `ENABLE_APPS`（空格分隔），脚本会自动展开为
  `luci-app-<短名>` + `luci-i18n-<短名>-zh-cn`（后者依赖前者，两者一起装上）。
  例：`ENABLE_APPS="filemanager rclone ttyd"`。文件末尾列出了全部 132 个可用短名。
- **例外**：`argon-config` / `cpufreq` / `diskman` 三个 app 包在官方源里
  （版本 `27.150.x`）**已内置简体中文**，脚本只装 app 包、不再补
  `luci-i18n-<短名>-zh-cn`（`26.236.x`）—— 否则 apk 会因文件冲突报
  `trying to overwrite ... owned by luci-app-...` 使 `make image` 失败。
- 少数不是 `luci-app-<短名>` 形式的包（`luci-proto-*` / `luci-mod-*` /
  openclash / passwall 等），取消对应行注释即可。

第三方插件只有 Clashoo 一个（已默认启用）。

### 如何自行增加第三方源

把源的 `packages.adb` **全路径**追加到 Image Builder 根目录的 `repositories` 文件，
并把签名公钥放进 `keys/` 目录。参考 `rockchip/build.sh` 中 Clashoo 的注入写法。

> 注意：`repositories` / `keys/` 是**构建期**注入，只让 `make image` 能找到并校验包。
> 若还希望**设备端**也能从该源安装，需像 Clashoo 那样额外把源与公钥写进 `files/`
> （见上一节说明）。

---

## 目录结构

```
.github/workflows/
  build-rockchip-immortalWrt-25.12.x.yml  # R2S 固件构建工作流
  clean-workflow.yml                      # 维护用：清理旧 Actions 运行记录
files/etc/uci-defaults/
  99-custom.sh                            # 首次启动初始化脚本（R2S 专用，网口映射硬编码）
rockchip/
  build.sh                                # 构建脚本 + Clashoo 源注入
  imm.config                              # Image Builder 配置
shell/
  custom-packages.sh                      # 可选软件包开关（第三方插件仅 Clashoo，已默认启用）
README.md                                 # 本文件
PACKAGES.md                               # 软件包来源与启用方式说明
SUPPORT.md                                # 支持的机型列表
LICENSE                                   # 许可证
.gitattributes                            # 强制 LF 换行（shell 脚本必需）
```

> 说明：`rockchip/build.sh` 在运行时还会生成 `files/etc/config/wan-settings`，
> 工作流会生成 `custom/custom_router_ip.txt`；这两者随固件发布，但**不属于仓库文件**。

---

## WAN 入站防火墙

本项目采用 **ImmortalWrt 25.12.x 官方镜像的防火墙策略**，做法是「**不干预**」：

- 固件**不提供** `/etc/config/firewall`（完全继承 `firewall4` 包的默认配置）；
- `99-custom.sh` 中**不修改任何区域（zone）、转发与通信规则的策略**，只做三件
  与策略无关的事（详见上文「首启初始化」）：
  ① 把已废弃的 `syn_flood` 迁移成 `synflood_protect`（保护**保持开启**），
     并删除 `fullcone6`（均为默认行为）；
  ② **修复历史遗留的非法 `enabled` 值** —— 旧版脚本曾把含字面单引号的 `'1'` 写进
     9 条默认通信规则，导致 fw4 判定选项非法、**整段规则被丢弃**；现按 `name` 匹配、
     只清非法值（删掉即回落默认「启用」），**不改变任何规则的启用/禁用意图**；
  ③ **关闭流量卸载**（`flow_offloading` / `flow_offloading_hw`），
     避免与 Clashoo 的 TPROXY 透明代理冲突。

因此固件在**区域与规则策略**上完全继承 `firewall4` 包的官方默认配置。官方默认的关键部分
（`firewall4` 上游 `root/etc/config/firewall`，ImmortalWrt `openwrt-25.12` 分支
引用的正是这一版，其 `Makefile` 以 `$(CP) -a $(PKG_BUILD_DIR)/root/*` 直接装入）：

```text
config defaults
	option input		REJECT
	option output		ACCEPT
	option forward		REJECT

config zone
	option name		lan
	option input		ACCEPT
	option output		ACCEPT
	option forward		ACCEPT

config zone
	option name		wan
	list   network		'wan'
	list   network		'wan6'
	option input		REJECT
	option output		ACCEPT
	option forward		DROP
	option masq		1
```

即 **WAN 入站默认「拒绝」**，不允许从 WAN 侧访问路由器后台。

因此刷机后请把电脑接到 **LAN 口（`eth1`，靠近 USB 口）**，再访问 `custom_router_ip`
（默认 `192.168.2.1`）。

如确实需要临时从 WAN 侧访问后台：

> 网络 → 防火墙 → wan 的入站数据 → 选择「接受」→ 保存并应用

> ⚠️ 放开后，WAN 侧同网段设备（乃至公网 IPv6，因为 `wan6` 的地址是全球可路由的）
> 都能访问**无密码**的 root 后台。调试完请务必改回「拒绝」。

> 说明：`99-custom.sh` 中原本还有两行让 ttyd / dropbear 监听**所有接口**
> （`uci delete ttyd.@ttyd[0].interface`、`uci set dropbear.@dropbear[0].Interface=''`），
> 现已**注释掉** —— 本项目只做局域网管理，这两个服务保持上游默认：
> ttyd 监听 `br-lan`（`option interface '@lan'`，init 会把 `@` 前缀解析成网络 `lan`
> 的设备），dropbear 监听 LAN 地址（`option Interface 'lan'`，init 会解析成
> LAN 的 IP 后以 `-p <地址>:22` 启动）。需要从其他接口调试时，
> 取消脚本里那两行的注释即可。

---

## 🎉 鸣谢

感谢以下项目与作者提供的上游工作（本项目最初派生自
[wukongdaily/ImmortalWrt-ImageBuilder](https://github.com/wukongdaily/ImmortalWrt-ImageBuilder)，
现已独立维护、不再跟随上游）：

<div align="left">

<a href="https://github.com/immortalwrt"><img src="https://avatars.githubusercontent.com/immortalwrt?v=4&s=80" width="80" height="80" alt="immortalwrt" /></a>
<a href="https://github.com/wukongdaily"><img src="https://avatars.githubusercontent.com/wukongdaily?v=4&s=80" width="80" height="80" alt="wukongdaily（悟空 / 上游）" /></a>
<a href="https://github.com/kenzok8"><img src="https://avatars.githubusercontent.com/kenzok8?v=4&s=80" width="80" height="80" alt="kenzok8（Clashoo 作者）" /></a>
<a href="https://github.com/sirpdboy"><img src="https://avatars.githubusercontent.com/sirpdboy?v=4&s=80" width="80" height="80" alt="sirpdboy" /></a>
<a href="https://github.com/sbwml"><img src="https://avatars.githubusercontent.com/sbwml?v=4&s=80" width="80" height="80" alt="sbwml" /></a>
<a href="https://github.com/eamonxg"><img src="https://avatars.githubusercontent.com/eamonxg?v=4&s=80" width="80" height="80" alt="eamonxg" /></a>
<a href="https://github.com/timsaya"><img src="https://avatars.githubusercontent.com/timsaya?v=4&s=80" width="80" height="80" alt="timsaya" /></a>

</div>
