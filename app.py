#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夜鹭机 管理面板 (Phase 2)
"""

VERSION = "1.0.2"

import glob
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import threading
import time
import socket
import functools
import hashlib
import re
from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    session,
    redirect,
    url_for,
    send_file,
)

app = Flask(__name__)

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
    "server_ip": "",
    "secret_key": None,
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

# secret_key 持久化：首次生成后保存，重启不再刷新 session
if not config.get("secret_key"):
    config["secret_key"] = secrets.token_hex(32)
    save_config()
app.secret_key = config["secret_key"]

# 全局安装状态
install_status = {
    "stage": "waiting",  # waiting, installing, completed, error
    "progress": 0,
    "message": "",
    "logs": [],
    "results": {},
    "current_stage": "",  # docker_pull, git_clone, npm_install, 等
}
install_generation = 0  # 安装代数，重试时递增，旧线程检测到不匹配则退出
MAX_LOG_BYTES = 2 * 1024 * 1024  # 日志总内存上限 2MB
_log_bytes = 0  # 当前日志占用字节数（近似追踪）

# 服务状态缓存（减少 subprocess 调用）
_status_cache = {"data": None, "time": 0}
_STATUS_CACHE_TTL = 5  # 秒

# 系统信息缓存
_sysinfo_cache = {"data": None, "time": 0}
_SYSINFO_CACHE_TTL = 30  # 秒（IP/磁盘/内存不会频繁变化）

# 镜像/加速配置（运行时可切换）
runtime_mirrors = {
    "npm_registry": "",  # 空=官方源, 或 https://registry.npmmirror.com
    "git_proxy": "",  # 空=直连GitHub, 或 https://gh.llkk.cc/
}

# 插件列表（按分类排列，顺序决定前端渲染顺序）
ASTRBOT_PLUGINS = [
    # ── 必装 ──
    {
        "name": "日志拓展插件 (LogPlus)",
        "repo": "https://github.com/lxfight/astrbot_plugin_logplus.git",
        "description": "排错必备，详细日志记录",
        "category": "必装",
        "selected": True,
        "required": True,
    },
    # ── 机器人管理类 ──
    {
        "name": "人际关系管理",
        "repo": "https://github.com/Zhalslar/astrbot_plugin_relationship.git",
        "description": "管理好友关系",
        "category": "机器人管理类",
        "selected": True,
    },
    {
        "name": "QQ群管",
        "repo": "https://github.com/Zhalslar/astrbot_plugin_qqadmin.git",
        "description": "QQ群管理功能",
        "category": "机器人管理类",
        "selected": True,
    },
    {
        "name": "使用状况查看",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_hangout.git",
        "description": "查看机器人使用状况统计",
        "category": "机器人管理类",
        "selected": True,
    },
    # ── 聊天优化类 ──
    {
        "name": "Conversa 主动回复",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_Conversa.git",
        "description": "LLM主动问候回复，适合机器人好友多的情况",
        "category": "聊天优化类",
        "selected": True,
        "conflict_note": "⚠️ 可能与「群聊主动回复Plus」冲突，建议二选一",
    },
    {
        "name": "群聊主动回复Plus",
        "repo": "https://github.com/Him666233/astrbot_plugin_group_chat_plus.git",
        "description": "功能强大的主动回复，token消耗高，设置复杂但非常强大，推荐等上手之后安装",
        "category": "聊天优化类",
        "selected": False,
        "conflict_note": "⚠️ 可能与「Conversa 主动回复」冲突，建议二选一",
    },
    {
        "name": "机器人防尬聊弱黑名单",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_Random_Reply.git",
        "description": "群聊里防止多个机器人无限聊天",
        "category": "聊天优化类",
        "selected": True,
    },
    {
        "name": "用户画像记忆 (SoulMap)",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_soulmap.git",
        "description": "用户画像记忆辅助系统",
        "category": "聊天优化类",
        "selected": True,
    },
    {
        "name": "好感度Pro（鸭版）",
        "repo": "https://github.com/Luna-channel/astrbot_plugin_favourpro.git",
        "description": "好感度系统",
        "category": "聊天优化类",
        "selected": True,
    },
    # ── 其他功能类 ──
    {
        "name": "QQ群总结分析",
        "repo": "https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis.git",
        "description": "QQ群聊天总结分析",
        "category": "其他功能类",
        "selected": False,
    },
    {
        "name": "表情包发送",
        "repo": "https://github.com/LunarMeal/astrbot_plugin_memes.git",
        "description": "发送表情包（类似插件很多，也可在插件市场自选）",
        "category": "其他功能类",
        "selected": False,
    },
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
    """添加日志（按总字节数限制，防止内存泄漏）"""
    global _log_bytes
    timestamp = time.strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    entry_size = len(log_entry.encode("utf-8", errors="replace"))
    install_status["logs"].append(log_entry)
    _log_bytes += entry_size
    # 超过上限时砍掉前半部分
    if _log_bytes > MAX_LOG_BYTES:
        half = len(install_status["logs"]) // 2
        removed = install_status["logs"][:half]
        install_status["logs"] = install_status["logs"][half:]
        _log_bytes -= sum(len(e.encode("utf-8", errors="replace")) for e in removed)
    print(log_entry)


def run_command(cmd, cwd=None, quiet=False):
    """执行命令并返回结果"""
    if not quiet:
        log(f"执行: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600
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
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # 只保留最后100行输出，避免内存积累
        output_lines = []
        max_output = 100
        try:
            for line in process.stdout:
                # 检查是否被新安装覆盖，是则杀掉子进程退出
                if generation is not None and generation != install_generation:
                    process.kill()
                    process.wait()
                    return False, "已被新安装取消"
                line = line.strip()
                if line:
                    output_lines.append(line)
                    if len(output_lines) > max_output:
                        output_lines = output_lines[-max_output:]
                    # 每行都立即写入日志，前端即刻可见
                    log(line[:120])
        finally:
            process.stdout.close()
        process.wait(timeout=timeout)
        if process.returncode != 0:
            log(f"命令返回码: {process.returncode}")
            return False, "\n".join(output_lines)
        return True, "\n".join(output_lines)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        log("命令超时")
        return False, "命令执行超时"
    except Exception as e:
        log(f"执行异常: {str(e)}")
        return False, str(e)


# ========== 服务管理 ==========


def check_port_health(port, timeout=2):
    """检查本地端口是否可达（TCP连接测试）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def get_service_status(name):
    """获取单个服务状态"""
    port_map = {
        "napcat": config.get("napcat_port", 6099),
        "astrbot": config.get("astrbot_port", 6185),
        "sillytavern": config.get("tavern_port", 18888),
    }
    if name == "sillytavern":
        ok, output = run_command("pm2 jlist", quiet=True)
        if ok:
            try:
                processes = json.loads(output)
                for p in processes:
                    if p.get("name") == "sillytavern":
                        result = {
                            "name": "sillytavern",
                            "status": p.get("pm2_env", {}).get("status", "unknown"),
                            "pid": p.get("pid", 0),
                            "uptime": p.get("pm2_env", {}).get("pm_uptime", 0),
                            "restarts": p.get("pm2_env", {}).get("restart_time", 0),
                            "memory": p.get("monit", {}).get("memory", 0),
                            "cpu": p.get("monit", {}).get("cpu", 0),
                        }
                        if result["status"] == "online":
                            result["health_ok"] = check_port_health(
                                port_map["sillytavern"]
                            )
                        else:
                            result["health_ok"] = False
                        return result
            except (json.JSONDecodeError, KeyError):
                pass
        return {
            "name": "sillytavern",
            "status": "stopped",
            "pid": 0,
            "uptime": 0,
            "restarts": 0,
            "memory": 0,
            "cpu": 0,
            "health_ok": False,
        }
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
                quiet=True,
            )
            if ok2:
                try:
                    parts = stats_out.strip().strip("'").split("|||")
                    mem_str = parts[0].split("/")[0].strip()
                    if "GiB" in mem_str:
                        mem = int(
                            float(mem_str.replace("GiB", "").strip())
                            * 1024
                            * 1024
                            * 1024
                        )
                    elif "MiB" in mem_str:
                        mem = int(
                            float(mem_str.replace("MiB", "").strip()) * 1024 * 1024
                        )
                    elif "KiB" in mem_str:
                        mem = int(float(mem_str.replace("KiB", "").strip()) * 1024)
                    cpu = float(parts[1].replace("%", "").strip())
                except (IndexError, ValueError):
                    pass
        health_ok = (
            check_port_health(port_map.get(name, 0)) if status == "running" else False
        )
        return {
            "name": name,
            "status": status,
            "pid": 0,
            "uptime": 0,
            "restarts": 0,
            "memory": mem,
            "cpu": cpu,
            "health_ok": health_ok,
        }


