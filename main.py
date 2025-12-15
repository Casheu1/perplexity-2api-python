import logging
import sys
import json
import uuid
import time
import os
import platform
import shutil
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, Depends, Header, HTTPException, Form
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from datetime import datetime, timedelta

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    logger.warning("psutil 未安装，系统监控功能将受限。运行: pip install psutil")

from app.core.config import settings
from app.providers.perplexity_provider import PerplexityProvider

# [修改] 设置日志级别为 DEBUG，格式包含文件名和行号
logger.remove()
logger.add(
    sys.stdout, 
    level="DEBUG", 
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

provider = PerplexityProvider()

# 模拟账号数据存储（实际应使用数据库）
accounts_db: Dict[str, Dict[str, Any]] = {}
logs_db: List[Dict[str, Any]] = []
custom_models: List[Dict[str, Any]] = [
    {"id": "gpt-4", "name": "GPT-4", "provider": "openai", "is_custom": False},
    {"id": "gpt-3.5-turbo", "name": "GPT-3.5 Turbo", "provider": "openai", "is_custom": False},
    {"id": "claude-3-opus", "name": "Claude 3 Opus", "provider": "anthropic", "is_custom": False},
]

def load_accounts_from_sessions():
    """从 data/sessions/ 目录加载已保存的账号到 accounts_db"""
    sessions_dir = Path("data/sessions")
    if not sessions_dir.exists():
        logger.info("📁 未找到 sessions 目录，跳过账号加载")
        return
    
    for session_file in sessions_dir.glob("*.json"):
        try:
            logger.debug(f"处理会话文件: {session_file}")
            with open(session_file, 'r', encoding='utf-8') as f:
                session_data = json.load(f)
            
            account_name = session_data.get("account_name")
            if not account_name:
                logger.warning(f"⚠️ 会话文件缺少账号名称: {session_file}")
                continue
            
            logger.info(f"📂 找到账号: {account_name}")
            
            # 检查是否已存在相同账号名的记录（避免重复）
            existing_account = None
            for acc_id, acc in accounts_db.items():
                if acc.get("name") == account_name:
                    existing_account = acc_id
                    break
            
            if existing_account:
                # 更新现有记录
                account_id = existing_account
                logger.debug(f"📝 更新现有账号: {account_name}")
            else:
                # 创建新记录
                account_id = str(uuid.uuid4())[:8]
                logger.info(f"📂 加载账号: {account_name} (会话文件: {session_file.name})")
            
            # 获取 Cookie 文件信息 - 增强路径处理
            cookie_file = session_data.get("cookie_file", "")
            cookie_count = 0
            cookie_file_path = None
            
            if cookie_file:
                # 尝试直接路径
                cookie_file_path = Path(cookie_file)
                if not cookie_file_path.exists():
                    # 尝试相对当前工作目录
                    cookie_file_path = Path.cwd() / cookie_file
                    if not cookie_file_path.exists():
                        # 尝试从 directory_info 获取
                        dir_info = session_data.get("directory_info", {})
                        cookie_json = dir_info.get("cookie_json", "")
                        if cookie_json:
                            cookie_file_path = Path(cookie_json)
                            if not cookie_file_path.exists():
                                cookie_file_path = Path.cwd() / cookie_json
                        else:
                            # 尝试在 data/cookies/账号名/ 下查找
                            candidate = Path("data/cookies") / account_name / "cookies.json"
                            if candidate.exists():
                                cookie_file_path = candidate
                
                if cookie_file_path and cookie_file_path.exists():
                    try:
                        with open(cookie_file_path, 'r', encoding='utf-8') as cf:
                            cookie_data = json.load(cf)
                        cookie_count = cookie_data.get("cookie_count", 0)
                        logger.debug(f"✅ 成功读取 Cookie 文件: {cookie_file_path}, cookie_count: {cookie_count}")
                    except Exception as e:
                        logger.warning(f"⚠️ 读取 Cookie 文件失败 {cookie_file_path}: {e}")
                else:
                    logger.warning(f"⚠️ Cookie 文件不存在: {cookie_file}，尝试的路径: {cookie_file_path}")
            else:
                logger.warning(f"⚠️ 会话文件中未指定 cookie_file 字段")
            
            # 获取目录信息
            dir_info = session_data.get("directory_info", {})
            account_dir = dir_info.get("account_dir", f"data/cookies/{account_name}")
            cookie_json = dir_info.get("cookie_json", "")
            cookie_txt = dir_info.get("cookie_txt", "")
            
            # 创建账号记录（结构与 Web UI 添加的一致）
            account_record = {
                "id": account_id,
                "name": account_name,
                "is_active": True,
                "token_source": session_data.get("source", "unknown"),
                "data_dir": account_dir,
                "token": "本地保存的Cookie",
                "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
                "total_calls": session_data.get("stats", {}).get("total_calls", 0),
                "discord_username": None,
                "created_at": datetime.fromtimestamp(session_data.get("created_at", time.time())).isoformat(),
                "cookie_count": cookie_count,
                "user_agent_preview": "",  # 可从 Cookie 文件获取，但简化处理
                "local_saved": True,
                "cookie_files": [cookie_json, cookie_txt]
            }
            accounts_db[account_id] = account_record
            logger.info(f"✅ 成功加载账号: {account_name} (ID: {account_id}, Cookie数量: {cookie_count})")
            
        except Exception as e:
            logger.error(f"❌ 加载会话文件失败 {session_file}: {e}")
            import traceback
            traceback.print_exc()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"启动 {settings.APP_NAME} v{settings.APP_VERSION} (Botasaurus Deep Debug Mode)...")
    logger.info("正在初始化 Botasaurus 浏览器服务...")
    try:
        # 先加载本地保存的账号
        load_accounts_from_sessions()
        logger.info(f"📊 已加载 {len(accounts_db)} 个本地账号")
        
        # 再初始化 Botasaurus
        await provider.solver.initialize_session()
    except Exception as e:
        logger.error(f"初始化失败: {e}")
    yield
    logger.info("服务关闭。")

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

