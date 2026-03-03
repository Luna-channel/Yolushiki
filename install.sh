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
INSTALLER_DIR="/tmp/yolushiki_installer"
# TODO: 替换为实际的下载地址
DOWNLOAD_BASE="https://raw.githubusercontent.com/Luna-channel/yolushiki/main/installer"

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

# 清理可能残留的旧管理面板进程（用户重跑脚本时避免9999端口冲突）
if command -v pm2 &> /dev/null; then
    pm2 stop yolushiki 2>/dev/null || true
    pm2 delete yolushiki 2>/dev/null || true
fi
pkill -f "python3.*app.py.*--port.*9999" 2>/dev/null || true
fuser -k 9999/tcp 2>/dev/null || true
sleep 1

echo -e "${BLUE}[1/5]${NC} 检测系统环境..."
sleep 0.5

# 检测系统
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo -e "      ${GREEN}✓${NC} 系统: $PRETTY_NAME"
else
    echo -e "      ${RED}✗${NC} 无法识别系统"
    exit 1
fi

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
    echo -e "      ${YELLOW}⚠${NC}  这个过程可能需要 1-2 分钟，请耐心等待..."
    echo ""
    
    # 更新系统
    run_with_spinner "更新软件包列表..." apt-get update -qq
    
    # 安装Python3
    if ! command -v python3 &> /dev/null; then
        run_with_spinner "安装 Python3..." apt-get install -y python3 python3-pip -qq
    fi
    
    # 安装pip（如果没有）
    if ! command -v pip3 &> /dev/null; then
        run_with_spinner "安装 pip3..." apt-get install -y python3-pip -qq
    fi
    
    # 安装Flask（直接用apt，Ubuntu系统最简单的方式）
    if ! python3 -c "import flask" 2>/dev/null; then
        run_with_spinner "安装 Flask..." apt-get install -y python3-flask
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

    for tpl in login.html index.html tutorial_napcat.html tutorial_astrbot.html tutorial_tavern.html; do
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
    echo -e "      ${YELLOW}⚠${NC}  请确保安装包完整（app.py, static/, templates/ 都在同一目录）"
    # 如果下载失败，使用内嵌的精简版（仅包含安装器核心功能）
    cat > "$YOLUSHIKI_DIR/app.py" << 'PYTHON_EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夜鹭机 一键部署 WebUI"""

import os
import subprocess
import threading
import time
import logging
from flask import Flask, render_template, jsonify, request

# 关闭 Flask 请求日志
_werkzeug_log = logging.getLogger('werkzeug')
_werkzeug_log.setLevel(logging.ERROR)

app = Flask(__name__)

install_status = {
    "stage": "waiting",
    "progress": 0,
    "message": "",
    "logs": [],
    "results": {}
}

config = {
    "astrbot_port": 6185,
    "napcat_port": 6099,
    "tavern_port": 18888,
    "install_plugins": True,
    "enable_multi_user": True
}

ASTRBOT_PLUGINS = [
    {"name": "日志拓展插件", "repo": "https://github.com/lxfight/astrbot_plugin_logplus.git", "description": "排错必备，详细日志记录", "selected": True},
    {"name": "Conversa 主动回复", "repo": "https://github.com/Luna-channel/astrbot_plugin_Conversa.git", "description": "LLM主动问候回复功能", "selected": True},
    {"name": "人际关系管理", "repo": "https://github.com/Zhalslar/astrbot_plugin_relationship.git", "description": "好友/群管理功能", "selected": True},
    {"name": "好感度Pro", "repo": "https://github.com/Luna-channel/astrbot_plugin_favourpro.git", "description": "伪记忆/好感度系统", "selected": True}
]

def log(message):
    timestamp = time.strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    install_status["logs"].append(log_entry)
    print(log_entry)

def run_command(cmd, cwd=None, stream=False):
    log(f"执行: {cmd}")
    try:
        if stream:
            # 实时输出模式（用于docker pull等）
            process = subprocess.Popen(
                cmd, shell=True, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            output_lines = []
            last_log_time = 0
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    line = line.strip()
                    output_lines.append(line)
                    # 每1秒更新一次日志
                    now = time.time()
                    if now - last_log_time >= 1:
                        # 显示下载进度
                        if any(k in line for k in ["Downloading", "Pulling", "Download", "Waiting", "Extracting", "complete"]):
                            log(f"  {line[:80]}")
                            last_log_time = now
                            # 同时打印到终端
                            print(f"\r  {line[:80]}", end="", flush=True)
            process.wait()
            return process.returncode == 0, "\n".join(output_lines)
        else:
            result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600)
            output = result.stdout + result.stderr
            if result.returncode != 0:
                log(f"命令失败: {output[-500:] if len(output) > 500 else output}")
            return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        log(f"命令超时（10分钟）")
        return False, "timeout"
    except Exception as e:
        log(f"执行异常: {str(e)}")
        return False, str(e)