def check_napcat_error():
    """检查 NapCat 最近日志中是否有连接错误"""
    ok, output = run_command("docker logs napcat --tail 30 2>&1", quiet=True)
    if not ok or not output:
        return ""
    error_keywords = [
        "error",
        "failed",
        "断开",
        "disconnect",
        "ECONNREFUSED",
        "timeout",
        "登录失败",
    ]
    lines = output.strip().split("\n")
    for line in reversed(lines):
        lower = line.lower()
        if any(kw.lower() in lower for kw in error_keywords):
            return line.strip()[:120]
    return ""


def get_all_services_status():
    """获取所有服务状态（带缓存，减少 subprocess 调用）"""
    now = time.time()
    if (
        _status_cache["data"] is not None
        and (now - _status_cache["time"]) < _STATUS_CACHE_TTL
    ):
        return _status_cache["data"]
    statuses = {
        "napcat": get_service_status("napcat"),
        "astrbot": get_service_status("astrbot"),
        "sillytavern": get_service_status("sillytavern"),
    }
    # M5: 检查 NapCat 日志中的连接错误
    if statuses["napcat"].get("status") == "running":
        napcat_err = check_napcat_error()
        if napcat_err:
            statuses["napcat"]["last_error"] = napcat_err
            statuses["napcat"]["error_hint"] = (
                "检测到连接异常，建议重启NapCat后重新扫码登录"
            )
    _status_cache["data"] = statuses
    _status_cache["time"] = now
    return statuses


def service_action(name, action):
    """对服务执行操作"""
    if name == "sillytavern":
        if action == "restart":
            ok, out = run_command("pm2 restart sillytavern", quiet=True)
            run_command("pm2 save", quiet=True)
        elif action == "stop":
            ok, out = run_command("pm2 stop sillytavern", quiet=True)
            run_command("pm2 save", quiet=True)
        elif action == "start":
            # 先尝试启动已有的 stopped 进程，避免 pm2 start server.js 创建重复进程
            ok, out = run_command("pm2 start sillytavern", quiet=True)
            if not ok:
                # 进程不存在，重新创建
                run_command("pm2 delete sillytavern 2>/dev/null || true", quiet=True)
                ok, out = run_command(
                    'pm2 start server.js --name "sillytavern" --max-memory-restart 300M',
                    cwd="/opt/sillytavern",
                    quiet=True,
                )
            run_command("pm2 save", quiet=True)
        else:
            return False, "未知操作"
        _status_cache["data"] = None  # 清除缓存，操作后立即刷新
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
        _status_cache["data"] = None  # 清除缓存，操作后立即刷新
        return ok, out


def get_service_logs(name, lines=50):
    """获取服务日志"""
    if name == "sillytavern":
        ok, output = run_command(
            f"pm2 logs sillytavern --nostream --lines {lines}", quiet=True
        )
    else:
        # docker logs 输出到 stderr，必须用 2>&1 合并
        ok, output = run_command(f"docker logs --tail {lines} {name} 2>&1", quiet=True)
    return output if ok else "无法获取日志"


def fetch_public_ip():
    """从多个源获取公网IP，成功后自动保存到配置（更可靠）"""
    # 多个公网IP查询源，按优先级排列
    ip_sources = [
        "curl -s --connect-timeout 5 --max-time 8 ifconfig.me",
        "curl -s --connect-timeout 5 --max-time 8 ip.sb",
        "curl -s --connect-timeout 5 --max-time 8 ipinfo.io/ip",
        "curl -s --connect-timeout 5 --max-time 8 icanhazip.com",
        "curl -s --connect-timeout 5 --max-time 8 api.ipify.org",
        "curl -s --connect-timeout 5 --max-time 8 checkip.amazonaws.com",
    ]
    for cmd in ip_sources:
        ok, out = run_command(cmd, quiet=True)
        if ok and out:
            ip = out.strip()
            # 验证是否为有效的IP格式（简单校验：包含点且不含非法字符）
            if (
                ip
                and "." in ip
                and len(ip) <= 15
                and all(c.isdigit() or c == "." for c in ip)
            ):
                # 成功获取，保存到配置避免重复请求
                config["server_ip"] = ip
                save_config()
                return ip
    return ""


def get_system_info():
    """获取系统信息（带缓存，减少外部请求和 subprocess 调用）"""
    now = time.time()
    if (
        _sysinfo_cache["data"] is not None
        and (now - _sysinfo_cache["time"]) < _SYSINFO_CACHE_TTL
    ):
        return _sysinfo_cache["data"]
    info = {}
    # IP（优先用已保存的，避免反复 curl 外网）
    saved_ip = config.get("server_ip", "")
    if saved_ip and saved_ip != "你的服务器IP":
        info["ip"] = saved_ip
    else:
        # 使用多源获取公网IP
        public_ip = fetch_public_ip()
        info["ip"] = public_ip if public_ip else "未知"
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
    _sysinfo_cache["data"] = info
    _sysinfo_cache["time"] = now
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
        "docker exec napcat cat /app/napcat/config/webui.json", quiet=True
    )
    if ok:
        try:
            data = json.loads(output.strip())
            return data.get("token", "")
        except (json.JSONDecodeError, KeyError):
            pass
    return ""


# ========== 系统兼容性 ==========

_cached_pkg_manager = None


def detect_pkg_manager():
    """检测系统包管理器，支持多发行版（结果缓存）"""
    global _cached_pkg_manager
    if _cached_pkg_manager is not None:
        return _cached_pkg_manager
    for cmd in ["apt-get", "dnf", "yum", "pacman", "zypper"]:
        ok, _ = run_command(f"which {cmd}", quiet=True)
        if ok:
            _cached_pkg_manager = cmd
            return cmd
    _cached_pkg_manager = "apt-get"
    return "apt-get"


def pkg_update():
    """通用包列表更新"""
    pm = detect_pkg_manager()
    ok = False
    if pm == "apt-get":
        ok, _ = run_command("apt-get update -qq")
    elif pm in ("dnf", "yum"):
        ok, _ = run_command(f"{pm} makecache -q")
    elif pm == "pacman":
        ok, _ = run_command("pacman -Sy --noconfirm")
    elif pm == "zypper":
        ok, _ = run_command("zypper refresh -q")
    if not ok:
        log(
            "⚠ 系统包列表更新失败，可能原因：网络不通或软件源不可用。后续安装可能受影响，但会继续尝试"
        )


def pkg_install(packages):
    """通用包安装"""
    pm = detect_pkg_manager()
    if pm == "apt-get":
        return run_command(f"apt-get install -y -qq {packages}")
    elif pm in ("dnf", "yum"):
        return run_command(f"{pm} install -y -q {packages}")
    elif pm == "pacman":
        return run_command(f"pacman -S --noconfirm --needed {packages}")
    elif pm == "zypper":
        return run_command(f"zypper install -y -n {packages}")
    return False, "未知包管理器"


# ========== 部署函数（保留 Phase 1 逻辑） ==========


def install_docker():
    """安装Docker"""
    log("检查Docker...")
    success, output = run_command("docker --version", quiet=True)
    if success:
        log(f"Docker已安装: {output.strip()}")
        # 确保 Docker 已启动且开机自启（预装的 Docker 可能未 start/enable）
        run_command("systemctl start docker", quiet=True)
        run_command("systemctl enable docker", quiet=True)
        # 确保镜像加速已配置（预装的 Docker 可能没配加速）
        configure_docker_mirrors()
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
    mirror_config = """{
  "registry-mirrors": [
    "https://docker.xuanyuan.me",
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ]
}"""
    try:
        with open(daemon_json, "w") as f:
            f.write(mirror_config)
        run_command("systemctl daemon-reload", quiet=True)
        run_command("systemctl restart docker", quiet=True)
        log("Docker镜像加速配置完成")
    except Exception as e:
        log(f"配置镜像加速失败: {e}")


# 国内 Docker Hub 代理前缀（按稳定性排序）
DOCKER_PROXY_PREFIXES = [
    "dockerpull.org",
    "docker.1ms.run",
    "docker.xuanyuan.me",
    "docker.m.daocloud.io",
    "hub.rat.dev",
    "docker.awsl9527.cn",
]


