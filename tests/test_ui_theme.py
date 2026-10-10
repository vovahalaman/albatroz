"""Design system: one source of colours, mirrored in .streamlit/config.toml; no stray colours in the UI code."""

import re
import tomllib
from pathlib import Path

from ui import theme

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b")


def test_streamlit_config_mirrors_theme_colours():
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())["theme"]
    assert cfg["base"] == "dark"
    assert cfg["primaryColor"] == theme.COLORS["accent"]
    assert cfg["backgroundColor"] == theme.COLORS["bg"]
    assert cfg["secondaryBackgroundColor"] == theme.COLORS["panel"]
    assert cfg["textColor"] == theme.COLORS["text"]


def test_no_hard_coded_colours_outside_theme():
    for name in ("app.py", "components.py", "backend.py"):
        assert not HEX.findall((ROOT / "ui" / name).read_text()), f"colour literal in ui/{name}"


def test_css_uses_the_tokens():
    css = theme.css()
    for key in ("bg", "panel", "border", "text", "muted", "accent", "warning"):
        assert theme.COLORS[key] in css
    assert "JetBrains Mono" in css and "Inter" in css
    assert theme.COLORS["accent"] in theme.map_css() or theme.COLORS["panel_2"] in theme.map_css()