def install_docker():
    log("检查Docker...")
    success, _ = run_command("docker --version")
    if success:
        log("Docker已安装")
        return True
    log("安装Docker（阿里云镜像）...")
    success, _ = run_command("curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun")
    if success:
        run_command("systemctl start docker")
        run_command("systemctl enable docker")
    return success

def install_nodejs():
    log("检查Node.js...")
    success, output = run_command("node --version")
    if success and "v" in output:
        version = int(output.strip().split(".")[0].replace("v", ""))
        if version >= 18:
            log(f"Node.js已安装: {output.strip()}")
            return True
    log("安装Node.js 20.x...")
    run_command("curl -fsSL https://deb.nodesource.com/setup_20.x | bash -")
    success, _ = run_command("apt-get install -y nodejs")
    return success

def install_pm2():
    log("检查PM2...")
    success, _ = run_command("pm2 --version")
    if success:
        log("PM2已安装")
        return True
    log("安装PM2...")
    success, _ = run_command("npm install -g pm2 --registry=https://registry.npmmirror.com")
    return success

def deploy_astrbot():
    log("部署AstrBot + NapCat...")
    astrbot_dir = "/opt/astrbot"
    run_command(f"mkdir -p {astrbot_dir}")
    
    # 直接内嵌yml内容，不需要下载
    yml_content = '''services:
  napcat:
    environment:
      - NAPCAT_UID=${NAPCAT_UID:-1000}
      - NAPCAT_GID=${NAPCAT_GID:-1000}
      - MODE=astrbot
    ports:
      - 6099:6099
    container_name: napcat
    restart: always
    image: mlikiowa/napcat-docker:latest
    volumes:
      - ./data:/AstrBot/data
      - ./napcat/config:/app/napcat/config
      - ./ntqq:/app/.config/QQ
    networks:
      - astrbot_network
  astrbot:
    environment:
      - TZ=Asia/Shanghai
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: always
    ports:
      - "6185:6185"
    volumes:
      - ./data:/AstrBot/data
    networks:
      - astrbot_network
networks:
  astrbot_network:
    driver: bridge
'''
    with open(f"{astrbot_dir}/astrbot.yml", "w") as f:
        f.write(yml_content)
    log("astrbot.yml 已生成")
    
    log("拉取Docker镜像（首次约需3-5分钟）...")
    log("拉取 napcat 镜像...")
    run_command("docker pull mlikiowa/napcat-docker:latest", cwd=astrbot_dir, stream=True)
    log("拉取 astrbot 镜像...")
    run_command("docker pull soulter/astrbot:latest", cwd=astrbot_dir, stream=True)
    
    log("启动Docker容器...")
    success, _ = run_command("docker compose -f astrbot.yml up -d", cwd=astrbot_dir)
    if not success:
        success, _ = run_command("docker-compose -f astrbot.yml up -d", cwd=astrbot_dir)
    return success