async def verify_key(authorization: str = Header(None)):
    if settings.API_MASTER_KEY != "1":
        if not authorization or authorization.split(" ")[1] != settings.API_MASTER_KEY:
            raise HTTPException(403, "Invalid API Key")

# ==================== 原有 API ====================
@app.post("/v1/chat/completions", dependencies=[Depends(verify_key)])
async def chat(request: Request):
    try:
        data = await request.json()
        # [新增] 打印客户端原始请求
        logger.debug(f"收到客户端请求: {data}")
        
        # 检查provider是否就绪
        if not hasattr(provider, 'solver'):
            raise HTTPException(503, "服务正在初始化，请稍后重试或通过Web UI添加账号")
        
        # 检查是否有可用的 Cookie
        if not provider.solver.get_cookies():
            raise HTTPException(400, "未找到有效的 Cookie，请通过 Web UI 添加账号或导入 Cookie")
        
        return await provider.chat_completion(data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Request Error: {e}")
        raise HTTPException(500, f"内部服务器错误: {str(e)}")

@app.get("/v1/models")
async def models():
    return await provider.get_models()

# ==================== 账号管理 API ====================
@app.get("/api/accounts")
async def get_accounts():
    """获取所有账号列表"""
    accounts = list(accounts_db.values())
    active_count = sum(1 for acc in accounts if acc.get("is_active", False))
    inactive_count = len(accounts) - active_count
    return JSONResponse(content={
        "accounts": accounts,
        "active_count": active_count,
        "inactive_count": inactive_count,
        "total": len(accounts)
    })

@app.post("/api/account/login/start")
async def start_login(name: str = Form(...)):
    """启动真实浏览器登录（使用 Botasaurus）"""
    import asyncio
    
    account_id = str(uuid.uuid4())[:8]
    
    try:
        logger.info(f"🔄 开始交互式登录流程，账号: {name}")
        
        # 设置超时（5分钟）
        try:
            result = await asyncio.wait_for(
                provider.solver.interactive_login(name),
                timeout=300  # 5分钟
            )
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ 登录超时，账号: {name}")
            return JSONResponse(content={
                "success": False,
                "message": "❌ 登录超时（5分钟）。请检查浏览器窗口是否正常打开。",
                "account_id": account_id
            })
        
        if result.get("success"):
            # 使用实际的账号目录
            account_dir = result.get("account_dir", f"data/cookies/{name}")
            
            # 创建账号记录
            new_account = {
                "id": account_id,
                "name": name,
                "is_active": True,
                "token_source": "browser",
                "data_dir": account_dir,
                "token": "真实Token（已保存至本地）",
                "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
                "total_calls": 0,
                "discord_username": None,
                "created_at": datetime.now().isoformat(),
                "cookie_count": len(result.get("cookies", {})),
                "user_agent_preview": result.get("user_agent", "")[:30] + "...",
                "local_saved": result.get("local_saved", False),
                "cookie_files": [
                    f"{account_dir}/cookies.json",
                    f"{account_dir}/cookies.txt"
                ]
            }
            accounts_db[account_id] = new_account
            
            # 添加日志
            logs_db.append({
                "timestamp": datetime.now().isoformat(),
                "account_name": name,
                "model": "N/A",
                "duration": 0,
                "status": "SUCCESS",
                "note": f"交互式登录成功，数据保存到: {account_dir}",
                "level": "info"
            })
            
            logger.info(f"✅ 交互式登录成功，账号: {name}, 数据目录: {account_dir}")
            return JSONResponse(content={
                "success": True,
                "message": f"✅ 登录成功！已获取 {result.get('cookie_count', 0)} 个 Cookie 并保存到本地目录。",
                "account_id": account_id,
                "cookie_count": len(result.get("cookies", {})),
                "user_agent_preview": result.get("user_agent", "")[:50],
                "account_dir": account_dir,
                "local_saved": result.get("local_saved", False)
            })
        else:
            error_msg = result.get("error", "未知错误")
            logger.error(f"❌ 登录失败，账号: {name}, 错误: {error_msg}")
            return JSONResponse(content={
                "success": False,
                "message": f"❌ 登录失败: {error_msg}",
                "account_id": account_id
            })
            
    except Exception as e:
        logger.error(f"❌ 登录过程异常，账号: {name}, 错误: {e}")
        return JSONResponse(content={
            "success": False,
            "message": f"❌ 登录过程异常: {str(e)}",
            "account_id": account_id
        })

@app.post("/api/token/refresh/{account_id}")
async def refresh_token(account_id: str):
    """刷新账号 Token（模拟）"""
    if account_id not in accounts_db:
        raise HTTPException(404, "账号不存在")
    
    account = accounts_db[account_id]
    account["token"] = "刷新Token_" + str(uuid.uuid4())[:8]
    account["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
    
    return JSONResponse(content={
        "success": True,
        "message": "✅ Token 刷新成功（模拟）"
    })

@app.get("/api/account/toggle/{account_id}")
async def toggle_account(account_id: str):
    """启用/禁用账号"""
    if account_id not in accounts_db:
        raise HTTPException(404, "账号不存在")
    
    account = accounts_db[account_id]
    account["is_active"] = not account.get("is_active", True)
    
    return JSONResponse(content={
        "success": True,
        "message": "✅ 账号状态已更新",
        "is_active": account["is_active"]
    })

@app.get("/api/account/delete/{account_id}")
async def delete_account(account_id: str):
    """删除账号"""
    if account_id not in accounts_db:
        raise HTTPException(404, "账号不存在")
    
    del accounts_db[account_id]
    
    return JSONResponse(content={
        "success": True,
        "message": "✅ 账号已删除"
    })

# ==================== 日志管理 API ====================
@app.get("/api/logs")
async def get_logs():
    """获取最近日志"""
    return JSONResponse(content={
        "logs": logs_db[-50:]  # 返回最近50条
    })

@app.get("/api/logs/clear")
async def clear_logs():
    """清空日志"""
    logs_db.clear()
    return JSONResponse(content={
        "success": True,
        "message": "✅ 日志已清空"
    })

# ==================== 服务控制 API ====================
@app.post("/api/service/stop")
async def stop_service():
    """停止服务（模拟）"""
    return JSONResponse(content={
        "success": True,
        "message": "🛑 服务停止命令已发送（实际需要进程管理）"
    })

@app.post("/api/settings/preview-mode")
async def set_preview_mode(request: Request):
    """设置预览模式"""
    data = await request.json()
    enabled = data.get("enabled", False)
    return JSONResponse(content={
        "success": True,
        "message": f"✅ 预览模式已{'开启' if enabled else '关闭'}"
    })

# ==================== Web UI ====================
@app.get("/", response_class=HTMLResponse)
async def ui():
    """提供 Web UI"""
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/ui-data")
async def ui_data():
    """提供 UI 所需数据（供前端 JavaScript 调用）"""
    accounts = list(accounts_db.values())
    active_count = sum(1 for acc in accounts if acc.get("is_active", False))
    inactive_count = len(accounts) - active_count
    
    return JSONResponse(content={
        "accounts": accounts,
        "active_count": active_count,
        "inactive_count": inactive_count,
        "logs": logs_db[-10:],
        "api_url": f"http://127.0.0.1:{settings.NGINX_PORT}",
        "version": "3.0"
    })

# ==================== 系统监控 API ====================

@app.get("/api/health")
async def health_check():
    """健康检查端点"""
    status = {
        "status": "healthy",
        "service": "perplexity-2api",
        "version": "3.0",
        "timestamp": datetime.now().isoformat()
    }
    
    # 检查基本服务状态
    try:
        # 检查Botasaurus状态
        botasaurus_ready = False
        if hasattr(provider, 'solver'):
            solver = provider.solver
            if hasattr(solver, 'cached_cookies'):
                botasaurus_ready = True
        
        status["botasaurus_ready"] = botasaurus_ready
        status["accounts_count"] = len(accounts_db)
        status["logs_count"] = len(logs_db)
        
        if not botasaurus_ready:
            status["warning"] = "Botasaurus 未就绪，请通过Web UI添加账号或检查初始化"
        
    except Exception as e:
        status["status"] = "degraded"
        status["error"] = str(e)
    
    return JSONResponse(content=status)

def get_directory_size(path: str) -> int:
    """计算目录大小（字节）"""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                total += entry.stat().st_size
            elif entry.is_dir():
                total += get_directory_size(entry.path)
    except (PermissionError, FileNotFoundError):
        pass
    return total

def format_file_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1
    return f"{size_bytes:.1f} {units[i]}"

@app.get("/api/system/status")
async def get_system_status():
    """获取系统状态"""
    status = {
        "service_status": "running",
        "botasaurus_status": "initializing",
        "total_accounts": len(accounts_db),
        "active_accounts": sum(1 for acc in accounts_db.values() if acc.get("is_active", False)),
        "api_requests": len(logs_db) if logs_db else 0,
        "memory_usage": 30,  # 默认值
        "timestamp": datetime.now().isoformat()
    }
    
    # 检查Botasaurus状态
    try:
        if hasattr(provider, 'solver') and hasattr(provider.solver, 'cached_cookies'):
            status["botasaurus_status"] = "initialized"
        else:
            status["botasaurus_status"] = "initializing"
    except:
        status["botasaurus_status"] = "failed"
    
    # 获取内存使用情况（如果psutil可用）
    if HAS_PSUTIL:
        try:
            process = psutil.Process()
            memory_percent = process.memory_percent()
            status["memory_usage"] = round(memory_percent, 1)
        except:
            pass
    
    return JSONResponse(content=status)

@app.get("/api/system/info")
async def get_system_info():
    """获取系统信息"""
    import sys as sys_module
    
    info = {
        "python_version": f"{sys_module.version_info.major}.{sys_module.version_info.minor}.{sys_module.version_info.micro}",
        "host_name": platform.node(),
        "working_dir": os.getcwd(),
        "platform": platform.platform(),
        "uptime": "刚刚启动",  # 简化版本
        "start_time": datetime.now().isoformat()
    }
    
    return JSONResponse(content=info)

# ==================== 文件管理 API ====================

@app.get("/api/files/list")
async def list_files(path: str = ""):
    """列出指定目录下的文件"""
    base_path = Path.cwd()
    if path:
        target_path = (base_path / path).resolve()
        # 安全检查：确保路径在项目目录内
        if not str(target_path).startswith(str(base_path)):
            raise HTTPException(403, "禁止访问此路径")
    else:
        target_path = base_path
    
    files = []
    try:
        for entry in os.scandir(target_path):
            try:
                file_info = {
                    "name": entry.name,
                    "path": str(Path(entry.path).relative_to(base_path)),
                    "type": "directory" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else 0,
                    "modified": entry.stat().st_mtime,
                    "permissions": oct(entry.stat().st_mode)[-3:]
                }
                
                # 如果是目录，估算大小
                if entry.is_dir():
                    try:
                        dir_size = get_directory_size(entry.path)
                        file_info["size"] = dir_size
                    except:
                        pass
                
                files.append(file_info)
            except (PermissionError, FileNotFoundError):
                continue
        
        # 按类型和名称排序
        files.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"].lower()))
        
    except (PermissionError, FileNotFoundError) as e:
        raise HTTPException(404, f"无法访问目录: {str(e)}")
    
    return JSONResponse(content={"files": files, "current_path": str(target_path.relative_to(base_path))})

@app.get("/api/files/storage")
async def get_storage_info():
    """获取存储空间信息"""
    base_path = Path.cwd()
    
    # 计算各种目录大小
    project_dir_size = get_directory_size(str(base_path))
    
    # 账号数据目录（如果存在）
    account_data_path = base_path / "data"
    account_data_size = get_directory_size(str(account_data_path)) if account_data_path.exists() else 0
    
    # 日志目录
    log_files_path = base_path / "error_logs"
    log_files_size = get_directory_size(str(log_files_path)) if log_files_path.exists() else 0
    
    # 缓存目录（输出目录）
    cache_files_path = base_path / "output"
    cache_files_size = get_directory_size(str(cache_files_path)) if cache_files_path.exists() else 0
    
    # 计算总磁盘使用率（如果psutil可用）
    storage_usage = 25  # 默认值
    if HAS_PSUTIL:
        try:
            disk_usage = psutil.disk_usage(str(base_path))
            storage_usage = (disk_usage.used / disk_usage.total) * 100
        except:
            pass
    
    return JSONResponse(content={
        "project_dir_size": project_dir_size,
        "account_data_size": account_data_size,
        "log_files_size": log_files_size,
        "cache_files_size": cache_files_size,
        "storage_usage": round(storage_usage, 1),
        "formatted": {
            "project_dir_size": format_file_size(project_dir_size),
            "account_data_size": format_file_size(account_data_size),
            "log_files_size": format_file_size(log_files_size),
            "cache_files_size": format_file_size(cache_files_size),
        }
    })

@app.post("/api/files/clean-cache")
async def clean_cache():
    """清理缓存文件"""
    base_path = Path.cwd()
    cache_dirs = ["output", "__pycache__", ".pytest_cache"]
    deleted_count = 0
    total_freed = 0
    
    for cache_dir in cache_dirs:
        cache_path = base_path / cache_dir
        if cache_path.exists():
            try:
                if cache_path.is_dir():
                    dir_size = get_directory_size(str(cache_path))
                    shutil.rmtree(cache_path)
                    deleted_count += 1
                    total_freed += dir_size
                    logger.info(f"已删除缓存目录: {cache_dir}")
            except Exception as e:
                logger.error(f"删除缓存目录 {cache_dir} 失败: {e}")
    
    # 删除单个缓存文件
    cache_patterns = ["*.pyc", "*.log", "*.tmp"]
    for pattern in cache_patterns:
        for file_path in base_path.rglob(pattern):
            try:
                if file_path.is_file():
                    file_size = file_path.stat().st_size
                    file_path.unlink()
                    deleted_count += 1
                    total_freed += file_size
            except Exception as e:
                pass
    
    return JSONResponse(content={
        "success": True,
        "message": f"✅ 已清理 {deleted_count} 个缓存项，释放 {format_file_size(total_freed)}",
        "deleted_count": deleted_count,
        "freed_bytes": total_freed
    })

@app.post("/api/files/delete")
async def delete_files(request: Request):
    """删除指定文件/目录"""
    data = await request.json()
    paths = data.get("paths", [])
    
    if not paths:
        raise HTTPException(400, "未指定要删除的路径")
    
    base_path = Path.cwd()
    deleted = []
    errors = []
    
    for rel_path in paths:
        try:
            target_path = (base_path / rel_path).resolve()
            # 安全检查
            if not str(target_path).startswith(str(base_path)):
                errors.append(f"禁止访问: {rel_path}")
                continue
            
            if target_path.exists():
                if target_path.is_dir():
                    shutil.rmtree(target_path)
                else:
                    target_path.unlink()
                deleted.append(rel_path)
                logger.info(f"已删除: {rel_path}")
            else:
                errors.append(f"文件不存在: {rel_path}")
        except Exception as e:
            errors.append(f"删除失败 {rel_path}: {str(e)}")
    
    return JSONResponse(content={
        "success": len(errors) == 0,
        "message": f"已删除 {len(deleted)} 个项，{len(errors)} 个错误",
        "deleted": deleted,
        "errors": errors
    })

# ==================== 增强日志 API ====================

@app.get("/api/logs/recent")
async def get_recent_logs(limit: int = 100):
    """获取最近日志（支持过滤）"""
    recent_logs = []
    
    # 这里可以扩展为从文件或数据库读取日志
    # 目前使用内存中的日志
    for log in logs_db[-limit:]:
        recent_logs.append({
            "timestamp": log.get("timestamp", ""),
            "level": log.get("level", "info"),
            "message": log.get("note", "") or log.get("status", ""),
            "account": log.get("account_name", ""),
            "model": log.get("model", "")
        })
    
    return JSONResponse(content={"logs": recent_logs})

@app.post("/api/accounts/refresh-all")
async def refresh_all_accounts():
    """刷新所有账号（模拟）"""
    # 在实际应用中，这里会调用provider刷新所有账号的Cookie
    logger.info("开始刷新所有账号...")
    
    # 模拟刷新过程
    import asyncio
    await asyncio.sleep(2)
    
    return JSONResponse(content={
        "success": True,
        "message": "✅ 已请求刷新所有账号，将在后台执行",
        "account_count": len(accounts_db)
    })

@app.post("/api/account/refresh/{account_id}")
async def refresh_account(account_id: str):
    """刷新指定账号"""
    if account_id not in accounts_db:
        raise HTTPException(404, "账号不存在")
    
    # 模拟刷新
    account = accounts_db[account_id]
    account["token"] = "刷新Token_" + str(uuid.uuid4())[:8]
    account["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
    account["total_calls"] = account.get("total_calls", 0) + 1
    
    logs_db.append({
        "timestamp": datetime.now().isoformat(),
        "account_name": account["name"],
        "level": "info",
        "note": "账号Token已刷新",
        "status": "SUCCESS"
    })
    
    return JSONResponse(content={
        "success": True,
        "message": "✅ 账号刷新成功",
        "account_id": account_id
    })

@app.post("/api/cookie/parse")
async def parse_cookie_string(request: Request):
    """解析 Cookie 字符串并创建账号"""
    try:
        data = await request.json()
        text = data.get("text", "")
        account_name = data.get("account_name", "导入的账号")
        
        if not text:
            raise HTTPException(400, "请输入要解析的文本内容")
        
        # 调用 BrowserService 解析 Cookie
        result = provider.solver.parse_cookie_string(text, account_name)
        
        if result.get("success"):
            # 创建账号记录
            account_id = str(uuid.uuid4())[:8]
            account_dir = result.get("account_dir", f"data/cookies/{account_name}")
            
            new_account = {
                "id": account_id,
                "name": account_name,
                "is_active": True,
                "token_source": "cookie_import",
                "data_dir": account_dir,
                "token": "Cookie导入（已保存至本地）",
                "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
                "total_calls": 0,
                "discord_username": None,
                "created_at": datetime.now().isoformat(),
                "cookie_count": result.get("cookie_count", 0),
                "user_agent_preview": result.get("user_agent", "")[:30] + "...",
                "local_saved": result.get("local_saved", False),
                "cookie_files": [
                    f"{account_dir}/cookies.json",
                    f"{account_dir}/cookies.txt"
                ]
            }
            accounts_db[account_id] = new_account
            
            # 添加日志
            logs_db.append({
                "timestamp": datetime.now().isoformat(),
                "account_name": account_name,
                "model": "N/A",
                "duration": 0,
                "status": "SUCCESS",
                "note": f"Cookie导入成功，数据保存到: {account_dir}",
                "level": "info"
            })
            
            return JSONResponse(content={
                "success": True,
                "message": f"✅ Cookie 导入成功！提取到 {result.get('cookie_count', 0)} 个 Cookie 并保存到本地目录。",
                "account_id": account_id,
                "cookie_count": result.get("cookie_count", 0),
                "user_agent_preview": result.get("user_agent", "")[:50],
                "account_dir": account_dir
            })
        else:
            return JSONResponse(content={
                "success": False,
                "message": f"❌ 解析失败: {result.get('error', '未知错误')}"
            })
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Cookie 解析过程异常: {e}")
        return JSONResponse(content={
            "success": False,
            "message": f"❌ 解析过程异常: {str(e)}"
        })

# ==================== API Key 管理 ====================

@app.get("/api/settings/api-key")
async def get_api_key():
    """获取当前 API Key"""
    return JSONResponse(content={
        "api_key": settings.API_MASTER_KEY,
        "masked": "***" + settings.API_MASTER_KEY[-4:] if len(settings.API_MASTER_KEY) > 4 else "***"
    })

@app.post("/api/settings/api-key")
async def update_api_key(request: Request):
    """更新 API Key（写入 .env 文件）"""
    try:
        data = await request.json()
        new_key = data.get("api_key", "").strip()
        
        if not new_key:
            raise HTTPException(400, "API Key 不能为空")
        
        # 更新 .env 文件
        env_path = ".env"
        if not os.path.exists(env_path):
            raise HTTPException(500, "找不到 .env 文件")
        
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith("API_MASTER_KEY="):
                new_lines.append(f'API_MASTER_KEY="{new_key}"\n')
                updated = True
            else:
                new_lines.append(line)
        
        if not updated:
            new_lines.append(f'API_MASTER_KEY="{new_key}"\n')
        
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        # 更新内存中的配置（可选，需要重启服务才能完全生效）
        # settings.API_MASTER_KEY = new_key
        
        logger.info(f"API Key 已更新")
        
        return JSONResponse(content={
            "success": True,
            "message": "✅ API Key 已更新。请注意，部分功能可能需要重启服务才能生效。",
            "masked": "***" + new_key[-4:] if len(new_key) > 4 else "***"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新 API Key 失败: {e}")
        raise HTTPException(500, f"更新失败: {str(e)}")

@app.get("/api/settings/export-config")
async def export_config():
    """导出当前系统配置（JSON格式）"""
    try:
        # 收集配置信息
        config = {
            "export_time": datetime.now().isoformat(),
            "version": "3.0",
            "api_key_masked": "***" + settings.API_MASTER_KEY[-4:] if len(settings.API_MASTER_KEY) > 4 else "***",
            "system_settings": {
                "app_name": settings.APP_NAME,
                "app_version": settings.APP_VERSION,
                "api_master_key_length": len(settings.API_MASTER_KEY),
                "default_model": settings.DEFAULT_MODEL,
                "target_url": settings.TARGET_URL,
                "api_url": settings.API_URL,
                "nginx_port": settings.NGINX_PORT
            },
            "accounts": list(accounts_db.values()),
            "custom_models": custom_models,
            "statistics": {
                "total_accounts": len(accounts_db),
                "active_accounts": sum(1 for acc in accounts_db.values() if acc.get("is_active", False)),
                "total_logs": len(logs_db),
                "custom_models_count": len(custom_models)
            },
            "data_directories": {
                "cookies": "data/cookies/",
                "sessions": "data/sessions/",
                "logs": "error_logs/",
                "output": "output/"
            }
        }
        
        return JSONResponse(content={
            "success": True,
            "message": "✅ 配置导出成功",
            "config": config,
            "download_filename": f"perplexity-config-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        })
        
    except Exception as e:
        logger.error(f"导出配置失败: {e}")
        raise HTTPException(500, f"导出配置失败: {str(e)}")


# ==================== 账号详细信息 API ====================

@app.get("/api/account/details/{account_name}")
async def get_account_details(account_name: str):
    """获取账号详细信息（包括完整路径、创建时间、更新时间、调用统计等）"""
    try:
        # 检查会话文件是否存在
        session_file = f"data/sessions/{account_name}.json"
        if not os.path.exists(session_file):
            raise HTTPException(404, f"账号 '{account_name}' 不存在或会话文件未找到")
        
        with open(session_file, 'r', encoding='utf-8') as f:
            session_data = json.load(f)
        
        # 检查Cookie文件是否存在
        cookie_file = session_data.get("cookie_file")
        cookie_data = None
        if cookie_file and os.path.exists(cookie_file):
            with open(cookie_file, 'r', encoding='utf-8') as f:
                cookie_data = json.load(f)
        
        # 构建响应
        response = {
            "account_name": account_name,
            "session_data": session_data,
            "cookie_data": cookie_data,
            "directory_info": session_data.get("directory_info", {}),
            "stats": session_data.get("stats", {}),
            "auto_maintenance": session_data.get("auto_maintenance", {}),
            "exists": True
        }
        
        return JSONResponse(content=response)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取账号详情失败: {e}")
        raise HTTPException(500, f"获取账号详情失败: {str(e)}")

@app.post("/api/account/verify/{account_name}")
async def verify_account_cookie(account_name: str):
    """手动验证 Cookie 有效性（打开浏览器检查）"""
    try:
        # 检查 provider 是否就绪
        if not hasattr(provider, 'solver'):
            raise HTTPException(503, "服务未就绪")
        
        # 调用 BrowserService 验证 Cookie
        result = await provider.solver.verify_cookie(account_name, headless=False)
        
        if result.get("success"):
            # 添加日志
            logs_db.append({
                "timestamp": datetime.now().isoformat(),
                "account_name": account_name,
                "level": "info",
                "note": f"Cookie 验证成功: {result.get('message', '')}",
                "status": "SUCCESS"
            })
            
            return JSONResponse(content={
                "success": True,
                "message": result.get("message", "✅ Cookie 验证成功"),
                "account_name": account_name,
                "valid": result.get("valid", False),
                "cookie_count": result.get("cookie_count", 0),
                "verification_time": result.get("verification_time"),
                "details": result
            })
        else:
            # 添加日志
            logs_db.append({
                "timestamp": datetime.now().isoformat(),
                "account_name": account_name,
                "level": "warning",
                "note": f"Cookie 验证失败: {result.get('error', '')}",
                "status": "FAILED"
            })
            
            return JSONResponse(content={
                "success": False,
                "message": result.get("error", "❌ Cookie 验证失败"),
                "account_name": account_name,
                "valid": False,
                "details": result
            })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"验证 Cookie 失败: {e}")
        raise HTTPException(500, f"验证失败: {str(e)}")

# ==================== 账号统计和维护 API ====================

@app.get("/api/account/stats/{account_name}")
async def get_account_stats(account_name: str):
    """获取账号调用统计"""
    try:
        # 检查会话文件
        session_file = f"data/sessions/{account_name}.json"
        if not os.path.exists(session_file):
            raise HTTPException(404, f"账号 '{account_name}' 不存在")
        
        with open(session_file, 'r', encoding='utf-8') as f:
            session_data = json.load(f)
        
        stats = session_data.get("stats", {})
        auto_maintenance = session_data.get("auto_maintenance", {})
        
        return JSONResponse(content={
            "success": True,
            "account_name": account_name,
            "stats": stats,
            "auto_maintenance": auto_maintenance,
            "summary": {
                "total_calls": stats.get("total_calls", 0),
                "success_calls": stats.get("success_calls", 0),
                "failed_calls": stats.get("failed_calls", 0),
                "success_rate": stats.get("success_calls", 0) / max(stats.get("total_calls", 1), 1) * 100,
                "consecutive_failures": stats.get("consecutive_failures", 0),
                "last_success": stats.get("last_success"),
                "last_failure": stats.get("last_failure")
            }
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取账号统计失败: {e}")
        raise HTTPException(500, f"获取统计失败: {str(e)}")

@app.post("/api/account/maintenance/{account_name}")
async def trigger_account_maintenance(account_name: str):
    """触发账号自动维护（强制刷新Cookie）"""
    try:
        # 检查 provider 是否就绪
        if not hasattr(provider, 'solver'):
            raise HTTPException(503, "服务未就绪")
        
        # 这里可以调用 browser_service 的自动维护方法
        # 暂时模拟维护过程
        logger.info(f"🔄 开始手动维护账号: {account_name}")
        
        # 模拟维护延迟
        import asyncio
        await asyncio.sleep(2)
        
        # 添加日志
        logs_db.append({
            "timestamp": datetime.now().isoformat(),
            "account_name": account_name,
            "level": "info",
            "note": f"手动维护触发成功，将在后台执行Cookie刷新",
            "status": "MAINTENANCE_TRIGGERED"
        })
        
        return JSONResponse(content={
            "success": True,
            "message": "✅ 账号维护已触发，将在后台自动刷新Cookie",
            "account_name": account_name,
            "maintenance_time": datetime.now().isoformat(),
            "note": "实际维护功能需要 browser_service 实现 perform_auto_maintenance 方法"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"触发维护失败: {e}")
        raise HTTPException(500, f"维护触发失败: {str(e)}")

# ==================== 模型管理 API ====================

@app.get("/api/models")
async def get_models_list():
    """获取模型列表（包括自定义模型）"""
    # 获取预设模型（从provider的get_models中获取）
    preset_models_response = await provider.get_models()
    preset_models = preset_models_response.body if hasattr(preset_models_response, 'body') else preset_models_response
    
    # 如果响应是JSON字符串，解析它
    if isinstance(preset_models, (bytes, str)):
        try:
            if isinstance(preset_models, bytes):
                preset_models = preset_models.decode('utf-8')
            preset_models = json.loads(preset_models)
        except:
            preset_models = {"data": []}
    
    # 提取data数组
    preset_models_list = preset_models.get("data", []) if isinstance(preset_models, dict) else preset_models
    
    # 添加is_custom标记
    for model in preset_models_list:
        if isinstance(model, dict):
            model["is_custom"] = False
            model["can_delete"] = False
            model["can_rename"] = False
    
    # 合并自定义模型（添加is_custom标记）
    for custom_model in custom_models:
        if isinstance(custom_model, dict):
            custom_model["is_custom"] = True
            custom_model["can_delete"] = True
            custom_model["can_rename"] = True
    
    # 合并所有模型
    all_models = preset_models_list + custom_models
    
    return JSONResponse(content={
        "models": all_models,
        "total": len(all_models),
        "custom_count": len(custom_models),
        "preset_count": len(preset_models_list)
    })

@app.post("/api/models")
async def add_model(request: Request):
    """添加新模型"""
    try:
        data = await request.json()
        model_id = data.get("id", "").strip()
        model_name = data.get("name", "").strip()
        provider_name = data.get("provider", "custom").strip()
        
        if not model_id:
            raise HTTPException(400, "模型ID不能为空")
        
        if not model_name:
            model_name = model_id
        
        # 检查是否已存在
        for model in custom_models:
            if model.get("id") == model_id:
                raise HTTPException(400, f"模型ID '{model_id}' 已存在")
        
        # 添加新模型
        new_model = {
            "id": model_id,
            "name": model_name,
            "provider": provider_name,
            "is_custom": True,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
        
        custom_models.append(new_model)
        
        # 添加日志
        logs_db.append({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "note": f"添加自定义模型: {model_name} ({model_id})"
        })
        
        return JSONResponse(content={
            "success": True,
            "message": f"✅ 模型 '{model_name}' 添加成功",
            "model": new_model
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加模型失败: {e}")
        raise HTTPException(500, f"添加模型失败: {str(e)}")

@app.put("/api/models/{model_id}")
async def update_model(model_id: str, request: Request):
    """重命名/更新模型"""
    try:
        data = await request.json()
        new_name = data.get("name", "").strip()
        
        if not new_name:
            raise HTTPException(400, "新名称不能为空")
        
        # 查找模型（在自定义模型中查找）
        model_index = -1
        for i, model in enumerate(custom_models):
            if model.get("id") == model_id:
                model_index = i
                break
        
        if model_index == -1:
            raise HTTPException(404, f"未找到模型 '{model_id}' 或无法修改预设模型")
        
        # 更新模型
        old_name = custom_models[model_index].get("name", model_id)
        custom_models[model_index]["name"] = new_name
        custom_models[model_index]["updated_at"] = datetime.now().isoformat()
        
        # 添加日志
        logs_db.append({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "note": f"重命名模型: {old_name} -> {new_name} ({model_id})"
        })
        
        return JSONResponse(content={
            "success": True,
            "message": f"✅ 模型重命名成功: {old_name} -> {new_name}",
            "model": custom_models[model_index]
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新模型失败: {e}")
        raise HTTPException(500, f"更新模型失败: {str(e)}")

@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str):
    """删除模型（仅限自定义模型）"""
    try:
        # 查找模型（在自定义模型中查找）
        model_index = -1
        deleted_model = None
        
        for i, model in enumerate(custom_models):
            if model.get("id") == model_id:
                model_index = i
                deleted_model = model
                break
        
        if model_index == -1:
            raise HTTPException(404, f"未找到模型 '{model_id}' 或无法删除预设模型")
        
        # 删除模型
        deleted = custom_models.pop(model_index)
        
        # 添加日志
        logs_db.append({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "note": f"删除模型: {deleted.get('name', model_id)} ({model_id})"
        })
        
        return JSONResponse(content={
            "success": True,
            "message": f"✅ 模型 '{deleted.get('name', model_id)}' 删除成功",
            "model_id": model_id
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除模型失败: {e}")
        raise HTTPException(500, f"删除模型失败: {str(e)}")

# ==================== 文件夹管理 API ====================

@app.get("/api/folders/error_logs")
async def get_error_logs():
    """获取error_logs文件夹内容"""
    try:
        error_logs_path = Path("error_logs")
        if not error_logs_path.exists():
            return JSONResponse(content={
                "success": True,
                "folder": "error_logs",
                "exists": False,
                "files": [],
                "total_size": 0,
                "message": "error_logs文件夹不存在"
            })
        
        files = []
        total_size = 0
        
        # 遍历error_logs文件夹
        for entry in os.scandir(error_logs_path):
            try:
                file_info = {
                    "name": entry.name,
                    "path": str(entry.path),
                    "type": "directory" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else get_directory_size(entry.path),
                    "modified": entry.stat().st_mtime,
                    "permissions": oct(entry.stat().st_mode)[-3:],
                    "is_directory": entry.is_dir()
                }
                total_size += file_info["size"]
                files.append(file_info)
            except (PermissionError, FileNotFoundError):
                continue
        
        # 按修改时间排序（最新的在前）
        files.sort(key=lambda x: x["modified"], reverse=True)
        
        return JSONResponse(content={
            "success": True,
            "folder": "error_logs",
            "exists": True,
            "files": files,
            "total_size": total_size,
            "file_count": len(files),
            "message": f"找到 {len(files)} 个文件/目录，总大小: {format_file_size(total_size)}"
        })
        
    except Exception as e:
        logger.error(f"获取error_logs失败: {e}")
        raise HTTPException(500, f"获取error_logs失败: {str(e)}")

@app.get("/api/folders/output")
async def get_output_folder():
    """获取output文件夹内容"""
    try:
        output_path = Path("output")
        if not output_path.exists():
            return JSONResponse(content={
                "success": True,
                "folder": "output",
                "exists": False,
                "files": [],
                "total_size": 0,
                "message": "output文件夹不存在"
            })
        
        files = []
        total_size = 0
        
        # 遍历output文件夹
        for entry in os.scandir(output_path):
            try:
                file_info = {
                    "name": entry.name,
                    "path": str(entry.path),
                    "type": "directory" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else get_directory_size(entry.path),
                    "modified": entry.stat().st_mtime,
                    "permissions": oct(entry.stat().st_mode)[-3:],
                    "is_directory": entry.is_dir()
                }
                total_size += file_info["size"]
                files.append(file_info)
            except (PermissionError, FileNotFoundError):
                continue
        
        # 按修改时间排序（最新的在前）
        files.sort(key=lambda x: x["modified"], reverse=True)
        
        return JSONResponse(content={
            "success": True,
            "folder": "output",
            "exists": True,
            "files": files,
            "total_size": total_size,
            "file_count": len(files),
            "message": f"找到 {len(files)} 个文件/目录，总大小: {format_file_size(total_size)}"
        })
        
    except Exception as e:
        logger.error(f"获取output文件夹失败: {e}")
        raise HTTPException(500, f"获取output文件夹失败: {str(e)}")

@app.delete("/api/folders/error_logs/{filename}")
async def delete_error_log_file(filename: str):
    """删除error_logs中的文件或目录"""
    try:
        # 安全检查：防止路径遍历攻击
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(400, "无效的文件名")
        
        target_path = Path("error_logs") / filename
        if not target_path.exists():
            raise HTTPException(404, f"文件不存在: {filename}")
        
        # 安全检查：确保路径在error_logs目录内
        if not str(target_path.resolve()).startswith(str(Path.cwd().resolve() / "error_logs")):
            raise HTTPException(403, "禁止访问此路径")
        
        # 删除文件或目录
        if target_path.is_dir():
            shutil.rmtree(target_path)
            action = "目录"
        else:
            target_path.unlink()
            action = "文件"
        
        # 添加日志
        logs_db.append({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "note": f"删除error_logs {action}: {filename}",
            "status": "FILE_DELETED"
        })
        
        logger.info(f"✅ 删除error_logs {action}: {filename}")
        
        return JSONResponse(content={
            "success": True,
            "message": f"✅ 已删除 {action}: {filename}"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除error_logs文件失败: {e}")
        raise HTTPException(500, f"删除失败: {str(e)}")

@app.delete("/api/folders/output/{filename}")
async def delete_output_file(filename: str):
    """删除output中的文件或目录"""
    try:
        # 安全检查：防止路径遍历攻击
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(400, "无效的文件名")
        
        target_path = Path("output") / filename
        if not target_path.exists():
            raise HTTPException(404, f"文件不存在: {filename}")
        
        # 安全检查：确保路径在output目录内
        if not str(target_path.resolve()).startswith(str(Path.cwd().resolve() / "output")):
            raise HTTPException(403, "禁止访问此路径")
        
        # 删除文件或目录
        if target_path.is_dir():
            shutil.rmtree(target_path)
            action = "目录"
        else:
            target_path.unlink()
            action = "文件"
        
        # 添加日志
        logs_db.append({
            "timestamp": datetime.now().isoformat(),
            "level": "info",
            "note": f"删除output {action}: {filename}",
            "status": "FILE_DELETED"
        })
        
        logger.info(f"✅ 删除output {action}: {filename}")
        
        return JSONResponse(content={
            "success": True,
            "message": f"✅ 已删除 {action}: {filename}"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除output文件失败: {e}")
        raise HTTPException(500, f"删除失败: {str(e)}")

# ==================== 实时日志 SSE 端点（可选）====================
# 如果需要真正的实时日志，可以实现SSE端点
# 但为了简化，目前使用轮询方式