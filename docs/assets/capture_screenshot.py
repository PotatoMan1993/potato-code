"""生成 README 用的界面截图（无头运行，不发起真实 LLM 请求）。

运行：`.venv/Scripts/python.exe docs/assets/capture_screenshot.py`
产物：
- `docs/assets/screenshot.svg`：Textual 直接导出的矢量源图；
- `docs/assets/screenshot.png`：README 实际引用的位图（用本机 Edge/Chrome 渲染，
  避免不同平台的 SVG 字体差异导致中文错位）。

说明：Rich 导出 SVG 时用 `len(text)` 估算 `textLength`，中文占两格却被按一格压缩，
会出现文字重叠；这里统一去掉该属性，交给渲染器按字体实际宽度排布。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

from potatocode import __version__
from potatocode.config import ProviderConfig
from potatocode.permission.engine import new_engine
from potatocode.tool import new_default_registry
from potatocode.tui.app import PotatoCodeApp

ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(ASSETS_DIR))
SVG_PATH = os.path.join(ASSETS_DIR, "screenshot.svg")
PNG_PATH = os.path.join(ASSETS_DIR, "screenshot.png")


def _find_browser() -> str | None:
    """查找可用于无头渲染的 Chromium 浏览器；找不到时返回 None。"""
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _render_png(svg_path: str, png_path: str) -> None:
    """用无头浏览器把 SVG 渲染为 PNG；没有可用浏览器时只保留 SVG。"""
    browser = _find_browser()
    if browser is None:
        print("未找到 Edge/Chrome，跳过 PNG 渲染；SVG 已生成")
        return
    svg_text = Path(svg_path).read_text(encoding="utf-8")
    view_box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg_text)
    if view_box is None:
        width, height = 1482, 929
    else:
        width, height = int(float(view_box.group(1))), int(float(view_box.group(2)))
    try:
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--allow-file-access-from-files",
                f"--screenshot={png_path}",
                f"--window-size={width},{height}",
                Path(svg_path).as_uri(),
            ],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        print(f"PNG 渲染失败，仅保留 SVG：{error}")
        return
    print(f"已生成截图：{png_path}")


async def capture() -> None:
    """以桩配置启动 TUI，输入 `/` 唤起补全菜单后导出截图。"""
    provider_config = ProviderConfig(
        name="Claude",
        protocol="anthropic",
        api_key="sk-ant-placeholder",
        model="claude-sonnet-4-5",
    )
    registry = new_default_registry()
    engine, _error = new_engine(PROJECT_ROOT)
    app = PotatoCodeApp(
        [provider_config],
        __version__,
        registry,
        engine,
        workspace="D:/projects/potato-code",
    )
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        svg = app.export_screenshot(title="PotatoCode — 终端 AI 编程助手")
    # Rich 的 SVG 导出用 len(text) 估算 textLength，中文占两格却被按一格压缩，
    # 会出现文字重叠。去掉该属性，由渲染器按字体实际宽度排布。
    svg = re.sub(r'\s+textLength="[^"]*"', "", svg)
    with open(SVG_PATH, "w", encoding="utf-8") as file:
        file.write(svg)
    print(f"已生成截图：{SVG_PATH}")
    _render_png(SVG_PATH, PNG_PATH)


if __name__ == "__main__":
    asyncio.run(capture())
