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

第三方软件只注入 **dllkids（kenzok8）** 一个源（默认只启用该源的 **nikki**），
由 `rockchip/build.sh` 在构建时注入，详见下文「预置软件与第三方源」。

---

## 可用工作流

| 工作流文件 | 名称 | 包管理 | 说明 |
| --- | --- | --- | --- |
| `.github/workflows/build-rockchip-immortalWrt-25.12.x.yml` | Build R2S ImmortalWrt 25.12.x | apk | 构建固件，默认 `25.12.2`；产物同时发到 Release 与 Artifact（保留 90 天） |
| `.github/workflows/clean-workflow.yml` | Cleanup Old Workflow Runs | — | 维护用：手动清理旧的 Actions 运行记录（默认只保留最近 1 天）。⚠️ **删运行记录会连带删掉那次的 Artifact**，想留档请先下载，或把 `keep_days` 调大 |

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
3. 构建完成后，固件有两个去处：
   - **Release**（tag `Autobuild`）：永远只有**一条**，每次构建**原地覆盖**附件
     （`*.img.gz`），供「最新固件」一键下载。该 tag **不会**随构建移动，
     Release 正文里的「构建提交」才是本次固件的真实来源。
   - **Artifacts**（Actions → 那次运行 → 页面最下方）：本次构建的固件归档，
     名为 `r2s-immortalwrt-<版本>-r<序号>`，保留 **90 天**。需要**历史版本**时用这里。

> ⚠️ 维护提醒：`clean-workflow.yml` 默认只保留最近 1 天的运行记录，而
> **删除运行记录会连带删除该次运行的 Artifact**。想留档请先把 `keep_days` 调大。

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
- **WAN6：DHCPv6 客户端**（由官方 `config_generate` 自动生成），向光猫请求 IPv6
  地址与 **PD 前缀**，供下游 LAN 分配 IPv6 使用。
  `99-custom.sh` **不设置** `wan6` 的任何选项 —— `reqprefix` 保持未设，
  而 netifd 的 `dhcpv6.sh` 里是
  `[ -z "$reqprefix" -o "$reqprefix" = "auto" ] && reqprefix=0`，
  即「未设」与 `auto` 完全等价（请求任意长度前缀），故实际行为与显式写 `auto` 相同。
- **WAN 入站防火墙：采用 ImmortalWrt 25.12.x 官方镜像的策略（入站拒绝）**，
  不允许从 WAN 侧访问后台；刷机后请从 **LAN 口**访问。详见下文「WAN 入站防火墙」章节。
- **网口（R2S 本体）**：`eth0` 为 WAN（默认 DHCP），`eth1` 为 LAN，
  LAN 地址为工作流中填写的 `custom_router_ip`。对应关系见下节。
- **默认主题：Argon**（ImmortalWrt 官方默认）。固件同时装了 Bootstrap，
  可在 LuCI「系统 → 系统 → 语言和界面」里切换 —— 两个主题各带一个
  `/etc/uci-defaults/30_luci-theme-*` 脚本会争抢 `luci.main.mediaurlbase`，
  其中 Argon 那个是**无条件**改写、且按文件名字母序先于 Bootstrap 执行，
  所以默认会落到 Argon。`99-custom.sh` **不干预**主题；
  若想让默认变成 Bootstrap，在 `99-custom.sh` 末尾追加两行即可：
  `uci set luci.main.mediaurlbase='/luci-static/bootstrap'` 与 `uci commit luci`。
- **网络 / DHCP / 防火墙等一律保持 ImmortalWrt 官方镜像默认** ——
  `99-custom.sh` **只写工作流传入的地址参数**（WAN 接入方式与 LAN 管理地址），
  其余调优项（`packet_steering`、`multipath`、DHCP/DNS 各项、`fullcone`、
  `syn_flood`、`flow_offloading` 等）**一律不做改动**，
  逐项说明见下文「首启初始化（`99-custom.sh`）」。
- **网页终端 / SSH 只监听局域网**：`ttyd` 监听 `br-lan`、`dropbear` 监听 LAN 地址，
  均为上游默认，`99-custom.sh` 不干预。
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
（执行后该脚本自动删除）。

本脚本**只做一件事**：把工作流构建时传入的地址参数写进 uci 配置。
除此之外**不做任何定制**，其余配置全部保持 ImmortalWrt 官方镜像默认 ——
行为与官方 Image Builder 直接产出的镜像一致。

