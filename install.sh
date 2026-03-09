#!/bin/bash
# ============================================================
# 夜鹭机 一键部署脚本
# 
# 使用方法：
# 1. 在1Panel文件管理器中，把这个文件拖到 /opt 目录
# 2. 打开1Panel终端，输入: bash /opt/yolushiki/install.sh
# 3. 按照浏览器提示操作即可
# ============================================================

# 不使用 set -e，手动处理错误

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# 配置
INSTALLER_PORT=9999
LOG_FILE="/tmp/yolushiki_install.log"

# 初始化日志文件
echo "======== 夜鹭机安装日志 ========" > "$LOG_FILE"
echo "开始时间: $(date)" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# 加载动画函数
spin() {
    local pid=$1
    local msg=$2
    local spinstr='|/-\\'
    while [ -d /proc/$pid ]; do
        local temp=${spinstr#?}
        printf "\r      ${YELLOW}⚙${NC}  %s [%c]" "$msg" "$spinstr"
        local spinstr=$temp${spinstr%"$temp"}
        sleep 0.1
    done
    printf "\r      ${GREEN}✓${NC}  %s      \n" "$msg"
}

# 带进度的命令执行
run_with_spinner() {
    local msg=$1
    shift
    "$@" > /dev/null 2>&1 &
    spin $! "$msg"
}
clear
echo -e "${CYAN}"
cat << 'EOF'
    
    ╔═══════════════════════════════════════════════════════╗
    ║                                                       ║
    ║              🌙  夜  鹭  机  🌙                        ║
    ║                                                       ║
    ║         AstrBot + SillyTavern 一键部署                ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝
    
EOF
echo -e "${NC}"

# 检查root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}╔═══════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ❌ 请使用 root 用户运行！            ║${NC}"
    echo -e "${RED}║                                       ║${NC}"
    echo -e "${RED}║  请执行: sudo su                      ║${NC}"
    echo -e "${RED}║  然后重新运行安装命令                 ║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════╝${NC}"
    exit 1
fi

# 强制确保9999端口可用（夜鹭机管理面板优先级最高）
if command -v pm2 &> /dev/null; then
    pm2 stop yolushiki 2>/dev/null || true
    pm2 delete yolushiki 2>/dev/null || true
fi
pkill -f "python3.*app.py.*--port.*9999" 2>/dev/null || true
fuser -k 9999/tcp 2>/dev/null || true
# 二次确认端口释放
sleep 1
if command -v ss &> /dev/null && ss -tlnp 2>/dev/null | grep -q ":9999 "; then
    echo -e "      ${YELLOW}⚠${NC}  9999端口仍被占用，强制释放..."
    fuser -k -9 9999/tcp 2>/dev/null || true
    sleep 1
fi

echo -e "${BLUE}[1/5]${NC} 检测系统环境..."
sleep 0.5

# 检测系统和包管理器
PKG_MANAGER=""
OS_FAMILY=""
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo -e "      ${GREEN}✓${NC} 系统: $PRETTY_NAME"
    case "$ID" in
        ubuntu|debian|linuxmint|pop|deepin|kali|elementary)
            OS_FAMILY="debian"
            PKG_MANAGER="apt-get"
            ;;
        centos|rhel|rocky|almalinux|ol|amzn)
            OS_FAMILY="rhel"
            if command -v dnf &> /dev/null; then
                PKG_MANAGER="dnf"
            else
                PKG_MANAGER="yum"
            fi
            ;;
        fedora)
            OS_FAMILY="fedora"
            PKG_MANAGER="dnf"
            ;;
        arch|manjaro|endeavouros)
            OS_FAMILY="arch"
            PKG_MANAGER="pacman"
            ;;
        opensuse*|sles)
            OS_FAMILY="suse"
            PKG_MANAGER="zypper"
            ;;
        *)
            # 尝试通过包管理器推断
            if command -v apt-get &> /dev/null; then
                OS_FAMILY="debian"
                PKG_MANAGER="apt-get"
            elif command -v dnf &> /dev/null; then
                OS_FAMILY="rhel"
                PKG_MANAGER="dnf"
            elif command -v yum &> /dev/null; then
                OS_FAMILY="rhel"
                PKG_MANAGER="yum"
            elif command -v pacman &> /dev/null; then
                OS_FAMILY="arch"
                PKG_MANAGER="pacman"
            else
                echo -e "      ${RED}✗${NC} 不支持的系统: $ID"
                echo -e "      ${YELLOW}⚠${NC}  目前支持: Ubuntu/Debian/CentOS/RHEL/Fedora/Arch/openSUSE"
                exit 1
            fi
            echo -e "      ${YELLOW}⚠${NC}  未知发行版 $ID，通过包管理器推断为 $OS_FAMILY 系"
            ;;
    esac
    echo -e "      ${GREEN}✓${NC} 包管理器: $PKG_MANAGER ($OS_FAMILY 系)"
