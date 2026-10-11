"""Navegador design system: every colour, size and font lives here. .streamlit/config.toml mirrors COLORS.

Rules: dark background, panels slightly lighter, one accent for primary actions, one warning colour for
penalties, muted gray for secondary text. Sans-serif for UI text, monospace for coordinates, timers and
numbers. Sized to stay readable on a projector.
"""

from __future__ import annotations

COLORS = {
    "bg": "#0F1115",
    "panel": "#171A21",
    "panel_2": "#1E222B",        # inset areas inside a panel
    "border": "#2A2F3A",
    "text": "#E6E8EC",
    "muted": "#8B93A1",
    "accent": "#4F8CFF",          # primary actions, current stage, chosen items
    "on_accent": "#FFFFFF",       # text on accent
    "accent_soft": "rgba(79,140,255,0.14)",
    "warning": "#F2A541",         # penalties and costs
    "warning_soft": "rgba(242,165,65,0.14)",
    "error": "#EF5B5B",           # only for failures
    "error_soft": "rgba(239,91,91,0.14)",
}
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}  # px
RADIUS = 10  # px
FONTS = {
    "ui": "'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "mono": "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace",
}
SIZE = {"base": 16, "small": 13, "label": 12, "title": 20, "status": 22, "brand": 26, "big": 32, "huge": 44}  # px
GOOGLE_FONTS = ("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"
                "&family=JetBrains+Mono:wght@400;500;600&display=swap")

# Dark basemap without an API key (Esri Canvas, attribution required); labels as a separate layer.
MAP_TILES = ("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
             "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}")
MAP_ATTR = "Esri, HERE, Garmin, © OpenStreetMap contributors"


def map_css() -> str:
    """Styles injected into the Leaflet iframe (controls and attribution in the dark theme)."""
    c, f = COLORS, FONTS
    return f"""<style>
.leaflet-container {{ background: {c["bg"]}; font-family: {f["ui"]}; }}
.leaflet-bar a, .leaflet-bar a:hover {{ background: {c["panel_2"]}; color: {c["text"]}; border-color: {c["border"]}; }}
.leaflet-control-attribution, .leaflet-control-scale-line {{ background: rgba(15,17,21,.7) !important;
  color: {c["muted"]} !important; border-color: {c["border"]} !important; font-size: 10px; }}
.leaflet-control-attribution a {{ color: {c["muted"]} !important; }}
.leaflet-tooltip {{ background: {c["panel"]}; color: {c["text"]}; border: 1px solid {c["border"]};
  font-family: {f["ui"]}; box-shadow: none; }}
</style>"""