| 分类 | 项 | 来源 |
| --- | --- | --- |
| WAN | `network.wan` 的 `proto` / `ipaddr` / `gateway` / `dns` | 工作流输入 `wan_proto` / `wan_ip` / `wan_gateway` / `wan_dns`（默认 `dhcp`，即官方默认） |
| LAN | `network.lan.ipaddr` | 工作流输入 `custom_router_ip`（默认 `192.168.2.1/24`） |

补充说明：

- **网口固定**：WAN = `eth0`（板载 GMAC）、LAN = `eth1`（USB RTL8153），
  与官方 `board.d/02_network` 的 `ucidef_set_interfaces_lan_wan 'eth1' 'eth0'` 一致，
  脚本不做任何接口探测。
- **WAN6 / IPv6 双栈不在本脚本设置**：官方 `/bin/config_generate` 在 `proto=dhcp`
  时已自动生成 `network.wan6`（`proto=dhcpv6`、`device=eth0`），向光猫请求
  IPv6 地址与 PD 前缀，供下游 LAN 下发 IPv6 —— 故 IPv6 双栈不受影响。
  该脚本在 **preinit_main** 阶段运行（`/lib/preinit/82_config_generate`，
  `boot_hook_add preinit_main do_config_generate`），**早于**本脚本，
  所以本脚本的 `uci set` 不会被它覆盖。
- **有意不写入的**：`packet_steering`、`multipath`、DHCP/DNS 各项
  （含 ImmortalWrt 特有的 `dns_redirect`）、防火墙（`fullcone` / `syn_flood` /
  `flow_offloading`）、默认主题、`ttyd` / `dropbear` 监听范围 ——
  **全部保持 ImmortalWrt 官方默认**。

> ⚠️ **若你启用了透明代理（默认 nikki）**：ImmortalWrt 的防火墙出厂配置
> （由 `001-firewall4-add-support-for-fullcone-nat.patch` 写入）是
> `flow_offloading 1` / `flow_offloading_hw 1` / `fullcone 1` / `fullcone6 0`。
> 其中 **flow offload 会让首包之后的流量绕过 netfilter 钩子，使 TPROXY 透明代理失效**，
> 请在 LuCI「网络 → 防火墙 → 常规设置」里关闭「软件流量分载 / 硬件流量分载」。
> 同理 dnsmasq 出厂 `dns_redirect 1` 会把所有过路 UDP/53 劫持到本机，
> 若代理需要自己接管 DNS，也需一并关闭（LuCI「网络 → DHCP/DNS → 高级设置」）。

---

## 预置软件与第三方源

### 官方源

使用 ImmortalWrt 官方 Image Builder 与官方软件源，因此：

- 官方软件源中的**全部 kmod** 都可直接安装（kmods 源由 Image Builder 自动纳入，
  其依赖精确的内核版本，官方内核天然匹配）。
- 代理插件（默认 nikki）依赖的 `kmod-tun`、`kmod-nft-tproxy`、`kmod-nft-socket`、`kmod-inet-diag`
  等内核模块均由官方 kmods 源解析。

> **注意：首启后官方源地址会被改写为 ImmortalWrt 官方镜像。**
> 固件里的 `default-settings-chn` 带一个 `/etc/uci-defaults/99-default-settings-chinese`，
> 它会把 `/etc/apk/repositories.d/distfeeds.list` 中的
> `https://downloads.immortalwrt.org` 替换为 `https://mirrors.vsean.net/openwrt`
> （该地址会重定向到中科大镜像）。这是 **ImmortalWrt 官方行为**，不是本分支的定制，
> 无需处理；因此看到设备端源地址与官方域名不一致属正常现象。
> 该操作会留下一个 `distfeeds.list.bak`，但 apk 只读取 `.list` 后缀的文件，
> 这个备份不会被加载，无副作用。

### 第三方源：dllkids（kenzok8）

`rockchip/build.sh` 在 `make image` 之前注入 dllkids（kenzok8）的 APK 源与签名公钥：

```sh
CLASHOO_FEED="https://down.dllkids.xyz/openwrt-feed/25.12/aarch64_generic/packages.adb"
CLASHOO_KEY_URL="https://down.dllkids.xyz/openwrt-feed/keys/dllkids-feed.pub.pem"
```

