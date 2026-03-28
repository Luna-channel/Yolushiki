#!/bin/bash
# ============================================================
# 夜鹭机 一键安装引导脚本
#
# 使用方法（在服务器终端粘贴这一行）：
# bash <(curl -fsSL https://ghfast.top/https://raw.githubusercontent.com/Luna-channel/Yolushiki/master/setup.sh)
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

if [ -d "$INSTALL_DIR" ]; then
    echo -e "      ${YELLOW}⚠${NC}  检测到已有安装，将只更新代码文件（保留配置和用户数据）"
fi

# 下载到临时目录，不直接动安装目录
TEMP_DL="/tmp/yolushiki_download"
rm -rf "$TEMP_DL"

# 国内 GitHub 代理列表（自动尝试，哪个通用哪个）
GH_PROXIES=(
    "https://ghfast.top/"
    "https://gh.llkk.cc/"
    "https://ghproxy.cc/"
    ""
)

DOWNLOAD_OK=0

# 方法1: git clone 到临时目录（依次尝试代理）
for proxy in "${GH_PROXIES[@]}"; do
    if [ "$DOWNLOAD_OK" -eq 1 ]; then break; fi
    clone_url="${proxy}${REPO_URL}.git"
    if [ -n "$proxy" ]; then
        echo -e "      ${CYAN}💡${NC}  尝试代理下载: ${proxy}..."
    else
        echo -e "      ${CYAN}💡${NC}  尝试直连 GitHub..."
    fi
    rm -rf "$TEMP_DL" 2>/dev/null
    if timeout 60 git clone --depth 1 "$clone_url" "$TEMP_DL" > /dev/null 2>&1; then
        DOWNLOAD_OK=1
        echo -e "      ${GREEN}✓${NC}  下载完成（git clone）"
    fi
done

# 方法2: curl 下载 tarball 到临时目录（依次尝试代理）
if [ "$DOWNLOAD_OK" -eq 0 ]; then
    echo -e "      ${YELLOW}⚠${NC}  git clone 全部失败，尝试下载压缩包..."
    for proxy in "${GH_PROXIES[@]}"; do
        if [ "$DOWNLOAD_OK" -eq 1 ]; then break; fi
        tarball_url="${proxy}${REPO_URL}/archive/refs/heads/master.tar.gz"
        if [ -n "$proxy" ]; then
            echo -e "      ${CYAN}💡${NC}  尝试代理: ${proxy}..."
        else
            echo -e "      ${CYAN}💡${NC}  尝试直连..."
        fi
        rm -f /tmp/yolushiki.tar.gz 2>/dev/null
        if timeout 60 curl -fsSL "$tarball_url" -o /tmp/yolushiki.tar.gz 2>/dev/null; then
            tar xzf /tmp/yolushiki.tar.gz -C /tmp 2>/dev/null
            if [ -d "/tmp/Yolushiki-master" ]; then
                mv /tmp/Yolushiki-master "$TEMP_DL"
                rm -f /tmp/yolushiki.tar.gz
                DOWNLOAD_OK=1
                echo -e "      ${GREEN}✓${NC}  下载完成（tarball）"
            fi
        fi
    done
fi

# 都失败的话提示用户
if [ "$DOWNLOAD_OK" -eq 0 ]; then
    echo ""
    echo -e "${RED}  ❌ 所有下载方式均失败！${NC}"
    echo ""
    echo -e "  ${YELLOW}解决方法：${NC}"
    echo -e "  1. 在电脑上下载: ${CYAN}${REPO_URL}${NC}"
    echo -e "  2. 通过1Panel文件管理器上传到 ${CYAN}/opt/yolushiki/${NC}"
    echo -e "  3. 运行: ${CYAN}bash /opt/yolushiki/install.sh${NC}"
    echo ""
    exit 1
fi

# 从临时目录把代码文件同步到安装目录（只覆盖代码，不删除用户数据）
mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static"

# 复制核心代码文件
for f in app.py install.sh setup.sh README.md astrbot.yml; do
    if [ -f "$TEMP_DL/$f" ]; then
        cp "$TEMP_DL/$f" "$INSTALL_DIR/$f"
    fi
done

# 复制 templates 和 static（覆盖旧版，但不删除用户可能添加的额外文件）
if [ -d "$TEMP_DL/templates" ]; then
    cp -r "$TEMP_DL/templates/"* "$INSTALL_DIR/templates/" 2>/dev/null
fi
if [ -d "$TEMP_DL/static" ]; then
    cp -r "$TEMP_DL/static/"* "$INSTALL_DIR/static/" 2>/dev/null
fi

echo -e "      ${GREEN}✓${NC}  代码文件已更新（config.json 等用户数据已保留）"

# 清理临时目录
rm -rf "$TEMP_DL"

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
