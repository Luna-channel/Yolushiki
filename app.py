#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夜鹭机 管理面板 (Phase 2)
"""

import json
import os
import secrets
import subprocess
import threading
import time
import functools
from flask import Flask, render_template, jsonify, request, session, redirect, url_for

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ========== 持久化配置 ==========

CONFIG_DIR = "/opt/yeluji"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "token": None,
    "installed": False,
    "astrbot_port": 6185,
    "napcat_port": 6099,
    "tavern_port": 18888,
    "install_plugins": True,
    "enable_multi_user": True,
    "server_ip": ""
}


def load_config():
    """从 config.json 加载持久化配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
            merged = {**DEFAULT_CONFIG, **saved}
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config():
    """保存配置到 config.json（明文存储）"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


config = load_config()

# 全局安装状态
install_status = {
    "stage": "waiting",  # waiting, installing, completed, error
    "progress": 0,
    "message": "",
    "logs": [],
    "results": {}
}

# 插件列表
ASTRBOT_PLUGINS = [
    {
        "name": "日志拓展插件",
        "repo": "https://github.com/lxfight/astrbot_plugin_logplus.git",
        "description": "排错必备，详细日志记录",
        "selected": True
    },
    {
        "name": "Conversa 主动回复",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_Conversa.git",
        "description": "LLM主动问候回复功能",
        "selected": True
    },
    {
        "name": "人际关系管理",
        "repo": "https://github.com/Zhalslar/astrbot_plugin_relationship.git",
        "description": "好友/群管理功能",
        "selected": True
    },
    {
        "name": "好感度Pro",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_favourpro.git",
        "description": "伪记忆/好感度系统",
        "selected": True
    }
]

# ========== 认证中间件 ==========

# 不需要认证的路径
AUTH_EXEMPT = {"/login", "/api/auth/login", "/api/auth/setup", "/api/auth/check"}


def login_required(f):
    """登录验证装饰器"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not config.get("token"):
            # Token未设置，允许访问（首次使用）
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "未登录", "code": 401}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def log(message):
    """添加日志"""
    timestamp = time.strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    install_status["logs"].append(log_entry)
    print(log_entry)