> - `aarch64_generic` 必须与目标的 `ARCH_PACKAGES` 一致，否则 apk 会以架构不符拒绝。
> - 25.12 的配置启用了 `CONFIG_SIGNATURE_CHECK`，**必须**提供公钥，
>   否则安装时会报 `UNTRUSTED signature`。
> - 该源除默认启用的 nikki 外，还提供 clashoo / luci-app-ssr-plus / passwall2 /
>   openclash / mihomo 等。**这些代理插件互斥，只能启用一个** —— 详见下一节
>   「代理插件互斥」。
> - **该源与公钥也会随固件发布**，因此设备端可直接 `apk update` / `apk add` /
>   `apk upgrade` 该源的包。做法：`rockchip/build.sh` 除注入构建期用的
>   `repositories` / `keys/`（`make image` 通过 `--repositories-file` / `--keys-dir`
>   读取，这两个本身不会被拷进固件）之外，还把源与公钥写进 `files/`，随固件落到
>   `/etc/apk/repositories.d/customfeeds.list` 与 `/etc/apk/keys/dllkids-feed.pub.pem`。
>   （`customfeeds.list` 是 `apk` 包为「用户自加第三方源」预留的 conffile，
>   上游默认就是三行注释占位；`CONFIG_SIGNATURE_CHECK=y`，设备端缺公钥会报
>   `UNTRUSTED signature`。）

### 代理插件互斥

dllkids 源里所有**透明代理接管型**插件都只能启用**一个**。本分支默认启用
**nikki**（eBPF / TUN 体系）；若想换成别的（例如 Clashoo、Daed、OpenClash），
必须**先停旧的，再开新的**。

**为什么不能双开**：这些插件每一套都会独自接管整个透明代理 ——
各自写 nftables/iptables 的 `REDIRECT` / `TPROXY` 规则、各自劫持 DNS、
抢占同一批端口（7890-7895 / 9090 / 1080 / 53 …）、各自注册 `/etc/init.d`
脚本与 UCI 配置包。双开的后果是规则打架、LuCI 配置错乱、DNS 解析冲突 ——
轻则无法科学上网，重则刷机后直接断网。

**已核实存在、且互斥的代理栈**（27 个）：

| 代理栈 | 标志包 |
| --- | --- |
| nikki（默认） | `luci-app-nikki` / `nikki` |
| Clashoo | `luci-app-clashoo` / `clashoo` |
| OpenClash | `luci-app-openclash` |
| PassWall | `luci-app-passwall` |
| PassWall2 | `luci-app-passwall2` |
| SSR-Plus | `luci-app-ssr-plus` |
| homeproxy | `luci-app-homeproxy` |
| Daed 系 | `luci-app-dae` / `luci-app-daed` / `luci-app-daed-next` / `luci-app-daede` |
| shadowsocks 系 | `luci-app-shadowsocks` / `luci-app-shadowsocks-libev` / `luci-app-shadowsocks-rust` |
| redsocks 系 | `luci-app-redsocks` / `luci-app-redsocks2` |
| domain-proxy | `luci-app-domain-proxy`（redsocks 的另一种前端：走 dnsmasq+ipset 域名分流） |
| xray | `luci-app-xray` |
| v2raya | `v2raya` |
| ss-redir | `luci-app-ss-redir` |
| nekobox | `luci-app-nekobox` |
| honk | `luci-app-honk` |
| jederproxy | `luci-app-jederproxy` |
| xjay | `luci-app-xjay` |
| mproxy | `luci-app-mproxy` |
| miniproxy | `luci-app-miniproxy` |
| fchomo | `luci-app-fchomo` |

> 后 9 项是 2026-10-12「联动审计」补入的（原表只有 18 套）。起因：dllkids
> 源有 **406 个 `luci-app-*`**，人工维护的黑名单必然滞后。审计方法（可复现）：
> 从源目录列表按 ① 包名关键词（clash / ss / xray / v2ray / proxy / dae …）
> ② apk 体积（自带内核的前端必然 ≥ 数 MiB —— nekobox 10 MiB、honk 9.6 MiB、
> openclash 7.8 MiB；而纯配置页只有几 KiB —— redsocks 2.5 KiB、ss-redir
> 3.5 KiB）两个维度筛出候选，再用上游仓库 README 定性（domain-proxy 由
> Makefile 的 `LUCI_TITLE:="Domain Proxy for redsocks."` 定性）。末三个
> （mproxy / miniproxy / fchomo）按包名与体积推断，置信度略低，若属误判删行即可。

**不算冲突**（可与其他栈共存）：