else
    echo -e "      ${RED}✗${NC} 无法识别系统（缺少 /etc/os-release）"
    exit 1
fi

# 通用包安装函数
pkg_update() {
    case "$OS_FAMILY" in
        debian)  apt-get update -qq ;;
        rhel|fedora) $PKG_MANAGER makecache -q 2>/dev/null || true ;;
        arch)    pacman -Sy --noconfirm 2>/dev/null || true ;;
        suse)    zypper refresh -q 2>/dev/null || true ;;
    esac
}

pkg_install() {
    case "$OS_FAMILY" in
        debian)  apt-get install -y -qq "$@" ;;
        rhel|fedora) $PKG_MANAGER install -y -q "$@" ;;
        arch)    pacman -S --noconfirm --needed "$@" ;;
        suse)    zypper install -y -n "$@" ;;
    esac
}

# 检查并安装 curl 和 git（基础依赖）
for cmd in curl git; do
    if ! command -v $cmd &> /dev/null; then
        echo -e "      ${YELLOW}⚠${NC}  $cmd 未安装，正在安装..."
        pkg_install $cmd > /dev/null 2>&1
    fi
done

# 检测内存
MEM_TOTAL=$(free -m | awk '/^Mem:/{print $2}')
if [ "$MEM_TOTAL" -lt 1800 ]; then
    echo -e "      ${YELLOW}⚠${NC} 内存: ${MEM_TOTAL}MB (建议2GB以上)"
else
    echo -e "      ${GREEN}✓${NC} 内存: ${MEM_TOTAL}MB"
fi

echo -e "${BLUE}[2/5]${NC} 检查依赖环境..."
echo ""

# 计数器
NEED_INSTALL=0

# 检查Python3
if command -v python3 &> /dev/null; then
    echo -e "      ${GREEN}✓${NC}  Python3: $(python3 --version 2>&1 | awk '{print $2}') [已安装]"
else
    echo -e "      ${YELLOW}○${NC}  Python3: 需要安装"
    NEED_INSTALL=1
fi

# 检查pip3
if command -v pip3 &> /dev/null; then
    echo -e "      ${GREEN}✓${NC}  pip3: 已安装"
else
    echo -e "      ${YELLOW}○${NC}  pip3: 需要安装"
    NEED_INSTALL=1
fi

# 检查Flask
if python3 -c "import flask" 2>/dev/null; then
    echo -e "      ${GREEN}✓${NC}  Flask: 已安装"
else
    echo -e "      ${YELLOW}○${NC}  Flask: 需要安装"
    NEED_INSTALL=1
fi

echo ""