def run_command(cmd, cwd=None, quiet=False):
    """执行命令并返回结果"""
    if not quiet:
        log(f"执行: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            if not quiet:
                log(f"命令失败: {result.stderr}")
            return False, result.stderr
        return True, result.stdout
    except subprocess.TimeoutExpired:
        if not quiet:
            log("命令超时")
        return False, "命令执行超时"
    except Exception as e:
        if not quiet:
            log(f"执行异常: {str(e)}")
        return False, str(e)


# ========== 服务管理 ==========

def get_service_status(name):
    """获取单个服务状态"""
    if name == "sillytavern":
        ok, output = run_command("pm2 jlist", quiet=True)
        if ok:
            try:
                processes = json.loads(output)
                for p in processes:
                    if p.get("name") == "sillytavern":
                        return {
                            "name": "sillytavern",
                            "status": p.get("pm2_env", {}).get("status", "unknown"),
                            "pid": p.get("pid", 0),
                            "uptime": p.get("pm2_env", {}).get("pm_uptime", 0),
                            "restarts": p.get("pm2_env", {}).get("restart_time", 0),
                            "memory": p.get("monit", {}).get("memory", 0),
                            "cpu": p.get("monit", {}).get("cpu", 0),
                        }
            except (json.JSONDecodeError, KeyError):
                pass
        return {"name": "sillytavern", "status": "stopped", "pid": 0,
                "uptime": 0, "restarts": 0, "memory": 0, "cpu": 0}
    else:
        # Docker 容器 (astrbot / napcat)
        ok, output = run_command(
            f"docker inspect --format='{{{{.State.Status}}}}' {name}", quiet=True
        )
        status = output.strip().strip("'") if ok else "stopped"
        # 获取容器资源占用
        mem = 0
        cpu = 0.0
        if status == "running":
            ok2, stats_out = run_command(
                f"docker stats {name} --no-stream --format='{{{{.MemUsage}}}}|||{{{{.CPUPerc}}}}'",
                quiet=True
            )
            if ok2:
                try:
                    parts = stats_out.strip().strip("'").split("|||")
                    mem_str = parts[0].split("/")[0].strip()
                    if "GiB" in mem_str:
                        mem = int(float(mem_str.replace("GiB", "").strip()) * 1024 * 1024 * 1024)
                    elif "MiB" in mem_str:
                        mem = int(float(mem_str.replace("MiB", "").strip()) * 1024 * 1024)
                    elif "KiB" in mem_str:
                        mem = int(float(mem_str.replace("KiB", "").strip()) * 1024)
                    cpu = float(parts[1].replace("%", "").strip())
                except (IndexError, ValueError):
                    pass
        return {"name": name, "status": status, "pid": 0,
                "uptime": 0, "restarts": 0, "memory": mem, "cpu": cpu}


def get_all_services_status():
    """获取所有服务状态"""
    return {
        "napcat": get_service_status("napcat"),
        "astrbot": get_service_status("astrbot"),
        "sillytavern": get_service_status("sillytavern"),
    }


def service_action(name, action):
    """对服务执行操作"""
    if name == "sillytavern":
        if action == "restart":
            ok, out = run_command("pm2 restart sillytavern", quiet=True)
        elif action == "stop":
            ok, out = run_command("pm2 stop sillytavern", quiet=True)
        elif action == "start":
            ok, out = run_command(
                'pm2 start server.js --name "sillytavern"',
                cwd="/opt/sillytavern", quiet=True
            )
        else:
            return False, "未知操作"
        return ok, out
    else:
        # Docker 容器
        if action == "restart":
            ok, out = run_command(f"docker restart {name}", quiet=True)
        elif action == "stop":
            ok, out = run_command(f"docker stop {name}", quiet=True)
        elif action == "start":
            ok, out = run_command(f"docker start {name}", quiet=True)
        else:
            return False, "未知操作"
        return ok, out


def get_service_logs(name, lines=50):
    """获取服务日志"""
    if name == "sillytavern":
        ok, output = run_command(f"pm2 logs sillytavern --nostream --lines {lines}", quiet=True)
    else:
        ok, output = run_command(f"docker logs --tail {lines} {name}", quiet=True)
        if not ok:
            # docker logs 有时输出到 stderr
            ok2, output2 = run_command(f"docker logs --tail {lines} {name} 2>&1", quiet=True)
            if ok2:
                output = output2
                ok = True
    return output if ok else "无法获取日志"


def get_system_info():
    """获取系统信息"""
    info = {}
    # IP
    ok, out = run_command("curl -s --connect-timeout 3 ifconfig.me || curl -s --connect-timeout 3 ip.sb", quiet=True)
    info["ip"] = out.strip() if ok else config.get("server_ip", "未知")
    # 内存
    ok, out = run_command("free -b | grep Mem", quiet=True)
    if ok:
        parts = out.split()
        info["mem_total"] = int(parts[1]) if len(parts) > 1 else 0
        info["mem_used"] = int(parts[2]) if len(parts) > 2 else 0
    else:
        info["mem_total"] = 0
        info["mem_used"] = 0
    # 磁盘
    ok, out = run_command("df -B1 / | tail -1", quiet=True)
    if ok:
        parts = out.split()
        info["disk_total"] = int(parts[1]) if len(parts) > 1 else 0
        info["disk_used"] = int(parts[2]) if len(parts) > 2 else 0
    else:
        info["disk_total"] = 0
        info["disk_used"] = 0
    # 运行时间
    ok, out = run_command("cat /proc/uptime", quiet=True)
    info["uptime_seconds"] = int(float(out.split()[0])) if ok else 0
    return info


def get_napcat_token():
    """从 NapCat 容器获取 WebUI 登录 token"""
    # 方法1：直接读宿主机挂载文件
    webui_json = "/opt/astrbot/napcat/config/webui.json"
    if os.path.exists(webui_json):
        try:
            with open(webui_json, "r") as f:
                data = json.load(f)
            token = data.get("token", "")
            if token:
                return token
        except Exception:
            pass
    # 方法2：docker exec 读取容器内文件
    ok, output = run_command(
        "docker exec napcat cat /app/napcat/config/webui.json",
        quiet=True
    )
    if ok:
        try:
            data = json.loads(output.strip())
            return data.get("token", "")
        except (json.JSONDecodeError, KeyError):
            pass
    return ""


# ========== 部署函数（保留 Phase 1 逻辑） ==========

def install_docker():
    """安装Docker"""
    log("检查Docker...")
    success, _ = run_command("docker --version")
    if success:
        log("Docker已安装")
        return True
    log("安装Docker...")
    success, _ = run_command("curl -fsSL https://get.docker.com | bash")
    if not success:
        return False
    run_command("systemctl start docker")
    run_command("systemctl enable docker")
    return True


def install_nodejs():
    """安装Node.js"""
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
    """安装PM2"""
    log("检查PM2...")
    success, _ = run_command("pm2 --version")
    if success:
        log("PM2已安装")
        return True
    log("安装PM2...")
    success, _ = run_command("npm install -g pm2")
    return success


def deploy_astrbot():
    """部署AstrBot + NapCat"""
    log("部署AstrBot + NapCat...")
    astrbot_dir = "/opt/astrbot"
    run_command(f"mkdir -p {astrbot_dir}")
    yml_url = "https://raw.githubusercontent.com/NapNeko/NapCat-Docker/main/compose/astrbot.yml"
    success, _ = run_command(f"wget -O astrbot.yml {yml_url}", cwd=astrbot_dir)
    if not success:
        success, _ = run_command(f"curl -o astrbot.yml {yml_url}", cwd=astrbot_dir)
    if not success:
        log("下载astrbot.yml失败")
        return False
    log("启动Docker容器...")
    success, _ = run_command("docker compose -f astrbot.yml up -d", cwd=astrbot_dir)
    if not success:
        success, _ = run_command("docker-compose -f astrbot.yml up -d", cwd=astrbot_dir)
    return success


def deploy_sillytavern():
    """部署SillyTavern"""
    log("部署SillyTavern...")
    tavern_dir = "/opt/sillytavern"
    if not os.path.exists(tavern_dir):
        success, _ = run_command(
            f"git clone https://gh.llkk.cc/https://github.com/SillyTavern/SillyTavern.git {tavern_dir}"
        )
        if not success:
            success, _ = run_command(
                f"git clone https://github.com/SillyTavern/SillyTavern.git {tavern_dir}"
            )
        if not success:
            return False
    log("安装npm依赖...")
    install_success = False
    for attempt in range(3):
        if attempt > 0:
            log(f"npm install 重试第 {attempt} 次...")
            run_command("rm -rf node_modules", cwd=tavern_dir)
            run_command("npm cache clean --force", cwd=tavern_dir)
        success, _ = run_command("npm install --no-audit --no-fund --loglevel=error", cwd=tavern_dir)
        if success:
            install_success = True
            break
    if not install_success:
        log("npm依赖安装失败，已重试3次")
        return False
    # 直接写入完整config.yaml
    # 关键：必须包含 securityOverride: true，否则 listen:true + whitelistMode:false 时
    # SillyTavern 的 logSecurityAlert() 会直接 process.exit(1) 杀死进程
    log("配置SillyTavern...")
    multi_user = str(config['enable_multi_user']).lower()
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
    config_path = os.path.join(tavern_dir, "config.yaml")
    with open(config_path, "w") as f:
        f.write(config_content)
    log(f"config.yaml 已写入（端口:{config['tavern_port']}, securityOverride:true）")
    log("使用PM2启动SillyTavern...")
    run_command("pm2 delete sillytavern 2>/dev/null || true")
    success, _ = run_command(
        'pm2 start server.js --name "sillytavern"',
        cwd=tavern_dir
    )
    if success:
        run_command("pm2 save")
        run_command("pm2 startup 2>/dev/null || true")
        time.sleep(3)
        alive, status_output = run_command("pm2 show sillytavern")
        if alive and "online" in status_output.lower():
            log(f"SillyTavern 已在端口 {config['tavern_port']} 成功启动")
        else:
            log("SillyTavern 可能未正常启动，请检查 pm2 logs sillytavern")
    return success


def install_astrbot_plugins(selected_plugins):
    """安装AstrBot插件"""
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
            success, _ = run_command(
                f"git clone {plugin['repo']} {plugin_name}",
                cwd=plugins_dir
            )
            if not success:
                log(f"插件安装失败: {plugin['name']}")


def configure_firewall():
    """配置防火墙"""
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
    """执行安装"""
    global install_status
    try:
        install_status["stage"] = "installing"

        install_status["progress"] = 5
        install_status["message"] = "更新系统..."
        log("更新系统包...")
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

        # 等待 NapCat 容器初始化完成后获取 token
        install_status["progress"] = 65
        install_status["message"] = "获取NapCat登录Token..."
        log("等待NapCat容器初始化...")
        napcat_token = ""
        for _ in range(10):  # 最多等30秒
            time.sleep(3)
            napcat_token = get_napcat_token()
            if napcat_token:
                log(f"NapCat WebUI Token: {napcat_token}")
                break
        if not napcat_token:
            log("未能自动获取NapCat Token，请手动查看: cat /opt/astrbot/napcat/config/webui.json")
        config["napcat_token"] = napcat_token

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

        # 获取服务器IP并持久化
        ok, ip_out = run_command("curl -s --connect-timeout 5 ifconfig.me || curl -s --connect-timeout 5 ip.sb", quiet=True)
        server_ip = ip_out.strip() if ok else "你的服务器IP"
        config["server_ip"] = server_ip
        config["installed"] = True
        save_config()

        # 构造 NapCat URL（带 token 参数可直接登录）
        napcat_base = f"http://{server_ip}:{config['napcat_port']}"
        napcat_token = config.get("napcat_token", "")
        napcat_url = f"{napcat_base}/webui?token={napcat_token}" if napcat_token else napcat_base

        install_status["results"] = {
            "server_ip": server_ip,
            "napcat_url": napcat_url,
            "napcat_token": napcat_token,
            "astrbot_url": f"http://{server_ip}:{config['astrbot_port']}",
            "tavern_url": f"http://{server_ip}:{config['tavern_port']}"
        }

        # 将夜鹭机管理面板自身注册为 PM2 常驻服务
        log("注册夜鹭机管理面板为常驻服务...")
        run_command("pm2 delete yeluji 2>/dev/null || true", quiet=True)
        yeluji_app = os.path.join(CONFIG_DIR, "app.py")
        if os.path.exists(yeluji_app):
            run_command(
                f'pm2 start {yeluji_app} --name "yeluji" --interpreter python3 -- --port 9999 --host 0.0.0.0',
                quiet=True
            )
            run_command("pm2 save", quiet=True)

        log("夜鹭机安装完成！")

    except Exception as e:
        install_status["stage"] = "error"
        install_status["message"] = str(e)
        log(f"安装失败: {str(e)}")


# ========== 路由：认证 ==========

@app.route("/login")
def login_page():
    """登录页面"""
    if not config.get("token"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/auth/check")
def auth_check():
    """检查认证状态"""
    return jsonify({
        "token_set": bool(config.get("token")),
        "authenticated": bool(session.get("authenticated")),
        "installed": bool(config.get("installed"))
    })


@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    """首次设置 Token"""
    if config.get("token"):
        return jsonify({"error": "Token已设置，无法重复设置"}), 400
    data = request.json or {}
    token = data.get("token", "").strip()
    token_confirm = data.get("token_confirm", "").strip()
    if not token:
        return jsonify({"error": "Token不能为空"}), 400
    if token != token_confirm:
        return jsonify({"error": "两次输入不一致"}), 400
    config["token"] = token
    save_config()
    session["authenticated"] = True
    session.permanent = True
    return jsonify({
        "success": True,
        "message": f"Token已保存至 {CONFIG_FILE}"
    })


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Token 登录"""
    data = request.json or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "请输入Token"}), 400
    if token != config.get("token"):
        return jsonify({"error": "Token错误"}), 401
    session["authenticated"] = True
    session.permanent = True
    return jsonify({"success": True})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """退出登录"""
    session.clear()
    return jsonify({"success": True})


@app.route("/api/auth/change-token", methods=["POST"])
@login_required
def auth_change_token():
    """修改 Token"""
    data = request.json or {}
    old_token = data.get("old_token", "").strip()
    new_token = data.get("new_token", "").strip()
    new_token_confirm = data.get("new_token_confirm", "").strip()
    if old_token != config.get("token"):
        return jsonify({"error": "旧Token错误"}), 401
    if not new_token:
        return jsonify({"error": "新Token不能为空"}), 400
    if new_token != new_token_confirm:
        return jsonify({"error": "两次输入不一致"}), 400
    config["token"] = new_token
    save_config()
    session.clear()
    return jsonify({"success": True, "message": "Token已修改，请重新登录"})


# ========== 路由：页面 ==========

@app.route("/")
@login_required
def index():
    """主页：根据状态显示安装向导或仪表盘"""
    # 传给前端的 config 副本（排除 token，确保可 JSON 序列化）
    safe_config = {
        "installed": config.get("installed", False),
        "astrbot_port": config.get("astrbot_port", 6185),
        "napcat_port": config.get("napcat_port", 6099),
        "tavern_port": config.get("tavern_port", 18888),
        "server_ip": config.get("server_ip", ""),
        "install_plugins": config.get("install_plugins", True),
        "enable_multi_user": config.get("enable_multi_user", True)
    }
    return render_template("index.html",
                           plugins=ASTRBOT_PLUGINS,
                           config=safe_config)


@app.route("/tutorial/<name>")
@login_required
def tutorial(name):
    """教程页面"""
    templates = ["napcat", "astrbot", "tavern"]
    if name not in templates:
        return redirect(url_for("index"))
    return render_template(f"tutorial_{name}.html")


# ========== 路由：服务管理 API ==========

@app.route("/api/services/status")
@login_required
def api_services_status():
    """获取所有服务状态"""
    return jsonify(get_all_services_status())


@app.route("/api/services/<name>/<action>", methods=["POST"])
@login_required
def api_service_action(name, action):
    """服务操作：start/stop/restart"""
    valid_names = {"napcat", "astrbot", "sillytavern"}
    valid_actions = {"start", "stop", "restart"}
    if name not in valid_names:
        return jsonify({"error": f"未知服务: {name}"}), 400
    if action not in valid_actions:
        return jsonify({"error": f"未知操作: {action}"}), 400
    ok, output = service_action(name, action)
    return jsonify({"success": ok, "output": output})


@app.route("/api/services/<name>/logs")
@login_required
def api_service_logs(name):
    """获取服务日志"""
    valid_names = {"napcat", "astrbot", "sillytavern"}
    if name not in valid_names:
        return jsonify({"error": f"未知服务: {name}"}), 400
    lines = request.args.get("lines", 50, type=int)
    output = get_service_logs(name, lines)
    return jsonify({"logs": output})


# ========== 路由：NapCat Token ==========

@app.route("/api/napcat/token")
@login_required
def api_napcat_token():
    """获取 NapCat WebUI Token"""
    token = config.get("napcat_token", "")
    if not token:
        token = get_napcat_token()
        if token:
            config["napcat_token"] = token
            save_config()
    server_ip = config.get("server_ip", "")
    napcat_port = config.get("napcat_port", 6099)
    napcat_base = f"http://{server_ip}:{napcat_port}" if server_ip else ""
    napcat_url = f"{napcat_base}/webui?token={token}" if token and napcat_base else napcat_base
    return jsonify({
        "token": token,
        "url": napcat_url,
        "config_path": "/opt/astrbot/napcat/config/webui.json"
    })


# ========== 路由：系统信息 ==========

@app.route("/api/system/info")
@login_required
def api_system_info():
    """系统信息"""
    return jsonify(get_system_info())


@app.route("/api/system/resources")
@login_required
def api_system_resources():
    """各服务资源占用"""
    services = get_all_services_status()
    result = {}
    for svc_name, svc_info in services.items():
        result[svc_name] = {
            "status": svc_info["status"],
            "memory": svc_info["memory"],
            "cpu": svc_info["cpu"],
        }
    return jsonify(result)


# ========== 路由：安装 API（保留） ==========

@app.route("/api/status")
@login_required
def get_install_status():
    """安装进度"""
    return jsonify(install_status)


@app.route("/api/config")
@login_required
def api_get_config():
    """获取当前配置（不含 Token）"""
    safe_config = {k: v for k, v in config.items() if k != "token"}
    return jsonify(safe_config)


@app.route("/api/plugins")
@login_required
def get_plugins():
    """获取插件列表"""
    return jsonify(ASTRBOT_PLUGINS)


@app.route("/api/install", methods=["POST"])
@login_required
def start_install():
    """开始安装"""
    global install_status
    install_status = {
        "stage": "installing",
        "progress": 0,
        "message": "开始安装...",
        "logs": [],
        "results": {}
    }
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
    save_config()
    thread = threading.Thread(target=do_install)
    thread.start()
    return jsonify({"success": True})


# ========== 启动 ==========

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    # 设置 session 有效期 7 天
    from datetime import timedelta
    app.permanent_session_lifetime = timedelta(days=7)

    print(f"""
╔═══════════════════════════════════════════════════════╗
║              夜鹭机 管理面板 v2.0                      ║
╠═══════════════════════════════════════════════════════╣
║  请在浏览器中打开:                                     ║
║  http://你的服务器IP:{args.port}                       ║
║                                                       ║
║  Token 存储位置: {CONFIG_FILE:<36s} ║
╚═══════════════════════════════════════════════════════╝
""")

    app.run(host=args.host, port=args.port, debug=False)
