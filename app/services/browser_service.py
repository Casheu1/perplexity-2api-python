import logging
import asyncio
import os
import time
import json
from typing import Dict, Any, List
from botasaurus.browser_decorator import browser
from app.core.config import settings

logger = logging.getLogger(__name__)

# Botasaurus 浏览器配置
BROWSER_OPTIONS = {
    "headless": True,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "window_size": (1366, 768)
}

# 交互式登录配置（显示浏览器窗口）
INTERACTIVE_BROWSER_OPTIONS = {
    "headless": False,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "window_size": (1280, 800)
}

class BrowserService:
    def __init__(self):
        self.cached_cookies: Dict[str, str] = {}
        self.cached_user_agent: str = settings.PPLX_USER_AGENT
        self.last_refresh_time = 0
        self.refresh_interval = 300  # 5分钟内不重复刷新

    async def initialize_session(self):
        """初始化：优先扫描本地保存的 Cookie 文件，其次尝试 .env 文件"""
        logger.info("🚀 正在初始化浏览器服务 (Botasaurus)...")
        try:
            # 1. 优先扫描本地 data/cookies/ 目录下的 Cookie 文件
            local_cookies_found = False
            cookies_dir = os.path.join("data", "cookies")
            
            if os.path.exists(cookies_dir):
                # 查找所有子目录中的 cookies.json 文件
                cookie_files = []
                for account_dir in os.listdir(cookies_dir):
                    account_path = os.path.join(cookies_dir, account_dir)
                    if os.path.isdir(account_path):
                        cookie_file = os.path.join(account_path, "cookies.json")
                        if os.path.exists(cookie_file):
                            # 获取文件修改时间用于排序
                            mtime = os.path.getmtime(cookie_file)
                            cookie_files.append((mtime, cookie_file, account_dir))
                
                # 按修改时间排序（最新的优先）
                cookie_files.sort(reverse=True)
                
                if cookie_files:
                    # 加载最新的 Cookie 文件
                    mtime, cookie_file, account_dir = cookie_files[0]
                    try:
                        with open(cookie_file, 'r', encoding='utf-8') as f:
                            cookie_data = json.load(f)
                        
                        cookies_dict = cookie_data.get("cookies", {})
                        user_agent = cookie_data.get("user_agent", self.cached_user_agent)
                        
                        if cookies_dict:
                            # 清理 Cookie 键名：使用正则表达式提取有效的 cookie 名称
                            cleaned_cookies = {}
                            import re
                            # 有效的 cookie 名称模式：字母、数字、点、连字符、下划线
                            cookie_name_pattern = r'[a-zA-Z0-9._-]+'
                            for key, value in cookies_dict.items():
                                # 查找所有可能的 cookie 名称匹配
                                matches = re.findall(cookie_name_pattern, key)
                                if matches:
                                    # 选择最长的匹配（通常是最完整的 cookie 名称）
                                    cleaned_key = max(matches, key=len)
                                    # 特殊处理：确保关键 cookie 名称标准化
                                    if "pplx.visitor-id" in cleaned_key.lower():
                                        cleaned_key = "pplx.visitor-id"
                                    elif "session-token" in cleaned_key.lower():
                                        cleaned_key = "__Secure-next-auth.session-token"
                                    elif "cf_clearance" in cleaned_key.lower():
                                        cleaned_key = "cf_clearance"
                                else:
                                    # 如果没有找到有效匹配，使用原始键名并清理转义字符
                                    cleaned_key = key.replace('^\"', '').replace('\"', '').strip()
                                cleaned_cookies[cleaned_key] = value
                                # 调试日志：显示清理前后的键名
                                if key != cleaned_key:
                                    logger.debug(f"Cookie 键名清理: '{key}' -> '{cleaned_key}'")
                            
                            self.cached_cookies = cleaned_cookies
                            self.cached_user_agent = user_agent
                            self.last_refresh_time = time.time()  # 设置最后刷新时间，避免立即触发刷新
                            local_cookies_found = True
                            logger.info(f"📦 从本地目录加载了 {len(self.cached_cookies)} 个 Cookie (账号: {account_dir})")
                            logger.debug(f"Cookie 键名: {list(self.cached_cookies.keys())}")
                    except Exception as e:
                        logger.warning(f"⚠️ 加载本地 Cookie 文件失败，跳过: {e}")
            
            # 2. 如果未找到本地 Cookie，尝试从 .env 文件加载
            if not local_cookies_found:
                initial_cookies_list = settings.get_initial_cookies_dict()
                if initial_cookies_list:
                    self.cached_cookies = {c["name"]: c["value"] for c in initial_cookies_list}
                    logger.info(f"📦 从 .env 加载了 {len(self.cached_cookies)} 个初始 Cookie")
                    
                    # 尝试预热（非强制，失败不影响启动）
                    try:
                        await self.refresh_context(force=True)
                    except Exception as e:
                        logger.warning(f"⚠️ 初始预热失败，但不影响服务启动: {e}")
                        logger.info("💡 请通过 Web UI 添加有效的账号 Cookie")
                else:
                    logger.info("ℹ️ 未找到初始 Cookie，服务已正常启动")
                    logger.info("💡 请通过 Web UI 添加账号或导入 Cookie 以启用 API 功能")
                    # 设置空缓存，等待用户添加
                    self.cached_cookies = {}
            else:
                # 本地 Cookie 加载成功，记录日志
                logger.info("✅ 本地 Cookie 加载成功，API 功能已启用")
                
        except Exception as e:
            logger.error(f"❌ 初始化过程中出现意外错误: {e}")
            logger.info("💡 服务将继续启动，但请通过 Web UI 添加账号")

    @browser(**BROWSER_OPTIONS)
    @staticmethod
    def _refresh_cookies_with_browser(driver, data) -> Dict[str, str]:
        """
        Botasaurus 核心函数：访问页面，处理验证，返回最新 Cookie
        data参数：可以是初始Cookie字典，或包含cookies和user_agent的字典
        """
        # 处理两种数据格式
        if isinstance(data, dict) and "cookies" in data:
            # 新格式：包含cookies和user_agent的字典
            initial_cookies = data.get("cookies", {})
            user_agent = data.get("user_agent")
        else:
            # 旧格式：直接的cookie字典
            initial_cookies = data
            user_agent = None
        
        # User-Agent already set in browser options, skip runtime setting
        
        # 如果有初始 Cookie，先设置（添加必要的字段）
        if initial_cookies:
            logger.info(f"尝试设置 {len(initial_cookies)} 个初始 Cookie")
            # 创建完整的 Cookie 对象，包含 Botasaurus 需要的所有字段
            cookies_list = []
            for name, value in initial_cookies.items():
                cookie_obj = {
                    "name": name,
                    "value": value,
                    "domain": ".perplexity.ai",  # 使用根域，让子域也能访问
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax"
                }
                cookies_list.append(cookie_obj)
            
            try:
                driver.add_cookies(cookies_list)
                logger.debug(f"✅ 成功设置 {len(cookies_list)} 个初始 Cookie")
                logger.debug(f"Cookie 名称: {list(initial_cookies.keys())}")
            except Exception as e:
                logger.warning(f"⚠️ 设置初始 Cookie 失败: {e}")
                logger.info("💡 Botasaurus 将尝试自行获取 Cookie")

        # 访问目标页面（使用 google_get 和 bypass_cloudflare 更好地处理 Cloudflare 验证）
        driver.google_get(settings.TARGET_URL, bypass_cloudflare=True)
        
        # 等待页面加载完成（使用sleep等待）
        driver.sleep(5)
        
        # 检查是否还在验证页面（更全面的检查）
        title = driver.title
        current_url = driver.current_url
        logger.debug(f"页面标题: {title}, URL: {current_url}")
        
        # 检查多个Cloudflare标志：标题、URL、页面内容
        is_cloudflare = (
            "Just a moment" in title or 
            "Cloudflare" in title or 
            "cloudflare" in current_url.lower() or
            "challenge" in current_url.lower() or
            "verify" in current_url.lower()
        )
        
        if is_cloudflare:
            logger.warning("⚠️ 检测到 Cloudflare 验证页面，Botasaurus 可能正在处理...")
            
            # 尝试通过页面内容进一步确认
            try:
                page_text = driver.run_js("return document.body.innerText || ''")
                if "cloudflare" in page_text.lower() or "ddos" in page_text.lower() or "verifying" in page_text.lower():
                    logger.warning("⚠️ 页面内容确认是 Cloudflare 验证页面")
            except:
                pass
            
            # 等待额外时间让验证完成（可能是自动或需要手动）
            driver.sleep(15)
            
            # 再次检查
            title = driver.title
            current_url = driver.current_url
            is_still_cloudflare = (
                "Just a moment" in title or 
                "Cloudflare" in title or 
                "cloudflare" in current_url.lower()
            )
            
            if is_still_cloudflare:
                logger.error("❌ 仍然在 Cloudflare 验证页面，尝试不同的策略...")
                
                # 策略1：刷新页面
                driver.reload()
                driver.sleep(10)
                
                # 再次检查
                title = driver.title
                if "Just a moment" in title or "Cloudflare" in title:
                    logger.error("❌ 刷新后仍然在验证页面，尝试访问不同URL...")
                    
                    # 策略2：尝试直接访问登录页面而不是首页
                    driver.get("https://www.perplexity.ai/login")
                    driver.sleep(10)
                    
                    # 最后一次检查
                    title = driver.title
                    if "Just a moment" in title or "Cloudflare" in title:
                        logger.error("❌ 所有策略都失败，Cloudflare 验证可能无法自动绕过")
                        # 继续执行，让用户手动处理或返回错误
        
        # 获取所有 Cookie（优先使用 get_cookies_dict）
        cookies_dict = {}
        try:
            cookies_dict = driver.get_cookies_dict()
            logger.debug(f"使用 get_cookies_dict 获取到 {len(cookies_dict)} 个 Cookie")
        except AttributeError:
            try:
                cookies = driver.get_cookies()
                cookies_dict = {c["name"]: c["value"] for c in cookies}
                logger.debug(f"使用 get_cookies 获取到 {len(cookies_dict)} 个 Cookie")
            except AttributeError:
                # 最后尝试通过JavaScript获取
                cookie_str = driver.run_js("return document.cookie")
                if cookie_str:
                    cookies_dict = {pair.split("=")[0]: "=".join(pair.split("=")[1:]) for pair in cookie_str.split("; ") if pair}
                    logger.debug(f"使用 JavaScript 获取到 {len(cookies_dict)} 个 Cookie")
                else:
                    logger.debug("未获取到任何 Cookie")
        
        # 记录所有 Cookie 键以便调试
        logger.debug(f"Cookie 键: {list(cookies_dict.keys())}")
        
        # 检查关键 Cookie
        if "pplx.visitor-id" not in cookies_dict:
            raise Exception("❌ 未找到关键 Cookie pplx.visitor-id")
        
        logger.info(f"✅ Botasaurus 成功获取 {len(cookies_dict)} 个 Cookie")
        return cookies_dict

    def _update_env_file(self, new_cookies: Dict[str, str]):
        """
        [持久化] 将最新的 Cookie 写回 .env 文件
        """
        try:
            # 构造 Cookie 字符串
            cookie_str = "; ".join([f"{k}={v}" for k, v in new_cookies.items()])
            env_path = ".env"
            
            if not os.path.exists(env_path):
                return

            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            new_lines = []
            updated = False
            for line in lines:
                if line.startswith("PPLX_COOKIE="):
                    new_lines.append(f'PPLX_COOKIE="{cookie_str}"\n')
                    updated = True
                else:
                    new_lines.append(line)
            
            if not updated:
                new_lines.append(f'PPLX_COOKIE="{cookie_str}"\n')

            with open(env_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            
            logger.info("💾 最新 Cookie 已自动保存到 .env 文件 (持久化成功)")
            
        except Exception as e:
            logger.error(f"❌ 保存 Cookie 到文件失败: {e}")

    async def refresh_context(self, force=False):
        """
        使用 Botasaurus 启动浏览器，访问页面，自动过盾，更新 Cookie
        """
        if not force and (time.time() - self.last_refresh_time < self.refresh_interval) and self.cached_cookies:
            return True

        logger.info("🔄 启动 Botasaurus 浏览器进行会话保活/续期...")
        
        try:
            # 准备数据：包含初始Cookie和User-Agent
            data = {
                "cookies": self.cached_cookies,
                "user_agent": self.cached_user_agent
            }
            
            # Botasaurus 是同步的，在异步环境中使用线程池运行
            new_cookies = await asyncio.to_thread(
                self.__class__._refresh_cookies_with_browser, 
                data
            )
            
            # 检查Botasaurus是否返回了有效结果
            if new_cookies is None:
                logger.error("❌ Botasaurus 返回了 None（可能在调试模式或遇到验证问题）")
                return False
            
            if not isinstance(new_cookies, dict):
                logger.error(f"❌ Botasaurus 返回了非字典类型: {type(new_cookies)}")
                return False
            
            # 更新缓存
            self.cached_cookies = new_cookies
            self.last_refresh_time = time.time()
            logger.info(f"✅ Cookie 刷新成功! 数量: {len(self.cached_cookies)}")
            
            # 自动写回文件
            self._update_env_file(new_cookies)
            
            return True
            
        except Exception as e:
            logger.error(f"❌ 浏览器操作异常: {e}")
            return False

    def get_headers(self) -> Dict[str, str]:
        import re
        
        # 从 User-Agent 中提取 Chrome 版本
        chrome_version = "142"  # 默认值
        if self.cached_user_agent:
            match = re.search(r'Chrome/(\d+)\.', self.cached_user_agent)
            if match:
                chrome_version = match.group(1)
        
        # 清理 User-Agent：移除可能的转义字符和多余字符
        user_agent = self.cached_user_agent
        if user_agent:
            # 移除末尾可能存在的 ^ 或其他转义字符
            user_agent = user_agent.rstrip('^" ').replace('^\"', '').replace('\"', '')
        
        return {
            "Host": "www.perplexity.ai",
            "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/event-stream",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Content-Type": "application/json",
            "Origin": settings.TARGET_URL,
            "Referer": f"{settings.TARGET_URL}/search/new",
            "Priority": "u=1, i",
            "sec-ch-ua": f'"Google Chrome";v="{chrome_version}", "Chromium";v="{chrome_version}", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-perplexity-request-reason": "perplexity-query-state-provider"
        }

    def _update_env_with_cookies_and_ua(self, cookies: Dict[str, str], user_agent: str = None):
        """
        同时更新 .env 文件中的 Cookie 和 User-Agent
        """
        try:
            env_path = ".env"
            if not os.path.exists(env_path):
                return

            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            new_lines = []
            cookie_updated = False
            ua_updated = False
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            ua = user_agent or self.cached_user_agent
            
            for line in lines:
                if line.startswith("PPLX_COOKIE="):
                    new_lines.append(f'PPLX_COOKIE="{cookie_str}"\n')
                    cookie_updated = True
                elif line.startswith("PPLX_USER_AGENT="):
                    new_lines.append(f'PPLX_USER_AGENT="{ua}"\n')
                    ua_updated = True
                else:
                    new_lines.append(line)
            
            if not cookie_updated:
                new_lines.append(f'PPLX_COOKIE="{cookie_str}"\n')
            if not ua_updated:
                new_lines.append(f'PPLX_USER_AGENT="{ua}"\n')

            with open(env_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            
            logger.info("💾 Cookie 和 User-Agent 已保存到 .env 文件")
            
        except Exception as e:
            logger.error(f"❌ 保存到 .env 文件失败: {e}")

    def _save_account_data(self, account_name: str, cookies: Dict[str, str], user_agent: str = None, 
                           is_update: bool = False, source: str = "manual") -> str:
        """
        将账号数据保存到本地目录（data/cookies/和data/sessions/）
        增强版本：包含调用统计、时间戳和账号状态信息
        
        Args:
            account_name: 账号名称
            cookies: Cookie字典
            user_agent: User-Agent字符串
            is_update: 是否为更新操作（False表示新建）
            source: 数据来源（"manual", "import", "browser", "auto_refresh"）
        
        Returns:
            账号目录路径，失败返回None
        """
        try:
            # 创建账号目录
            account_dir = os.path.join("data", "cookies", account_name)
            os.makedirs(account_dir, exist_ok=True)
            
            # 保存Cookie到JSON文件
            cookie_file = os.path.join(account_dir, "cookies.json")
            cookie_data = {
                "account_name": account_name,
                "cookies": cookies,
                "user_agent": user_agent or self.cached_user_agent,
                "saved_at": time.time(),
                "cookie_count": len(cookies),
                "version": "2.0"  # 新版本标记
            }
            with open(cookie_file, 'w', encoding='utf-8') as f:
                json.dump(cookie_data, f, indent=2, ensure_ascii=False)
            
            # 保存Cookie为文本格式（兼容原有格式）
            cookie_txt_file = os.path.join(account_dir, "cookies.txt")
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            with open(cookie_txt_file, 'w', encoding='utf-8') as f:
                f.write(f"# {account_name} 的 Cookie\n")
                f.write(f"# 保存时间: {time.ctime()}\n")
                f.write(f"# User-Agent: {user_agent or self.cached_user_agent}\n")
                f.write(f"# 来源: {source}\n\n")
                f.write(cookie_str)
            
            # 保存会话信息（增强版）
            session_file = os.path.join("data", "sessions", f"{account_name}.json")
            
            # 如果是更新，尝试读取现有会话信息以保持统计
            session_data = {
                "account_name": account_name,
                "created_at": time.time() if not is_update else self._get_session_value(session_file, "created_at", time.time()),
                "updated_at": time.time(),
                "last_login": time.time(),
                "last_used": None,  # 最后调用时间
                "cookie_file": cookie_file,
                "status": "active",
                "source": source,
                "stats": {
                    "total_calls": self._get_session_value(session_file, "stats.total_calls", 0),
                    "success_calls": self._get_session_value(session_file, "stats.success_calls", 0),
                    "failed_calls": self._get_session_value(session_file, "stats.failed_calls", 0),
                    "consecutive_failures": self._get_session_value(session_file, "stats.consecutive_failures", 0),
                    "last_success": self._get_session_value(session_file, "stats.last_success", None),
                    "last_failure": self._get_session_value(session_file, "stats.last_failure", None)
                },
                "auto_maintenance": {
                    "enabled": True,
                    "last_check": None,
                    "failure_count": 0,
                    "next_check": time.time() + 3600  # 1小时后检查
                },
                "directory_info": {
                    "account_dir": account_dir,
                    "cookie_json": cookie_file,
                    "cookie_txt": cookie_txt_file,
                    "session_file": session_file
                },
                "version": "2.0"
            }
            
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(session_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"💾 账号数据已保存到本地目录: {account_dir} (来源: {source})")
            return account_dir
            
        except Exception as e:
            logger.error(f"❌ 保存账号数据失败: {e}")
            return None
    
    def _get_session_value(self, session_file: str, key_path: str, default_value: Any) -> Any:
        """
        从会话文件中读取指定键的值
        
        Args:
            session_file: 会话文件路径
            key_path: 键路径，如 "stats.total_calls"
            default_value: 默认值
        
        Returns:
            读取到的值或默认值
        """
        if not os.path.exists(session_file):
            return default_value
        
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 支持嵌套键路径
            keys = key_path.split('.')
            value = data
            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return default_value
            return value
        except Exception:
            return default_value

    @browser(**INTERACTIVE_BROWSER_OPTIONS)
    @staticmethod
    def _interactive_login_with_browser(driver, data) -> Dict[str, Any]:
        """
        交互式登录：打开浏览器窗口，让用户手动登录，返回 Cookie 和 User-Agent
        data: 包含 account_name 的字典
        """
        account_name = data.get("account_name", "新账号")
        logger.info(f"🔄 启动交互式登录流程: {account_name}")
        
        # 导航到 Perplexity 首页（登录页面），使用 google_get 和 bypass_cloudflare 处理 Cloudflare 验证
        driver.google_get("https://www.perplexity.ai", bypass_cloudflare=True)
        
        # 等待页面加载并检查 Cloudflare 验证状态
        driver.sleep(5)
        
        # 检查是否还在验证页面
        title = driver.title
        current_url = driver.current_url
        logger.debug(f"页面标题: {title}, URL: {current_url}")
        
        if "Just a moment" in title or "Cloudflare" in title or "cloudflare" in current_url:
            logger.warning("⚠️ 检测到 Cloudflare 验证页面，需要手动处理...")
            
            # 使用 driver.prompt() 暂停执行，让用户手动完成验证
            # 这会在控制台显示提示，等待用户按 Enter 继续
            prompt_message = (
                f"⚠️ 检测到 Cloudflare 验证页面！\n\n"
                f"账号: {account_name}\n"
                f"当前页面: {current_url}\n\n"
                f"请在浏览器窗口中手动完成 Cloudflare 验证：\n"
                f"1. 如果需要，点击验证按钮\n"
                f"2. 等待页面跳转到 Perplexity\n"
                f"3. 验证完成后，按 Enter 键继续登录流程\n\n"
                f"按 Enter 键继续..."
            )
            
            try:
                driver.prompt(prompt_message)
                logger.info("✅ 用户已确认完成 Cloudflare 验证")
                
                # 验证后等待页面稳定
                driver.sleep(5)
                
                # 检查是否仍然在验证页面
                title = driver.title
                current_url = driver.current_url
                if "Just a moment" in title or "Cloudflare" in title:
                    logger.warning("⚠️ 验证后仍然在 Cloudflare 页面，尝试刷新...")
                    driver.reload()
                    driver.sleep(8)
            except Exception as e:
                logger.warning(f"⚠️ driver.prompt() 失败（可能是非交互模式），继续执行: {e}")
                # 如果 prompt 失败，等待自动验证
                driver.sleep(15)
        
        # 显示登录提示信息
        alert_message = f"请登录您的 Perplexity 账户\\n\\n账号: {account_name}\\n\\n登录完成后，请保持页面打开并点击确定按钮。"
        driver.run_js(f"alert('{alert_message}');")
        
        # 等待用户关闭 alert 并登录
        driver.sleep(15)  # 给用户时间关闭弹窗并开始登录
        
        logger.info("⏳ 等待用户登录...")
        
        # 检查是否登录成功（查找关键 Cookie）
        for i in range(40):  # 最多等待 40*3 = 120秒（2分钟）
            # 获取所有 Cookie（优先使用 get_cookies_dict）
            cookies_dict = {}
            try:
                cookies_dict = driver.get_cookies_dict()
                logger.debug(f"使用 get_cookies_dict 获取到 {len(cookies_dict)} 个 Cookie")
            except AttributeError:
                try:
                    cookies = driver.get_cookies()
                    cookies_dict = {c["name"]: c["value"] for c in cookies}
                    logger.debug(f"使用 get_cookies 获取到 {len(cookies_dict)} 个 Cookie")
                except AttributeError:
                    # 最后尝试通过JavaScript获取
                    cookie_str = driver.run_js("return document.cookie")
                    if cookie_str:
                        cookies_dict = {pair.split("=")[0]: "=".join(pair.split("=")[1:]) for pair in cookie_str.split("; ") if pair}
                        logger.debug(f"使用 JavaScript 获取到 {len(cookies_dict)} 个 Cookie")
                    else:
                        logger.debug("未获取到任何 Cookie")
            
            # 记录所有 Cookie 键以便调试
            logger.debug(f"Cookie 键: {list(cookies_dict.keys())}")
            
            # 检查关键 Cookie（Perplexity 使用 pplx.visitor-id 和 session-token）
            if "pplx.visitor-id" in cookies_dict:
                logger.info(f"✅ 登录成功！获取到 {len(cookies_dict)} 个 Cookie")
                
                # 获取当前 User-Agent
                user_agent = driver.user_agent
                
                # 显示成功提示
                driver.run_js("alert('✅ 登录成功！Cookie 已捕获。\\n\\n现在可以关闭浏览器窗口。');")
                driver.sleep(3)  # 让用户看到提示
                
                return {
                    "cookies": cookies_dict,
                    "user_agent": user_agent,
                    "account_name": account_name,
                    "success": True,
                    "cookie_count": len(cookies_dict)
                }
            
            # 每3秒检查一次
            driver.sleep(3)
            
            # 每10次检查显示一次状态
            if i % 10 == 0:
                remaining = 40 - i
                logger.info(f"⏳ 等待登录... 剩余时间: {remaining*3}秒")
        
        # 超时，登录失败
        driver.run_js("alert('❌ 登录超时，未检测到有效 Cookie。\\n\\n请确保已成功登录 Perplexity 账户。');")
        driver.sleep(5)
        raise Exception("❌ 登录超时，未检测到有效 Cookie。请确保已成功登录。")

    async def interactive_login(self, account_name: str = "新账号") -> Dict[str, Any]:
        """
        异步包装：执行交互式登录并更新配置
        """
        logger.info(f"🚀 开始交互式登录: {account_name}")
        
        try:
            # 在单独的线程中运行 Botasaurus 同步函数
            result = await asyncio.to_thread(
                self.__class__._interactive_login_with_browser,
                {"account_name": account_name}
            )
            
            if result.get("success"):
                # 更新缓存
                self.cached_cookies = result["cookies"]
                self.cached_user_agent = result["user_agent"]
                self.last_refresh_time = time.time()
                
                # 保存到 .env 文件（全局配置）
                self._update_env_with_cookies_and_ua(
                    result["cookies"], 
                    result["user_agent"]
                )
                
                # 保存到本地目录（账号特定数据）
                account_dir = self._save_account_data(
                    account_name,
                    result["cookies"],
                    result["user_agent"],
                    source="browser"
                )
                
                # 更新返回结果
                result["account_dir"] = account_dir
                result["local_saved"] = account_dir is not None
                
                logger.info(f"✅ 交互式登录完成！账号: {account_name}, 数据目录: {account_dir}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ 交互式登录失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "account_name": account_name
            }

    def get_cookies(self) -> Dict[str, str]:
        return self.cached_cookies

    def parse_cookie_string(self, text: str, account_name: str = "导入的账号") -> Dict[str, Any]:
        """
        从任意文本中提取 Cookie 和 User-Agent（类似 config_wizard.py）
        支持格式：HAR JSON、PowerShell、cURL、纯文本 Cookie 字符串
        """
        import re
        import json
        
        logger.info(f"🔍 开始解析 Cookie 字符串，账号: {account_name}")
        
        cookie_str = ""
        user_agent = ""
        text = text.strip()
        
        # 1. 尝试 JSON 解析（HAR 格式）
        if text.startswith('{') or text.startswith('['):
            try:
                data = json.loads(text)
                # 递归搜索 Cookie 和 User-Agent
                def search_json(obj, path=""):
                    nonlocal cookie_str, user_agent
                    if isinstance(obj, dict):
                        for key, value in obj.items():
                            if isinstance(key, str) and key.lower() == 'cookie' and isinstance(value, str):
                                cookie_str = value
                            elif isinstance(key, str) and 'user-agent' in key.lower() and isinstance(value, str):
                                user_agent = value
                            elif isinstance(value, (dict, list)):
                                search_json(value, f"{path}.{key}")
                    elif isinstance(obj, list):
                        for item in obj:
                            search_json(item, path)
                
                search_json(data)
            except:
                pass  # 不是有效的 JSON
        
        # 2. 如果还没找到，尝试 PowerShell 格式
        if not cookie_str:
            pattern = r'New-Object System\.Net\.Cookie\("([^"]+)",\s*"([^"]+)"'
            matches = re.findall(pattern, text)
            if matches:
                cookie_parts = []
                for key, value in matches:
                    cookie_parts.append(f"{key}={value}")
                cookie_str = "; ".join(cookie_parts)
        
        # 3. 如果还没找到，尝试通用正则（key=value 格式）
        if not cookie_str:
            # 寻找包含 pplx.visitor-id 的行
            lines = text.splitlines()
            for line in lines:
                if "pplx.visitor-id" in line and "=" in line:
                    if "Cookie:" in line:
                        cookie_str = line.split("Cookie:", 1)[1].strip()
                    elif ";" in line and "=" in line:
                        cookie_str = line.strip()
                    break
        
        # 4. 尝试直接解析为 Cookie 字符串（可能用户直接粘贴了 Cookie）
        if not cookie_str and "=" in text and ";" in text:
            # 检查是否看起来像 Cookie 字符串
            cookie_candidates = re.findall(r'([^=;]+=[^=;]+)(?:;|$)', text)
            if cookie_candidates and len(cookie_candidates) > 1:
                cookie_str = "; ".join(cookie_candidates)
        
        # 5. 提取 User-Agent
        if not user_agent:
            ua_match = re.search(r'User-Agent["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', text, re.IGNORECASE)
            if ua_match:
                user_agent = ua_match.group(1).strip()
        
        # 6. 如果还是没有 User-Agent，使用默认值
        if not user_agent:
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        
        # 7. 处理结果
        if cookie_str:
            # 解析 Cookie 字符串为字典
            cookies_dict = {}
            for pair in cookie_str.split(';'):
                pair = pair.strip()
                if '=' in pair:
                    key, value = pair.split('=', 1)
                    cookies_dict[key.strip()] = value.strip()
            
            logger.info(f"✅ 解析成功！提取到 {len(cookies_dict)} 个 Cookie")
            
            # 保存账号数据
            account_dir = self._save_account_data(account_name, cookies_dict, user_agent, source="import")
            
            return {
                "success": True,
                "account_name": account_name,
                "cookie_count": len(cookies_dict),
                "user_agent": user_agent,
                "cookies_dict": cookies_dict,
                "account_dir": account_dir,
                "local_saved": account_dir is not None
            }
        else:
            logger.warning("❌ 未能从文本中提取到有效的 Cookie")
            return {
                "success": False,
                "error": "未能从文本中提取到有效的 Cookie。请确保内容包含 'pplx.visitor-id' 或完整的 Cookie 字符串。",
                "account_name": account_name
            }

    def get_account_session(self, account_name: str) -> Dict[str, Any]:
        """
        获取账号会话数据
        """
        session_file = os.path.join("data", "sessions", f"{account_name}.json")
        if not os.path.exists(session_file):
            return None
        
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取会话文件失败: {e}")
            return None

    async def verify_cookie(self, account_name: str, headless: bool = True) -> Dict[str, Any]:
        """
        验证 Cookie 有效性（可选是否显示浏览器）
        
        Args:
            account_name: 账号名称
            headless: 是否使用无头模式（True为后台验证，False为显示浏览器）
        
        Returns:
            验证结果字典
        """
        logger.info(f"🔍 开始验证 Cookie 有效性，账号: {account_name}")
        
        # 获取会话数据
        session_data = self.get_account_session(account_name)
        if not session_data:
            return {
                "success": False,
                "valid": False,
                "error": "账号会话数据不存在",
                "account_name": account_name
            }
        
        cookie_file = session_data.get("cookie_file")
        if not cookie_file or not os.path.exists(cookie_file):
            return {
                "success": False,
                "valid": False,
                "error": "Cookie 文件不存在",
                "account_name": account_name
            }
        
        try:
            with open(cookie_file, 'r', encoding='utf-8') as f:
                cookie_data = json.load(f)
        except Exception as e:
            logger.error(f"读取 Cookie 文件失败: {e}")
            return {
                "success": False,
                "valid": False,
                "error": f"读取 Cookie 文件失败: {e}",
                "account_name": account_name
            }
        
        cookies = cookie_data.get("cookies", {})
        user_agent = cookie_data.get("user_agent", self.cached_user_agent)
        
        if not cookies:
            return {
                "success": False,
                "valid": False,
                "error": "Cookie 数据为空",
                "account_name": account_name
            }
        
        # 准备验证数据
        data = {
            "cookies": cookies,
            "user_agent": user_agent,
            "account_name": account_name
        }
        
        try:
            # 使用 Botasaurus 验证 Cookie
            # 注意：这里使用 _refresh_cookies_with_browser，但仅用于验证
            # 我们传入现有 Cookie，检查是否能正常访问
            result = await asyncio.to_thread(
                self.__class__._refresh_cookies_with_browser,
                data
            )
            
            # 如果成功返回 Cookie 字典，说明验证通过
            if result and isinstance(result, dict) and "pplx.visitor-id" in result:
                # 更新会话数据中的最后验证时间
                session_data["last_verification"] = time.time()
                session_data["verification_status"] = "valid"
                
                # 保存更新后的会话数据
                session_file = os.path.join("data", "sessions", f"{account_name}.json")
                with open(session_file, 'w', encoding='utf-8') as f:
                    json.dump(session_data, f, indent=2, ensure_ascii=False)
                
                return {
                    "success": True,
                    "valid": True,
                    "account_name": account_name,
                    "cookie_count": len(result),
                    "message": "✅ Cookie 验证通过！",
                    "verification_time": time.time()
                }
            else:
                return {
                    "success": False,
                    "valid": False,
                    "account_name": account_name,
                    "error": "Cookie 验证失败：未获取到有效 Cookie",
                    "verification_time": time.time()
                }
                
        except Exception as e:
            logger.error(f"Cookie 验证过程异常: {e}")
            return {
                "success": False,
                "valid": False,
                "account_name": account_name,
                "error": f"验证过程异常: {str(e)}",
                "verification_time": time.time()
            }