def pull_single_image_via_proxy(image, generation=None):
    """通过国内代理前缀拉取单个 Docker 镜像，成功后 retag 为原名
    返回 True/False
    """
    # 先检查本地是否已有
    ok, _ = run_command(f"docker image inspect {image}", quiet=True)
    if ok:
        log(f"镜像已存在本地: {image}")
        return True
    for proxy in DOCKER_PROXY_PREFIXES:
        proxy_image = f"{proxy}/{image}"
        log(f"尝试代理拉取: {proxy_image}")
        install_status["message"] = f"通过 {proxy} 拉取 {image}..."
        # 用 timeout 命令包裹，防止代理卡死时 stdout 永久阻塞
        ok, _ = run_command_stream(
            f"timeout 300 docker pull {proxy_image}",
            timeout=360,
            generation=generation,
        )
        if ok:
            # retag 为原始镜像名
            run_command(f"docker tag {proxy_image} {image}", quiet=True)
            run_command(f"docker rmi {proxy_image}", quiet=True)
            log(f"✓ 镜像拉取成功: {image}（来源: {proxy}）")
            return True
        else:
            log(f"✗ {proxy} 拉取失败，尝试下一个...")
    return False


def pull_all_images_via_proxy(generation=None):
    """通过代理前缀拉取所有需要的 Docker 镜像
    返回 True 表示全部成功
    """
    images = ["soulter/astrbot:latest", "mlikiowa/napcat-docker:latest"]
    for img in images:
        if not pull_single_image_via_proxy(img, generation=generation):
            log(f"❌ 所有代理源均无法拉取 {img}")
            return False
    return True


def check_and_fix_dns():
    """检查 DNS 解析是否正常（仅测试国内可达的域名，不测试被墙的域名）
    只在 DNS 解析本身失败时才修复，网络连接失败不算 DNS 问题。
    """
    # 只测试国内一定能解析的域名（不包含 Docker Hub 等被墙域名）
    test_domains = [
        "registry.npmmirror.com",
        "mirrors.aliyun.com",
        "www.baidu.com",
    ]
    resolve_fail_count = 0
    for domain in test_domains:
        try:
            ip = socket.gethostbyname(domain)
            log(f"DNS 解析: {domain} → {ip}")
        except socket.gaierror as e:
            log(f"⚠ DNS 解析失败: {domain} → {e}")
            resolve_fail_count += 1

    # 只有多数域名解析失败才判定 DNS 有问题（单个失败可能是临时问题）
    if resolve_fail_count >= 2:
        log(f"DNS 解析异常（{resolve_fail_count}/{len(test_domains)} 失败），尝试修复...")
        log("添加 DNS: 阿里(223.5.5.5) + 腾讯(119.29.29.29)...")
        # 修复方式：在 resolv.conf 前面插入国内 DNS，保留原有配置
        try:
            resolv = "/etc/resolv.conf"
            existing = ""
            if os.path.exists(resolv) and not os.path.islink(resolv):
                with open(resolv, "r") as f:
                    existing = f.read()
            # 避免重复添加
            if "223.5.5.5" not in existing:
                new_content = "nameserver 223.5.5.5\nnameserver 119.29.29.29\n" + existing
                if not os.path.islink(resolv):
                    with open(resolv, "w") as f:
                        f.write(new_content)
                    log("resolv.conf 已更新（前插国内 DNS，保留原有配置）")
        except Exception as e:
            log(f"修改 resolv.conf 失败: {e}")
        # 对 systemd-resolved 系统，添加 DNS 而非覆盖
        resolved_conf = "/etc/systemd/resolved.conf"
        if os.path.exists(resolved_conf):
            try:
                with open(resolved_conf, "r") as f:
                    content = f.read()
                if "223.5.5.5" not in content:
                    # 替换或追加 DNS 行
                    if "DNS=" in content:
                        content = re.sub(
                            r"^DNS=.*$",
                            "DNS=223.5.5.5 119.29.29.29",
                            content,
                            flags=re.MULTILINE,
                        )
                    else:
                        content = content.rstrip() + "\nDNS=223.5.5.5 119.29.29.29\n"
                    with open(resolved_conf, "w") as f:
                        f.write(content)
                    run_command("systemctl restart systemd-resolved", quiet=True)
                    log("systemd-resolved DNS 已更新")
            except Exception as e:
                log(f"修改 systemd-resolved 失败: {e}")
        # 验证
        time.sleep(2)
        for domain in test_domains:
            try:
                ip = socket.gethostbyname(domain)
                log(f"修复后验证: {domain} → {ip} ✓")
            except Exception as e:
                log(f"修复后验证失败: {domain} → {e}")
        return False
    log("DNS 解析正常")
    return True


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
    # NodeSource 脚本支持 Debian/Ubuntu/RHEL/CentOS/Fedora
    run_command("curl -fsSL https://deb.nodesource.com/setup_20.x | bash -")
    pm = detect_pkg_manager()
    if pm == "apt-get":
        success, _ = run_command("apt-get install -y nodejs")
    elif pm in ("dnf", "yum"):
        success, _ = run_command(f"{pm} install -y nodejs")
    elif pm == "pacman":
        success, _ = run_command("pacman -S --noconfirm nodejs npm")
    else:
        success, _ = run_command("apt-get install -y nodejs")
    return success


def install_pm2():
    """安装PM2"""
    log("检查PM2...")
    success, output = run_command("pm2 --version", quiet=True)
    if success:
        log(f"PM2已安装: {output.strip()}")
        _setup_pm2_logrotate()
        return True
    log("PM2未安装，开始安装...")
    npm_registry = runtime_mirrors.get("npm_registry", "")
    if npm_registry:
        success, _ = run_command(f"npm install -g pm2 --registry={npm_registry}")
    else:
        success, _ = run_command("npm install -g pm2")
    if success:
        _setup_pm2_logrotate()
    return success


def _setup_pm2_logrotate():
    """配置 PM2 日志轮转，防止日志无限增长"""
    ok, _ = run_command("pm2 describe pm2-logrotate", quiet=True)
    if ok:
        return  # 已安装
    log("配置 PM2 日志轮转...")
    run_command("pm2 install pm2-logrotate", quiet=True)
    # 单个日志文件最大 10MB，保留 3 个旧文件
    run_command("pm2 set pm2-logrotate:max_size 10M", quiet=True)
    run_command("pm2 set pm2-logrotate:retain 3", quiet=True)
    run_command("pm2 set pm2-logrotate:compress true", quiet=True)


def generate_tavern_config_yaml():
    """生成 SillyTavern config.yaml 内容（统一维护，避免多处重复）"""
    # 关键：必须包含 securityOverride: true，否则 listen:true + whitelistMode:false 时
    # SillyTavern 的 logSecurityAlert() 会直接 process.exit(1) 杀死进程
    return f"""dataRoot: ./data
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
port: {config["tavern_port"]}
whitelistMode: false
basicAuthMode: false
enableUserAccounts: true
enableDiscreetLogin: false
sessionTimeout: 86400
securityOverride: true
disableCsrfProtection: false
"""


def generate_astrbot_yml():
    """生成 astrbot.yml 内容（统一维护，避免多处重复）"""
    napcat_port = config.get("napcat_port", 6099)
    astrbot_port = config.get("astrbot_port", 6185)
    return f'''services:
  napcat:
    environment:
      - MODE=astrbot
    ports:
      - {napcat_port}:6099
    container_name: napcat
    restart: always
    image: mlikiowa/napcat-docker:latest
    volumes:
      - ./data:/AstrBot/data
      - ./napcat/config:/app/napcat/config
      - ./ntqq:/app/.config/QQ
    networks:
      - astrbot_network
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://127.0.0.1:6099/webui || exit 1"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
  astrbot:
    environment:
      - TZ=Asia/Shanghai
    image: soulter/astrbot:latest
    container_name: astrbot
    restart: always
    ports:
      - "{astrbot_port}:6185"
    volumes:
      - ./data:/AstrBot/data
    networks:
      - astrbot_network
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://127.0.0.1:6185/ || exit 1"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    deploy:
      resources:
        limits:
          memory: 512M
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
networks:
  astrbot_network:
    driver: bridge
'''


