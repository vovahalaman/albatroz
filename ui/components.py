"""UI building blocks. Each function renders one part with classes from ui/theme.py (no colour literals)."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

import folium
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
from streamlit_folium import st_folium

from navegador.core.state import State
from navegador.schemas import MCQ, Submission
from ui import theme
from ui.runner import STAGES, RunView


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def label(text: str) -> None:
    html(f'<p class="nv-label">{escape(text)}</p>')


def fmt_mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def header(status: str = "", kind: str = "") -> None:
    line = f'<div class="nv-status {kind}"><span>{escape(status)}</span></div>' if status else ""
    html(f'<div class="nv-topbar"><div class="nv-brand">Navegador</div>{line}</div>')


def stage_chips(view: RunView) -> None:
    s, codes = view.state, [c for c, _ in STAGES]
    done_all = s.status == "done" and not view.busy
    idx = codes.index(s.stage) if s.stage in codes else len(codes)
    chips = []
    for i, (code, name) in enumerate(STAGES):
        if done_all or i < idx:
            cls = "done"
        elif i == idx:
            cls = "wait" if (s.pending_mcq is not None and not view.busy) else ("error" if view.error else "current")
        else:
            cls = ""
        chips.append(f'<span class="nv-chip {cls}">{code} {escape(name)}</span>')
    html(f'<div class="nv-chips">{"".join(chips)}</div>')


@st.cache_data(show_spinner=False, max_entries=16)
def _preview(path: str, mtime: float, width: int = 1600) -> Image.Image:
    with Image.open(path) as im:
        im.draft("RGB", (width, width // 2))
        return im.convert("RGB").resize((width, width // 2), Image.Resampling.LANCZOS)


def panorama(path: Path) -> None:
    st.image(_preview(str(path), path.stat().st_mtime), width="stretch")


def question_card(mcq: MCQ, enabled: bool) -> str | None:
    """The agent's question with one button per option. Returns the clicked option id."""
    label("The agent asks")
    html(f'<p class="nv-question">{escape(mcq.question)}</p>')
    clicked = None
    for o in mcq.options:
        key = f"opt-{mcq.mcq_id}-{o.id}" + ("-none" if o.cluster_id is None else "")
        if st.button(f"{o.id} · {o.text}", key=key, disabled=not enabled, width="stretch"):
            clicked = o.id
    if any(o.cluster_id is None for o in mcq.options):
        html('<p class="nv-muted">"None of these" rules out the listed areas; the agent searches again.</p>')
    return clicked


def _bar(value: float) -> str:
    return f'<div class="nv-bar"><div style="width:{max(0.0, min(1.0, value)) * 100:.0f}%"></div></div>'


def findings(view: RunView) -> None:
    f = view.findings
    tabs = st.tabs(["Detective", "Geographer", "Reader", "Critic"])
    with tabs[0]:
        det = f.get("detective") or {}
        if det.get("summary"):
            html(f"<p>{escape(str(det['summary']))}</p>")
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


def candidate_map(state: State, sub: Submission, height: int = 460) -> None:
    pts = [(c.lat, c.lng) for c in sub.candidates]
    m = folium.Map(location=pts[0], zoom_start=12 if state.label == "urban" else 8, tiles=None, control_scale=True)
    base, labels = theme.MAP_TILES
    folium.TileLayer(base, attr=theme.MAP_ATTR, name="base", max_zoom=16).add_to(m)
    folium.TileLayer(labels, attr=theme.MAP_ATTR, name="labels", overlay=True, max_zoom=16).add_to(m)
    m.get_root().header.add_child(folium.Element(theme.map_css()))
    c = theme.COLORS
    total = sum(h.weight for h in state.hypotheses) or 1.0
    for h in state.hypotheses:  # fixed pixel size scaled by weight: visible at every zoom
        share = h.weight / total
        folium.CircleMarker((h.lat, h.lng), radius=5 + 18 * share, color=c["accent"], weight=1, fill=True,
                            fill_opacity=0.15 + 0.45 * share, tooltip=f"{h.label} · {share:.0%}").add_to(m)
    seen: dict[tuple[float, float], list[int]] = {}
    for cand in sub.candidates:
        seen.setdefault((cand.lat, cand.lng), []).append(cand.rank)
    for (lat, lng), ranks in seen.items():
        tag = f"{ranks[0]}" if len(ranks) == 1 else f"{ranks[0]}–{ranks[-1]}"
        icon = folium.DivIcon(html=f'<div style="font-family:{theme.FONTS["mono"]};font-size:13px;font-weight:600;'
                                   f'color:{c["on_accent"]};background:{c["accent"]};border-radius:999px;'
                                   f'padding:2px 8px;white-space:nowrap;transform:translate(-50%,-50%);'
                                   f'display:inline-block">{tag}</div>')
        folium.Marker((lat, lng), icon=icon, tooltip=f"rank {tag}: {lat:.6f}, {lng:.6f}").add_to(m)
    if len(seen) > 1:
        m.fit_bounds(pts, padding=(40, 40))
    st_folium(m, height=height, use_container_width=True, returned_objects=[], key=f"map-{state.pano_id}")


def copy_button(text: str, caption: str = "Copy 10 coordinates") -> None:
    """A big button that copies `text` to the clipboard (Clipboard API, with a fallback for iframes)."""
    c, f, r = theme.COLORS, theme.FONTS, theme.RADIUS
    payload = json.dumps(text)
    components.html(f"""
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
<style>body{{margin:0;background:transparent}}</style>""", height=64)


def downloads(pano_id: str, log_json: str, sub: Submission) -> None:
    c1, c2 = st.columns(2)
    c1.download_button("Download log.json", data=log_json, file_name=f"{pano_id}_log.json", mime="application/json",
                       width="stretch", key=f"dl-log-{pano_id}")
    c2.download_button("Download submission.json", data=sub.model_dump_json(indent=2), width="stretch",
                       file_name=f"{pano_id}_submission.json", mime="application/json", key=f"dl-sub-{pano_id}")
