from astrbot.api import logger
from playwright.async_api import (
    Page,
    ViewportSize,
    async_playwright,
    Browser,
    Playwright,
)


_playwright_instance: Playwright | None = None
_browser_instance: Browser | None = None


async def get_browser() -> Browser | None:
    """获取或创建复用的浏览器实例"""
    global _playwright_instance, _browser_instance

    if _browser_instance and _playwright_instance:
        try:
            if _browser_instance.is_connected():
                return _browser_instance
        except Exception:
            pass

    try:
        _playwright_instance = await async_playwright().start()

        chrome_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

        _browser_instance = await _playwright_instance.chromium.launch(
            headless=True,
            args=chrome_args,
        )
        return _browser_instance
    except Exception as e:
        logger.error(f"初始化浏览器失败: {e}")
        return None


async def close_browser():
    """关闭浏览器实例"""
    global _playwright_instance, _browser_instance

    if _browser_instance:
        try:
            await _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None

    if _playwright_instance:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None


async def create_page(
    width: int = 1400,
    height: int = 1000,
    scale_factor: int = 2,
    is_mobile: bool = False,
) -> Page | None:
    browser = await get_browser()
    if not browser:
        return None

    try:
        context = await browser.new_context(
            viewport=ViewportSize(width=width, height=height),
            device_scale_factor=scale_factor,
            is_mobile=is_mobile,
            has_touch=is_mobile,
            bypass_csp=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        page = await context.new_page()

        async def handle_route(route):
            request = route.request
            url = request.url

            steam_domains = [
                "steamcdn-a.akamaihd.net",
                "steamcommunity.com",
                "steampowered.com",
                "cloudflare.steamstatic.com",
                "cdn.cloudflare.steamstatic.com",
                "cdn.steamstatic.com",
                "media.steampowered.com",
            ]

            is_steam_image = any(domain in url for domain in steam_domains) and (
                ".jpg" in url or ".png" in url or ".gif" in url or "/images/" in url
            )

            if is_steam_image:
                headers = {
                    **request.headers,
                    "Referer": "https://steamcommunity.com/",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                }
                await route.continue_(headers=headers)
            else:
                await route.continue_()

        await context.route("**/*", handle_route)

        await page.set_extra_http_headers(
            {
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

        return page
    except Exception as e:
        logger.error(f"创建页面失败: {e}")
        return None


async def render_html_to_image(
    html_content: str,
    selector: str = "body",
    width: int = 1400,
    scale_factor: int = 2,
    is_mobile: bool = False,
    full_page: bool = True,
    timeout: int = 30000,
    image_type: str = "jpeg",
    quality: int = 90,
) -> bytes | None:
    page = await create_page(
        width=width,
        height=1000,
        scale_factor=scale_factor,
        is_mobile=is_mobile,
    )
    if not page:
        return None

    try:
        await page.set_content(
            html_content, wait_until="domcontentloaded", timeout=timeout
        )

        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout, 10000))
        except Exception:
            pass

        await page.wait_for_timeout(1000)

        try:
            img_status = await page.evaluate("""
                () => {
                    const images = document.querySelectorAll('img');
                    let loaded = 0;
                    let failed = 0;
                    let pending = 0;
                    const failedUrls = [];
                    
                    images.forEach(img => {
                        if (img.complete && img.naturalHeight > 0) {
                            loaded++;
                        } else if (img.complete) {
                            failed++;
                            failedUrls.push(img.src);
                        } else {
                            pending++;
                        }
                    });
                    
                    return { total: images.length, loaded, failed, pending, failedUrls };
                }
            """)
            logger.info(f"[Playwright] 图片状态: {img_status}")
            if img_status.get("failedUrls"):
                logger.warning(
                    f"[Playwright] 加载失败的图片 URL: {img_status['failedUrls']}"
                )

            await page.evaluate("""
                () => {
                    const images = document.querySelectorAll('img');
                    return Promise.all(
                        Array.from(images).map(img => {
                            if (img.complete) return Promise.resolve();
                            return new Promise((resolve) => {
                                img.onload = resolve;
                                img.onerror = resolve;
                                setTimeout(resolve, 5000);
                            });
                        })
                    );
                }
            """)
        except Exception as e:
            logger.warning(f"[Playwright] 检查图片状态失败: {e}")

        # Get the actual content height and resize viewport to fit all content
        try:
            content_height = await page.evaluate("""
                () => {
                    const container = document.querySelector('.container');
                    const body = document.body;
                    const scrollHeight = Math.max(
                        container ? container.scrollHeight : 0,
                        body ? body.scrollHeight : 0,
                        document.documentElement.scrollHeight
                    );
                    return scrollHeight;
                }
            """)
            if content_height and content_height > 0:
                # Add some padding and resize viewport
                new_height = content_height + 100
                await page.set_viewport_size({"width": width, "height": new_height})
                # Wait for layout to adjust
                await page.wait_for_timeout(500)
                logger.info(f"[Playwright] 调整视口高度为 {new_height}px")
        except Exception as e:
            logger.warning(f"[Playwright] 调整视口高度失败: {e}")

        content_selector = ".container"
        locator = page.locator(content_selector)

        if await locator.count() > 0:
            screenshot_bytes = await locator.screenshot(
                type=image_type,
                quality=quality if image_type == "jpeg" else None,
                omit_background=False,
                animations="disabled",
            )
        else:
            body_locator = page.locator("body")
            screenshot_bytes = await body_locator.screenshot(
                type=image_type,
                quality=quality if image_type == "jpeg" else None,
                omit_background=False,
                animations="disabled",
            )

        return screenshot_bytes
    except Exception as e:
        logger.error(f"渲染 HTML 失败: {e}")
        return None
    finally:
        if page:
            try:
                context = page.context
                await page.close()
                await context.close()
            except Exception:
                pass
