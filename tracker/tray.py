"""System tray icon.

Menu items: Status (label), Open Dashboard, Pause Tracking, Quit.
Uses pystray + Pillow to render a simple circle icon that turns grey
when paused and green when running.
"""

from __future__ import annotations

import webbrowser
from typing import Callable

from PIL import Image, ImageDraw
import pystray

from .config import API_HOST, API_PORT, Config


def _make_icon(paused: bool) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (128, 128, 128, 255) if paused else (46, 160, 67, 255)
    draw.ellipse((6, 6, size - 6, size - 6), fill=color)
    return img


def build_tray(config: Config, on_quit: Callable[[], None]) -> pystray.Icon:
    def status_text(_item: pystray.MenuItem) -> str:
        return f"Status: {'Paused' if config.paused else 'Running'}"

    def open_dashboard(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        # Prefer a central dashboard URL when baked into config; otherwise
        # fall through to the local /ping so the menu item does *something*
        # useful (confirms the tracker is alive).
        url = getattr(config, "dashboard_url", "") or ""
        if url:
            webbrowser.open(url)
        else:
            webbrowser.open(f"http://{API_HOST}:{API_PORT}/ping")

    def toggle_pause(icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        config.set_paused(not config.paused)
        icon.icon = _make_icon(config.paused)
        icon.update_menu()

    def quit_app(icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Dashboard", open_dashboard),
        pystray.MenuItem(
            lambda _item: "Resume Tracking" if config.paused else "Pause Tracking",
            toggle_pause,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    return pystray.Icon(
        "claude-tracker",
        _make_icon(config.paused),
        "Claude Usage Tracker",
        menu,
    )
