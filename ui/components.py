"""UI building blocks. Each function renders one part with classes from ui/theme.py (no colour literals)."""

from __future__ import annotations

import base64
import io
import json
from html import escape
from pathlib import Path

import folium
import streamlit as st
from PIL import Image
from streamlit_folium import st_folium

from navegador.core.state import State
from navegador.schemas import MCQ, Submission
from ui import streetview, theme
from ui.runner import OptionPoint, RunView, clean_summary

VIEWER_MAX_W = 4096  # px: the 360 viewer gets a downscaled copy so it loads fast
VIEWER_H = 460  # the 360 viewer and the Street View frame
PAIR_H = VIEWER_H + 58  # their panels: padding + header label + frame + padding
NOTE_H = 32  # room for the satellite-fallback note under the Street View frame
MCQ_PREVIEW_H = 300  # Street View preview of a question option's point
RESULT_ZOOM = {"urban": 15, "non_urban": 11}  # map zoom on the result
PANNELLUM = "https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build"


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def label(text: str) -> None:
    html(f'<p class="nv-label">{escape(text)}</p>')


def fmt_mss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def brand() -> None:
    html('<div class="nv-brand">Navegador</div>')


def timer(text: str, done: bool = False) -> None:
    html(f'<div class="nv-clock{" done" if done else ""}">{escape(text)}</div>')


def progress_bar(fraction: float, line: str, kind: str = "") -> None:
    """One bar for the whole run (CSS transition keeps it smooth between 1 s ticks) and one line under it."""
    pct = max(0.0, min(1.0, fraction)) * 100
    html(f'<div class="nv-progress {kind}"><div style="width:{pct:.1f}%"></div></div>'
         f'<p class="nv-stage-line {kind}">{escape(line)}</p>')


def label_chip(label_: str, reason: str, default: bool) -> None:
    """Read-only chip with the detected label; the reason is the tooltip."""
    text = f"Detected: {label_.replace('_', '-')}" + (" (default)" if default else "")
    html(f'<p class="nv-chip-row"><span class="nv-pill accent nv-detected" title="{escape(reason, quote=True)}">'
         f'{escape(text)}</span></p>')


def result_card(place: str, share: float, summary: str) -> None:
    pct = f"{share:.0%}" if share else ""
    html(f'<div class="nv-result"><div class="nv-result-place">{escape(place)}'
         f'{f" <span class=nv-result-pct>{pct}</span>" if pct else ""}</div>'
         f'{f"<p class=nv-result-summary>{escape(summary)}</p>" if summary else ""}</div>')


@st.cache_data(show_spinner=False, max_entries=8)
def viewer_html(path: str, mtime: float, north_offset: float | None = None) -> str:
    """Pannellum page for one panorama. Identical input → identical HTML, so Streamlit keeps the iframe
    across reruns. With `north_offset` (compass bearing of the image centre) the viewer shows a compass;
    that changes the HTML once, so the view (yaw, pitch, zoom) is kept in localStorage and restored."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > VIEWER_MAX_W:
            im = im.resize((VIEWER_MAX_W, VIEWER_MAX_W // 2), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    data = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    c, r = theme.COLORS, theme.RADIUS
    config = {"type": "equirectangular", "panorama": data, "autoLoad": True, "showZoomCtrl": True,
              "mouseZoom": True, "hfov": 100, "minHfov": 30, "maxHfov": 120, "showFullscreenCtrl": True}
    if north_offset is not None:
        config |= {"compass": True, "northOffset": round(north_offset, 1)}
    store = json.dumps(f"nv-view-{Path(path).name}-{int(mtime)}")
    return f"""<link rel="stylesheet" href="{PANNELLUM}/pannellum.css">
<script src="{PANNELLUM}/pannellum.js"></script>
<style>html,body{{margin:0;background:{c['bg']};overflow:hidden}}
#pano{{width:100%;height:{VIEWER_H}px;box-sizing:border-box;border-radius:{r}px;overflow:hidden;
  border:1px solid {c['border']}}}</style>