def deploy_sillytavern():
    log("部署SillyTavern...")
    tavern_dir = "/opt/sillytavern"
    if not os.path.exists(tavern_dir):
        log("克隆SillyTavern...")
        success, _ = run_command(f"git clone https://gh.llkk.cc/https://github.com/SillyTavern/SillyTavern.git {tavern_dir}")
        if not success:
            # 原始地址兜底
            success, _ = run_command(f"git clone https://github.com/SillyTavern/SillyTavern.git {tavern_dir}")
        if not success:
            return False
    log("安装npm依赖...")
    # 使用淘宝镜像加速 npm
    npm_cmd = "npm install --no-audit --no-fund --loglevel=error --registry=https://registry.npmmirror.com"
    install_success = False
    for attempt in range(3):
        if attempt > 0:
            log(f"npm install 重试第 {attempt} 次...")
            run_command("rm -rf node_modules", cwd=tavern_dir)
            run_command("npm cache clean --force", cwd=tavern_dir)
        success, _ = run_command(npm_cmd, cwd=tavern_dir)
        if success:
            install_success = True
            break
    if not install_success:
        log("npm依赖安装失败，已重试3次")
        return False
    log("配置SillyTavern...")
    config_path = os.path.join(tavern_dir, "config.yaml")
    multi_user = str(config['enable_multi_user']).lower()
    # 直接写入完整config.yaml（不再依赖timeout+sed的脆弱方式）
    config_content = f"""dataRoot: ./data
listen: true
listenAddress:
  ipv4: 0.0.0.0
  ipv6: '[::]'
protocol:
  ipv4: true
  ipv6: false
dnsPreferIPv6: false
browserLaunch:
  enabled: false
  browser: 'default'
  hostname: 'auto'
  port: -1
  avoidLocalhost: false
port: {config['tavern_port']}
whitelistMode: false
basicAuthMode: false
enableUserAccounts: {multi_user}
enableDiscreetLogin: true
sessionTimeout: 86400
securityOverride: true
disableCsrfProtection: false
"""
    with open(config_path, "w") as f:
        f.write(config_content)
    log(f"config.yaml 已写入（端口:{config['tavern_port']}, securityOverride:true）")
    log("使用PM2启动SillyTavern...")
    run_command("pm2 delete sillytavern 2>/dev/null || true")
    success, _ = run_command(f'pm2 start server.js --name "sillytavern"', cwd=tavern_dir)
    if success:
        run_command("pm2 save")
        run_command("pm2 startup 2>/dev/null || true")
        # 等待几秒确认进程存活
        import time as _t
        _t.sleep(5)
        alive, status_output = run_command("pm2 show sillytavern")
        if alive and "online" in status_output.lower():
            log(f"SillyTavern 已在端口 {config['tavern_port']} 成功启动")
            set_sillytavern_password(tavern_dir)
        else:
            log("⚠ SillyTavern 可能未正常启动，请检查 pm2 logs sillytavern")
    return success