# 如果有需要安装的依赖
if [ "$NEED_INSTALL" -eq 1 ]; then
    echo -e "${BLUE}[3/5]${NC} 安装缺失依赖..."
    echo -e "      ${YELLOW}⚠${NC}  这个过程大约需要 5 分钟，请耐心等待，不要中断或关闭终端！"
    echo ""
    
    # 更新软件包列表
    run_with_spinner "更新软件包列表..." pkg_update
    
    # 安装Python3
    if ! command -v python3 &> /dev/null; then
        case "$OS_FAMILY" in
            debian) run_with_spinner "安装 Python3..." pkg_install python3 python3-pip ;;
            rhel|fedora) run_with_spinner "安装 Python3..." pkg_install python3 python3-pip ;;
            arch) run_with_spinner "安装 Python3..." pkg_install python python-pip ;;
            suse) run_with_spinner "安装 Python3..." pkg_install python3 python3-pip ;;
        esac
    fi
    
    # 安装pip（如果没有）
    if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
        case "$OS_FAMILY" in
            debian) run_with_spinner "安装 pip3..." pkg_install python3-pip ;;
            rhel|fedora) run_with_spinner "安装 pip3..." pkg_install python3-pip ;;
            arch) run_with_spinner "安装 pip..." pkg_install python-pip ;;
            suse) run_with_spinner "安装 pip3..." pkg_install python3-pip ;;
        esac
    fi
    
    # 安装Flask
    if ! python3 -c "import flask" 2>/dev/null; then
        case "$OS_FAMILY" in
            debian) run_with_spinner "安装 Flask..." pkg_install python3-flask ;;
            *)
                # 非Debian系统用pip安装Flask
                PIP_CMD="pip3"
                command -v pip3 &> /dev/null || PIP_CMD="pip"
                run_with_spinner "安装 Flask (pip)..." $PIP_CMD install flask
                ;;
        esac
    fi
    
    # 检查
    if python3 -c "import flask" 2>/dev/null; then
        echo -e "      ${GREEN}✓${NC}  所有依赖安装完成"
    else
        echo "" >> "$LOG_FILE"
        echo "======== Flask 安装失败 ========" >> "$LOG_FILE"
        echo "结束时间: $(date)" >> "$LOG_FILE"
        
        echo ""
        echo -e "${RED}══════════════════════════════════════════════════${NC}"
        echo -e "${RED}  ❌ Flask 安装失败！${NC}"
        echo -e "${RED}══════════════════════════════════════════════════${NC}"
        echo ""
        echo -e "${YELLOW}  请把以下文件发送给开发者进行排查：${NC}"
        echo -e "${CYAN}  📄 $LOG_FILE${NC}"
        echo ""
        echo -e "${YELLOW}  查看日志命令：${NC}"
        echo -e "${CYAN}  cat $LOG_FILE${NC}"
        echo ""
        echo -e "${RED}══════════════════════════════════════════════════${NC}"
        exit 1
    fi
else
    echo -e "${BLUE}[3/5]${NC} 依赖检查完成"
    echo -e "      ${GREEN}✓${NC}  所有依赖已就绪，跳过安装"
fi

echo ""
echo -e "${BLUE}[4/5]${NC} 准备安装向导..."
echo -e "      ${CYAN}💡${NC}  马上就好，正在生成配置界面..."
echo ""

# 创建安装器目录
YOLUSHIKI_DIR="/opt/yolushiki"
mkdir -p "$YOLUSHIKI_DIR/templates"

# 获取脚本所在目录（安装包目录）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$YOLUSHIKI_DIR/static"
mkdir -p "$YOLUSHIKI_DIR/templates"

# 检查是否需要复制（源目录和目标目录不同时才复制）
if [ "$SCRIPT_DIR" = "$YOLUSHIKI_DIR" ]; then
    echo -e "      ${GREEN}✓${NC}  脚本已在目标目录，跳过复制"
    COPY_OK=1
else
    echo -e "      ${CYAN}💡${NC}  复制管理面板文件..."
    COPY_OK=1

    if [ -f "$SCRIPT_DIR/app.py" ]; then
        cp "$SCRIPT_DIR/app.py" "$YOLUSHIKI_DIR/app.py" || COPY_OK=0
    else
        echo -e "      ${RED}✗${NC}  找不到 app.py"
        COPY_OK=0
    fi

    if [ -f "$SCRIPT_DIR/static/logo.png" ]; then
        cp "$SCRIPT_DIR/static/logo.png" "$YOLUSHIKI_DIR/static/logo.png" || COPY_OK=0
    else
        echo -e "      ${YELLOW}⚠${NC}  找不到 logo.png（非必须）"
    fi

    for tpl in login.html index.html tutorial_napcat.html tutorial_astrbot.html tutorial_tavern.html tutorial_server.html; do
        if [ -f "$SCRIPT_DIR/templates/$tpl" ]; then
            cp "$SCRIPT_DIR/templates/$tpl" "$YOLUSHIKI_DIR/templates/$tpl" || COPY_OK=0
        else
            echo -e "      ${RED}✗${NC}  找不到 templates/$tpl"
            COPY_OK=0
        fi
    done
fi