<div id="pano"></div>
<script>
const key = {store}, cfg = {json.dumps(config)};
try {{ Object.assign(cfg, JSON.parse(localStorage.getItem(key) || "{{}}")); }} catch (e) {{}}
const v = window.nvViewer = pannellum.viewer("pano", cfg);
const save = () => {{ try {{ localStorage.setItem(key, JSON.stringify(
  {{yaw: v.getYaw(), pitch: v.getPitch(), hfov: v.getHfov()}})); }} catch (e) {{}} }};
setInterval(save, 1000);
</script>"""


def viewer(path: Path | None, north_offset: float | None = None) -> None:
    """The interactive 360 view (drag to look around, scroll to zoom; compass when the sun gave north)."""
    label("Panorama")
    if path is None:
        html(f'<div class="nv-placeholder" style="height:{VIEWER_H}px">The 360 view appears here after upload.</div>')
        return
    st.iframe(viewer_html(str(path), path.stat().st_mtime, north_offset), height=VIEWER_H, alt="360 panorama")


def _link(href: str, text: str) -> str:
    return f'<a class="nv-link" href="{escape(href, quote=True)}" target="_blank" rel="noopener">{escape(text)}</a>'


def _option_points(mcq: MCQ, option_id: str, points: list[OptionPoint], can_preview: bool,
                   preview: tuple[str, int] | None) -> tuple[str, int] | None:
    """The agent's candidate points behind one option: name, share, radius, copyable coordinates, links and
    (with an embed key) a Street View preview toggle. Returns (option id, point index) when Preview is clicked."""
    asked = None
    for i, p in enumerate(points):
        html(f'<p class="nv-muted nv-point">{escape(p.label)} · {p.share:.0%} · ±{p.radius_m:,.0f} m</p>')
        cols = st.columns([2.3, 1, 1, 0.9] if can_preview else [2.3, 1, 1], vertical_alignment="center", gap="small")
        cols[0].code(f"{p.lat:.6f}, {p.lng:.6f}", language=None)  # Streamlit's code block has a copy icon
        cols[1].markdown(_link(streetview.streetview_link(p.lat, p.lng), "Street View"), unsafe_allow_html=True)
        cols[2].markdown(_link(streetview.maps_link(p.lat, p.lng), "Google Maps"), unsafe_allow_html=True)
        if can_preview:
            on = preview == (option_id, i)
            if cols[3].button("Hide" if on else "Preview", key=f"pv-{mcq.mcq_id}-{option_id}-{i}",
                              type="secondary" if on else "tertiary", width="stretch"):
                asked = (option_id, i)
    return asked


def question_card(mcq: MCQ, enabled: bool, points: dict[str, list[OptionPoint]] | None = None,
                  preview: tuple[str, int] | None = None, label_: str = "urban"
                  ) -> tuple[str | None, tuple[str, int] | None]:
    """The agent's question with one button per option and, under each, the points the agent has in mind
    (to check on a map before answering). Returns (clicked option id, clicked preview (option id, index))."""
    label("The agent asks")
    html(f'<p class="nv-question">{escape(mcq.question)}</p>')
    points = points or {}
    key_ = streetview.embed_key()
    clicked = asked = None
    for o in mcq.options:
        key = f"opt-{mcq.mcq_id}-{o.id}" + ("-none" if o.cluster_id is None else "")
        if st.button(f"{o.id} · {o.text}", key=key, disabled=not enabled, width="stretch"):
            clicked = o.id
        if o.id in points:
            asked = _option_points(mcq, o.id, points[o.id], key_ is not None, preview) or asked
    if any(o.cluster_id is None for o in mcq.options):
        html('<p class="nv-muted">"None of these" rules out the listed areas; the agent searches again.</p>')
    shown = points.get(preview[0], [])[preview[1]] if preview and preview[1] < len(points.get(preview[0], [])) else None
    if shown is not None and key_ is not None:
        emb = streetview.embed_for(shown.lat, shown.lng, label_, key_)
        label(f"Preview · {preview[0]} · {shown.label}")
        st.iframe(emb.url, height=MCQ_PREVIEW_H, alt="Street View preview")
        if emb.note:
            html(f'<p class="nv-muted nv-warn" style="margin:0">{escape(emb.note)}</p>')
    return clicked, asked


def _bar(value: float) -> str:
    return f'<div class="nv-bar"><div style="width:{max(0.0, min(1.0, value)) * 100:.0f}%"></div></div>'


def findings(view: RunView) -> None:
    f = view.findings
    tabs = st.tabs(["Detective", "Geographer", "Reader", "Critic"])
    with tabs[0]:
        det = f.get("detective") or {}
        if det.get("summary"):
            st.markdown(clean_summary(str(det["summary"])))  # rendered Markdown, never raw symbols
        hyps = sorted(view.state.hypotheses, key=lambda h: -h.weight)
        if hyps:
            total = sum(h.weight for h in hyps) or 1.0
            rows = "".join(f"<tr><td>{escape(h.label)}</td><td>{_bar(h.weight / total)}</td>"
                           f"<td class='num'>{h.weight / total:.0%}</td><td class='num'>{h.radius_m:,.0f} m</td></tr>"
                           for h in hyps)
            html(f"<table class='nv-table'><tr><th>Hypothesis</th><th style='width:35%'>Weight</th><th></th>"
                 f"<th>Radius</th></tr>{rows}</table>")
        elif not det:
            html('<p class="nv-muted">No Detective result.</p>')
    with tabs[1]:
        g = f.get("geographer")
        if not g:
            html('<p class="nv-muted">No Geographer result.</p>')
        else:
            cp = g.get("country_probs", {})
            html("<div class='nv-kv'>" + "".join(f"<span class='k'>{escape(k.upper())}</span>"
                                                   f"<span class='v'>{float(v):.0%}</span>" for k, v in cp.items())
                 + "</div>")
            rows = "".join(f"<tr><td>{escape(r.get('region', ''))}</td>"
                           f"<td class='num'>{float(r.get('prob', 0)):.0%}</td>"
                           f"<td class='nv-muted'>{escape(r.get('why', ''))}</td></tr>"
                           for r in g.get("region_guesses", []))
            if rows:
                html(f"<table class='nv-table'><tr><th>Region</th><th>Prob</th><th>Why</th></tr>{rows}</table>")
            html(f"<p class='nv-muted'>Terrain: {escape(str(g.get('terrain', '')))} · "
                 f"Season: {escape(str(g.get('season', '')))}</p>")
    with tabs[2]:
        r = f.get("reader")
        texts = (r or {}).get("texts", [])
        if not texts:
            html('<p class="nv-muted">No text found.</p>' if r else '<p class="nv-muted">No Reader result.</p>')
        else:
            rows = "".join(f"<tr><td class='nv-mono'>{escape(t.get('text', ''))}</td>"
                           f"<td>{escape(t.get('kind', ''))}</td><td>{escape(t.get('language', ''))}</td>"
                           f"<td class='num'>{escape(t.get('view', ''))}</td>"
                           f"<td class='num'>{float(t.get('confidence', 0)):.2f}</td></tr>" for t in texts)
            html(f"<table class='nv-table'><tr><th>Text</th><th>Kind</th><th>Lang</th><th>View</th><th>Conf</th></tr>"
                 f"{rows}</table>")
    with tabs[3]:
        a = f.get("advocate") or {}
        if not a.get("objections"):
            html('<p class="nv-muted">No objections (the Critic is off or found nothing).</p>')
        else:
            mult = a.get("weight_multipliers", {})
            rows = "".join(f"<tr><td>{escape(o['hypothesis_label'])}</td>"
                           f"<td class='nv-sev-{escape(o['severity'])}'>{escape(o['severity'])}</td>"
                           f"<td>{escape(o['issue'])}</td><td class='num'>"
                           f"{'×%.2f' % mult[o['hypothesis_label']] if o['hypothesis_label'] in mult else ''}</td></tr>"
                           for o in a["objections"])
            html(f"<table class='nv-table'><tr><th>Hypothesis</th><th>Severity</th><th>Objection</th>"
                 f"<th>Weight</th></tr>{rows}</table>")


def candidate_map(state: State, sub: Submission, selected: int, height: int = 420) -> tuple[float, float] | None:
    """Map with the 10 candidates (grouped when identical); returns the last clicked (lat, lng), if any."""
    pts = [(c.lat, c.lng) for c in sub.candidates]
    focus = next((p for c, p in zip(sub.candidates, pts) if c.rank == selected), pts[0])
    m = folium.Map(location=focus, zoom_start=RESULT_ZOOM.get(state.label, 12), tiles=None, control_scale=True)
    base, labels = theme.MAP_TILES
    folium.TileLayer(base, attr=theme.MAP_ATTR, name="base", max_zoom=16).add_to(m)
    folium.TileLayer(labels, attr=theme.MAP_ATTR, name="labels", overlay=True, max_zoom=16).add_to(m)
    m.get_root().header.add_child(folium.Element(theme.map_css()))
    c = theme.COLORS
    total = sum(h.weight for h in state.hypotheses) or 1.0
    for h in state.hypotheses:  # faint circles, pixel size by weight (visible at any zoom)
        share = h.weight / total
        folium.CircleMarker((h.lat, h.lng), radius=6 + 30 * share, color=c["accent"], weight=1, opacity=0.35,
                            fill=True, fill_opacity=0.06 + 0.14 * share, tooltip=f"{h.label} · {share:.0%}").add_to(m)
    seen: dict[tuple[float, float], list[int]] = {}
    for cand in sub.candidates:
        seen.setdefault((cand.lat, cand.lng), []).append(cand.rank)
    for (lat, lng), ranks in seen.items():
        tag = f"{ranks[0]}" if len(ranks) == 1 else f"{ranks[0]}–{ranks[-1]}"
        on = selected in ranks
        fg, bg, ring = ((c["on_accent"], c["accent"], c["on_accent"]) if on else (c["text"], c["panel_2"], c["accent"]))
        icon = folium.DivIcon(html=f'<div style="font-family:{theme.FONTS["mono"]};font-size:13px;font-weight:600;'
                                   f'color:{fg};background:{bg};border:2px solid {ring};border-radius:999px;'
                                   f'padding:2px 8px;white-space:nowrap;transform:translate(-50%,-50%);'
                                   f'display:inline-block;cursor:pointer">{tag}</div>')
        folium.Marker((lat, lng), icon=icon, tooltip=f"rank {tag}: {lat:.6f}, {lng:.6f}").add_to(m)
    if len(seen) > 1:  # different candidates: show them all; identical ones: zoom in on the result
        m.fit_bounds(pts, padding=(40, 40), max_zoom=RESULT_ZOOM.get(state.label, 12))
    out = st_folium(m, height=height, use_container_width=True, returned_objects=["last_object_clicked"],
                    key=f"map-{state.pano_id}")
    click = (out or {}).get("last_object_clicked")
    return (click["lat"], click["lng"]) if click else None


def _links(c2, c3, cand) -> None:
    c2.markdown(f'<a class="nv-link" href="{streetview.streetview_link(cand.lat, cand.lng)}" target="_blank" '
                f'rel="noopener">Street View</a>', unsafe_allow_html=True)
    c3.markdown(f'<a class="nv-link" href="{streetview.maps_link(cand.lat, cand.lng)}" target="_blank" '
                f'rel="noopener">Google Maps</a>', unsafe_allow_html=True)


def candidate_rows(sub: Submission, selected: int) -> int | None:
    """The coordinates with Street View / Google Maps links. All 10 identical → one row marked ×10;
    otherwise the ranked list, where clicking a row selects it. Returns the clicked rank."""
    cands = sorted(sub.candidates, key=lambda x: x.rank)
    if len({(c.lat, c.lng) for c in cands}) == 1:
        c1, c2, c3 = st.columns([3.4, 1, 1], vertical_alignment="center", gap="small")  # room for "×10"
        c1.markdown(f'<p class="nv-row-x10 nv-mono">{cands[0].lat:.6f}, {cands[0].lng:.6f}'
                    f' <span class="nv-pill accent">×10</span></p>', unsafe_allow_html=True)
        _links(c2, c3, cands[0])
        return None
    clicked = None
    for cand in cands:
        c1, c2, c3 = st.columns([2.4, 1, 1], vertical_alignment="center", gap="small")
        if c1.button(f"{cand.rank:>2}   {cand.lat:.6f}, {cand.lng:.6f}", key=f"cand-{cand.rank}",
                     type="secondary" if cand.rank == selected else "tertiary", width="stretch"):
            clicked = cand.rank
        _links(c2, c3, cand)
    return clicked


def streetview_panel(lat: float, lng: float, rank: int, label_: str) -> None:
    """Embedded Street View of the selected coordinate (satellite fallback), or the enable hint."""
    key = streetview.embed_key()
    label(f"Street View · #{rank} · {lat:.6f}, {lng:.6f}")
    if key is None:
        html(f'<div class="nv-placeholder" style="height:{VIEWER_H}px;padding:0 24px;text-align:center">'
             f'{escape(streetview.ENABLE_HINT)}</div>')
        return
    emb = streetview.embed_for(lat, lng, label_, key)
    st.iframe(emb.url, height=VIEWER_H - (NOTE_H if emb.note else 0), alt="Street View")  # panel keeps its height
    if emb.note:
        html(f'<p class="nv-muted nv-warn" style="margin:0">{escape(emb.note)}</p>')


def copy_button(text: str, caption: str = "Copy 10 coordinates") -> None:
    """A big button that copies `text` to the clipboard (Clipboard API, with a fallback for iframes)."""
    c, f, r = theme.COLORS, theme.FONTS, theme.RADIUS
    payload = json.dumps(text)
    st.iframe(f"""