- 状态页 / 服务端：`luci-app-xray-status`（xray 的配套状态页）、
  `luci-app-ssrserver-python` / `luci-app-ssr-mudb-server` / `luci-app-v2ray-server`、
  `microsocks` / `gost` / `softethervpn`、`tun2socks`（助手）
- DNS 与过滤层：`luci-app-dnsproxy` / `luci-app-dnscrypt-proxy2`（DNS）、
  `luci-app-ikoolproxy`（广告过滤）、`luci-app-haproxy-tcp`（负载均衡）、
  `smartdns` / `mosdns` / `chinadns-ng` / `adguardhome`
- 数据包：`geoview` / `v2ray-geoip` / `v2ray-geosite`

> 以上 `luci-app-*` 与 `rockchip/build.sh` 里的 `_PROXY_OK` 白名单**逐项对应**
> （它们会命中兜底关键词，但已知不接管透明代理，故显式排除）。

**换栈三步走**（改 `shell/custom-packages.sh`）：

1. 注释掉「一」里当前启用的 nikki 行；
2. 取消目标栈那一行的注释（**只取消一行**）；
3. 重跑构建。

**不用靠记性 —— 构建期硬校验**：`rockchip/build.sh` 在合并完 `PACKAGES`
之后会检查代理栈数量，检测到 ≥2 套就 **`exit 1`** 并打印是哪几套、怎么改：

```
ERROR: 检测到 2 套代理插件同时启用：nikki clashoo
```

确需同时安装（仅实验用途、不指望能上网）时可显式放行：

```sh
ALLOW_PROXY_CONFLICT=1 bash rockchip/build.sh
```

> ⚠ 校验用**整词**匹配，不会误判：`luci-app-passwall2` ≠ `luci-app-passwall`、
> `luci-app-xray-status` ≠ `luci-app-xray`、`luci-app-daed-next` ≠ `luci-app-daed`。
>
> 💡 另有**兜底提示**（只提示、不报错）：黑名单挡不住源里新冒出来的代理前端，
> 所以 `build.sh` 还会扫一遍已选包，凡 `luci-app-*` 命中代理关键词
> （clash / ss / xray / v2ray / proxy / nekobox / dae …）又不在上表里的，
> 会打印「疑似代理类」并提醒你补 `_PROXY_STACKS`。状态页 / 服务端 / DNS 层
> （`luci-app-xray-status`、`luci-app-ssrserver-python`、`luci-app-v2ray-server`
> 等）已在白名单里，不会被误提示。
> 新增代理栈时，记得同步更新 `rockchip/build.sh` 里的 `_PROXY_STACKS` 表、
> 本表、以及 `shell/custom-packages.sh` 文件头「注意事项 2」的清单 —— 三处同步。

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

第三方插件默认启用 **nikki** 一个（见上一节「代理插件互斥」；换栈时必须先停旧的）。

### 如何自行增加第三方源

把源的 `packages.adb` **全路径**追加到 Image Builder 根目录的 `repositories` 文件，
并把签名公钥放进 `keys/` 目录。参考 `rockchip/build.sh` 中 dllkids 源的注入写法。

> 注意：`repositories` / `keys/` 是**构建期**注入，只让 `make image` 能找到并校验包。
> 若还希望**设备端**也能从该源安装，需像 dllkids 那样额外把源与公钥写进 `files/`
> （见上一节说明）。

---

## 目录结构

```
.github/workflows/
  build-rockchip-immortalWrt-25.12.x.yml  # R2S 固件构建工作流
  clean-workflow.yml                      # 维护用：清理旧 Actions 运行记录
files/etc/uci-defaults/
  99-custom.sh                            # 首启初始化：只写工作流传入的 WAN/LAN 地址
rockchip/
  build.sh                                # 构建脚本 + dllkids 源注入 + 代理互斥校验
  imm.config                              # Image Builder 配置
shell/
  custom-packages.sh                      # 可选软件包开关（第三方插件默认仅 nikki）
README.md                                 # 本文件
PACKAGES.md                               # 软件包来源与启用方式说明
SUPPORT.md                                # 支持的机型列表
LICENSE                                   # 许可证
.gitattributes                            # 强制 LF 换行（shell 脚本必需）
.gitignore                                # 忽略构建期产物（bin/ custom/ files/etc/…）
```

