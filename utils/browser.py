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
    height: int = 10000,
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
        )
        page = await context.new_page()
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
        height=10000,
        scale_factor=scale_factor,
        is_mobile=is_mobile,
    )
    if not page:
        return None

    try:
        await page.set_content(html_content, wait_until="networkidle", timeout=timeout)

        locator = page.locator(selector)
        if await locator.count() > 0:
            screenshot_bytes = await locator.screenshot(
                type=image_type,
                quality=quality if image_type == "jpeg" else None,
                omit_background=False,
                animations="disabled",
            )
        else:
            screenshot_bytes = await page.screenshot(
                full_page=full_page,
                type=image_type,
                quality=quality if image_type == "jpeg" else None,
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