def css() -> str:
    """Global stylesheet, injected once per page run."""
    c, s, f, z, r = COLORS, SPACE, FONTS, SIZE, RADIUS
    return f"""
<style>
@import url('{GOOGLE_FONTS}');
html, body, [class*="css"], .stApp, .stMarkdown, button, input, textarea {{
  font-family: {f["ui"]}; font-size: {z["base"]}px; color: {c["text"]};
}}
.stApp {{ background: {c["bg"]}; }}
header[data-testid="stHeader"], #MainMenu, footer, [data-testid="stToolbar"] {{ display: none !important; }}
.block-container {{ padding: {s["lg"]}px {s["xl"]}px {s["xl"]}px {s["xl"]}px !important; max-width: 1480px !important; }}
[data-testid="stVerticalBlock"] {{ gap: {s["lg"]}px; }}  /* one rhythm between stacked panels */

/* panels = bordered containers */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {{
  background: {c["panel"]}; border: 1px solid {c["border"]} !important; border-radius: {r}px !important;
}}
[data-testid="stExpander"] details, [data-testid="stTabs"] {{ border-color: {c["border"]}; }}

/* typography helpers */
.stMarkdown p {{ margin-bottom: {s["sm"]}px; }}
.nv-label, .stMarkdown p.nv-label {{ font-size: {z["label"]}px !important; letter-spacing: .08em; text-transform: uppercase; color: {c["muted"]};
  font-weight: 600; margin: 0 0 {s["sm"]}px 0; }}
.nv-title, .stMarkdown p.nv-title {{ font-size: {z["title"]}px !important; font-weight: 600; margin: 0; }}
.nv-muted, .stMarkdown p.nv-muted {{ color: {c["muted"]}; font-size: {z["small"]}px !important; }}
.nv-mono {{ font-family: {f["mono"]}; font-variant-numeric: tabular-nums; }}
.nv-big {{ font-family: {f["mono"]}; font-size: {z["big"]}px; font-weight: 600; line-height: 1.1; }}
.nv-huge {{ font-family: {f["mono"]}; font-size: {z["huge"]}px; font-weight: 600; line-height: 1; }}
.nv-warn {{ color: {c["warning"]}; }}
.nv-accent {{ color: {c["accent"]}; }}
.nv-error {{ color: {c["error"]}; }}

/* top bar */
.nv-topbar {{ display: flex; align-items: center; gap: {s["xl"]}px; }}
.nv-brand {{ font-weight: 700; font-size: {z["brand"]}px; letter-spacing: .02em; line-height: 1.2; }}
.nv-brand span {{ color: {c["muted"]}; font-weight: 500; }}
.nv-status {{ flex: 1; display: flex; align-items: center; gap: {s["md"]}px; padding: {s["sm"]}px {s["lg"]}px;
  border-radius: {r}px; background: {c["accent_soft"]}; border: 1px solid {c["accent"]}; font-weight: 500; }}
.nv-status {{ font-size: {z["title"]}px; }}
.nv-status.warn {{ background: {c["warning_soft"]}; border-color: {c["warning"]}; }}
.nv-status.done {{ background: {c["accent"]}; border-color: {c["accent"]}; color: {c["on_accent"]}; }}
.nv-status.error {{ background: {c["error_soft"]}; border-color: {c["error"]}; }}
.nv-status .nv-next {{ font-size: {z["label"]}px; letter-spacing: .08em; text-transform: uppercase;
  color: {c["muted"]}; font-weight: 600; }}
.nv-timer {{ text-align: right; }}
.nv-timer .nv-huge.warn {{ color: {c["warning"]}; }}

/* pills and stage chips */
.nv-pill {{ display: inline-block; padding: 2px {s["sm"]}px; border-radius: 999px; font-size: {z["small"]}px;
  border: 1px solid {c["border"]}; color: {c["muted"]}; white-space: nowrap; }}
.nv-pill.accent {{ color: {c["accent"]}; border-color: {c["accent"]}; background: {c["accent_soft"]}; }}
.nv-pill.warn {{ color: {c["warning"]}; border-color: {c["warning"]}; background: {c["warning_soft"]}; }}
.nv-pill.error {{ color: {c["error"]}; border-color: {c["error"]}; background: {c["error_soft"]}; }}
.nv-pill.solid {{ color: {c["bg"]}; background: {c["accent"]}; border-color: {c["accent"]}; }}
.nv-chips {{ display: flex; flex-wrap: wrap; gap: {s["xs"]}px; }}
.nv-chip {{ flex: 1 1 0; text-align: center; white-space: nowrap; font-family: {f["mono"]}; font-size: {z["label"]}px;
  padding: {s["xs"]}px 2px;
  border-radius: 6px; border: 1px solid {c["border"]}; color: {c["muted"]}; }}
.nv-chip.done {{ color: {c["text"]}; background: {c["panel_2"]}; }}
.nv-chip.current {{ color: {c["accent"]}; border-color: {c["accent"]}; background: {c["accent_soft"]}; }}
.nv-chip.wait {{ color: {c["warning"]}; border-color: {c["warning"]}; background: {c["warning_soft"]}; }}
.nv-chip.error {{ color: {c["error"]}; border-color: {c["error"]}; }}

/* queue rows */
.nv-row {{ display: flex; justify-content: space-between; align-items: center; gap: {s["sm"]}px; }}
.nv-file {{ font-family: {f["mono"]}; font-size: {z["small"]}px; overflow: hidden; text-overflow: ellipsis; }}

/* key/value lists and tables */
.nv-kv {{ display: grid; grid-template-columns: auto 1fr; gap: {s["xs"]}px {s["md"]}px; font-size: {z["small"]}px; }}
.nv-kv .k {{ color: {c["muted"]}; }}
.nv-kv .v {{ font-family: {f["mono"]}; text-align: right; }}
.nv-table {{ width: 100%; border-collapse: collapse; font-size: {z["small"]}px; }}
.nv-table th {{ text-align: left; color: {c["muted"]}; font-weight: 600; font-size: {z["label"]}px;
  text-transform: uppercase; letter-spacing: .06em; padding: {s["xs"]}px {s["sm"]}px;
  border-bottom: 1px solid {c["border"]}; }}
.nv-table td {{ padding: {s["xs"]}px {s["sm"]}px; border-bottom: 1px solid {c["border"]}; }}
.nv-table td.num {{ font-family: {f["mono"]}; text-align: right; white-space: nowrap; }}
.nv-bar {{ height: 6px; border-radius: 3px; background: {c["panel_2"]}; overflow: hidden; }}
.nv-bar > div {{ height: 100%; background: {c["accent"]}; }}
.nv-sev-high {{ color: {c["error"]}; }} .nv-sev-medium {{ color: {c["warning"]}; }} .nv-sev-low {{ color: {c["muted"]}; }}

.nv-quote {{ background: {c["panel_2"]}; border-left: 3px solid {c["accent"]}; border-radius: 6px;
  padding: {s["sm"]}px {s["md"]}px; margin: 0 0 {s["sm"]}px 0; white-space: pre-wrap; }}
.nv-guard {{ background: {c["warning_soft"]}; border: 1px solid {c["warning"]}; border-radius: {r}px;
  padding: {s["sm"]}px {s["md"]}px; margin: 0 0 {s["sm"]}px 0; }}

.nv-link, .stMarkdown a.nv-link {{ color: {c["accent"]} !important; font-size: {z["small"]}px; font-weight: 600;
  text-decoration: none; white-space: nowrap; }}
.nv-link:hover {{ text-decoration: underline; }}
.nv-placeholder {{ display: flex; align-items: center; justify-content: center; color: {c["muted"]};
  background: {c["panel"]}; border: 1px dashed {c["border"]}; border-radius: {r}px; }}
[class*="st-key-cand-"] button {{ font-family: {f["mono"]}; justify-content: flex-start !important;
  text-align: left; min-height: 36px; white-space: pre; }}
[class*="st-key-cand-"] button > div, [class*="st-key-cand-"] button p {{ justify-content: flex-start !important;
  text-align: left; width: 100%; }}

/* header: app name left, clock right */
.nv-clock {{ font-family: {f["mono"]}; font-size: {z["big"]}px; font-weight: 600; text-align: right;
  font-variant-numeric: tabular-nums; line-height: 1.2; }}
.nv-clock.done {{ color: {c["accent"]}; font-size: {z["title"] + 4}px; }}

/* one progress bar for the whole run */
.nv-progress {{ margin-top: {s["xs"]}px; height: 12px; border-radius: 999px; background: {c["panel_2"]}; border: 1px solid {c["border"]};
  overflow: hidden; }}
.nv-progress > div {{ height: 100%; background: {c["accent"]}; border-radius: 999px; transition: width 1s linear; }}
.nv-progress.warn > div {{ background: {c["warning"]}; }}
.nv-progress.error > div {{ background: {c["error"]}; }}
.nv-stage-line, .stMarkdown p.nv-stage-line {{ margin: {s["md"]}px 0 {s["xl"]}px 0; font-size: {z["status"]}px !important;
  font-weight: 500; }}
.nv-stage-line.warn {{ color: {c["warning"]}; }} .nv-stage-line.error {{ color: {c["error"]}; }}
.nv-stage-line.done {{ color: {c["accent"]}; }}

/* result card */
.nv-result {{ padding: {s["sm"]}px 0; }}
.nv-result-place {{ font-size: {z["huge"] - 8}px; font-weight: 700; line-height: 1.15; }}
.nv-result-pct {{ font-family: {f["mono"]}; color: {c["accent"]}; font-size: {z["big"] - 4}px; margin-left: {s["md"]}px; }}
.nv-result-summary, .stMarkdown p.nv-result-summary {{ font-size: {z["title"]}px !important; color: {c["muted"]};
  margin: {s["sm"]}px 0 0 0; }}
.nv-row-x10, .stMarkdown p.nv-row-x10 {{ font-size: {z["title"]}px !important; margin: 0; }}

.nv-gap {{ height: {s["sm"]}px; }}
/* coordinates panel: row, copy button and downloads spread over the panel height (no empty gap below) */
.st-key-coords-panel {{ justify-content: space-between !important; }}

.nv-chip-row, .stMarkdown p.nv-chip-row {{ margin: {s["xs"]}px 0 {s["md"]}px 0; }}
.st-key-header {{ margin-bottom: {s["lg"]}px; }}  /* air between the header and what follows */
.nv-detected {{ font-size: {z["base"]}px; padding: {s["xs"]}px {s["md"]}px; cursor: help; }}

/* the one big Start button */
.st-key-start button {{ min-height: 64px !important; font-size: {z["title"]}px !important; }}
[data-testid="stFileUploaderDropzone"] {{ background: {c["panel_2"]}; border: 1px dashed {c["border"]};
  border-radius: {r}px; }}
[data-testid="stExpander"] details {{ background: {c["panel"]}; border: 1px solid {c["border"]};
  border-radius: {r}px; }}
[data-testid="stExpander"] summary p {{ font-weight: 600; }}

/* question card: answer options are one choice group (outlined accent), not several primary buttons */
[class*="st-key-opt-"] button {{ background: {c["accent_soft"]} !important; border: 1px solid {c["accent"]} !important;
  color: {c["text"]} !important; font-weight: 600; min-height: 52px; font-size: {z["base"] + 2}px !important; }}
[class*="st-key-opt-"] button:hover {{ background: {c["accent"]} !important; color: {c["on_accent"]} !important; }}
[class*="st-key-opt-"].st-key-none button, [class*="st-key-opt-"][class*="-none"] button {{
  background: transparent !important; border-color: {c["border"]} !important; color: {c["muted"]} !important; }}
/* question card */
.nv-question, .stMarkdown p.nv-question {{ font-size: {z["title"]}px !important; font-weight: 600; line-height: 1.35; margin: 0 0 {s["md"]}px 0; }}
.nv-cost, .stMarkdown p.nv-cost {{ font-family: {f["mono"]}; color: {c["warning"]}; font-size: {z["small"]}px !important; }}

/* widgets */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-secondary"] {{ border-radius: {r}px;
  font-weight: 600; padding: {s["sm"]}px {s["lg"]}px; min-height: 44px; }}
[data-testid="stBaseButton-secondary"] {{ border: 1px solid {c["border"]}; background: {c["panel_2"]}; color: {c["text"]}; }}
[data-testid="stBaseButton-secondary"]:hover {{ border-color: {c["accent"]}; color: {c["text"]}; }}
[data-testid="stBaseButton-primary"] {{ background: {c["accent"]}; border: 1px solid {c["accent"]}; color: {c["on_accent"]}; }}
[data-testid="stBaseButton-primary"]:hover {{ filter: brightness(1.08); color: {c["on_accent"]}; }}
[data-testid="stBaseButton-primary"]:disabled, [data-testid="stBaseButton-secondary"]:disabled {{ opacity: .4; }}
[data-testid="stBaseButton-tertiary"] {{ justify-content: flex-start; font-family: {f["mono"]}; color: {c["text"]};
  padding: 2px {s["sm"]}px; min-height: 32px; border-radius: 6px; }}
[data-testid="stBaseButton-tertiary"]:hover {{ background: {c["panel_2"]}; color: {c["accent"]}; }}
textarea, input {{ background: {c["panel_2"]} !important; border-radius: {r}px !important; }}
[data-testid="stCode"] code, [data-testid="stCode"] pre {{ font-family: {f["mono"]} !important; font-size: {z["small"]}px; }}
[data-testid="stTabs"] button p {{ font-size: {z["small"]}px; font-weight: 600; }}
[data-testid="stImage"] img {{ border-radius: {r}px; border: 1px solid {c["border"]}; }}
iframe {{ border-radius: {r}px; }}
</style>
"""
