import base64
import json
import difflib
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict, List, Any
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api import message_components as Comp
from astrbot.api import llm_tool
from .steam_api import SteamAPI
from .utils.browser import render_html_to_image
from .utils.env_manager import EnvManager
from jinja2 import Template


@register(
    "steam_game",
    "bvzrays",
    "Steam Player Data Visualization",
    "1.6.0",
    "https://github.com/bvzrays/astrbot_plugin_steamgame",
)
class SteamGamePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.api_key = self.config.get("steam_api_key", "")
        self.proxy = self.config.get("proxy", "")
        self.image_quality = int(self.config.get("image_quality", 90))
        self.image_quality = max(10, min(100, self.image_quality))
        self.recommend_source_limit = max(
            10, int(self.config.get("recommend_source_limit", 40))
        )
        self.recommend_result_limit = max(
            3, int(self.config.get("recommend_result_limit", 6))
        )

        if not self.api_key:
            logger.warning(
                "Steam API Key not set in config! Plugin will not work correctly."
            )

        self.steam_api = SteamAPI(self.api_key, self.proxy, logger=logger)

        # Data storage for bindings
        plugin_dir = Path(__file__).resolve().parent
        plugin_name = plugin_dir.name
        self.data_dir: Path = StarTools.get_data_dir(plugin_name)
        self.data_file: Path = self.data_dir / "steam_binding.json"
        self.cover_dir: Path = self.data_dir / "covers"
        self.templates_dir: Path = plugin_dir / "templates"
        self.assets_dir: Path = plugin_dir / "assets"
        self._default_icon_base64: str | None = None

        # Playwright 环境管理器
        self.env_manager = EnvManager(str(self.data_dir))
        self._playwright_ready = False

        self.bindings, self.group_bindings = self._load_bindings()
        logger.info(
            f"SteamGamePlugin: 已载入 {len(self.bindings)} 个绑定，数据文件 {self.data_file}"
        )

    def _load_bindings(self):
        if self.data_file.exists():
            try:
                with self.data_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Backward compatibility: older versions stored a flat dict
                    if isinstance(data, dict) and "users" in data and "groups" in data:
                        return data.get("users", {}), data.get("groups", {})
                    if isinstance(data, dict):
                        return data, {}
            except Exception as e:
                logger.error(f"Failed to load bindings: {e}")
                return {}, {}
        return {}, {}

    def _save_bindings(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.data_file.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "users": self.bindings,
                        "groups": self.group_bindings,
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.error(f"Failed to save bindings: {e}")

    def _link_user_to_group(self, user_id: str, group_id: Optional[str]) -> bool:
        """Track which group has access to which binding."""
        if not group_id:
            return False
        steam_id = self.bindings.get(user_id)
        if not steam_id:
            return False
        group_map = self.group_bindings.setdefault(group_id, {})
        if group_map.get(user_id) != steam_id:
            group_map[user_id] = steam_id
            return True
        return False

    def _sync_group_binding_value(self, user_id: str) -> bool:
        """Ensure historical group bindings use the latest steam id."""
        changed = False
        steam_id = self.bindings.get(user_id)
        if not steam_id:
            return False
        for group_map in self.group_bindings.values():
            if user_id in group_map and group_map[user_id] != steam_id:
                group_map[user_id] = steam_id
                changed = True
        return changed

    def _format_playtime(self, minutes):
        if minutes < 60:
            return f"{minutes} 分钟"
        hours = minutes / 60
        days = hours / 24
        return f"{int(hours)}h ({days:.1f}d)"

    async def _aggregate_achievements(
        self, steam_id: str, games: list, limit: int = 12
    ) -> dict:
        """Estimate achievement progress by sampling top games."""
        unlocked = 0
        total = 0
        if not games or not steam_id:
            return {"unlocked": 0, "total": 0}

        sampled_games = games[:limit]
        for game in sampled_games:
            app_id = game.get("appid")
            if not app_id:
                continue
            stats = await self.steam_api.get_user_stats_for_game(steam_id, app_id)
            if not stats:
                continue
            schema = await self.steam_api.get_schema_for_game(app_id)
            if not schema:
                continue
            achievements_schema = schema.get("availableGameStats", {}).get(
                "achievements", []
            )
            total += len(achievements_schema)

            user_achievements = stats.get("achievements", [])
            unlocked += sum(
                1
                for ach in user_achievements
                if ach.get("achieved", 0) == 1 or ach.get("unlocktime")
            )

        return {"unlocked": unlocked, "total": total}

    def _build_metric(
        self,
        label: str,
        left_value: float,
        right_value: float,
        left_display: Optional[str] = None,
        right_display: Optional[str] = None,
    ) -> dict:
        if left_display is None:
            left_display = str(left_value)
        if right_display is None:
            right_display = str(right_value)

        if left_value > right_value:
            left_result, right_result = "win", "lose"
        elif left_value < right_value:
            left_result, right_result = "lose", "win"
        else:
            left_result = right_result = "draw"

        badge_map = {"win": "WIN!", "lose": "LOSE", "draw": "DRAW"}

        return {
            "label": label,
            "left": {
                "value": left_display,
                "result": left_result,
                "badge": badge_map[left_result],
            },
            "right": {
                "value": right_display,
                "result": right_result,
                "badge": badge_map[right_result],
            },
        }

    def _bytes_to_data_uri(self, data: bytes, mime: str = "jpeg") -> str:
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/{mime};base64,{encoded}"

    def _load_cached_cover(self, dest_path: Path) -> Optional[str]:
        if not dest_path.exists():
            return None
        try:
            with dest_path.open("rb") as f:
                data = f.read()
            mime = "png" if dest_path.suffix.lower() == ".png" else "jpeg"
            return self._bytes_to_data_uri(data, mime)
        except Exception as e:
            logger.warning(f"Failed to read cached cover {dest_path}: {e}")
            return None

    async def _download_cover(self, url: str, dest_path: Path) -> Optional[bytes]:
        try:
            headers = {
                "Referer": "https://steamcommunity.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, proxy=self.proxy) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with dest_path.open("wb") as f:
                            f.write(data)
                        return data
        except Exception as e:
            logger.warning(f"Failed to download cover {url}: {e}")
        return None

    async def _ensure_cover_uri(self, app_id: int, variant: str = "poster") -> str:
        if not app_id:
            return ""
        app_id = str(app_id)
        base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}"
        url_candidates = []
        if variant == "hero":
            url_candidates = [
                f"{base}/library_hero.jpg",
                f"{base}/library_hero.png",
                f"{base}/header.jpg",
            ]
        else:
            url_candidates = [
                f"{base}/library_600x900.jpg",
                f"{base}/library_600x900.png",
                f"{base}/header.jpg",
            ]

        for url in url_candidates:
            ext = ".png" if url.lower().endswith(".png") else ".jpg"
            dest_path = self.cover_dir / f"{app_id}_{variant}{ext}"
            cached = self._load_cached_cover(dest_path)
            if cached:
                return cached
            data = await self._download_cover(url, dest_path)
            if data:
                mime = "png" if ext == ".png" else "jpeg"
                return self._bytes_to_data_uri(data, mime)

        # Download failed, fall back to last candidate URL
        return url_candidates[-1]

    async def _decorate_games_with_cover(self, games, variant: str = "poster"):
        tasks = []
        index_map = []
        for idx, game in enumerate(games):
            appid = game.get("appid")
            if not appid:
                continue
            tasks.append(self._ensure_cover_uri(appid, variant))
            index_map.append(idx)

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, cover in zip(index_map, results):
            if isinstance(cover, Exception):
                logger.warning(f"Cover fetch failed: {cover}")
                continue
            games[idx]["cover_uri"] = cover

    def _ensure_static_avatar(
        self, summary: Optional[Dict[str, Any]], size: str = "full"
    ) -> str:
        """
        Steam 会在用户设置动态头像时返回 gif，这里将其转换为 jpg，避免 HTML 渲染时出现动图。
        """
        if not summary:
            return ""
        avatar_url = (
            summary.get("avatarfull", "")
            if size == "full"
            else summary.get("avatarmedium", "")
        )
        avatar_hash = summary.get("avatarhash")
        if avatar_url and avatar_url.endswith(".gif"):
            if avatar_hash:
                suffix_map = {
                    "full": "_full",
                    "medium": "_medium",
                    "small": "",
                }
                suffix = suffix_map.get(size, "_full")
                avatar_url = f"https://avatars.cloudflare.steamstatic.com/{avatar_hash}{suffix}.jpg"
            else:
                avatar_url = avatar_url[:-4] + ".jpg"
        if avatar_url:
            summary["avatarfull"] = avatar_url
        return avatar_url

    async def _ensure_avatar_uri(self, summary: Dict[str, Any]) -> str:
        """下载头像并转为 base64，避免防盗链问题"""
        avatar_url = summary.get("avatarfull", "")
        if not avatar_url:
            return ""

        avatar_hash = summary.get("avatarhash", "unknown")
        dest_path = self.cover_dir / f"avatar_{avatar_hash}.jpg"

        cached = self._load_cached_cover(dest_path)
        if cached:
            return cached

        data = await self._download_cover(avatar_url, dest_path)
        if data:
            return self._bytes_to_data_uri(data, "jpeg")

        return avatar_url

    def _get_default_icon_base64(self) -> str:
        """获取默认图标 base64 数据 URI（用于图片加载失败时显示）"""
        if self._default_icon_base64 is not None:
            logger.info(f"[默认图标] 使用缓存，长度: {len(self._default_icon_base64)}")
            return self._default_icon_base64

        default_icon_path = self.assets_dir / "default_icon.png"
        logger.info(f"[默认图标] 尝试读取: {default_icon_path}")

        if default_icon_path.exists():
            try:
                with default_icon_path.open("rb") as f:
                    data = f.read()
                    self._default_icon_base64 = self._bytes_to_data_uri(data, "png")
                    logger.info(
                        f"[默认图标] 读取成功，base64 长度: {len(self._default_icon_base64)}"
                    )
                    return self._default_icon_base64
            except Exception as e:
                logger.error(f"[默认图标] 读取失败: {e}")
        else:
            logger.error(f"[默认图标] 文件不存在: {default_icon_path}")

        # 如果读取失败，返回空字符串
        self._default_icon_base64 = ""
        return self._default_icon_base64

    async def _init_playwright(self):
        """初始化 Playwright 环境"""
        if self.env_manager.is_installed():
            self._playwright_ready = True
            logger.info("[Playwright] 已安装，跳过初始化")
            return True

        try:
            await self.env_manager.install_dependencies()
            self._playwright_ready = self.env_manager.is_installed()
            return self._playwright_ready
        except Exception as e:
            logger.error(f"Playwright 初始化失败: {e}")
            return False

    async def terminate(self):
        """插件卸载时清理资源，关闭 Playwright 浏览器"""
        from .utils.browser import close_browser

        await close_browser()
        logger.info("SteamGame 插件已卸载，浏览器资源已清理")

    async def _render_html_local(
        self,
        template_content: str,
        template_data: dict,
        width: int = 880,
        image_type: str = "jpeg",
        quality: int = 90,
        timeout: int = 30000,
    ) -> str:
        """
        使用本地 Playwright 渲染 HTML 模板

        :param template_content: Jinja2 HTML 模板内容
        :param template_data: 模板数据
        :param width: 渲染宽度
        :param image_type: 图片类型 (jpeg/png)
        :param quality: JPEG 质量 (1-100)
        :param timeout: 渲染超时时间 (ms)
        :return: 图片文件路径
        """
        if not self._playwright_ready:
            logger.info("[Playwright] 首次使用，初始化 Playwright...")
            if not await self._init_playwright():
                raise RuntimeError("Playwright 初始化失败，无法渲染图片")

        template = Template(template_content)

        # 注入默认图标到模板数据
        default_icon = self._get_default_icon_base64()
        template_data["default_icon"] = default_icon
        logger.info(f"[模板渲染] 注入 default_icon，长度: {len(default_icon)}")

        html_content = template.render(**template_data)

        screenshot_bytes = await render_html_to_image(
            html_content=html_content,
            selector="body",
            width=width,
            scale_factor=2,
            is_mobile=False,
            full_page=True,
            timeout=timeout,
            image_type=image_type,
            quality=quality,
        )

        if not screenshot_bytes:
            raise RuntimeError("Playwright 渲染失败")

        temp_dir = tempfile.gettempdir()
        import time

        filename = f"steam_{int(time.time() * 1000)}.{image_type}"
        filepath = os.path.join(temp_dir, filename)

        with open(filepath, "wb") as f:
            f.write(screenshot_bytes)

        logger.info(f"图片已渲染: {filepath} ({len(screenshot_bytes)} bytes)")
        return filepath

    async def _resolve_target(
        self, event: AstrMessageEvent, target: str, allow_fallback: bool = True
    ) -> str:
        """
        Resolve Steam ID from argument.

        Priority:
        1. If target is a valid Steam64ID (17 digits starting with 7656): use directly
        2. If message contains @mention: use mentioned user's binding
        3. If allow_fallback: use sender's own binding

        Args:
            target: Can be empty or Steam64ID (7656...)
            allow_fallback: Whether to fallback to sender's binding when no target specified

        Returns:
            Steam64ID string or None if not found
        """
        save_needed = False
        group_id = event.get_group_id()
        steam_id = None

        # 1. Check if target is a valid Steam64ID (17 digits, typically starts with 7656)
        if target:
            target = target.strip()
            if target.isdigit() and len(target) == 17 and target.startswith("7656"):
                steam_id = target
                logger.debug(f"[SteamID解析] 识别为Steam64ID: {steam_id}")

        # 2. If no steam_id from target param, check message @mentions
        if not steam_id:
            for component in event.message_obj.message:
                if isinstance(component, Comp.At):
                    target_user_id = str(component.qq)
                    steam_id = self.bindings.get(target_user_id)
                    if steam_id:
                        logger.debug(
                            f"[SteamID解析] @提及 {target_user_id} -> {steam_id}"
                        )
                        if group_id and self._link_user_to_group(
                            target_user_id, group_id
                        ):
                            save_needed = True
                    else:
                        logger.debug(
                            f"[SteamID解析] @提及 {target_user_id} 未绑定SteamID"
                        )
                    break  # Only use first @mention

        # 3. Fallback to sender's own binding
        if not steam_id and allow_fallback:
            user_id = str(event.get_sender_id())
            steam_id = self.bindings.get(user_id)
            if steam_id:
                logger.debug(f"[SteamID解析] 使用发送者绑定: {steam_id}")
                if group_id and self._link_user_to_group(user_id, group_id):
                    save_needed = True

        # Save group bindings if updated
        if save_needed:
            self._save_bindings()

        return steam_id

    @filter.command("绑定steam")
    async def bind(self, event: AstrMessageEvent, steam_id: str = ""):
        """绑定 Steam ID（在新的群聊中可不填参数同步已有绑定）"""
        user_id = str(event.get_sender_id())
        group_id = event.get_group_id()
        message = ""

        data_changed = False

        if steam_id:
            # Validate Steam ID (must be 64-bit integer, usually 17 digits)
            if not steam_id.isdigit() or len(steam_id) != 17:
                yield event.plain_result(
                    "绑定失败：请输入正确的 17 位 Steam ID 64 (例如 76561198000000000)。"
                )
                return
            self.bindings[user_id] = steam_id
            data_changed = True
            message = f"绑定成功！已关联 Steam ID: {steam_id}"
        else:
            if user_id not in self.bindings:
                yield event.plain_result(
                    "你还没有绑定 Steam ID，请使用 /绑定steam <SteamID64>。"
                )
                return
            steam_id = self.bindings[user_id]
            message = "已将现有绑定同步至当前群聊。"

        if self._sync_group_binding_value(user_id):
            data_changed = True
        if self._link_user_to_group(user_id, group_id):
            data_changed = True
        if data_changed:
            self._save_bindings()
        yield event.plain_result(message)

    async def _render_profile(self, event: AstrMessageEvent, steam_id: str, mode: str):
        if not self.api_key:
            yield event.plain_result("请先在配置文件中设置 Steam API Key。")
            return

        if not steam_id:
            yield event.plain_result(
                "未找到绑定的 Steam ID。请先绑定 (/绑定steam <id>) 或指定 ID。"
            )
            return

        # Fetch Data (force refresh for summary mode to get current playing status)
        summary = await self.steam_api.get_player_summaries(
            steam_id, force_refresh=(mode == "summary")
        )
        if not summary:
            yield event.plain_result(
                "未找到该 Steam 用户，请检查 ID 是否正确，或检查网络/代理设置。"
            )
            return
        self._ensure_static_avatar(summary)
        avatar_uri = await self._ensure_avatar_uri(summary)
        if avatar_uri:
            summary["avatarfull"] = avatar_uri

        is_private = summary.get("communityvisibilitystate", 1) != 3

        owned_games = []
        recent_games = []
        hero_cover = summary.get("avatarfull", "")

        if not is_private:
            # Always fetch owned games to show total count and playtime
            owned_games = await self.steam_api.get_owned_games(steam_id)
            recent_games = await self.steam_api.get_recently_played_games(steam_id)
            await self._decorate_games_with_cover(owned_games, "poster")
            await self._decorate_games_with_cover(recent_games, "poster")
            if owned_games:
                hero_cover = await self._ensure_cover_uri(
                    owned_games[0]["appid"], "hero"
                )
                if not hero_cover:
                    hero_cover = summary.get("avatarfull", "")

        # Process Data
        for game in owned_games:
            game["playtime_forever_formatted"] = self._format_playtime(
                game.get("playtime_forever", 0)
            )

        for game in recent_games:
            game["playtime_2weeks_formatted"] = self._format_playtime(
                game.get("playtime_2weeks", 0)
            )

        # Mosaic Layout Logic (Only for Library mode)
        mosaic_games = []
        if mode == "library" and owned_games:
            mosaic_games = owned_games[:100]  # Take top 100
            for i, game in enumerate(mosaic_games):
                if i == 0:
                    game["grid_class"] = "span-4x4"
                elif i < 5:
                    game["grid_class"] = "span-2x2"
                elif i < 15:
                    game["grid_class"] = "span-2x1" if i % 2 == 0 else "span-1x2"
                else:
                    game["grid_class"] = "span-1x1"

        # Check if playing
        playing_game = None
        if summary.get("gameextrainfo"):
            playing_game = {
                "name": summary.get("gameextrainfo"),
                "appid": summary.get("gameid"),
            }
            cover_uri = await self._ensure_cover_uri(summary.get("gameid"), "hero")
            playing_game["cover_uri"] = cover_uri or hero_cover

        # Render
        template_path = self.templates_dir / "profile.html"
        with template_path.open("r", encoding="utf-8") as f:
            template_content = f.read()

        bans_data = await self.steam_api.get_player_bans(steam_id)
        ban_info = bans_data[0] if bans_data else None

        img_url = await self._render_html_local(
            template_content,
            {
                "player": summary,
                "owned_games": mosaic_games if mode == "library" else owned_games,
                "recent_games": recent_games,
                "total_games": len(owned_games),
                "total_playtime": self._format_playtime(
                    sum(g.get("playtime_forever", 0) for g in owned_games)
                ),
                "is_private": is_private,
                "mode": mode,
                "playing_game": playing_game,
                "hero_cover": hero_cover,
                "ban_info": ban_info,
            },
            width=880,
            image_type="jpeg",
            quality=self.image_quality,
        )
        yield event.image_result(img_url)

    @filter.command("steam动态")
    async def steam_activity(self, event: AstrMessageEvent, target: str = ""):
        """查看 Steam 动态 (头像 + 最近活动)

        用法:
        /steam动态 - 查看自己的动态
        /steam动态 @某人 - 查看@用户的动态
        /steam动态 7656... - 查看指定Steam64ID的动态
        """
        steam_id = await self._resolve_target(event, target)
        async for result in self._render_profile(event, steam_id, "summary"):
            yield result

    @filter.command("steam游戏库")
    async def steam_library(self, event: AstrMessageEvent, target: str = ""):
        """查看 Steam 完整游戏库 (Mosaic 墙)

        用法:
        /steam游戏库 - 查看自己的游戏库
        /steam游戏库 @某人 - 查看@用户的游戏库
        /steam游戏库 7656... - 查看指定Steam64ID的游戏库
        """
        steam_id = await self._resolve_target(event, target)
        async for result in self._render_profile(event, steam_id, "library"):
            yield result

    @filter.command("steam成就")
    async def steam_achievement(self, event: AstrMessageEvent, game_name: str):
        """查看 Steam 游戏成就 (/steam成就 <游戏名>)"""
        if not game_name:
            yield event.plain_result("请输入游戏名称，例如：/steam成就 黑神话")
            return

        steam_id = await self._resolve_target(
            event, ""
        )  # Always check sender's achievements
        if not steam_id:
            yield event.plain_result("请先绑定 Steam ID。")
            return

        # 1. Search for game in owned games
        owned_games = await self.steam_api.get_owned_games(steam_id)

        # Fuzzy Search Logic
        game_names = [g["name"] for g in owned_games]
        matches = difflib.get_close_matches(game_name, game_names, n=5, cutoff=0.4)

        target_game = None

        # Exact match check (case-insensitive)
        for game in owned_games:
            if game_name.lower() == game["name"].lower():
                target_game = game
                break

        if not target_game:
            # Substring match check
            for game in owned_games:
                if game_name.lower() in game["name"].lower():
                    target_game = game
                    break

        if not target_game:
            if matches:
                # If multiple matches found, ask user to be specific
                # But for better UX, if the first match is very close, we might just use it?
                # Let's just list them.
                msg = "未找到精确匹配的游戏，你是不是想找：\n"
                for i, m in enumerate(matches):
                    msg += f"{i + 1}. {m}\n"
                msg += "请尝试使用更完整的名称。"
                yield event.plain_result(msg)
                return
            else:
                yield event.plain_result(
                    f"在你拥有的游戏中未找到包含“{game_name}”的游戏。"
                )
                return

        app_id = target_game["appid"]

        # 2. Fetch Schema & Stats
        schema = await self.steam_api.get_schema_for_game(app_id)
        achievements_all = (
            schema.get("availableGameStats", {}).get("achievements", [])
            if schema
            else []
        )
        if not achievements_all:
            yield event.plain_result(
                f"《{target_game['name']}》似乎没有可查询的 Steam 成就。"
            )
            return

        stats = await self.steam_api.get_user_stats_for_game(steam_id, app_id)
        user_achievements = stats.get("achievements", []) if stats else []
        user_achievements_map = {a["name"]: a for a in user_achievements}

        unlocked_count = sum(
            1
            for a in user_achievements_map.values()
            if a.get("achieved", 0) == 1 or a.get("unlocktime")
        )
        total_count = len(achievements_all)
        completion_rate = (unlocked_count / total_count * 100) if total_count > 0 else 0

        unlocked_display = []
        locked_display = []
        for ach in achievements_all:
            base_info = {
                "name": ach.get("displayName", ach.get("name", "")),
                "icon": ach.get("icon"),
                "desc": ach.get("description", ""),
            }
            if ach.get("name") in user_achievements_map:
                info = dict(base_info)
                info["unlocktime"] = user_achievements_map[ach["name"]].get(
                    "unlocktime", 0
                )
                unlocked_display.append(info)
            else:
                locked_display.append(base_info)

        unlocked_display.sort(key=lambda x: x.get("unlocktime", 0), reverse=True)
        display_achievements = unlocked_display[:6]
        if len(display_achievements) < 8:
            display_achievements.extend(locked_display[: 8 - len(display_achievements)])

        cover_uri = await self._ensure_cover_uri(app_id, "hero")

        render_data = {
            "game": target_game,
            "unlocked": unlocked_count,
            "total": total_count,
            "rate": f"{completion_rate:.1f}",
            "achievements": display_achievements,
            "player_name": event.get_sender_name(),
            "game_cover": cover_uri,
        }

        template_path = self.templates_dir / "achievement.html"
        if not template_path.exists():
            yield event.plain_result("成就模板尚未上传。")
            return

        with template_path.open("r", encoding="utf-8") as f:
            template_content = f.read()

        img_url = await self._render_html_local(
            template_content,
            render_data,
            width=800,
            image_type="jpeg",
            quality=self.image_quality,
        )
        yield event.image_result(img_url)

    # ==================== LLM Tools ====================

    @llm_tool(name="get_steam_library")
    async def get_steam_library(
        self,
        event: AstrMessageEvent,
        steam_id: str = "",
        target_user_id: str = "",
        limit: int = 50,
        sort_by: str = "playtime",
    ) -> str:
        """获取用户的Steam游戏库信息，包括游戏数量、总游戏时长、最近游玩的游戏等。
        当用户询问自己的游戏库、拥有哪些游戏、游戏时长统计，或查询@某人的游戏库时调用此工具。
        建议设置limit为50以获得较全面的游戏列表，但也可以根据用户问题调整数量。

        Args:
            steam_id(string): 可选。用户的Steam64ID（17位数字，以7656开头）。如果不提供，则使用当前用户的绑定。
            target_user_id(string): 可选。目标用户的平台ID（如QQ号），用于查询@某人的游戏库。如果消息中有@某人，应该传入被@用户的ID。
            limit(number): 可选。返回的游戏数量。默认为50，可根据需要调整。
            sort_by(string): 可选。排序方式，可选值：playtime(游玩时长,默认)、recent(最近游玩)、name(游戏名称)、appid(游戏新旧)。默认为playtime。

        Returns:
            str: 游戏库信息的文本描述，包含游戏数量、总时长、游戏列表等
        """
        user_id = str(event.get_sender_id())
        target_steam_id = None

        # Priority 1: Check for Steam64ID parameter
        if steam_id:
            steam_id = steam_id.strip()
            if (
                steam_id.isdigit()
                and len(steam_id) == 17
                and steam_id.startswith("7656")
            ):
                target_steam_id = steam_id
            else:
                return f"错误：提供的Steam ID格式不正确。Steam64ID应为17位数字且以7656开头。"

        # Priority 2: Check for @mention target_user_id
        if not target_steam_id and target_user_id:
            target_user_id = target_user_id.strip()
            target_steam_id = self.bindings.get(target_user_id)
            if not target_steam_id:
                return f"该用户尚未绑定Steam账号，无法查询其游戏库。"

        # Priority 3: Check message @mentions (if no explicit target specified)
        if not target_steam_id and not target_user_id:
            for component in event.message_obj.message:
                if isinstance(component, Comp.At):
                    mentioned_id = str(component.qq)
                    target_steam_id = self.bindings.get(mentioned_id)
                    if target_steam_id:
                        break

        # Priority 4: Use sender's own binding
        if not target_steam_id:
            target_steam_id = self.bindings.get(user_id)
            if not target_steam_id:
                return f"您尚未绑定Steam账号。请使用 `/绑定steam <Steam64ID>` 进行绑定，或在使用此工具时提供Steam64ID参数。"

        # Fetch player summary
        summary = await self.steam_api.get_player_summaries(target_steam_id)
        if not summary:
            return f"无法获取Steam用户信息，请检查ID是否正确或API Key是否配置。"

        player_name = summary.get("personaname", "未知用户")

        # Check privacy
        is_private = summary.get("communityvisibilitystate", 1) != 3
        if is_private:
            return f"用户 {player_name} 的Steam资料是私密的，无法获取游戏库信息。"

        # Fetch owned games
        owned_games = await self.steam_api.get_owned_games(target_steam_id)
        if not owned_games:
            return f"用户 {player_name} 的游戏库为空或无法访问。"

        # Calculate stats
        total_games = len(owned_games)
        total_minutes = sum(g.get("playtime_forever", 0) for g in owned_games)
        total_hours = total_minutes / 60

        # Sort games based on sort_by parameter
        sort_by = sort_by.lower() if sort_by else "playtime"
        if sort_by == "recent":
            # Sort by last played time (rtime_last_played), most recent first
            sorted_games = sorted(
                owned_games,
                key=lambda x: x.get("rtime_last_played", 0),
                reverse=True,
            )
            sort_label = "最近游玩"
        elif sort_by == "name":
            # Sort by game name alphabetically
            sorted_games = sorted(owned_games, key=lambda x: x.get("name", "").lower())
            sort_label = "游戏名称"
        elif sort_by == "appid":
            # Sort by appid (approximate game release order, lower = older games)
            sorted_games = sorted(owned_games, key=lambda x: x.get("appid", 0))
            sort_label = "游戏新旧"
        else:
            # Default: sort by playtime
            sorted_games = sorted(
                owned_games,
                key=lambda x: x.get("playtime_forever", 0),
                reverse=True,
            )
            sort_label = "游玩时长"

        # Use provided limit, default to 50, cap at 100 to avoid too long responses
        game_limit = max(1, min(limit, 100)) if limit else 50
        top_games = sorted_games[:game_limit]

        # Get recently played games
        recent_games = await self.steam_api.get_recently_played_games(target_steam_id)

        # Format result
        lines = [f"🎮 {player_name} 的Steam游戏库"]
        lines.append(f"- 游戏总数：{total_games} 款")
        lines.append(
            f"- 总游戏时长：{self._format_playtime(total_minutes)} ({total_hours:.1f}小时)"
        )

        if top_games:
            lines.append(f"\n📊 按{sort_label}排序（前{len(top_games)}款）：")
            for i, game in enumerate(top_games, 1):
                name = game.get("name", "未知游戏")
                hours = game.get("playtime_forever", 0) / 60
                lines.append(f"  {i}. {name} - {hours:.1f}小时")

        if recent_games:
            lines.append(f"\n🕐 最近2周游玩的游戏：")
            for game in recent_games[:5]:
                name = game.get("name", "未知游戏")
                hours_2weeks = game.get("playtime_2weeks", 0) / 60
                lines.append(f"  - {name} - {hours_2weeks:.1f}小时")

        # Check currently playing
        if summary.get("gameextrainfo"):
            lines.append(f"\n▶️ 当前正在玩：{summary.get('gameextrainfo')}")

        return "\n".join(lines)

    @llm_tool(name="get_steam_activity")
    async def get_steam_activity(
        self,
        event: AstrMessageEvent,
        steam_id: str = "",
        target_user_id: str = "",
        recent_games_limit: int = 10,
    ) -> str:
        """获取用户的Steam最近动态和活动状态，包括在线状态、正在玩的游戏、最近游玩记录等。
        当用户询问自己的Steam状态、最近在玩什么游戏、是否在线，或查询@某人的状态时调用此工具。
        建议设置recent_games_limit为10以获取合理的最近游戏列表。

        Args:
            steam_id(string): 可选。用户的Steam64ID（17位数字，以7656开头）。如果不提供，则使用当前用户的绑定。
            target_user_id(string): 可选。目标用户的平台ID（如QQ号），用于查询@某人的状态。如果消息中有@某人，应该传入被@用户的ID。
            recent_games_limit(number): 可选。返回的最近2周游玩游戏数量。默认为10，可根据需要调整。

        Returns:
            str: Steam活动状态的文本描述
        """
        user_id = str(event.get_sender_id())
        target_steam_id = None

        # Priority 1: Check for Steam64ID parameter
        if steam_id:
            steam_id = steam_id.strip()
            if (
                steam_id.isdigit()
                and len(steam_id) == 17
                and steam_id.startswith("7656")
            ):
                target_steam_id = steam_id
            else:
                return f"错误：提供的Steam ID格式不正确。Steam64ID应为17位数字且以7656开头。"

        # Priority 2: Check for explicit target_user_id parameter
        if not target_steam_id and target_user_id:
            target_user_id = target_user_id.strip()
            target_steam_id = self.bindings.get(target_user_id)
            if not target_steam_id:
                return f"该用户尚未绑定Steam账号，无法查询其状态。"

        # Priority 3: Check message @mentions (if no explicit target specified)
        if not target_steam_id and not target_user_id:
            for component in event.message_obj.message:
                if isinstance(component, Comp.At):
                    mentioned_id = str(component.qq)
                    target_steam_id = self.bindings.get(mentioned_id)
                    if target_steam_id:
                        break

        # Priority 4: Use sender's own binding
        if not target_steam_id:
            target_steam_id = self.bindings.get(user_id)
            if not target_steam_id:
                return f"您尚未绑定Steam账号。请使用 `/绑定steam <Steam64ID>` 进行绑定，或在使用此工具时提供Steam64ID参数。"

        # Fetch player summary
        summary = await self.steam_api.get_player_summaries(
            target_steam_id, force_refresh=True
        )
        if not summary:
            return f"无法获取Steam用户信息，请检查ID是否正确或API Key是否配置。"

        player_name = summary.get("personaname", "未知用户")

        # Parse online status
        persona_state = summary.get("personastate", 0)
        status_map = {
            0: "离线",
            1: "在线",
            2: "忙碌",
            3: "离开",
            4: "打盹",
            5: "想交易",
            6: "想玩",
        }
        status = status_map.get(persona_state, "未知")

        lines = [f"👤 {player_name} 的Steam状态"]
        lines.append(f"- 当前状态：{status}")

        # Check if playing a game
        game_info = summary.get("gameextrainfo")
        game_id = summary.get("gameid")
        if game_info:
            lines.append(f"- 正在游玩：{game_info}")
            if game_id:
                lines.append(f"- 游戏ID：{game_id}")
        else:
            lines.append(f"- 当前未在游戏中")

        # Last logoff time
        last_logoff = summary.get("lastlogoff")
        if last_logoff:
            from datetime import datetime

            logoff_time = datetime.fromtimestamp(last_logoff)
            lines.append(f"- 上次在线：{logoff_time.strftime('%Y-%m-%d %H:%M')}")

        # Check privacy
        is_private = summary.get("communityvisibilitystate", 1) != 3
        if is_private:
            lines.append(f"\n⚠️ 注意：该用户的Steam资料是私密的，部分信息可能无法获取。")
        else:
            # Get recently played games
            recent_games = await self.steam_api.get_recently_played_games(
                target_steam_id
            )
            if recent_games:
                # Use provided limit, default to 10, cap at 20 to avoid too long responses
                games_limit = (
                    max(1, min(recent_games_limit, 20)) if recent_games_limit else 10
                )
                lines.append(
                    f"\n🕐 最近2周游玩的游戏（前{min(len(recent_games), games_limit)}款）："
                )
                for game in recent_games[:games_limit]:
                    name = game.get("name", "未知游戏")
                    hours_2weeks = game.get("playtime_2weeks", 0) / 60
                    total_hours = game.get("playtime_forever", 0) / 60
                    lines.append(f"  - {name}")
                    lines.append(
                        f"    近2周：{hours_2weeks:.1f}小时 | 总计：{total_hours:.1f}小时"
                    )

        return "\n".join(lines)

    @llm_tool(name="bind_steam_account")
    async def bind_steam_account(self, event: AstrMessageEvent, steam_id: str) -> str:
        """帮助用户绑定Steam账号到当前平台账号。绑定后可以使用其他Steam相关工具查询自己的游戏库和动态。
        当用户想要绑定Steam账号、查询自己的Steam信息但尚未绑定时调用此工具。

        Args:
            steam_id(string): 用户的Steam64ID（17位数字，以7656开头）。可以从Steam个人资料URL获取。

        Returns:
            str: 绑定结果的提示信息
        """
        if not steam_id:
            return "错误：请提供Steam64ID。您可以在Steam个人资料页面的URL中找到，例如：https://steamcommunity.com/profiles/76561198xxxxxxxxxx/"

        steam_id = steam_id.strip()

        # Validate Steam ID format
        if not steam_id.isdigit():
            return f"错误：Steam ID必须是数字。您提供的ID包含非数字字符。"

        if len(steam_id) != 17:
            return f"错误：Steam64ID必须是17位数字。您提供的ID是{len(steam_id)}位。"

        if not steam_id.startswith("7656"):
            return f"错误：Steam64ID应以7656开头。请确认您使用的是64位Steam ID。"

        user_id = str(event.get_sender_id())
        group_id = event.get_group_id()

        # Verify the Steam ID is valid by fetching player summary
        summary = await self.steam_api.get_player_summaries(steam_id)
        if not summary:
            return f"无法验证Steam ID，请确认ID是否正确，或检查Steam API Key配置。"

        player_name = summary.get("personaname", "未知用户")

        # Perform binding
        self.bindings[user_id] = steam_id

        # Update group binding
        save_needed = False
        if self._sync_group_binding_value(user_id):
            save_needed = True
        if group_id and self._link_user_to_group(user_id, group_id):
            save_needed = True

        self._save_bindings()

        return f"✅ Steam账号绑定成功！\n\n用户：{player_name}\nSteam64ID：{steam_id}\n\n您现在可以使用 `/steam动态`、`/steam游戏库` 等命令查看自己的Steam信息，也可以让AI助手帮您查询游戏库和动态。"

    @llm_tool(name="get_group_steam_bindings")
    async def get_group_steam_bindings(self, event: AstrMessageEvent) -> str:
        """获取当前群聊中已绑定Steam账号的用户列表。当用户询问群里有谁绑定了Steam、群友Steam账号列表、或者需要了解群内Steam用户情况时调用此工具。

        Args:
            无需参数

        Returns:
            str: 群内已绑定Steam的用户列表，包含用户ID和对应的Steam64ID
        """
        group_id = event.get_group_id()
        if not group_id:
            return "此工具只能在群聊中使用，当前不在群聊环境中。"

        group_binding_map = self.group_bindings.get(group_id, {})
        if not group_binding_map:
            return f"当前群聊（{group_id}）暂无用户绑定Steam账号。用户可以使用 `/绑定steam <Steam64ID>` 进行绑定。"

        lines = [f"📋 群聊 {group_id} 已绑定Steam的用户列表："]
        lines.append(f"共 {len(group_binding_map)} 人已绑定：")
        lines.append("")
        lines.append("| 用户ID | Steam64ID |")
        lines.append("|--------|-----------|")
        for user_id, steam_id in group_binding_map.items():
            lines.append(f"| {user_id} | {steam_id} |")

        return "\n".join(lines)

    @filter.command("steam对比")
    async def steam_compare(self, event: AstrMessageEvent, target: str = ""):
        """对比两人游戏库

        用法:
        /steam对比 - 与第一个@用户对比
        /steam对比 @某人 - 与指定用户对比
        /steam对比 7656... - 与指定Steam64ID对比
        """
        # Fix: Directly get sender's ID from binding, don't use _resolve_target(event, "")
        # because it might pick up the @mention in the message intended for the target.
        sender_user_id = str(event.get_sender_id())
        my_id = self.bindings.get(sender_user_id)

        target_id = await self._resolve_target(event, target, allow_fallback=False)

        if not my_id:
            yield event.plain_result("你还没有绑定 Steam ID 哦。")
            return

        if not target_id:
            yield event.plain_result("目标用户未绑定 Steam ID，或未指定对比对象。")
            return

        if my_id == target_id:
            yield event.plain_result("不能和自己对比哦。")
            return

        # Fetch both
        my_games = await self.steam_api.get_owned_games(my_id)
        target_games = await self.steam_api.get_owned_games(target_id)

        if not my_games or not target_games:
            yield event.plain_result(
                "无法获取双方的游戏库，请检查 Steam API Key 或网络代理。"
            )
            return

        my_summary = await self.steam_api.get_player_summaries(my_id) or {}
        target_summary = await self.steam_api.get_player_summaries(target_id) or {}
        self._ensure_static_avatar(my_summary)
        self._ensure_static_avatar(target_summary)

        # Calculate Intersection
        my_game_ids = {g["appid"] for g in my_games}
        target_game_ids = {g["appid"] for g in target_games}
        common_ids = my_game_ids.intersection(target_game_ids)

        common_games = []
        for gid in common_ids:
            # Find game info
            g = next((x for x in my_games if x["appid"] == gid), None)
            if g:
                common_games.append(g)

        # Sort by my playtime
        common_games.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)

        # Calculate unique games
        only_me_ids = my_game_ids - target_game_ids
        only_target_ids = target_game_ids - my_game_ids

        only_me = [g for g in my_games if g["appid"] in only_me_ids]
        only_target = [g for g in target_games if g["appid"] in only_target_ids]

        # Sort unique games by playtime
        only_me.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)
        only_target.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)

        my_total_minutes = sum(g.get("playtime_forever", 0) for g in my_games)
        target_total_minutes = sum(g.get("playtime_forever", 0) for g in target_games)

        # Achievement aggregation (sample top games to avoid heavy requests)
        my_ach_task = asyncio.create_task(self._aggregate_achievements(my_id, my_games))
        target_ach_task = asyncio.create_task(
            self._aggregate_achievements(target_id, target_games)
        )
        my_achievements, target_achievements = await asyncio.gather(
            my_ach_task, target_ach_task
        )

        if not common_games:
            yield event.plain_result("双方似乎没有共同拥有的游戏。")
            return

        top_common = common_games[:12]
        await self._decorate_games_with_cover(top_common, "poster")

        render_data = {
            "me": {
                "personaname": my_summary.get("personaname", "Player 1"),
                "avatarfull": my_summary.get("avatarfull", ""),
                "count": len(my_games),
            },
            "target": {
                "personaname": target_summary.get("personaname", "Player 2"),
                "avatarfull": target_summary.get("avatarfull", ""),
                "count": len(target_games),
            },
            "common_games": top_common,
            "common_count": len(common_games),
            "only_me": only_me[:12],
            "only_target": only_target[:12],
            "metrics": [
                self._build_metric("游戏数量", len(my_games), len(target_games)),
                self._build_metric(
                    "总时长",
                    my_total_minutes,
                    target_total_minutes,
                    left_display=self._format_playtime(my_total_minutes),
                    right_display=self._format_playtime(target_total_minutes),
                ),
                self._build_metric(
                    "成就完成数",
                    my_achievements.get("unlocked", 0),
                    target_achievements.get("unlocked", 0),
                    left_display=f"{my_achievements.get('unlocked', 0)}/{my_achievements.get('total', 0)}"
                    if my_achievements.get("total")
                    else f"{my_achievements.get('unlocked', 0)}/-",
                    right_display=f"{target_achievements.get('unlocked', 0)}/{target_achievements.get('total', 0)}"
                    if target_achievements.get("total")
                    else f"{target_achievements.get('unlocked', 0)}/-",
                ),
            ],
        }

        template_path = self.templates_dir / "compare.html"
        if not template_path.exists():
            yield event.plain_result("对比模板尚未上传。")
            return
        with template_path.open("r", encoding="utf-8") as f:
            template_content = f.read()
        img_url = await self._render_html_local(
            template_content,
            render_data,
            width=800,
            image_type="jpeg",
            quality=self.image_quality,
        )
        yield event.image_result(img_url)

    @filter.command("steam推荐")
    async def steam_recommend(self, event: AstrMessageEvent, target: str = ""):
        """群友热门游戏推荐

        用法:
        /steam推荐 - 为自己生成推荐
        /steam推荐 @某人 - 为@用户生成推荐
        /steam推荐 7656... - 为指定Steam64ID生成推荐
        """
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令。")
            return

        group_binding_map = self.group_bindings.get(group_id, {})
        if not group_binding_map:
            yield event.plain_result("本群暂无绑定信息，无法生成推荐。")
            return

        target_steam_id = await self._resolve_target(event, target)
        if not target_steam_id:
            yield event.plain_result("未找到目标用户的 Steam 绑定。")
            return

        user_games = await self.steam_api.get_owned_games(target_steam_id)
        if not user_games:
            yield event.plain_result("无法获取目标用户的游戏库。")
            return

        user_appids = {g.get("appid") for g in user_games}

        others = [
            sid for sid in group_binding_map.values() if sid and sid != target_steam_id
        ]
        if not others:
            yield event.plain_result("群内没有其他已绑定的用户，暂无法推荐。")
            return

        tasks = [self.steam_api.get_owned_games(steam_id) for steam_id in others]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        recommendations = {}
        for steam_id, games in zip(others, results):
            if not isinstance(games, list):
                continue
            for game in games[: self.recommend_source_limit]:
                appid = game.get("appid")
                if not appid or appid in user_appids:
                    continue
                minutes = game.get("playtime_forever", 0)
                if minutes <= 0:
                    continue
                entry = recommendations.setdefault(
                    appid,
                    {
                        "appid": appid,
                        "name": game.get("name", f"App {appid}"),
                        "score": 0,
                        "owners": set(),
                        "cover_uri": game.get("cover_uri"),
                    },
                )
                entry["score"] += minutes
                entry["owners"].add(steam_id)

        if not recommendations:
            yield event.plain_result(
                "未找到可推荐的游戏，可能你已经拥有群友的热门作品。"
            )
            return

        top_items = sorted(
            recommendations.values(),
            key=lambda x: (x["score"], len(x["owners"])),
            reverse=True,
        )[: self.recommend_result_limit]

        await self._decorate_games_with_cover(top_items, "poster")

        summary_cache = {}

        async def get_summary_cached(steam_id: str):
            if steam_id not in summary_cache:
                summary_cache[steam_id] = (
                    await self.steam_api.get_player_summaries(steam_id) or {}
                )
                self._ensure_static_avatar(summary_cache[steam_id])
            return summary_cache[steam_id]

        render_recommendations = []
        for item in top_items:
            hours = item["score"] / 60
            owner_avatars = []
            for owner_id in list(item["owners"])[:6]:
                summary = await get_summary_cached(owner_id)
                self._ensure_static_avatar(summary)
                avatar = summary.get("avatarfull")
                if avatar:
                    owner_avatars.append(avatar)
            render_recommendations.append(
                {
                    "name": item["name"],
                    "score": item["score"],
                    "playtime": f"{hours:.1f}",
                    "owners": len(item["owners"]),
                    "owner_avatars": owner_avatars,
                    "cover_uri": item.get("cover_uri"),
                }
            )

        target_summary = await get_summary_cached(target_steam_id)
        self._ensure_static_avatar(target_summary)
        render_data = {
            "target": {
                "personaname": target_summary.get(
                    "personaname", event.get_sender_name()
                ),
                "avatar": target_summary.get("avatarfull", ""),
            },
            "recommendations": render_recommendations,
        }

        template_path = self.templates_dir / "recommend.html"
        if not template_path.exists():
            yield event.plain_result("推荐模板尚未上传。")
            return
        with template_path.open("r", encoding="utf-8") as f:
            template_content = f.read()

        img_url = await self._render_html_local(
            template_content,
            render_data,
            width=800,
            image_type="jpeg",
            quality=self.image_quality,
        )
        yield event.image_result(img_url)

    @filter.command("steam联动")
    async def steam_network(self, event: AstrMessageEvent, target: str = ""):
        """群内 Steam 好友联动与同玩提醒

        用法:
        /steam联动 - 分析整个群的好友关系
        /steam联动 @某人 - 分析特定用户的好友关系
        /steam联动 7656... - 分析特定Steam64ID的好友关系
        """
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令。")
            return

        group_binding_map = self.group_bindings.get(group_id, {})
        if not group_binding_map:
            yield event.plain_result("本群暂无绑定信息。")
            return

        # Resolve target if specified
        target_steam_id = None
        if target:
            target_steam_id = await self._resolve_target(
                event, target, allow_fallback=False
            )
            if not target_steam_id:
                yield event.plain_result("未找到目标用户的 Steam 绑定。")
                return

        steam_to_user = {
            steam: user for user, steam in group_binding_map.items() if steam
        }
        steam_ids = list(steam_to_user.keys())

        # If target specified, filter to only analyze target's relationships with group
        if target_steam_id:
            # Check if target is in the group bindings
            if target_steam_id not in steam_to_user:
                yield event.plain_result("目标用户不在本群绑定列表中。")
                return
            # We'll analyze this specific user's friends within the group
            analysis_scope = [target_steam_id]
            target_name_prefix = ""
        else:
            # Analyze all group members
            if len(steam_ids) < 2:
                yield event.plain_result("至少需要两位已绑定用户才能分析联动。")
                return
            analysis_scope = steam_ids
            target_name_prefix = ""

        summary_cache: Dict[str, Dict[str, Any]] = {}

        async def get_summary_cached(steam_id: str):
            if steam_id not in summary_cache:
                summary_cache[steam_id] = (
                    await self.steam_api.get_player_summaries(steam_id) or {}
                )
            return summary_cache[steam_id]

        friend_tasks = {
            sid: asyncio.create_task(self.steam_api.get_friend_list(sid))
            for sid in analysis_scope
        }

        playing_map: Dict[str, Dict[str, Any]] = {}
        for sid in analysis_scope:
            summary = await get_summary_cached(sid)
            game_id = summary.get("gameid")
            if summary.get("gameextrainfo") and game_id:
                playing_entry = playing_map.setdefault(
                    str(game_id),
                    {"name": summary.get("gameextrainfo"), "players": []},
                )
                playing_entry["players"].append(sid)

        edges = set()
        for sid, task in friend_tasks.items():
            friends = await task
            for fid in friends:
                if fid in steam_to_user and sid in steam_to_user and fid != sid:
                    pair = tuple(sorted([sid, fid]))
                    edges.add(pair)

        def display_name(steam_id: str) -> str:
            summary = summary_cache.get(steam_id, {})
            self._ensure_static_avatar(summary)
            return summary.get("personaname") or steam_id

        # Customize title based on analysis scope
        if target_steam_id:
            target_name = display_name(target_steam_id)
            lines = [f"👥 {target_name} 的 Steam 联动概览"]
        else:
            lines = ["👥 群内 Steam 联动概览"]
        if edges:
            lines.append(f"- 发现 {len(edges)} 对群友互为 Steam 好友：")
            for idx, (a, b) in enumerate(list(edges)[:10], start=1):
                lines.append(f"  {idx}. {display_name(a)} ↔ {display_name(b)}")
            if len(edges) > 10:
                lines.append(f"  … 其余 {len(edges) - 10} 对略")
        else:
            lines.append("- 暂未发现群友之间的 Steam 好友关系。")

        active_groups = [
            entry for entry in playing_map.values() if len(entry["players"]) > 1
        ]
        if active_groups:
            lines.append("\n🔥 正在一起玩的游戏：")
            for entry in active_groups:
                names = [display_name(sid) for sid in entry["players"]]
                lines.append(f"- {entry['name']}: {', '.join(names)}")
        else:
            lines.append("\n🔥 暂时没有群友在同一款游戏里。")

        yield event.plain_result("\n".join(lines))

    @filter.command("steam排行")
    async def steam_top(self, event: AstrMessageEvent, dimension: str = "游戏数"):
        """群内排行 (/steam排行 [游戏数/时长])"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令。")
            return
        # Ensure caller至少同步
        if self._link_user_to_group(str(event.get_sender_id()), group_id):
            self._save_bindings()

        # Map dimension to internal key
        dim_map = {
            "游戏数": "count",
            "数量": "count",
            "时长": "time",
            "时间": "time",
            "肝度": "time",
        }
        sort_by = dim_map.get(dimension, "count")
        group_binding_map = self.group_bindings.get(group_id, {})
        if not group_binding_map:
            yield event.plain_result(
                "本群尚无用户绑定 Steam ID。请先使用 /绑定steam <SteamID64> 或在本群输入 /绑定steam 同步已有绑定。"
            )
            return

        title = "群内 Steam 游戏数排行" if sort_by == "count" else "群内 Steam 肝帝排行"
        yield event.plain_result(f"正在统计{title}，请稍候...")

        rank_data = []

        tasks = []
        user_ids = []

        for user_id, steam_id in group_binding_map.items():
            tasks.append(self.steam_api.get_owned_games(steam_id))
            user_ids.append(user_id)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Also fetch summaries for avatars
        summary_tasks = [
            self.steam_api.get_player_summaries(group_binding_map[uid])
            for uid in user_ids
        ]
        summaries = await asyncio.gather(*summary_tasks, return_exceptions=True)

        for i, games in enumerate(results):
            if isinstance(games, list):
                user_id = user_ids[i]
                summary = summaries[i] if isinstance(summaries[i], dict) else {}
                self._ensure_static_avatar(summary)

                # Calculate metrics
                game_count = len(games)
                total_minutes = sum(g.get("playtime_forever", 0) for g in games)

                # Sort games by playtime for display
                games.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)

                top_games = games[:5]
                await self._decorate_games_with_cover(top_games, "poster")

                rank_data.append(
                    {
                        "user_id": user_id,
                        "name": summary.get("personaname", f"User {user_id}"),
                        "avatar": summary.get("avatarfull", ""),
                        "count": game_count,
                        "time_minutes": total_minutes,
                        "time_str": self._format_playtime(total_minutes),
                        "top_games": top_games,  # Top 5 games for display
                    }
                )

        # Sort
        if sort_by == "time":
            rank_data.sort(key=lambda x: x["time_minutes"], reverse=True)
        else:
            rank_data.sort(key=lambda x: x["count"], reverse=True)

        if not rank_data:
            yield event.plain_result("无法获取排行数据。")
            return

        render_data = {
            "title": title,
            "sort_by": sort_by,
            "ranks": rank_data[:10],  # Top 10
        }

        template_path = self.templates_dir / "group_rank.html"
        with template_path.open("r", encoding="utf-8") as f:
            template_content = f.read()

        img_url = await self._render_html_local(
            template_content,
            render_data,
            width=800,
            image_type="jpeg",
            quality=self.image_quality,
        )
        yield event.image_result(img_url)
