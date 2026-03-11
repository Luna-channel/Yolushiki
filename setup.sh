#!/bin/bash
# ============================================================
# 夜鹭机 一键安装引导脚本
#
# 使用方法（在服务器终端粘贴这一行）：
# bash <(curl -fsSL https://raw.githubusercontent.com/Luna-channel/Yolushiki/master/setup.sh)
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

REPO_URL="https://github.com/Luna-channel/Yolushiki"
INSTALL_DIR="/opt/yolushiki"

clear
echo -e "${CYAN}"
cat << 'EOF'

    ╔═══════════════════════════════════════════════════════╗
    ║                                                       ║
    ║              🌙  夜  鹭  机  🌙                        ║
    ║                                                       ║
    ║              一键安装引导脚本                          ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

EOF
echo -e "${NC}"

# 检查 root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}╔═══════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ❌ 请使用 root 用户运行！            ║${NC}"
    echo -e "${RED}║                                       ║${NC}"
    echo -e "${RED}║  请执行: sudo su                      ║${NC}"
    echo -e "${RED}║  然后重新运行安装命令                 ║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════╝${NC}"
    exit 1
fi

# 检测包管理器
PKG_MANAGER=""
if command -v apt-get &> /dev/null; then
    PKG_MANAGER="apt-get"
elif command -v dnf &> /dev/null; then
    PKG_MANAGER="dnf"
elif command -v yum &> /dev/null; then
    PKG_MANAGER="yum"
elif command -v pacman &> /dev/null; then
    PKG_MANAGER="pacman"
elif command -v zypper &> /dev/null; then
    PKG_MANAGER="zypper"
else
    echo -e "${RED}  ❌ 无法识别包管理器，请确认你的系统是 Linux${NC}"
    exit 1
fi

echo -e "${BLUE}[1/3]${NC} 检查环境..."

# 对 apt-get 系统先修复 dpkg（防止上次安装中断导致包管理器损坏）
if [ "$PKG_MANAGER" = "apt-get" ]; then
    if dpkg --audit 2>&1 | grep -qi "."; then
        echo -e "      ${YELLOW}⚠${NC}  修复包管理器..."
        dpkg --configure -a > /dev/null 2>&1 || true
    fi
fi

# 安装 curl 和 git
for cmd in curl git; do
    if ! command -v $cmd &> /dev/null; then
        echo -e "      ${YELLOW}⚠${NC}  $cmd 未安装，正在安装..."
        case "$PKG_MANAGER" in
            apt-get)  apt-get update -qq && apt-get install -y -qq $cmd ;;
            dnf|yum)  $PKG_MANAGER install -y -q $cmd ;;
            pacman)   pacman -Sy --noconfirm $cmd ;;
            zypper)   zypper install -y -n $cmd ;;
        esac > /dev/null 2>&1
        if ! command -v $cmd &> /dev/null; then
            echo -e "      ${RED}✗${NC}  $cmd 安装失败，请手动安装后重试"
            exit 1
        fi
    fi
done

echo -e "      ${GREEN}✓${NC}  环境检查完成"

# 下载文件
echo ""
echo -e "${BLUE}[2/3]${NC} 下载夜鹭机文件..."

# 如果目录已存在，先备份配置再清理
if [ -d "$INSTALL_DIR" ]; then
    echo -e "      ${YELLOW}⚠${NC}  检测到已有安装，更新文件..."
    # 保留 config.json（用户的 Token 配置）
    if [ -f "$INSTALL_DIR/config.json" ]; then
        cp "$INSTALL_DIR/config.json" /tmp/yolushiki_config_backup.json
    fi
    rm -rf "$INSTALL_DIR"
fi

# 优先用 git clone
DOWNLOAD_OK=0

echo -e "      ${CYAN}💡${NC}  正在从 GitHub 下载..."
if git clone --depth 1 "$REPO_URL.git" "$INSTALL_DIR" > /dev/null 2>&1; then
    DOWNLOAD_OK=1
    echo -e "      ${GREEN}✓${NC}  下载完成（git clone）"
fi

# git 失败时用 curl 下载 tarball
if [ "$DOWNLOAD_OK" -eq 0 ]; then
    echo -e "      ${YELLOW}⚠${NC}  git clone 失败，尝试直接下载压缩包..."
    mkdir -p "$INSTALL_DIR"
    if curl -fsSL "$REPO_URL/archive/refs/heads/master.tar.gz" -o /tmp/yolushiki.tar.gz; then
        tar xzf /tmp/yolushiki.tar.gz -C /tmp
        # GitHub tarball 解压后目录名为 Yolushiki-master
        if [ -d "/tmp/Yolushiki-master" ]; then
            cp -r /tmp/Yolushiki-master/* "$INSTALL_DIR/"
            rm -rf /tmp/Yolushiki-master /tmp/yolushiki.tar.gz
            DOWNLOAD_OK=1
            echo -e "      ${GREEN}✓${NC}  下载完成（tarball）"
        fi
    fi
fi

# 都失败的话提示用户
if [ "$DOWNLOAD_OK" -eq 0 ]; then
    echo -e "${RED}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ❌ 下载失败！                                           ║${NC}"
    echo -e "${RED}║                                                           ║${NC}"
    echo -e "${RED}║  可能原因：                                               ║${NC}"
    echo -e "${RED}║  1. 服务器无法访问 GitHub                                 ║${NC}"
    echo -e "${RED}║  2. 网络连接不稳定                                        ║${NC}"
    echo -e "${RED}║                                                           ║${NC}"
    echo -e "${RED}║  解决方法：                                               ║${NC}"
    echo -e "${RED}║  手动下载 yolushiki 文件夹上传到 /opt/yolushiki/          ║${NC}"
    echo -e "${RED}║  然后运行: bash /opt/yolushiki/install.sh                 ║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════════════════════════╝${NC}"
    exit 1
fi

# 恢复 config.json
if [ -f /tmp/yolushiki_config_backup.json ]; then
    cp /tmp/yolushiki_config_backup.json "$INSTALL_DIR/config.json"
    rm -f /tmp/yolushiki_config_backup.json
    echo -e "      ${GREEN}✓${NC}  已恢复之前的 Token 配置"
fi

# 清理 git 元数据（不需要保留 .git 目录）
rm -rf "$INSTALL_DIR/.git"

# 验证关键文件
echo ""
echo -e "${BLUE}[3/3]${NC} 验证文件完整性..."

MISSING=0
for f in app.py install.sh templates/index.html templates/login.html templates/tutorial_napcat.html templates/tutorial_astrbot.html templates/tutorial_tavern.html templates/tutorial_server.html; do
    if [ ! -f "$INSTALL_DIR/$f" ]; then
        echo -e "      ${RED}✗${NC}  缺少文件: $f"
        MISSING=1
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo -e "${RED}  ❌ 文件不完整，请重试或手动上传${NC}"
    exit 1
fi

echo -e "      ${GREEN}✓${NC}  文件完整"

# 给 install.sh 执行权限并运行
chmod +x "$INSTALL_DIR/install.sh"

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ 下载完成！正在启动安装向导...${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
sleep 1

# 运行安装脚本
bash "$INSTALL_DIR/install.sh"