def deploy_astrbot():
    """部署AstrBot + NapCat"""
    log("部署AstrBot + NapCat...")
    astrbot_dir = "/opt/astrbot"
    run_command(f"mkdir -p {astrbot_dir}")
    yml_content = generate_astrbot_yml()
    with open(f"{astrbot_dir}/astrbot.yml", "w") as f:
        f.write(yml_content)
    log("astrbot.yml 已生成（含镜像加速）")
    # 确保 NapCat 挂载目录存在
    for sub in ["napcat/config", "ntqq", "data"]:
        os.makedirs(os.path.join(astrbot_dir, sub), exist_ok=True)
    # 拉取镜像：优先通过国内代理前缀逐个拉取，失败再 fallback 到 compose pull
    log("拉取Docker镜像...")
    install_status["message"] = "拉取 Docker 镜像中..."
    install_status["current_stage"] = "docker_pull"
    pull_ok = pull_all_images_via_proxy(generation=install_generation)
    if not pull_ok:
        log("代理拉取未全部成功，尝试 docker compose pull（走 daemon 镜像加速）...")
        install_status["message"] = "通过 docker compose 拉取镜像..."
        pull_ok, _ = run_command_stream(
            "docker compose -f astrbot.yml pull",
            cwd=astrbot_dir,
            generation=install_generation,
        )
        if not pull_ok:
            pull_ok, _ = run_command_stream(
                "docker-compose -f astrbot.yml pull",
                cwd=astrbot_dir,
                generation=install_generation,
            )
    if not pull_ok:
        log(
            "❌ Docker镜像拉取失败。可能原因：网络连接不稳定或镜像源不可用。建议操作：点击安装页面的'镜像加速'按钮更换加速源后重试"
        )
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
            generation=install_generation,
        )
        if not success and git_proxy:
            log("代理克隆失败，尝试直接连接 GitHub...")
            run_command(f"rm -rf {tavern_dir}")
            success, _ = run_command_stream(
                f"git clone --depth 1 --progress https://github.com/SillyTavern/SillyTavern.git {tavern_dir}",
                generation=install_generation,
            )
        if not success:
            log(
                "❌ SillyTavern 仓库克隆失败。可能原因：服务器无法访问 GitHub。建议操作：点击‘镜像加速’添加 Git 代理后重试"
            )
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
        success, output = run_command_stream(
            npm_cmd, cwd=tavern_dir, generation=install_generation, timeout=900
        )
        # 即使返回码非0，也检查 node_modules 是否实际存在
        node_modules = os.path.join(tavern_dir, "node_modules")
        if success:
            install_success = True
            break
        elif os.path.exists(node_modules) and len(os.listdir(node_modules)) > 10:
            log(
                "npm 返回码非0，但 node_modules 已存在（可能是 warning 导致），视为成功"
            )
            install_success = True
            break
    if not install_success:
        log(
            "❌ npm依赖安装失败（已重试3次）。可能原因：网络不稳定或 npm 源不可用。建议操作：点击‘镜像加速’设置 npm 镜像源后重试"
        )
        return False
    log("配置SillyTavern...")
    config_content = generate_tavern_config_yaml()
    config_path = os.path.join(tavern_dir, "config.yaml")
    with open(config_path, "w") as f:
        f.write(config_content)
    log(f"config.yaml 已写入（端口:{config['tavern_port']}, securityOverride:true）")
    log("使用PM2启动SillyTavern...")
    run_command("pm2 delete sillytavern 2>/dev/null || true")
    success, _ = run_command(
        'pm2 start server.js --name "sillytavern" --max-memory-restart 300M',
        cwd=tavern_dir,
    )
    if success:
        run_command("pm2 save", quiet=True)
        # 配置 PM2 开机自启（记录结果，失败时用户可手动执行）
        startup_ok, startup_out = run_command("pm2 startup", quiet=True)
        if startup_ok:
            log("PM2 开机自启已配置")
        else:
            log(
                "PM2 开机自启配置可能未成功（非root用户请手动执行 pm2 startup 输出的命令）"
            )
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
    storage_dir = os.path.join(tavern_dir, "data", "_storage")

    # 确保存储目录存在
    os.makedirs(storage_dir, exist_ok=True)

    # 获取用户设置的用户名和密码
    username = config.get("tavern_username", "admin")
    password = config.get("tavern_password", "")
    if not password:
        password = secrets.token_urlsafe(12)
        log(f"未设置密码，自动生成: {password}")

    # 短暂等待看 SillyTavern 是否自动创建了 default-user（最多10秒）
    target_file = None
    for i in range(10):
        user_files = glob.glob(os.path.join(storage_dir, "*.json"))
        for f in user_files:
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    if (
                        isinstance(data.get("key"), str)
                        and "default-user" in data["key"]
                    ):
                        target_file = f
                        break
            except:
                continue
        if target_file:
            log(f"找到已有用户文件: {os.path.basename(target_file)}")
            break
        time.sleep(1)

    # 生成 salt 和 hash（与 SillyTavern 的 crypto.scrypt 兼容，使用 hex 编码）
    salt = os.urandom(16).hex()
    password_hash = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"), n=16384, r=8, p=1, dklen=64
    )
    password_hash_hex = password_hash.hex()

    try:
        if target_file:
            # 修改已有文件
            with open(target_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            data["value"]["password"] = password_hash_hex
            data["value"]["salt"] = salt
            data["value"]["name"] = username
            data["value"]["admin"] = True
            data["value"]["enabled"] = True
        else:
            # SillyTavern 未自动创建用户文件，手动创建
            log("SillyTavern 未自动创建用户文件，手动创建...")
            target_file = os.path.join(storage_dir, "user-default-user.json")
            data = {
                "key": "user:default-user",
                "value": {
                    "handle": "default-user",
                    "name": username,
                    "created": int(time.time() * 1000),
                    "password": password_hash_hex,
                    "salt": salt,
                    "admin": True,
                    "enabled": True,
                    "avatar": None,
                    "block": [],
                },
            }

        with open(target_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

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
                f"git clone {plugin['repo']} {plugin_name}", cwd=plugins_dir
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
            # 确保 Docker 开机自启（预装的 Docker 可能未 enable）
            run_command("systemctl enable docker", quiet=True)
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

        # ========== DNS 健康检查 ==========
        install_status["progress"] = 4
        install_status["message"] = "检查网络环境..."
        install_status["current_stage"] = "dns_check"
        log("===== DNS 健康检查 =====")
        check_and_fix_dns()

        if cancelled():
            return

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
            install_status["current_stage"] = "dep_update"
            pkg_update()
        else:
            log("所有依赖已就绪，跳过安装")

        if cancelled():
            return

        if need_docker and not has_docker:
            install_status["progress"] = 8
            install_status["message"] = "安装 Docker..."
            install_status["current_stage"] = "dep_docker"
            if not install_docker():
                raise Exception(
                    "Docker安装失败。可能原因：网络无法访问 Docker 官方服务器。请检查服务器网络连接"
                )

        if cancelled():
            return

        if need_tavern and not has_nodejs:
            install_status["progress"] = 15
            install_status["message"] = "安装 Node.js..."
            install_status["current_stage"] = "dep_nodejs"
            if not install_nodejs():
                raise Exception(
                    "Node.js安装失败。可能原因：网络无法访问 NodeSource 服务器。请检查网络连接"
                )

        if cancelled():
            return

        if need_tavern and not has_pm2:
            install_status["progress"] = 20
            install_status["message"] = "安装 PM2..."
            install_status["current_stage"] = "dep_pm2"
            if not install_pm2():
                raise Exception(
                    "PM2安装失败。可能原因：NPM 无法访问。建议设置 npm 镜像加速后重试"
                )

        if cancelled():
            return

        # ========== 阶段3：按顺序部署服务 ==========
        log("===== 阶段3：部署服务 =====")
        napcat_token = ""

        if need_docker:
            install_status["progress"] = 25
            install_status["message"] = "部署 AstrBot + NapCat..."
            if not deploy_astrbot():
                raise Exception(
                    "AstrBot部署失败。可能原因：Docker镜像拉取或容器启动失败。建议更换 Docker 镜像源后重试"
                )

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
                log(
                    "未能自动获取NapCat Token，请手动查看: cat /opt/astrbot/napcat/config/webui.json"
                )
            config["napcat_token"] = napcat_token

            if config.get("install_plugins", False):
                install_status["progress"] = 60
                install_status["message"] = "安装 AstrBot 插件..."
                install_astrbot_plugins(ASTRBOT_PLUGINS)
        else:
            log("跳过 AstrBot 安装")

        if cancelled():
            return

        if need_tavern:
            install_status["progress"] = 65
            install_status["message"] = "部署 SillyTavern..."
            if not deploy_sillytavern():
                raise Exception(
                    "SillyTavern部署失败。可能原因：GitHub克隆或npm依赖安装失败。建议设置镜像加速后重试"
                )
        else:
            log("跳过 SillyTavern 安装")

        if cancelled():
            return

        install_status["progress"] = 92
        install_status["message"] = "配置防火墙..."
        configure_firewall()

        # ========== 阶段4：收尾 ==========
        install_status["progress"] = 95
        install_status["message"] = "保存配置..."

        # 获取服务器公网 IP（使用多源获取函数）
        log("获取服务器公网IP...")
        server_ip = fetch_public_ip()
        if not server_ip:
            # 所有公网源都失败，使用内网IP作为最后备选
            ok, ip_out = run_command("hostname -I | awk '{print $1}'", quiet=True)
            if ok and ip_out.strip():
                server_ip = ip_out.strip()
                log(f"公网IP获取失败，使用内网IP: {server_ip}")
        if not server_ip:
            server_ip = "你的服务器IP"
            log("⚠ 无法获取IP地址，请在管理面板手动设置")
        else:
            log(f"服务器IP: {server_ip}")
        config["server_ip"] = server_ip
        config["installed"] = True
        save_config()

        # 构造 NapCat URL（带 token 参数可直接登录）
        napcat_base = f"http://{server_ip}:{config['napcat_port']}"
        napcat_token = config.get("napcat_token", "")
        napcat_url = (
            f"{napcat_base}/webui?token={napcat_token}" if napcat_token else napcat_base
        )

        install_status["results"] = {
            "server_ip": server_ip,
            "napcat_url": napcat_url,
            "napcat_token": napcat_token,
            "astrbot_url": f"http://{server_ip}:{config['astrbot_port']}",
            "tavern_url": f"http://{server_ip}:{config['tavern_port']}",
            "tavern_username": config.get("tavern_username", "admin"),
            "tavern_password": config.get("tavern_password", ""),
        }

        # 将夜鹭机管理面板自身注册为 PM2 常驻服务
        install_status["progress"] = 98
        install_status["message"] = "注册常驻服务..."
        log("注册夜鹭机管理面板为常驻服务...")
        already_in_pm2, _ = run_command(
            "pm2 describe yolushiki 2>/dev/null", quiet=True
        )
        if already_in_pm2:
            log("夜鹭机已在 PM2 中运行，跳过重新注册")
            run_command("pm2 save", quiet=True)
        else:
            yolushiki_app = os.path.join(CONFIG_DIR, "app.py")
            if os.path.exists(yolushiki_app):
                run_command(
                    f'pm2 start {yolushiki_app} --name "yolushiki" --interpreter python3 -- --port 9999 --host 0.0.0.0',
                    quiet=True,
                )
                run_command("pm2 save", quiet=True)
        # 确保 PM2 开机自启（无论是否安装了酒馆）
        startup_ok, _ = run_command("pm2 startup", quiet=True)
        if startup_ok:
            log("PM2 开机自启已配置")
        else:
            log(
                "PM2 开机自启配置可能未成功（非root用户请手动执行 pm2 startup 输出的命令）"
            )

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
    return jsonify(
        {
            "token_set": bool(config.get("token")),
            "authenticated": bool(session.get("authenticated")),
            "installed": bool(config.get("installed")),
        }
    )


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
    return jsonify({"success": True, "message": f"Token已保存至 {CONFIG_FILE}"})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Token 登录"""
    data = request.json or {}
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "请输入Token"}), 400
    if token != config.get("token"):
        return jsonify(
            {
                "error": "Token错误。如果忘记了Token，请在服务器终端运行：cat /opt/yolushiki/config.json"
            }
        ), 401
    session["authenticated"] = True
    session.permanent = True
    return jsonify({"success": True})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """退出登录（仅清除会话，不关闭服务器）"""
    session.clear()
    return jsonify({"success": True})


@app.route("/api/system/shutdown", methods=["POST"])
@login_required
def system_shutdown():
    """关闭夜鹭机管理面板（独立于logout）"""
    session.clear()

    def shutdown_server():
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=shutdown_server, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/system/uninstall", methods=["POST"])
@login_required
def system_uninstall():
    """卸载/清理夜鹭机"""
    try:
        data = request.json or {}
        remove_containers = data.get("remove_containers", True)
        remove_images = data.get("remove_images", False)
        remove_data = data.get("remove_data", False)
        remove_sillytavern = data.get("remove_sillytavern", True)
        remove_dependencies = data.get("remove_dependencies", False)
        results = []
        if remove_containers:
            yml = "/opt/astrbot/astrbot.yml"
            if os.path.exists(yml):
                cmds = [
                    (
                        f"docker compose -f {yml} down --timeout 30",
                        "停止 AstrBot/NapCat 容器",
                    ),
                    (
                        f"docker-compose -f {yml} down --timeout 30",
                        "停止容器(兼容命令)",
                    ),
                ]
                for cmd, desc in cmds:
                    ok, out = run_command(cmd, quiet=True)
                    if ok:
                        results.append(f"✓ {desc}")
                        break
                else:
                    results.append("⚠ 容器停止失败")
            else:
                results.append("⚠ astrbot.yml 不存在，跳过容器停止")
        if remove_images:
            for img in [
                "mlikiowa/napcat-docker",
                "soulter/astrbot",
                "m.daocloud.io/docker.io/soulter/astrbot",
                "docker.io/soulter/astrbot",
                "docker.io/mlikiowa/napcat-docker",
            ]:
                run_command(f"docker rmi {img} 2>/dev/null", quiet=True)
            results.append("✓ 已删除相关 Docker 镜像")
        if remove_data:
            for d in ["/opt/astrbot"]:
                run_command(f"rm -rf {d}", quiet=True)
            results.append("✓ 已删除 AstrBot/NapCat 数据 (/opt/astrbot)")
        if remove_sillytavern:
            run_command("pm2 delete sillytavern 2>/dev/null", quiet=True)
            if remove_data:
                run_command("rm -rf /opt/sillytavern", quiet=True)
                results.append("✓ 已删除 SillyTavern 数据 (/opt/sillytavern)")
            else:
                results.append("✓ 已停止 SillyTavern（数据保留）")
        if remove_dependencies:
            run_command("npm uninstall -g pm2 2>/dev/null", quiet=True)
            results.append("✓ 已卸载 PM2")
            results.append(
                "⚠ Docker 和 Node.js 未自动卸载（可能被其他服务使用），如需卸载请手动操作"
            )
        config["installed"] = False
        save_config()
        results.append("✓ 安装状态已重置，刷新页面可重新安装")
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"success": False, "results": [f"❌ 卸载出错: {str(e)}"]})


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
    }
    return render_template(
        "index.html", plugins=ASTRBOT_PLUGINS, config=safe_config, version=VERSION
    )


@app.route("/tutorial/<name>")
@login_required
def tutorial(name):
    """教程页面"""
    templates = ["napcat", "astrbot", "tavern", "server"]
    if name not in templates:
        return redirect(url_for("index"))
    return render_template(
        f"tutorial_{name}.html",
        server_ip=config.get("server_ip", "你的服务器IP"),
        napcat_port=config.get("napcat_port", 6099),
        astrbot_port=config.get("astrbot_port", 6185),
        tavern_port=config.get("tavern_port", 18888),
    )


@app.route("/encyclopedia")
@login_required
def encyclopedia():
    """百科全书已改为侧边栏组件，旧路由重定向到首页"""
    return redirect(url_for("index"))


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
    napcat_url = (
        f"{napcat_base}/webui?token={token}" if token and napcat_base else napcat_base
    )
    return jsonify(
        {
            "token": token,
            "url": napcat_url,
            "config_path": "/opt/astrbot/napcat/config/webui.json",
        }
    )


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
        ok, output = run_command(
            "docker ps -a --format '{{.Names}}' | grep -w astrbot", quiet=True
        )
        return ok and "astrbot" in output
    elif name == "napcat":
        # 检查 NapCat 容器是否存在
        ok, output = run_command(
            "docker ps -a --format '{{.Names}}' | grep -w napcat", quiet=True
        )
        return ok and "napcat" in output
    return False


@app.route("/api/services/installed")
@login_required
def api_services_installed():
    """检测各服务安装状态"""
    return jsonify(
        {
            "napcat": check_service_installed("napcat"),
            "astrbot": check_service_installed("astrbot"),
            "sillytavern": check_service_installed("sillytavern"),
        }
    )


@app.route("/api/services/<name>/reinstall", methods=["POST"])
@login_required
def api_service_reinstall(name):
    """重装单个服务"""
    global install_status, install_generation, _log_bytes
    valid_names = {"napcat", "astrbot", "sillytavern"}
    if name not in valid_names:
        return jsonify({"error": f"未知服务: {name}"}), 400

    install_generation += 1
    _log_bytes = 0
    install_status = {
        "stage": "installing",
        "progress": 0,
        "message": f"重装 {name}...",
        "logs": [],
        "results": {},
        "current_stage": "",
    }
    # 在主线程中捕获请求数据（线程内无法访问 Flask request 上下文）
    reinstall_data = request.json or {}

    def do_reinstall():
        global install_status
        try:
            if name == "sillytavern":
                install_status["message"] = "重装 SillyTavern..."
                log("开始重装 SillyTavern...")
                # 先停止 PM2 进程再删除目录（避免进程崩溃导致502）
                run_command("pm2 stop sillytavern 2>/dev/null || true")
                run_command("pm2 delete sillytavern 2>/dev/null || true")
                install_status["progress"] = 10
                # 应用前端传入的重装参数
                if reinstall_data.get("tavern_port"):
                    config["tavern_port"] = int(reinstall_data["tavern_port"])
                if reinstall_data.get("tavern_username"):
                    config["tavern_username"] = reinstall_data["tavern_username"]
                if reinstall_data.get("tavern_password"):
                    config["tavern_password"] = reinstall_data["tavern_password"]
                save_config()
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
                    install_status["progress"] = 80
                    install_status["message"] = "获取 NapCat Token..."
                    napcat_token = ""
                    for _ in range(10):
                        time.sleep(3)
                        napcat_token = get_napcat_token()
                        if napcat_token:
                            log(f"NapCat WebUI Token: {napcat_token}")
                            break
                    config["napcat_token"] = napcat_token
                    save_config()
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


@app.route("/api/system/refresh-ip", methods=["POST"])
@login_required
def api_refresh_ip():
    """手动刷新公网IP（清除缓存并重新获取）"""
    global _sysinfo_cache
    old_ip = config.get("server_ip", "")
    # 临时清空，让 fetch_public_ip 重新写入
    config["server_ip"] = ""
    _sysinfo_cache = {"data": None, "time": 0}
    new_ip = fetch_public_ip()
    if new_ip:
        return jsonify(
            {"success": True, "ip": new_ip, "message": f"公网IP已更新: {new_ip}"}
        )
    else:
        # 获取失败，恢复旧值避免丢失
        if old_ip and old_ip != "你的服务器IP":
            config["server_ip"] = old_ip
        return jsonify(
            {
                "success": False,
                "ip": old_ip,
                "message": "无法获取公网IP，请检查服务器网络连接",
            }
        ), 500


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


# ========== 路由：错误报告 ==========


@app.route("/api/system/error-report")
@login_required
def api_error_report():
    """一键生成错误报告，包含设备信息、服务状态、安装进度、报错日志"""
    report = {}
    # 1. 系统信息
    ok, os_info = run_command("cat /etc/os-release 2>/dev/null | head -5", quiet=True)
    report["os"] = os_info.strip() if ok else "未知"
    ok, arch = run_command("uname -m", quiet=True)
    report["arch"] = arch.strip() if ok else "未知"
    ok, kernel = run_command("uname -r", quiet=True)
    report["kernel"] = kernel.strip() if ok else "未知"
    ok, mem = run_command("free -h | grep Mem | awk '{print $2}'", quiet=True)
    report["total_memory"] = mem.strip() if ok else "未知"
    ok, disk = run_command(
        "df -h / | tail -1 | awk '{print $2, $3, $4, $5}'", quiet=True
    )
    report["disk"] = disk.strip() if ok else "未知"

    # 2. 服务器 IP 属地
    server_ip = config.get("server_ip", "")
    report["server_ip"] = server_ip
    if server_ip and server_ip != "你的服务器IP":
        ok, region = run_command(
            f"curl -s --connect-timeout 3 'http://ip-api.com/json/{server_ip}?fields=country,regionName,city,isp&lang=zh-CN'",
            quiet=True,
        )
        report["ip_region"] = region.strip() if ok else "查询失败"
    else:
        report["ip_region"] = "未获取到IP"

    # 3. 依赖安装状态
    deps = {}
    ok, out = run_command("docker --version", quiet=True)
    deps["docker"] = out.strip() if ok else "未安装"
    ok, out = run_command("node --version", quiet=True)
    deps["nodejs"] = out.strip() if ok else "未安装"
    ok, out = run_command("npm --version", quiet=True)
    deps["npm"] = out.strip() if ok else "未安装"
    ok, out = run_command("pm2 --version", quiet=True)
    deps["pm2"] = out.strip() if ok else "未安装"
    ok, out = run_command("git --version", quiet=True)
    deps["git"] = out.strip() if ok else "未安装"
    report["dependencies"] = deps

    # 4. 各服务安装和运行状态
    services_status = get_all_services_status()
    svc_report = {}
    for name, info in services_status.items():
        svc_report[name] = {
            "status": info.get("status", "unknown"),
            "memory_mb": round(info.get("memory", 0) / 1024 / 1024, 1)
            if info.get("memory", 0) > 0
            else 0,
        }
    # 检查关键目录/文件是否存在
    svc_report["astrbot_dir_exists"] = os.path.exists("/opt/astrbot")
    svc_report["astrbot_yml_exists"] = os.path.exists("/opt/astrbot/astrbot.yml")
    svc_report["sillytavern_dir_exists"] = os.path.exists("/opt/sillytavern")
    svc_report["sillytavern_package_json"] = os.path.exists(
        "/opt/sillytavern/package.json"
    )
    svc_report["sillytavern_node_modules"] = os.path.exists(
        "/opt/sillytavern/node_modules"
    )
    report["services"] = svc_report

    # 5. 安装进度和日志
    report["install_stage"] = install_status.get("stage", "unknown")
    report["install_progress"] = install_status.get("progress", 0)
    report["install_message"] = install_status.get("message", "")
    report["install_current_stage"] = install_status.get("current_stage", "")
    # 最近50条日志
    logs = install_status.get("logs", [])
    report["recent_logs"] = logs[-50:] if len(logs) > 50 else logs

    # 6. 配置信息（脱敏）
    report["config"] = {
        "installed": config.get("installed", False),
        "astrbot_port": config.get("astrbot_port", 6185),
        "napcat_port": config.get("napcat_port", 6099),
        "tavern_port": config.get("tavern_port", 18888),
        "install_tavern": config.get("install_tavern", True),
        "install_astrbot": config.get("install_astrbot", True),
    }

    # 7. Docker 容器日志（最近 20 行）
    docker_logs = {}
    for cname in ["napcat", "astrbot"]:
        ok, out = run_command(f"docker logs {cname} --tail 20 2>&1", quiet=True)
        docker_logs[cname] = out.strip() if ok else "容器不存在或未运行"
    report["docker_logs"] = docker_logs

    # 8. NapCat WebUI 配置状态
    webui_json = "/opt/astrbot/napcat/config/webui.json"
    if os.path.exists(webui_json):
        try:
            with open(webui_json, "r") as f:
                wdata = json.load(f)
            report["napcat_webui"] = {
                "exists": True,
                "host": wdata.get("host", ""),
                "port": wdata.get("port", ""),
                "has_token": bool(wdata.get("token", "")),
            }
        except:
            report["napcat_webui"] = {"exists": True, "parse_error": True}
    else:
        report["napcat_webui"] = {"exists": False}

    # 9. SillyTavern config.yaml 关键字段
    tavern_config_path = "/opt/sillytavern/config.yaml"
    if os.path.exists(tavern_config_path):
        try:
            with open(tavern_config_path, "r") as f:
                tc = f.read()
            report["tavern_config"] = {
                "exists": True,
                "enableUserAccounts": "enableUserAccounts: true" in tc,
                "securityOverride": "securityOverride: true" in tc,
                "basicAuthMode": "basicAuthMode: true" in tc,
            }
        except:
            report["tavern_config"] = {"exists": True, "read_error": True}
    else:
        report["tavern_config"] = {"exists": False}

    # 10. 夜鹭机版本
    report["yolushiki_version"] = VERSION

    return jsonify(report)


# ========== 路由：安装 API（保留） ==========


@app.route("/api/status")
@login_required
def get_install_status():
    """安装进度"""
    return jsonify(install_status)


@app.route("/api/config")
@login_required
def api_get_config():
    """获取当前配置（排除敏感字段）"""
    sensitive_keys = {"token", "secret_key"}
    safe_config = {k: v for k, v in config.items() if k not in sensitive_keys}
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
    mirror = data.get("mirror", "xuanyuan")
    daemon_json = "/etc/docker/daemon.json"
    try:
        if mirror == "direct":
            # 海外服务器：清除镜像加速，直连 Docker Hub
            if os.path.exists(daemon_json):
                os.remove(daemon_json)
            run_command("systemctl daemon-reload", quiet=True)
            run_command("systemctl restart docker", quiet=True)
            time.sleep(3)  # 等待 Docker 完全重启
            return jsonify(
                {"success": True, "message": "已切换为直连 Docker Hub（无镜像加速）"}
            )
        mirrors_map = {
            "xuanyuan": ["https://docker.xuanyuan.me"],
            "daocloud": ["https://docker.m.daocloud.io"],
            "1ms": ["https://docker.1ms.run"],
        }
        mirrors = mirrors_map.get(mirror, mirrors_map["xuanyuan"])
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
            "tencent": "https://mirrors.tencent.com/npm/",
            "direct": "",
        }
        runtime_mirrors["npm_registry"] = npm_map.get(mirror_value, mirror_value)
        msg = f"npm 源已切换为: {runtime_mirrors['npm_registry'] or '官方源'}"
        log(msg)
        return jsonify({"success": True, "message": msg})
    elif mirror_type == "git":
        git_map = {
            "ghproxy": "https://gh.llkk.cc/",
            "gitclone": "https://gitclone.com/github.com/",
            "direct": "",
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
            "xuanyuan": ["https://docker.xuanyuan.me"],
            "daocloud": ["https://docker.m.daocloud.io"],
            "1ms": ["https://docker.1ms.run"],
        }
        mirrors = mirrors_map.get(mirror, mirrors_map["xuanyuan"])
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
    install_status["logs"].append(
        f"[{time.strftime('%H:%M:%S')}] === 重试阶段: {current_stage} ==="
    )

    def do_retry():
        global install_status
        try:
            if current_stage == "docker_pull":
                install_status["message"] = "重试拉取 Docker 镜像（代理模式）..."
                install_status["current_stage"] = "docker_pull"
                astrbot_dir = "/opt/astrbot"
                pull_ok = pull_all_images_via_proxy(generation=current_gen)
                if not pull_ok:
                    log("代理拉取未全部成功，尝试 docker compose pull...")
                    install_status["message"] = "通过 docker compose 拉取镜像..."
                    pull_ok, _ = run_command_stream(
                        "docker compose -f astrbot.yml pull",
                        cwd=astrbot_dir,
                        generation=current_gen,
                    )
                    if not pull_ok:
                        pull_ok, _ = run_command_stream(
                            "docker-compose -f astrbot.yml pull",
                            cwd=astrbot_dir,
                            generation=current_gen,
                        )
                if not pull_ok:
                    raise Exception("Docker镜像拉取失败")
                # 继续完成后续步骤
                log("镜像拉取成功，继续启动容器...")
                install_status["message"] = "启动 Docker 容器..."
                success, _ = run_command(
                    "docker compose -f astrbot.yml up -d", cwd=astrbot_dir
                )
                if not success:
                    success, _ = run_command(
                        "docker-compose -f astrbot.yml up -d", cwd=astrbot_dir
                    )
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

            elif current_stage == "dep_pm2":
                install_status["message"] = "重试安装 PM2..."
                install_status["current_stage"] = "dep_pm2"
                if not install_pm2():
                    raise Exception("PM2安装失败")
                log("PM2 安装成功，继续后续部署...")
                # PM2安装后继续部署 SillyTavern
                if config.get("install_tavern", True):
                    install_status["progress"] = 65
                    install_status["message"] = "部署 SillyTavern..."
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
                    npm_cmd = (
                        f"npm install --no-audit --no-fund --registry={npm_registry}"
                    )
                    log(f"使用npm镜像: {npm_registry}")
                else:
                    npm_cmd = "npm install --no-audit --no-fund"
                success, _ = run_command_stream(
                    npm_cmd, cwd=tavern_dir, generation=current_gen, timeout=900
                )
                node_modules = os.path.join(tavern_dir, "node_modules")
                if (
                    not success
                    and os.path.exists(node_modules)
                    and len(os.listdir(node_modules)) > 10
                ):
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
    config_content = generate_tavern_config_yaml()
    config_path = os.path.join(tavern_dir, "config.yaml")
    with open(config_path, "w") as f:
        f.write(config_content)
    log(f"config.yaml 已写入（端口:{config['tavern_port']}, securityOverride:true）")
    log("使用PM2启动SillyTavern...")
    run_command("pm2 delete sillytavern 2>/dev/null || true")
    success, _ = run_command(
        'pm2 start server.js --name "sillytavern" --max-memory-restart 300M',
        cwd=tavern_dir,
    )
    if success:
        run_command("pm2 save", quiet=True)
        startup_ok, _ = run_command("pm2 startup", quiet=True)
        if startup_ok:
            log("PM2 开机自启已配置")
        else:
            log(
                "PM2 开机自启配置可能未成功（非root用户请手动执行 pm2 startup 输出的命令）"
            )
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
    ok, ip_out = run_command(
        "curl -s --connect-timeout 3 ifconfig.me || curl -s --connect-timeout 3 ip.sb",
        quiet=True,
    )
    if ok and ip_out.strip():
        server_ip = ip_out.strip()
    if not server_ip:
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
    napcat_url = (
        f"{napcat_base}/webui?token={napcat_token}" if napcat_token else napcat_base
    )
    install_status["results"] = {
        "server_ip": server_ip,
        "napcat_url": napcat_url,
        "napcat_token": napcat_token,
        "astrbot_url": f"http://{server_ip}:{config['astrbot_port']}",
        "tavern_url": f"http://{server_ip}:{config['tavern_port']}",
        "tavern_username": config.get("tavern_username", "admin"),
        "tavern_password": config.get("tavern_password", ""),
    }
    install_status["progress"] = 98
    install_status["message"] = "注册常驻服务..."
    log("注册夜鹭机管理面板为常驻服务...")
    already_in_pm2, _ = run_command("pm2 describe yolushiki 2>/dev/null", quiet=True)
    if already_in_pm2:
        log("夜鹭机已在 PM2 中运行，跳过重新注册")
        run_command("pm2 save", quiet=True)
    else:
        yolushiki_app = os.path.join(CONFIG_DIR, "app.py")
        if os.path.exists(yolushiki_app):
            run_command(
                f'pm2 start {yolushiki_app} --name "yolushiki" --interpreter python3 -- --port 9999 --host 0.0.0.0',
                quiet=True,
            )
            run_command("pm2 save", quiet=True)
    # 确保 PM2 开机自启（无论是否安装了酒馆，此处是所有安装路径的必经之处）
    startup_ok, _ = run_command("pm2 startup", quiet=True)
    if startup_ok:
        log("PM2 开机自启已配置")
    else:
        log("PM2 开机自启配置可能未成功（非root用户请手动执行 pm2 startup 输出的命令）")
    install_status["progress"] = 100
    install_status["message"] = "安装完成！"
    install_status["stage"] = "completed"
    install_status["current_stage"] = ""
    log("夜鹭机安装完成！")


@app.route("/api/install", methods=["POST"])
@login_required
def start_install():
    """开始安装"""
    global install_status, install_generation, _log_bytes
    install_generation += 1
    current_gen = install_generation
    _log_bytes = 0
    install_status = {
        "stage": "installing",
        "progress": 0,
        "message": "开始安装...",
        "logs": [],
        "results": {},
        "current_stage": "",
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


# ========== 路由：搬家功能（备份/恢复） ==========

MIGRATION_TMP_DIR = os.path.join(CONFIG_DIR, "migration_tmp")

# 夜鹭机默认安装路径
DEFAULT_PATHS = {
    "tavern_path": "/opt/sillytavern",
    "astrbot_path": "/opt/astrbot/data",
    "napcat_path": "/opt/astrbot/napcat/config",
}

# 路径探测：常见安装位置 + Docker inspect + find
TAVERN_SEARCH_DIRS = [
    "/opt/sillytavern",
    "/root/SillyTavern",
    "/home/*/SillyTavern",
    "/srv/sillytavern",
    "/opt/SillyTavern",
]
ASTRBOT_SEARCH_DIRS = [
    "/opt/astrbot/data",
    "/root/astrbot/data",
    "/home/*/astrbot/data",
    "/srv/astrbot/data",
]
NAPCAT_SEARCH_DIRS = [
    "/opt/astrbot/napcat/config",
    "/opt/napcat/config",
    "/root/napcat/config",
    "/home/*/napcat/config",
]


def _detect_service_paths():
    """自动探测各服务安装路径"""
    result = {}

    # --- SillyTavern: 找 package.json 含 sillytavern ---
    for pattern in TAVERN_SEARCH_DIRS:
        for p in glob.glob(pattern):
            pkg = os.path.join(p, "package.json")
            if os.path.isfile(pkg):
                try:
                    with open(pkg, "r") as f:
                        d = json.load(f)
                    if "sillytavern" in d.get("name", "").lower():
                        result["tavern_path"] = p
                        break
                except Exception:
                    pass
            elif os.path.isdir(os.path.join(p, "data")):
                result["tavern_path"] = p
                break
    # Docker fallback: 检查 pm2 进程
    if "tavern_path" not in result:
        try:
            out = subprocess.check_output(
                ["pm2", "jlist"], timeout=5, stderr=subprocess.DEVNULL
            ).decode()
            for proc in json.loads(out):
                cwd = proc.get("pm2_env", {}).get("pm_cwd", "")
                if "sillytavern" in cwd.lower() and os.path.isdir(cwd):
                    result["tavern_path"] = cwd
                    break
        except Exception:
            pass

    # --- AstrBot: 找 data 目录 ---
    for pattern in ASTRBOT_SEARCH_DIRS:
        for p in glob.glob(pattern):
            if os.path.isdir(p):
                result["astrbot_path"] = p
                break
        if "astrbot_path" in result:
            break
    # Docker fallback
    if "astrbot_path" not in result:
        try:
            out = subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "astrbot",
                    "--format",
                    "{{range .Mounts}}{{.Source}}:{{.Destination}}\n{{end}}",
                ],
                timeout=5,
                stderr=subprocess.DEVNULL,
            ).decode()
            for line in out.strip().split("\n"):
                if ":" in line:
                    src, dst = line.rsplit(":", 1)
                    if "/data" in dst and os.path.isdir(src):
                        result["astrbot_path"] = src
                        break
        except Exception:
            pass

    # --- NapCat: 找 config 目录 ---
    for pattern in NAPCAT_SEARCH_DIRS:
        for p in glob.glob(pattern):
            if os.path.isdir(p):
                result["napcat_path"] = p
                break
        if "napcat_path" in result:
            break
    # Docker fallback
    if "napcat_path" not in result:
        try:
            out = subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "napcat",
                    "--format",
                    "{{range .Mounts}}{{.Source}}:{{.Destination}}\n{{end}}",
                ],
                timeout=5,
                stderr=subprocess.DEVNULL,
            ).decode()
            for line in out.strip().split("\n"):
                if ":" in line:
                    src, dst = line.rsplit(":", 1)
                    if "config" in dst.lower() and os.path.isdir(src):
                        result["napcat_path"] = src
                        break
        except Exception:
            pass

    return result


def _get_paths(data):
    """从请求数据中获取路径，如果是自定义模式则用自定义路径，否则用默认"""
    paths = {}
    paths["tavern_path"] = (
        data.get("tavern_path", "").strip() or DEFAULT_PATHS["tavern_path"]
    )
    paths["astrbot_path"] = (
        data.get("astrbot_path", "").strip() or DEFAULT_PATHS["astrbot_path"]
    )
    paths["napcat_path"] = (
        data.get("napcat_path", "").strip() or DEFAULT_PATHS["napcat_path"]
    )
    return paths


def _collect_migration_data(tmp_dir, paths):
    """收集需要备份的数据到临时目录，返回详情列表"""
    details = []

    # --- AstrBot: 完整 data 目录 ---
    astrbot_data = paths["astrbot_path"]
    if os.path.isdir(astrbot_data):
        dst = os.path.join(tmp_dir, "astrbot", "data")
        shutil.copytree(astrbot_data, dst, dirs_exist_ok=True)
        details.append("AstrBot data")

    # --- SillyTavern: 完整 data 目录 + config.yaml ---
    tavern_base = paths["tavern_path"]
    tavern_data = os.path.join(tavern_base, "data")
    if os.path.isdir(tavern_data):
        dst = os.path.join(tmp_dir, "sillytavern", "data")
        shutil.copytree(tavern_data, dst, dirs_exist_ok=True)
        details.append("SillyTavern data")
    # config.yaml
    tavern_cfg = os.path.join(tavern_base, "config.yaml")
    if os.path.isfile(tavern_cfg):
        dst_cfg = os.path.join(tmp_dir, "sillytavern", "config.yaml")
        os.makedirs(os.path.dirname(dst_cfg), exist_ok=True)
        shutil.copy2(tavern_cfg, dst_cfg)
        if "SillyTavern data" not in details:
            details.append("SillyTavern config")

    # --- NapCat: 完整配置（含 webui token） ---
    napcat_config = paths["napcat_path"]
    if os.path.isdir(napcat_config):
        dst = os.path.join(tmp_dir, "napcat", "config")
        shutil.copytree(napcat_config, dst, dirs_exist_ok=True)
        details.append("NapCat config")

    return details


@app.route("/api/migration/detect-paths")
@login_required
def api_migration_detect_paths():
    """自动探测各服务安装路径"""
    try:
        result = _detect_service_paths()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/migration/export", methods=["POST"])
@login_required
def api_migration_export():
    """打包备份数据"""
    try:
        data = request.get_json(silent=True) or {}
        paths = _get_paths(data)

        if os.path.exists(MIGRATION_TMP_DIR):
            shutil.rmtree(MIGRATION_TMP_DIR, ignore_errors=True)
        os.makedirs(MIGRATION_TMP_DIR, exist_ok=True)

        collect_dir = os.path.join(MIGRATION_TMP_DIR, "yolushiki_backup")
        os.makedirs(collect_dir, exist_ok=True)

        details = _collect_migration_data(collect_dir, paths)

        if not details:
            return jsonify({"success": False, "error": "未找到任何可备份的数据"}), 400

        meta = {
            "version": VERSION,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "contents": details,
            "paths": paths,
        }
        with open(os.path.join(collect_dir, "backup_meta.json"), "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"yolushiki_backup_{ts}.tar.gz"
        archive_path = os.path.join(MIGRATION_TMP_DIR, filename)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(collect_dir, arcname="yolushiki_backup")

        shutil.rmtree(collect_dir, ignore_errors=True)

        return jsonify({"success": True, "filename": filename, "details": details})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/migration/download")
@login_required
def api_migration_download():
    """下载打包好的备份文件"""
    filename = request.args.get("file", "")
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "无效文件名"}), 400
    filepath = os.path.join(MIGRATION_TMP_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(filepath, as_attachment=True, download_name=filename)


@app.route("/api/migration/import", methods=["POST"])
@login_required
def api_migration_import():
    """接收上传的备份文件并恢复"""
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "未选择文件"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"success": False, "error": "未选择文件"}), 400

        # 解析自定义路径
        paths_json = request.form.get("paths", "")
        custom_paths = {}
        if paths_json:
            try:
                custom_paths = json.loads(paths_json)
            except Exception:
                pass
        paths = _get_paths(custom_paths)

        if os.path.exists(MIGRATION_TMP_DIR):
            shutil.rmtree(MIGRATION_TMP_DIR, ignore_errors=True)
        os.makedirs(MIGRATION_TMP_DIR, exist_ok=True)

        upload_path = os.path.join(MIGRATION_TMP_DIR, "upload.tar.gz")
        f.save(upload_path)

        extract_dir = os.path.join(MIGRATION_TMP_DIR, "extract")
        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(upload_path, "r:gz") as tar:
            for member in tar.getmembers():
                member_path = os.path.normpath(member.name)
                if member_path.startswith("..") or os.path.isabs(member_path):
                    return jsonify(
                        {"success": False, "error": "备份文件包含非法路径，已拒绝"}
                    ), 400
            tar.extractall(extract_dir)

        backup_root = os.path.join(extract_dir, "yolushiki_backup")
        if not os.path.isdir(backup_root):
            items = os.listdir(extract_dir)
            if len(items) == 1 and os.path.isdir(os.path.join(extract_dir, items[0])):
                backup_root = os.path.join(extract_dir, items[0])
            else:
                return jsonify({"success": False, "error": "无效的备份文件结构"}), 400

        details = []

        # --- 恢复 AstrBot data ---
        astrbot_src = os.path.join(backup_root, "astrbot", "data")
        astrbot_dst = paths["astrbot_path"]
        if os.path.isdir(astrbot_src):
            os.makedirs(astrbot_dst, exist_ok=True)
            shutil.copytree(astrbot_src, astrbot_dst, dirs_exist_ok=True)
            details.append("AstrBot data")

        # --- 恢复 SillyTavern data + config ---
        tavern_src = os.path.join(backup_root, "sillytavern")
        tavern_dst = paths["tavern_path"]
        if os.path.isdir(tavern_src):
            for root, dirs, files in os.walk(tavern_src):
                rel = os.path.relpath(root, tavern_src)
                target_dir = os.path.join(tavern_dst, rel)
                os.makedirs(target_dir, exist_ok=True)
                for fname in files:
                    src_file = os.path.join(root, fname)
                    dst_file = os.path.join(target_dir, fname)
                    shutil.copy2(src_file, dst_file)
            details.append("SillyTavern data + config")

        # --- 恢复 NapCat config（完整覆盖） ---
        napcat_src = os.path.join(backup_root, "napcat", "config")
        napcat_dst = paths["napcat_path"]
        if os.path.isdir(napcat_src):
            os.makedirs(napcat_dst, exist_ok=True)
            shutil.copytree(napcat_src, napcat_dst, dirs_exist_ok=True)
            details.append("NapCat config")

        shutil.rmtree(MIGRATION_TMP_DIR, ignore_errors=True)

        if not details:
            return jsonify(
                {"success": False, "error": "备份文件中未找到可恢复的数据"}
            ), 400

        return jsonify({"success": True, "details": details})
    except tarfile.TarError:
        return jsonify({"success": False, "error": "文件不是有效的 tar.gz 格式"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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
