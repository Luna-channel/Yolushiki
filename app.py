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
import hashlib
import base64
from flask import Flask, render_template, jsonify, request, session, redirect, url_for

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ========== 持久化配置 ==========

CONFIG_DIR = "/opt/yolushiki"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "token": None,
    "installed": False,
    "astrbot_port": 6185,
    "napcat_port": 6099,
    "tavern_port": 18888,
    "install_plugins": True,
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
    "results": {},
    "current_stage": ""  # docker_pull, git_clone, npm_install, 等
}
install_generation = 0  # 安装代数，重试时递增，旧线程检测到不匹配则退出

# 镜像/加速配置（运行时可切换）
runtime_mirrors = {
    "npm_registry": "",       # 空=官方源, 或 https://registry.npmmirror.com
    "git_proxy": "",          # 空=直连GitHub, 或 https://gh.llkk.cc/
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
            # Token未设置，跳转到登录页（会显示首次设置弹窗）
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "请先设置Token", "code": 401}), 401
            return redirect(url_for("login_page"))
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


def run_command_stream(cmd, cwd=None, timeout=600, generation=None):
    """执行命令并实时输出日志（用于 Docker pull / git clone / npm install 等）
    每一行输出都立即写入 install_status["logs"]，前端轮询即可实时看到。
    generation: 传入当前安装代数，如果被新安装覆盖则中止
    """
    log(f"执行: {cmd}")
    try:
        process = subprocess.Popen(
            cmd, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        output_lines = []
        for line in process.stdout:
            # 检查是否被新安装覆盖，是则杀掉子进程退出
            if generation is not None and generation != install_generation:
                process.kill()
                return False, "已被新安装取消"
            line = line.strip()
            if line:
                output_lines.append(line)
                # 每行都立即写入日志，前端即刻可见
                log(line[:120])
        process.wait(timeout=timeout)
        if process.returncode != 0:
            log(f"命令返回码: {process.returncode}")
            return False, "\n".join(output_lines)
        return True, "\n".join(output_lines)
    except subprocess.TimeoutExpired:
        process.kill()
        log("命令超时")
        return False, "命令执行超时"
    except Exception as e:
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
    # 酒馆登录信息
    info["tavern_username"] = config.get("tavern_username", "")
    info["tavern_password"] = config.get("tavern_password", "")
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
    success, output = run_command("docker --version", quiet=True)
    if success:
        log(f"Docker已安装: {output.strip()}")
        return True
    log("Docker未安装，开始安装...")
    success, _ = run_command("curl -fsSL https://get.docker.com | bash")
    if not success:
        return False
    run_command("systemctl start docker")
    run_command("systemctl enable docker")
    # 只有新安装 Docker 时才自动配置镜像加速
    configure_docker_mirrors()
    return True


def configure_docker_mirrors():
    """配置Docker镜像加速（大陆服务器必须）"""
    daemon_json = "/etc/docker/daemon.json"
    # 检查是否已配置
    if os.path.exists(daemon_json):
        try:
            with open(daemon_json, "r") as f:
                content = f.read()
                if "registry-mirrors" in content:
                    log("Docker镜像加速已配置")
                    return
        except:
            pass
    log("配置Docker镜像加速...")
    mirror_config = '''{
  "registry-mirrors": [
    "https://docker.1panel.live",
    "https://dockerproxy.cn",
    "https://docker.m.daocloud.io"
  ]
}'''
    try:
        with open(daemon_json, "w") as f:
            f.write(mirror_config)
        run_command("systemctl daemon-reload", quiet=True)
        run_command("systemctl restart docker", quiet=True)
        log("Docker镜像加速配置完成")
    except Exception as e:
        log(f"配置镜像加速失败: {e}")


def install_nodejs():
    """安装Node.js"""
    log("检查Node.js...")
    success, output = run_command("node --version", quiet=True)
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
    success, output = run_command("pm2 --version", quiet=True)
    if success:
        log(f"PM2已安装: {output.strip()}")
        return True
    log("PM2未安装，开始安装...")
    npm_registry = runtime_mirrors.get("npm_registry", "")
    if npm_registry:
        success, _ = run_command(f"npm install -g pm2 --registry={npm_registry}")
    else:
        success, _ = run_command("npm install -g pm2")
    return success


def deploy_astrbot():
    """部署AstrBot + NapCat"""
    log("部署AstrBot + NapCat...")
    astrbot_dir = "/opt/astrbot"
    run_command(f"mkdir -p {astrbot_dir}")
    # 直接内嵌 yml（使用镜像加速地址，避免从 GitHub 下载）
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
    image: m.daocloud.io/docker.io/soulter/astrbot:latest
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
    log("astrbot.yml 已生成（含镜像加速）")
    # 拉取镜像：直接用流式模式，实时显示进度
    log("拉取Docker镜像（实时进度）...")
    install_status["message"] = "拉取 Docker 镜像中..."
    install_status["current_stage"] = "docker_pull"
    pull_ok, _ = run_command_stream(
        "docker compose -f astrbot.yml pull",
        cwd=astrbot_dir, generation=install_generation
    )
    if not pull_ok:
        pull_ok, _ = run_command_stream(
            "docker-compose -f astrbot.yml pull",
            cwd=astrbot_dir, generation=install_generation
        )
    if not pull_ok:
        log("Docker镜像拉取失败，请检查网络或更换Docker镜像源")
        return False
# 启动容器

    log("启动Docker容器...")
    install_status["message"] = "启动 Docker 容器..."
    success, _ = run_command("docker compose -f astrbot.yml up -d", cwd=astrbot_dir)
    if not success:
        success, _ = run_command("docker-compose -f astrbot.yml up -d", cwd=astrbot_dir)
    return success


def deploy_sillytavern():
    """部署SillyTavern"""
    log("部署SillyTavern...")
    tavern_dir = "/opt/sillytavern"
    package_json = os.path.join(tavern_dir, "package.json")
    # 检查 package.json 是否存在，目录存在但文件不存在说明之前克隆失败
    if os.path.exists(tavern_dir) and not os.path.exists(package_json):
        log("检测到不完整的 SillyTavern 目录，清理后重新克隆...")
        run_command(f"rm -rf {tavern_dir}")
    if not os.path.exists(tavern_dir):
        log("克隆 SillyTavern 仓库...")
        install_status["message"] = "克隆 SillyTavern 仓库..."
        install_status["current_stage"] = "git_clone"
        git_proxy = runtime_mirrors.get("git_proxy", "")
        if git_proxy:
            clone_url = f"{git_proxy}https://github.com/SillyTavern/SillyTavern.git"
            log(f"使用代理: {git_proxy}")
        else:
            clone_url = "https://github.com/SillyTavern/SillyTavern.git"
        success, _ = run_command_stream(
            f"git clone --depth 1 --progress {clone_url} {tavern_dir}",
            generation=install_generation
        )
        if not success and git_proxy:
            log("代理克隆失败，尝试直连 GitHub...")
            run_command(f"rm -rf {tavern_dir}")
            success, _ = run_command_stream(
                f"git clone --depth 1 --progress https://github.com/SillyTavern/SillyTavern.git {tavern_dir}",
                generation=install_generation
            )
        if not success:
            log("SillyTavern 仓库克隆失败")
            return False
    # 再次验证克隆是否成功（防止 git 返回成功但目录为空的情况）
    if not os.path.exists(package_json):
        log("克隆后 package.json 不存在，目录可能不完整")
        return False
    log("安装npm依赖...")
    install_status["message"] = "安装 npm 依赖（可能需要几分钟）..."
    install_status["current_stage"] = "npm_install"
    install_success = False
    npm_registry = runtime_mirrors.get("npm_registry", "")
    if npm_registry:
        npm_cmd = f"npm install --no-audit --no-fund --registry={npm_registry}"
        log(f"使用npm镜像: {npm_registry}")
    else:
        npm_cmd = "npm install --no-audit --no-fund"
    for attempt in range(3):
        if attempt > 0:
            log(f"npm install 重试第 {attempt} 次...")
            run_command("rm -rf node_modules", cwd=tavern_dir)
            run_command("npm cache clean --force", cwd=tavern_dir)
        success, output = run_command_stream(npm_cmd, cwd=tavern_dir, generation=install_generation, timeout=900)
        # 即使返回码非0，也检查 node_modules 是否实际存在
        node_modules = os.path.join(tavern_dir, "node_modules")
        if success:
            install_success = True
            break
        elif os.path.exists(node_modules) and len(os.listdir(node_modules)) > 10:
            log("npm 返回码非0，但 node_modules 已存在（可能是 warning 导致），视为成功")
            install_success = True
            break
    if not install_success:
        log("npm依赖安装失败，已重试3次")
        return False
    # 直接写入完整config.yaml
    # 关键：必须包含 securityOverride: true，否则 listen:true + whitelistMode:false 时
    # SillyTavern 的 logSecurityAlert() 会直接 process.exit(1) 杀死进程
    log("配置SillyTavern...")
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
enableUserAccounts: true
enableDiscreetLogin: false
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
        run_command("pm2 save", quiet=True)
        run_command("pm2 startup 2>/dev/null || true", quiet=True)
        time.sleep(5)  # 等待 SillyTavern 初始化存储
        alive, status_output = run_command("pm2 show sillytavern")
        if alive and "online" in status_output.lower():
            log(f"SillyTavern 已在端口 {config['tavern_port']} 成功启动")
            # 为 default-user 设置初始密码
            set_sillytavern_password(tavern_dir)
        else:
            log("SillyTavern 可能未正常启动，请检查 pm2 logs sillytavern")
    return success


def set_sillytavern_password(tavern_dir):
    """为 SillyTavern 设置用户指定的用户名和密码"""
    import glob
    storage_dir = os.path.join(tavern_dir, "data", "_storage")
    
    # 等待存储目录创建
    for _ in range(15):
        if os.path.exists(storage_dir):
            break
        time.sleep(1)
    
    if not os.path.exists(storage_dir):
        log("SillyTavern 存储目录未创建，跳过账号设置")
        return
    
    # 查找 default-user 的存储文件
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
        log("未找到 default-user 存储文件，跳过账号设置")
        return
    
    # 获取用户设置的用户名和密码
    username = config.get("tavern_username", "admin")
    password = config.get("tavern_password", "")
    if not password:
        password = secrets.token_urlsafe(12)
        log(f"未设置密码，自动生成: {password}")
    
    # 生成 salt 和 hash（与 SillyTavern 的 scrypt 算法兼容）
    salt = base64.b64encode(os.urandom(16)).decode('utf-8')
    password_hash = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt.encode('utf-8'),
        n=16384, r=8, p=1, dklen=64
    )
    password_hash_b64 = base64.b64encode(password_hash).decode('utf-8')
    
    # 更新用户记录
    try:
        with open(target_file, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        
        # 设置密码
        data["value"]["password"] = password_hash_b64
        data["value"]["salt"] = salt
        # 修改用户名（key 和 name 都改）
        data["key"] = f"user:{username}"
        data["value"]["name"] = username
        # 确保是管理员且启用
        data["value"]["admin"] = True
        data["value"]["enabled"] = True
        
        with open(target_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
        
        # 保存到配置
        config["tavern_username"] = username
        config["tavern_password"] = password
        save_config()
        log(f"SillyTavern 账号已设置 - 用户名: {username}")
        
        # 重启酒馆使改动生效
        log("重启 SillyTavern 以应用账号设置...")
        run_command("pm2 restart sillytavern", quiet=True)
        time.sleep(3)
    except Exception as e:
        log(f"设置 SillyTavern 账号失败: {e}")


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


def do_install(generation):
    """执行安装。
    流程：1.检查依赖 → 2.安装缺失依赖 → 3.按顺序部署服务（流式进度）
    generation 用于检测是否被新的安装请求覆盖。
    """
    global install_status

    def cancelled():
        return generation != install_generation

    need_docker = config.get("install_astrbot", True)
    need_tavern = config.get("install_tavern", True)

    try:
        install_status["stage"] = "installing"

        # ========== 阶段1：检查依赖 ==========
        install_status["progress"] = 2
        install_status["message"] = "检查服务器依赖..."
        log("===== 阶段1：检查依赖 =====")

        has_docker = False
        has_nodejs = False
        has_pm2 = False

        ok, out = run_command("docker --version", quiet=True)
        if ok:
            log(f"Docker 已安装: {out.strip()}")
            has_docker = True
        else:
            log("Docker 未安装")

        ok, out = run_command("node --version", quiet=True)
        if ok and "v" in out:
            ver = int(out.strip().split(".")[0].replace("v", ""))
            if ver >= 18:
                log(f"Node.js 已安装: {out.strip()}")
                has_nodejs = True
            else:
                log(f"Node.js 版本过低: {out.strip()}，需要 v18+")
        else:
            log("Node.js 未安装")

        ok, out = run_command("pm2 --version", quiet=True)
        if ok:
            log(f"PM2 已安装: {out.strip()}")
            has_pm2 = True
        else:
            log("PM2 未安装")

        # ========== 阶段2：安装缺失依赖 ==========
        install_status["progress"] = 5
        install_status["message"] = "安装缺失依赖..."
        log("===== 阶段2：安装缺失依赖 =====")

        missing = []
        if need_docker and not has_docker:
            missing.append("Docker")
        if need_tavern and not has_nodejs:
            missing.append("Node.js")
        if need_tavern and not has_pm2:
            missing.append("PM2")

        if missing:
            log(f"需要安装: {', '.join(missing)}")
            install_status["message"] = "更新系统包..."
            run_command("apt-get update -qq")
        else:
            log("所有依赖已就绪，跳过安装")

        if cancelled(): return

        if need_docker and not has_docker:
            install_status["progress"] = 8
            install_status["message"] = "安装 Docker..."
            if not install_docker():
                raise Exception("Docker安装失败")

        if cancelled(): return

        if need_tavern and not has_nodejs:
            install_status["progress"] = 15
            install_status["message"] = "安装 Node.js..."
            if not install_nodejs():
                raise Exception("Node.js安装失败")

        if cancelled(): return

        if need_tavern and not has_pm2:
            install_status["progress"] = 20
            install_status["message"] = "安装 PM2..."
            if not install_pm2():
                raise Exception("PM2安装失败")

        if cancelled(): return

        # ========== 阶段3：按顺序部署服务 ==========
        log("===== 阶段3：部署服务 =====")
        napcat_token = ""

        if need_docker:
            install_status["progress"] = 25
            install_status["message"] = "部署 AstrBot + NapCat..."
            if not deploy_astrbot():
                raise Exception("AstrBot部署失败")

            # 等待 NapCat 容器初始化完成后获取 token
            install_status["progress"] = 55
            install_status["message"] = "获取 NapCat 登录 Token..."
            log("等待NapCat容器初始化...")
            for _ in range(10):
                time.sleep(3)
                napcat_token = get_napcat_token()
                if napcat_token:
                    log(f"NapCat WebUI Token: {napcat_token}")
                    break
            if not napcat_token:
                log("未能自动获取NapCat Token，请手动查看: cat /opt/astrbot/napcat/config/webui.json")
            config["napcat_token"] = napcat_token

            if config.get("install_plugins", False):
                install_status["progress"] = 60
                install_status["message"] = "安装 AstrBot 插件..."
                install_astrbot_plugins(ASTRBOT_PLUGINS)
        else:
            log("跳过 AstrBot 安装")

        if cancelled(): return

        if need_tavern:
            install_status["progress"] = 65
            install_status["message"] = "部署 SillyTavern..."
            if not deploy_sillytavern():
                raise Exception("SillyTavern部署失败")
        else:
            log("跳过 SillyTavern 安装")

        if cancelled(): return

        install_status["progress"] = 92
        install_status["message"] = "配置防火墙..."
        configure_firewall()

        # ========== 阶段4：收尾 ==========
        install_status["progress"] = 95
        install_status["message"] = "保存配置..."

        # 获取服务器 IP：优先用本地命令，避免外部网络请求卡住
        server_ip = ""
        ok, ip_out = run_command("hostname -I | awk '{print $1}'", quiet=True)
        if ok and ip_out.strip():
            server_ip = ip_out.strip()
        if not server_ip:
            server_ip = "你的服务器IP"
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
            "tavern_url": f"http://{server_ip}:{config['tavern_port']}",
            "tavern_username": config.get("tavern_username", "admin"),
            "tavern_password": config.get("tavern_password", "")
        }

        # 将夜鹭机管理面板自身注册为 PM2 常驻服务
        install_status["progress"] = 98
        install_status["message"] = "注册常驻服务..."
        log("注册夜鹭机管理面板为常驻服务...")
        run_command("pm2 delete yolushiki 2>/dev/null || true", quiet=True)
        yolushiki_app = os.path.join(CONFIG_DIR, "app.py")
        if os.path.exists(yolushiki_app):
            run_command(
                f'pm2 start {yolushiki_app} --name "yolushiki" --interpreter python3 -- --port 9999 --host 0.0.0.0',
                quiet=True
            )
            run_command("pm2 save", quiet=True)

        install_status["progress"] = 100
        install_status["message"] = "安装完成！"
        install_status["stage"] = "completed"
        log("夜鹭机安装完成！")

    except Exception as e:
        install_status["stage"] = "error"
        install_status["message"] = str(e)
        log(f"安装失败: {str(e)}")


# ========== 路由：认证 ==========

@app.route("/login")
def login_page():
    """登录页面"""
    # 无论是否设置 token，都显示登录页（login.html 会根据情况显示设置弹窗或登录表单）
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
    """退出登录并关闭服务器"""
    session.clear()
    # 延迟关闭服务器，让响应先返回
    def shutdown_server():
        time.sleep(1)
        os._exit(0)
    threading.Thread(target=shutdown_server, daemon=True).start()
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
        "install_plugins": config.get("install_plugins", True)
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


# ========== 路由：服务安装状态检测 ==========

def check_service_installed(name):
    """检测服务是否已安装"""
    if name == "sillytavern":
        # 检查 SillyTavern 目录和 package.json 是否存在
        tavern_dir = "/opt/sillytavern"
        package_json = os.path.join(tavern_dir, "package.json")
        return os.path.exists(package_json)
    elif name == "astrbot":
        # 检查 AstrBot 容器是否存在
        ok, output = run_command("docker ps -a --format '{{.Names}}' | grep -w astrbot", quiet=True)
        return ok and "astrbot" in output
    elif name == "napcat":
        # 检查 NapCat 容器是否存在
        ok, output = run_command("docker ps -a --format '{{.Names}}' | grep -w napcat", quiet=True)
        return ok and "napcat" in output
    return False


@app.route("/api/services/installed")
@login_required
def api_services_installed():
    """检测各服务安装状态"""
    return jsonify({
        "napcat": check_service_installed("napcat"),
        "astrbot": check_service_installed("astrbot"),
        "sillytavern": check_service_installed("sillytavern")
    })


@app.route("/api/services/<name>/reinstall", methods=["POST"])
@login_required
def api_service_reinstall(name):
    """重装单个服务"""
    global install_status, install_generation
    valid_names = {"napcat", "astrbot", "sillytavern"}
    if name not in valid_names:
        return jsonify({"error": f"未知服务: {name}"}), 400
    
    install_generation += 1
    install_status = {
        "stage": "installing",
        "progress": 0,
        "message": f"重装 {name}...",
        "logs": [],
        "results": {},
        "current_stage": ""
    }
    
    def do_reinstall():
        global install_status
        try:
            if name == "sillytavern":
                install_status["message"] = "重装 SillyTavern..."
                log("开始重装 SillyTavern...")
                # 删除旧目录
                run_command("rm -rf /opt/sillytavern")
                install_status["progress"] = 20
                # 重新部署
                if deploy_sillytavern():
                    install_status["progress"] = 100
                    install_status["stage"] = "completed"
                    install_status["message"] = "SillyTavern 重装完成"
                    log("SillyTavern 重装完成")
                else:
                    raise Exception("SillyTavern 部署失败")
            elif name in ["astrbot", "napcat"]:
                install_status["message"] = "重装 AstrBot + NapCat..."
                log("开始重装 AstrBot + NapCat...")
                # 停止并删除容器
                run_command("docker stop astrbot napcat 2>/dev/null || true")
                run_command("docker rm astrbot napcat 2>/dev/null || true")
                install_status["progress"] = 20
                # 重新部署
                if deploy_astrbot():
                    install_status["progress"] = 100
                    install_status["stage"] = "completed"
                    install_status["message"] = "AstrBot + NapCat 重装完成"
                    log("AstrBot + NapCat 重装完成")
                else:
                    raise Exception("AstrBot 部署失败")
        except Exception as e:
            install_status["stage"] = "error"
            install_status["message"] = str(e)
            log(f"重装失败: {str(e)}")
    
    thread = threading.Thread(target=do_reinstall, daemon=True)
    thread.start()
    return jsonify({"success": True})


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


@app.route("/api/docker/mirror", methods=["POST"])
@login_required
def set_docker_mirror():
    """配置 Docker 镜像加速"""
    data = request.json or {}
    mirror = data.get("mirror", "1panel")
    daemon_json = "/etc/docker/daemon.json"
    try:
        if mirror == "direct":
            # 海外服务器：清除镜像加速，直连 Docker Hub
            if os.path.exists(daemon_json):
                os.remove(daemon_json)
            run_command("systemctl daemon-reload", quiet=True)
            run_command("systemctl restart docker", quiet=True)
            time.sleep(3)  # 等待 Docker 完全重启
            return jsonify({"success": True, "message": "已切换为直连 Docker Hub（无镜像加速）"})
        mirrors_map = {
            "1panel": ["https://docker.1panel.live"],
            "daocloud": ["https://docker.m.daocloud.io"],
            "aliyun": ["https://docker.mirrors.ustc.edu.cn", "https://hub-mirror.c.163.com"]
        }
        mirrors = mirrors_map.get(mirror, mirrors_map["1panel"])
        mirror_config = json.dumps({"registry-mirrors": mirrors}, indent=2)
        with open(daemon_json, "w") as f:
            f.write(mirror_config)
        run_command("systemctl daemon-reload", quiet=True)
        run_command("systemctl restart docker", quiet=True)
        time.sleep(3)  # 等待 Docker 完全重启
        return jsonify({"success": True, "message": f"已配置 {mirror} 镜像"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mirrors/set", methods=["POST"])
@login_required
def set_mirrors():
    """统一切换镜像（npm/git/docker）"""
    global runtime_mirrors
    data = request.json or {}
    mirror_type = data.get("type", "")  # npm, git, docker
    mirror_value = data.get("value", "")
    if mirror_type == "npm":
        npm_map = {
            "taobao": "https://registry.npmmirror.com",
            "tencent": "https://mirrors.cloud.tencent.com/npm/",
            "direct": ""
        }
        runtime_mirrors["npm_registry"] = npm_map.get(mirror_value, mirror_value)
        msg = f"npm 源已切换为: {runtime_mirrors['npm_registry'] or '官方源'}"
        log(msg)
        return jsonify({"success": True, "message": msg})
    elif mirror_type == "git":
        git_map = {
            "ghproxy": "https://gh.llkk.cc/",
            "gitclone": "https://gitclone.com/github.com/",
            "direct": ""
        }
        runtime_mirrors["git_proxy"] = git_map.get(mirror_value, mirror_value)
        msg = f"Git 代理已切换为: {runtime_mirrors['git_proxy'] or '直连 GitHub'}"
        log(msg)
        return jsonify({"success": True, "message": msg})
    elif mirror_type == "docker":
        # 复用已有的 Docker 镜像切换逻辑
        return set_docker_mirror_internal(mirror_value)
    return jsonify({"success": False, "error": "未知镜像类型"}), 400


def set_docker_mirror_internal(mirror):
    """Docker 镜像切换内部实现"""
    daemon_json = "/etc/docker/daemon.json"
    try:
        if mirror == "direct":
            if os.path.exists(daemon_json):
                os.remove(daemon_json)
            run_command("systemctl daemon-reload", quiet=True)
            run_command("systemctl restart docker", quiet=True)
            time.sleep(3)
            return jsonify({"success": True, "message": "已切换为直连 Docker Hub"})
        mirrors_map = {
            "1panel": ["https://docker.1panel.live"],
            "daocloud": ["https://docker.m.daocloud.io"],
            "aliyun": ["https://docker.mirrors.ustc.edu.cn", "https://hub-mirror.c.163.com"]
        }
        mirrors = mirrors_map.get(mirror, mirrors_map["1panel"])
        mirror_config = json.dumps({"registry-mirrors": mirrors}, indent=2)
        with open(daemon_json, "w") as f:
            f.write(mirror_config)
        run_command("systemctl daemon-reload", quiet=True)
        run_command("systemctl restart docker", quiet=True)
        time.sleep(3)
        return jsonify({"success": True, "message": f"已配置 {mirror} 镜像"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/install/retry-stage", methods=["POST"])
@login_required
def retry_current_stage():
    """仅重试当前失败的安装阶段，不重启整个安装流程"""
    global install_status, install_generation
    current_stage = install_status.get("current_stage", "")
    if not current_stage:
        return jsonify({"success": False, "error": "无可重试的阶段"}), 400

    install_generation += 1
    current_gen = install_generation
    # 保留之前的日志，不清空
    old_logs = list(install_status.get("logs", []))
    install_status["stage"] = "installing"
    install_status["logs"] = old_logs
    install_status["logs"].append(f"[{time.strftime('%H:%M:%S')}] === 重试阶段: {current_stage} ===")

    def do_retry():
        global install_status
        try:
            if current_stage == "docker_pull":
                install_status["message"] = "重试拉取 Docker 镜像..."
                install_status["current_stage"] = "docker_pull"
                astrbot_dir = "/opt/astrbot"
                pull_ok, _ = run_command_stream(
                    "docker compose -f astrbot.yml pull",
                    cwd=astrbot_dir, generation=current_gen
                )
                if not pull_ok:
                    pull_ok, _ = run_command_stream(
                        "docker-compose -f astrbot.yml pull",
                        cwd=astrbot_dir, generation=current_gen
                    )
                if not pull_ok:
                    raise Exception("Docker镜像拉取失败")
                # 继续完成后续步骤
                log("镜像拉取成功，继续启动容器...")
                install_status["message"] = "启动 Docker 容器..."
                success, _ = run_command("docker compose -f astrbot.yml up -d", cwd=astrbot_dir)
                if not success:
                    success, _ = run_command("docker-compose -f astrbot.yml up -d", cwd=astrbot_dir)
                if not success:
                    raise Exception("Docker容器启动失败")
                # 继续剩余安装流程
                _continue_install_after_docker(current_gen)

            elif current_stage == "git_clone":
                install_status["message"] = "重试克隆 SillyTavern..."
                install_status["current_stage"] = "git_clone"
                tavern_dir = "/opt/sillytavern"
                run_command(f"rm -rf {tavern_dir}")
                if not deploy_sillytavern():
                    raise Exception("SillyTavern部署失败")
                _finish_install(current_gen)

            elif current_stage == "npm_install":
                install_status["message"] = "重试安装 npm 依赖..."
                install_status["current_stage"] = "npm_install"
                tavern_dir = "/opt/sillytavern"
                # 清理旧 node_modules
                run_command("rm -rf node_modules", cwd=tavern_dir)
                run_command("npm cache clean --force", cwd=tavern_dir)
                npm_registry = runtime_mirrors.get("npm_registry", "")
                if npm_registry:
                    npm_cmd = f"npm install --no-audit --no-fund --registry={npm_registry}"
                    log(f"使用npm镜像: {npm_registry}")
                else:
                    npm_cmd = "npm install --no-audit --no-fund"
                success, _ = run_command_stream(npm_cmd, cwd=tavern_dir, generation=current_gen, timeout=900)
                node_modules = os.path.join(tavern_dir, "node_modules")
                if not success and os.path.exists(node_modules) and len(os.listdir(node_modules)) > 10:
                    log("npm 返回码非0，但 node_modules 已存在，视为成功")
                    success = True
                if not success:
                    raise Exception("npm依赖安装失败")
                # npm成功后继续剩余 SillyTavern 配置
                log("npm 依赖安装成功，继续配置...")
                _continue_sillytavern_config(tavern_dir)
                _finish_install(current_gen)
            else:
                raise Exception(f"不支持重试的阶段: {current_stage}")
        except Exception as e:
            install_status["stage"] = "error"
            install_status["message"] = str(e)
            log(f"重试失败: {str(e)}")

    thread = threading.Thread(target=do_retry, daemon=True)
    thread.start()
    return jsonify({"success": True, "stage": current_stage})


def _continue_sillytavern_config(tavern_dir):
    """SillyTavern npm安装后的配置步骤"""
    log("配置SillyTavern...")
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
enableUserAccounts: true
enableDiscreetLogin: false
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
        run_command("pm2 save", quiet=True)
        run_command("pm2 startup 2>/dev/null || true", quiet=True)
        time.sleep(5)
        alive, status_output = run_command("pm2 show sillytavern")
        if alive and "online" in status_output.lower():
            log(f"SillyTavern 已在端口 {config['tavern_port']} 成功启动")
            set_sillytavern_password(tavern_dir)
        else:
            log("SillyTavern 可能未正常启动，请检查 pm2 logs sillytavern")


def _continue_install_after_docker(generation):
    """Docker部署完成后，继续剩余安装步骤"""
    if generation != install_generation:
        return
    # 获取 NapCat token
    install_status["progress"] = 55
    install_status["message"] = "获取 NapCat 登录 Token..."
    log("等待NapCat容器初始化...")
    napcat_token = ""
    for _ in range(10):
        time.sleep(3)
        napcat_token = get_napcat_token()
        if napcat_token:
            log(f"NapCat WebUI Token: {napcat_token}")
            break
    config["napcat_token"] = napcat_token
    if config.get("install_plugins", False):
        install_status["progress"] = 60
        install_status["message"] = "安装 AstrBot 插件..."
        install_astrbot_plugins(ASTRBOT_PLUGINS)
    # 继续 SillyTavern
    if generation != install_generation:
        return
    if config.get("install_tavern", True):
        install_status["progress"] = 65
        install_status["message"] = "部署 SillyTavern..."
        if not deploy_sillytavern():
            raise Exception("SillyTavern部署失败")
    _finish_install(generation)


def _finish_install(generation):
    """完成安装收尾步骤"""
    if generation != install_generation:
        return
    install_status["progress"] = 92
    install_status["message"] = "配置防火墙..."
    configure_firewall()
    install_status["progress"] = 95
    install_status["message"] = "保存配置..."
    server_ip = ""
    ok, ip_out = run_command("hostname -I | awk '{print $1}'", quiet=True)
    if ok and ip_out.strip():
        server_ip = ip_out.strip()
    if not server_ip:
        server_ip = "你的服务器IP"
    config["server_ip"] = server_ip
    config["installed"] = True
    save_config()
    napcat_base = f"http://{server_ip}:{config['napcat_port']}"
    napcat_token = config.get("napcat_token", "")
    napcat_url = f"{napcat_base}/webui?token={napcat_token}" if napcat_token else napcat_base
    install_status["results"] = {
        "server_ip": server_ip,
        "napcat_url": napcat_url,
        "napcat_token": napcat_token,
        "astrbot_url": f"http://{server_ip}:{config['astrbot_port']}",
        "tavern_url": f"http://{server_ip}:{config['tavern_port']}",
        "tavern_username": config.get("tavern_username", "admin"),
        "tavern_password": config.get("tavern_password", "")
    }
    install_status["progress"] = 98
    install_status["message"] = "注册常驻服务..."
    log("注册夜鹭机管理面板为常驻服务...")
    run_command("pm2 delete yolushiki 2>/dev/null || true", quiet=True)
    yolushiki_app = os.path.join(CONFIG_DIR, "app.py")
    if os.path.exists(yolushiki_app):
        run_command(
            f'pm2 start {yolushiki_app} --name "yolushiki" --interpreter python3 -- --port 9999 --host 0.0.0.0',
            quiet=True
        )
        run_command("pm2 save", quiet=True)
    install_status["progress"] = 100
    install_status["message"] = "安装完成！"
    install_status["stage"] = "completed"
    install_status["current_stage"] = ""
    log("夜鹭机安装完成！")


@app.route("/api/install", methods=["POST"])
@login_required
def start_install():
    """开始安装"""
    global install_status
    global install_generation
    install_generation += 1
    current_gen = install_generation
    install_status = {
        "stage": "installing",
        "progress": 0,
        "message": "开始安装...",
        "logs": [],
        "results": {},
        "current_stage": ""
    }
    data = request.json or {}
    config["install_tavern"] = data.get("install_tavern", True)
    config["install_astrbot"] = data.get("install_astrbot", True)
    if "tavern_port" in data:
        config["tavern_port"] = int(data["tavern_port"])
    if "install_plugins" in data:
        config["install_plugins"] = data["install_plugins"]
    if "selected_plugins" in data:
        for i, plugin in enumerate(ASTRBOT_PLUGINS):
            plugin["selected"] = i in data["selected_plugins"]
    # 酒馆用户名密码
    if data.get("tavern_username"):
        config["tavern_username"] = data["tavern_username"]
    if data.get("tavern_password"):
        config["tavern_password"] = data["tavern_password"]
    save_config()
    thread = threading.Thread(target=do_install, args=(current_gen,), daemon=True)
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