def set_sillytavern_password(tavern_dir):
    import glob, hashlib, base64
    storage_dir = os.path.join(tavern_dir, "data", "_storage")
    for _ in range(10):
        if os.path.exists(storage_dir):
            break
        import time as _t2
        _t2.sleep(1)
    if not os.path.exists(storage_dir):
        log("SillyTavern 存储目录未创建，跳过密码设置")
        return
    user_files = glob.glob(os.path.join(storage_dir, "*.json"))
    target_file = None
    for f in user_files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if data.get("key") == "user:default-user":
                    target_file = f
                    break
        except:
            continue
    if not target_file:
        log("未找到 default-user 存储文件，跳过密码设置")
        return
    password = secrets.token_urlsafe(12)
    salt = base64.b64encode(os.urandom(16)).decode('utf-8')
    password_hash = hashlib.scrypt(password.encode('utf-8'), salt=salt.encode('utf-8'), n=16384, r=8, p=1, dklen=64)
    password_hash_b64 = base64.b64encode(password_hash).decode('utf-8')
    try:
        with open(target_file, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        data["value"]["password"] = password_hash_b64
        data["value"]["salt"] = salt
        with open(target_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
        config["tavern_password"] = password
        save_config()
        log(f"SillyTavern 初始密码已设置: {password}")
    except Exception as e:
        log(f"设置 SillyTavern 密码失败: {e}")

def install_astrbot_plugins(selected_plugins):
    log("安装AstrBot插件...")
    plugins_dir = "/opt/astrbot/data/plugins"
    run_command(f"mkdir -p {plugins_dir}")
    for plugin in selected_plugins:
        if plugin.get("selected"):
            plugin_name = plugin["repo"].split("/")[-1].replace(".git", "")
            plugin_path = os.path.join(plugins_dir, plugin_name)
            if os.path.exists(plugin_path):
                log(f"插件已存在: {plugin['name']}")
                continue
            log(f"安装插件: {plugin['name']}")
            mirror_repo = plugin['repo'].replace('https://github.com/', 'https://gh.llkk.cc/https://github.com/')
            success, _ = run_command(f"git clone {mirror_repo} {plugin_name}", cwd=plugins_dir)
            if not success:
                run_command(f"git clone {plugin['repo']} {plugin_name}", cwd=plugins_dir)

def configure_firewall():
    log("配置防火墙...")
    ports = [config["astrbot_port"], config["napcat_port"], config["tavern_port"]]
    ufw_exists, _ = run_command("which ufw")
    firewalld_exists, _ = run_command("which firewall-cmd")
    for port in ports:
        if ufw_exists:
            run_command(f"ufw allow {port}/tcp")
        elif firewalld_exists:
            run_command(f"firewall-cmd --permanent --add-port={port}/tcp")
    if firewalld_exists:
        run_command("firewall-cmd --reload")
    log("防火墙配置完成")

def do_install():
    global install_status
    try:
        install_status["stage"] = "installing"
        install_status["progress"] = 5
        install_status["message"] = "更新系统..."
        run_command("apt-get update")
        
        install_status["progress"] = 15
        install_status["message"] = "安装Docker..."
        if not install_docker():
            raise Exception("Docker安装失败")
        
        install_status["progress"] = 30
        install_status["message"] = "安装Node.js..."
        if not install_nodejs():
            raise Exception("Node.js安装失败")
        
        install_status["progress"] = 40
        install_status["message"] = "安装PM2..."
        if not install_pm2():
            raise Exception("PM2安装失败")
        
        install_status["progress"] = 50
        install_status["message"] = "部署AstrBot + NapCat..."
        if not deploy_astrbot():
            raise Exception("AstrBot部署失败")
        
        install_status["progress"] = 70
        install_status["message"] = "部署SillyTavern..."
        if not deploy_sillytavern():
            raise Exception("SillyTavern部署失败")
        
        if config["install_plugins"]:
            install_status["progress"] = 85
            install_status["message"] = "安装插件..."
            install_astrbot_plugins(ASTRBOT_PLUGINS)
        
        install_status["progress"] = 95
        install_status["message"] = "配置防火墙..."
        configure_firewall()
        
        install_status["progress"] = 100
        install_status["message"] = "安装完成！"
        install_status["stage"] = "completed"
        
        success, ip_output = run_command("curl -s ifconfig.me || curl -s ip.sb")
        server_ip = ip_output.strip() if success else "你的服务器IP"
        
        install_status["results"] = {
            "server_ip": server_ip,
            "napcat_url": f"http://{server_ip}:{config['napcat_port']}",
            "astrbot_url": f"http://{server_ip}:{config['astrbot_port']}",
            "tavern_url": f"http://{server_ip}:{config['tavern_port']}"
        }
        log("🎉 夜鹭机安装完成！")
    except Exception as e:
        install_status["stage"] = "error"
        install_status["message"] = str(e)
        log(f"❌ 安装失败: {str(e)}")

@app.route("/")
def index():
    safe_config = {
        "installed": False,
        "astrbot_port": config.get("astrbot_port", 6185),
        "napcat_port": config.get("napcat_port", 6099),
        "tavern_port": config.get("tavern_port", 18888),
        "server_ip": "",
        "install_plugins": config.get("install_plugins", True),
        "enable_multi_user": config.get("enable_multi_user", True)
    }
    return render_template("index.html", plugins=ASTRBOT_PLUGINS, config=safe_config)

@app.route("/api/status")
def get_status():
    return jsonify(install_status)

@app.route("/api/install", methods=["POST"])
def start_install():
    global config, install_status
    install_status = {"stage": "installing", "progress": 0, "message": "开始安装...", "logs": [], "results": {}}
    data = request.json or {}
    if "tavern_port" in data:
        config["tavern_port"] = int(data["tavern_port"])
    if "install_plugins" in data:
        config["install_plugins"] = data["install_plugins"]
    if "enable_multi_user" in data:
        config["enable_multi_user"] = data["enable_multi_user"]
    if "selected_plugins" in data:
        for i, plugin in enumerate(ASTRBOT_PLUGINS):
            plugin["selected"] = i in data["selected_plugins"]
    thread = threading.Thread(target=do_install)
    thread.start()
    return jsonify({"success": True})

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
PYTHON_EOF

    # 内嵌基础 index.html 模板
    cat > "$YOLUSHIKI_DIR/templates/index.html" << 'HTML_EOF'
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>夜鹭机 - 一键部署</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:linear-gradient(135deg,#0a1628 0%,#1a2a4a 100%);min-height:100vh;color:#e0e0e0;display:flex;align-items:center;justify-content:center}
        .container{max-width:700px;width:100%;padding:30px}
        .header{text-align:center;margin-bottom:30px}
        .header h1{font-size:2rem;color:#fff;margin-bottom:8px}
        .header p{color:#888}
        .card{background:rgba(255,255,255,0.05);border-radius:16px;padding:24px;margin-bottom:20px;border:1px solid rgba(255,255,255,0.1)}
        .card h2{color:#fff;margin-bottom:16px;font-size:1.1rem}
        .form-group{margin-bottom:16px}
        .form-group label{display:block;margin-bottom:6px;color:#ccc}
        .form-group input{width:100%;padding:10px 14px;border:1px solid rgba(255,255,255,0.2);border-radius:8px;background:rgba(0,0,0,0.3);color:#fff}
        .btn{display:block;width:100%;padding:14px;font-size:1rem;font-weight:600;border:none;border-radius:10px;cursor:pointer;background:linear-gradient(135deg,#4a8ecf 0%,#2d5a8e 100%);color:#fff}
        .btn:hover{transform:translateY(-2px);box-shadow:0 8px 25px rgba(45,90,142,0.4)}
        .btn:disabled{opacity:0.5;cursor:not-allowed;transform:none}
        .progress{display:none}
        .progress.active{display:block}
        .progress-bar{background:rgba(0,0,0,0.3);border-radius:10px;height:16px;overflow:hidden;margin-bottom:12px}
        .progress-fill{height:100%;background:linear-gradient(90deg,#4a8ecf 0%,#27ae60 100%);width:0%;transition:width 0.5s}
        .progress-text{text-align:center;color:#fff;margin-bottom:16px}
        .log-box{background:#060d18;border-radius:8px;padding:12px;height:180px;overflow-y:auto;font-family:monospace;font-size:0.8rem;color:#00ff00}
        .result{display:none}
        .result.active{display:block}
        .result-link{display:block;padding:16px;background:rgba(0,0,0,0.3);border-radius:10px;margin-bottom:10px;text-decoration:none;color:#4a8ecf}
        .result-link:hover{background:rgba(74,142,207,0.1)}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🌙 夜鹭机</h1>
            <p>一键部署 AstrBot + SillyTavern</p>
        </div>
        <div id="setup" class="card">
            <h2>端口配置</h2>
            <div class="form-group">
                <label>SillyTavern 端口</label>
                <input type="number" id="tavern-port" value="18888" min="1024" max="65535">
            </div>
            <button class="btn" onclick="startInstall()">🚀 开始安装</button>
        </div>
        <div id="progress" class="card progress">
            <h2>正在安装...</h2>
            <div class="progress-bar"><div class="progress-fill" id="bar"></div></div>
            <div class="progress-text" id="msg">准备中...</div>
            <div class="log-box" id="logs"></div>
        </div>
        <div id="result" class="card result">
            <h2>🎉 安装完成！</h2>
            <a class="result-link" id="link-napcat" href="#" target="_blank">📱 NapCat 扫码登录</a>
            <a class="result-link" id="link-astrbot" href="#" target="_blank">🤖 AstrBot 管理面板</a>
            <a class="result-link" id="link-tavern" href="#" target="_blank">🍺 SillyTavern 云酒馆</a>
        </div>
    </div>
    <script>
        const CONFIG = {{ config | tojson }};
        let interval = null;
        function startInstall() {
            const port = document.getElementById('tavern-port').value;
            if (port < 1024 || port > 65535) { alert('端口必须在1024-65535之间'); return; }
            document.getElementById('setup').style.display = 'none';
            document.getElementById('progress').classList.add('active');
            fetch('/api/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tavern_port: port, install_plugins: true, enable_multi_user: true, selected_plugins: [0,1,2,3] })
            });
            interval = setInterval(checkStatus, 2000);
        }
        function checkStatus() {
            fetch('/api/status').then(r => r.json()).then(data => {
                document.getElementById('bar').style.width = data.progress + '%';
                document.getElementById('msg').textContent = data.message + ' (' + data.progress + '%)';
                document.getElementById('logs').innerHTML = data.logs.map(l => '<div>' + l + '</div>').join('');
                document.getElementById('logs').scrollTop = 99999;
                if (data.stage === 'completed') {
                    clearInterval(interval);
                    document.getElementById('progress').classList.remove('active');
                    document.getElementById('result').classList.add('active');
                    document.getElementById('link-napcat').href = data.results.napcat_url;
                    document.getElementById('link-napcat').textContent = '📱 NapCat: ' + data.results.napcat_url;
                    document.getElementById('link-astrbot').href = data.results.astrbot_url;
                    document.getElementById('link-astrbot').textContent = '🤖 AstrBot: ' + data.results.astrbot_url;
                    document.getElementById('link-tavern').href = data.results.tavern_url;
                    document.getElementById('link-tavern').textContent = '🍺 SillyTavern: ' + data.results.tavern_url;
                } else if (data.stage === 'error') {
                    clearInterval(interval);
                    alert('安装失败: ' + data.message);
                }
            });
        }
    </script>
</body>
</html>
HTML_EOF

    echo -e "      ${YELLOW}⚠${NC}  使用内嵌备份版（功能受限）"
else
    echo -e "      ${GREEN}✓${NC}  管理面板文件复制完成"
fi

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