<button id="copy" style="width:100%;height:60px;border:1px solid {c['accent']};border-radius:{r}px;
  background:{c['accent']};color:{c['on_accent']};font:600 19px {f['ui']};cursor:pointer">{escape(caption)}</button>
<script>
const text = {payload}, b = document.getElementById("copy"), idle = b.textContent;
b.onclick = async () => {{
  let ok = false;
  try {{ await navigator.clipboard.writeText(text); ok = true; }} catch (e) {{
    const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
    try {{ ok = document.execCommand("copy"); }} catch (e2) {{ ok = false; }}
    t.remove();
  }}
  b.textContent = ok ? "Copied" : "Copy blocked: select the box above";
  setTimeout(() => (b.textContent = idle), 2000);
}};
</script>
<style>body{{margin:0;background:transparent}}</style>""", height=64, alt=caption)


def gap() -> None:
    html('<div class="nv-gap"></div>')


def downloads(pano_id: str, log_json: str, sub: Submission) -> None:
    c1, c2 = st.columns(2)
    c1.download_button("Download IMG_20261005_171832_00_163_log.json", data=log_json, file_name=f"{pano_id}_log.json", mime="application/json",
                       width="stretch", key=f"dl-log-{pano_id}")
    c2.download_button("Download IMG_20261005_171832_00_163_submission.json", data=sub.model_dump_json(indent=2), width="stretch",
                       file_name=f"{pano_id}_submission.json", mime="application/json", key=f"dl-sub-{pano_id}")