> 说明：`rockchip/build.sh` 在运行时还会生成 `files/etc/immortalwrt-builder/wan-settings`，
> 工作流会生成 `custom/custom_router_ip.txt`；这两者随固件发布，但**不属于仓库文件**。
>
> ⚠ 它们**不能**放在 `/etc/config/` 下：libuci 会 glob 整个配置目录，只跳过文件名含 `.`
> 的条目（`openwrt/uci` file.c: `get_filename()`），无后缀文件会被当成 uci 配置包解析，
> 使**任何** uci 调用（含 LuCI 软件包页）报 `uci: Parse error (invalid command) at line 1, byte 0`。
> 因此构建期辅助文件统一放 `/etc/immortalwrt-builder/`，详见 `rockchip/build.sh` 顶部说明。

---

## WAN 入站防火墙

本项目采用 **ImmortalWrt 25.12.x 官方镜像的防火墙策略**，做法是「**不干预**」：

- 固件**不提供** `/etc/config/firewall`（完全继承 `firewall4` 包的默认配置）；
- `99-custom.sh` 中**不修改任何防火墙选项**（连流量卸载也不动），
  出厂配置即 ImmortalWrt 官方默认。

官方默认的关键部分（`firewall4` 上游 `root/etc/config/firewall`，ImmortalWrt
`openwrt-25.12` 分支引用的正是这一版，其 `Makefile` 以
`$(CP) -a $(PKG_BUILD_DIR)/root/*` 直接装入；ImmortalWrt 另有
`001-firewall4-add-support-for-fullcone-nat.patch` 追加了 fullcone 与流量卸载开关）：

```text
config defaults
	option syn_flood		1
	option input			REJECT
	option output			ACCEPT
	option forward			REJECT
	option flow_offloading		1     # ← ImmortalWrt 补丁追加
	option flow_offloading_hw	1     # ← ImmortalWrt 补丁追加
	option fullcone			1     # ← ImmortalWrt 补丁追加
	option fullcone6		0     # ← ImmortalWrt 补丁追加

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

> ⚠️ 再次提醒：因为本脚本不干预防火墙，**`flow_offloading` 保持出厂「开启」**。
> 若启用 nikki 等 TPROXY 透明代理，请自行在
> LuCI「网络 → 防火墙 → 常规设置」关闭「软件流量分载 / 硬件流量分载」，
> 否则首包之后的流量会绕过 netfilter 钩子、代理不生效。

> 说明：`ttyd` / `dropbear` 保持上游默认的监听范围 —— `ttyd` 监听 `br-lan`
> （`option interface '@lan'`，init 会把 `@` 前缀解析成网络 `lan` 的设备），
> dropbear 监听 LAN 地址（`option Interface 'lan'`，init 会解析成 LAN 的 IP 后
> 以 `-p <地址>:22` 启动）。`99-custom.sh` **不干预**这两项；需要从其他接口调试时，
> 请在 LuCI「系统 → 管理权 / 服务」里自行放开。

---

## 🎉 鸣谢

感谢以下项目与作者提供的上游工作（本项目最初派生自
[wukongdaily/ImmortalWrt-ImageBuilder](https://github.com/wukongdaily/ImmortalWrt-ImageBuilder)，
现已独立维护、不再跟随上游）：

<div align="left">

<a href="https://github.com/immortalwrt"><img src="https://avatars.githubusercontent.com/immortalwrt?v=4&s=80" width="80" height="80" alt="immortalwrt" /></a>
<a href="https://github.com/wukongdaily"><img src="https://avatars.githubusercontent.com/wukongdaily?v=4&s=80" width="80" height="80" alt="wukongdaily（悟空 / 上游）" /></a>
<a href="https://github.com/kenzok8"><img src="https://avatars.githubusercontent.com/kenzok8?v=4&s=80" width="80" height="80" alt="kenzok8（dllkids 源作者）" /></a>
<a href="https://github.com/sirpdboy"><img src="https://avatars.githubusercontent.com/sirpdboy?v=4&s=80" width="80" height="80" alt="sirpdboy" /></a>
<a href="https://github.com/sbwml"><img src="https://avatars.githubusercontent.com/sbwml?v=4&s=80" width="80" height="80" alt="sbwml" /></a>
<a href="https://github.com/eamonxg"><img src="https://avatars.githubusercontent.com/eamonxg?v=4&s=80" width="80" height="80" alt="eamonxg" /></a>
<a href="https://github.com/timsaya"><img src="https://avatars.githubusercontent.com/timsaya?v=4&s=80" width="80" height="80" alt="timsaya" /></a>

</div>