if [ "$COPY_OK" -eq 0 ]; then
    echo -e "      ${RED}✗${NC}  部分文件复制失败"
    echo ""
    echo -e "${RED}══════════════════════════════════════════════════${NC}"
    echo -e "${RED}  ❌ 安装包文件不完整！${NC}"
    echo -e "${RED}══════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "${YELLOW}  请确保以下文件都在同一目录下：${NC}"
    echo -e "${CYAN}    - app.py（主程序）${NC}"
    echo -e "${CYAN}    - templates/ 目录（包含所有 .html 文件）${NC}"
    echo -e "${CYAN}    - static/ 目录（包含 logo 等资源）${NC}"
    echo ""
    echo -e "${YELLOW}  解决方法：${NC}"
    echo -e "${CYAN}    重新下载完整的安装包，将整个 yolushiki 文件夹${NC}"
    echo -e "${CYAN}    上传到服务器的 /opt 目录下，然后重新运行安装命令。${NC}"
    echo ""
    echo -e "${RED}══════════════════════════════════════════════════${NC}"
    exit 1
fi

echo -e "      ${GREEN}✓${NC}  管理面板文件复制完成"


echo -e "      ${GREEN}✓${NC} 安装向导已就绪"

echo -e "${BLUE}[5/5]${NC} 启动管理面板..."

# 获取服务器IP
echo -e "      ${YELLOW}⚙${NC}  获取服务器IP..."
SERVER_IP=$(curl -s --connect-timeout 5 ifconfig.me 2>/dev/null || curl -s --connect-timeout 5 ip.sb 2>/dev/null || hostname -I | awk '{print $1}')

# 放行管理面板端口
if command -v ufw &> /dev/null; then
    ufw allow ${INSTALLER_PORT}/tcp > /dev/null 2>&1
fi

echo ""
echo ""

# 构建URL
WEBUI_URL="http://${SERVER_IP}:${INSTALLER_PORT}"

# 使用OSC 8超链接（1Panel终端支持点击）
CLICKABLE_URL="\e]8;;${WEBUI_URL}\e\\${WEBUI_URL}\e]8;;\e\\"

echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                            ║${NC}"
echo -e "${GREEN}║   🎉  ${CYAN}夜鹭机管理面板已启动！${GREEN}                              ║${NC}"
echo -e "${GREEN}║                                                            ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "   ${CYAN}点击下方链接打开管理界面（可直接点击）：${NC}"
echo ""
echo -e "   ${GREEN}▶▶▶  ${CLICKABLE_URL}  ◀◀◀${NC}"
echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "   ${YELLOW}⚠ 如果打不开，请检查：${NC}"
echo -e "      1. 云服务商控制台是否放行了 ${INSTALLER_PORT} 端口"
echo -e "      2. 防火墙是否允许访问"
echo ""
echo -e "   ${CYAN}Token 存储位置: /opt/yolushiki/config.json${NC}"
echo -e "   ${CYAN}忘记 Token 请运行: cat /opt/yolushiki/config.json${NC}"
echo ""
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

# 启动夜鹭机管理面板
echo -e "${BLUE}正在启动夜鹭机管理面板...${NC}"

if command -v pm2 &> /dev/null; then
    # 检查 PM2 中是否已有 yolushiki 服务
    if pm2 describe yolushiki > /dev/null 2>&1; then
        pm2 stop yolushiki > /dev/null 2>&1 || true
        pm2 start yolushiki > /dev/null 2>&1
        echo -e "${GREEN}✓${NC} 夜鹭机管理面板已重启"
    else
        pm2 start "$YOLUSHIKI_DIR/app.py" --name "yolushiki" --interpreter python3 -- --port $INSTALLER_PORT --host 0.0.0.0 > /dev/null 2>&1
        pm2 save > /dev/null 2>&1
        pm2 startup > /dev/null 2>&1 || true
        echo -e "${GREEN}✓${NC} 夜鹭机管理面板已启动"
    fi
    echo -e "   访问地址: ${WEBUI_URL}"
else
    echo -e "${YELLOW}⚠${NC} PM2 尚未安装，使用后台模式启动..."
    # 杀掉可能存在的旧进程
    pkill -f "python3 app.py --port $INSTALLER_PORT" 2>/dev/null || true
    sleep 1
    cd "$YOLUSHIKI_DIR"
    nohup python3 app.py --port $INSTALLER_PORT --host 0.0.0.0 > /tmp/yolushiki.log 2>&1 &
    echo -e "${GREEN}✓${NC} 夜鹭机管理面板已在后台启动"
    echo -e "   访问地址: ${WEBUI_URL}"
    echo -e "   日志文件: /tmp/yolushiki.log"
fi

echo ""
echo -e "${GREEN}安装脚本执行完毕！${NC}"
