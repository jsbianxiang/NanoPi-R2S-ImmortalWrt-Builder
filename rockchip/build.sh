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

echo "Create wan-settings"
mkdir -p  /home/build/immortalwrt/files/etc/config

# DNS 留空时回落到网关（光猫一般自带 DNS 转发）
WAN_DNS="${WAN_DNS:-$WAN_GATEWAY}"
# WAN 接入方式：默认 dhcp（与 ImmortalWrt 官方默认一致），可选 static
WAN_PROTO="${WAN_PROTO:-dhcp}"

# 创建 WAN 配置文件（yml 传入 WAN_PROTO / WAN_IP / WAN_GATEWAY / WAN_DNS）
# 该文件随固件发布，供 99-custom.sh 首次启动时读取
cat << EOF > /home/build/immortalwrt/files/etc/config/wan-settings
wan_proto=${WAN_PROTO}
wan_ip=${WAN_IP}
wan_gateway=${WAN_GATEWAY}
wan_dns=${WAN_DNS}
EOF

echo "cat wan-settings"
cat /home/build/immortalwrt/files/etc/config/wan-settings

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
PACKAGES="$PACKAGES kmod-tcp-bbr"
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

make image PROFILE=$PROFILE PACKAGES="$PACKAGES" FILES="/home/build/immortalwrt/files" ROOTFS_PARTSIZE=$ROOTFS_PARTSIZE

if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Error: Build failed!"
    exit 1
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') - Build completed successfully."
