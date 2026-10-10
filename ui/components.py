"""UI building blocks. Each function renders one panel with classes from ui/theme.py (no inline colours)."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

import folium
import streamlit as st
from PIL import Image
from streamlit_folium import st_folium

from navegador.core.export import to_text
from navegador.core.state import State
from navegador.schemas import MCQ, Submission
from ui import theme
from ui.backend import RunView, breakdown, max_score, multiplier, penalty_factor


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def label(text: str) -> None:
    html(f'<p class="nv-label">{escape(text)}</p>')


def fmt_mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- top bar


def topbar(status: str, kind: str) -> None:
    """Brand and the 'what to do next' line (the timer is rendered by a fragment next to it)."""
    html(f"""
<div class="nv-topbar">
  <div class="nv-brand">Navegador <span>· operator</span></div>
  <div class="nv-status {kind}"><span class="nv-next">Next</span><span>{escape(status)}</span></div>
</div>""")


def timer(seconds_left: float, running: bool, backend_name: str) -> None:
    warn = " warn" if running and seconds_left <= 10 * 60 else ""
    state = "running" if running else "not started"
    html(f"""<div class="nv-timer"><div class="nv-huge{warn}">{fmt_mmss(seconds_left)}</div>
<div class="nv-muted">session {state} · {escape(backend_name)} backend</div></div>""")


# ---------------------------------------------------------------- run panels


def stage_chips(view: RunView) -> None:
    s = view.state
    codes = [c for c, _ in view.stages]
    cur = s.stage if s.stage in codes else ("S8" if s.status == "done" else codes[0])
    idx = codes.index(cur)
    chips = []
    for i, (code, name) in enumerate(view.stages):
        if s.status == "done" or i < idx:
            cls = "done"
        elif i == idx:
            cls = {"awaiting_human": "wait", "error": "error"}.get(s.status, "current")
        else:
            cls = ""
        chips.append(f'<span class="nv-chip {cls}">{code} {escape(name)}</span>')
    html(f'<div class="nv-chips">{"".join(chips)}</div>')


@st.cache_data(show_spinner=False, max_entries=64)
def _preview(path: str, mtime: float, width: int = 1600) -> Image.Image:
    with Image.open(path) as im:
        im.draft("RGB", (width, width // 2))
        return im.convert("RGB").resize((width, width // 2), Image.Resampling.LANCZOS)


def panorama(path: Path) -> None:
    if path.exists():
        st.image(_preview(str(path), path.stat().st_mtime), width="stretch")
    else:
        html(f'<p class="nv-muted">Panorama not found: {escape(str(path))}</p>')


def _bar(value: float) -> str:
    return f'<div class="nv-bar"><div style="width:{max(0.0, min(1.0, value)) * 100:.0f}%"></div></div>'


def findings(view: RunView) -> None:
    f = view.findings
    tabs = st.tabs(["Detective", "Geographer", "Reader", "Critic"])
    with tabs[0]:
        det = f.get("detective") or {}
        if det.get("summary"):
            html(f"<p>{escape(det['summary'])}</p>")
        hyps = sorted(view.state.hypotheses, key=lambda h: -h.weight)
        if hyps:
            total = sum(h.weight for h in hyps) or 1.0
            rows = "".join(f"<tr><td>{escape(h.label)}</td><td>{_bar(h.weight / total)}</td>"
                           f"<td class='num'>{h.weight / total:.0%}</td><td class='num'>{h.radius_m:,.0f} m</td></tr>"
                           for h in hyps)
            html(f"<table class='nv-table'><tr><th>Hypothesis</th><th style='width:35%'>Weight</th><th></th>"
                 f"<th>Radius</th></tr>{rows}</table>")
        elif not det:
            html('<p class="nv-muted">No hypotheses yet.</p>')
    with tabs[1]:
        g = f.get("geographer")
        if not g:
            html('<p class="nv-muted">No Geographer result yet.</p>')
        else:
            cp = g.get("country_probs", {})
            html("<div class='nv-kv'>" + "".join(f"<span class='k'>{k.upper()}</span><span class='v'>{v:.0%}</span>"
                                                   for k, v in cp.items()) + "</div>")
            rows = "".join(f"<tr><td>{escape(r['region'])}</td><td class='num'>{r['prob']:.0%}</td>"
                           f"<td class='nv-muted'>{escape(r.get('why', ''))}</td></tr>" for r in g.get("region_guesses", []))
            if rows:
                html(f"<table class='nv-table'><tr><th>Region</th><th>Prob</th><th>Why</th></tr>{rows}</table>")
            html(f"<p class='nv-muted'>Terrain: {escape(g.get('terrain', ''))} · Season: {escape(g.get('season', ''))}</p>")
    with tabs[2]:
        r = f.get("reader")
        if not r:
            html('<p class="nv-muted">No Reader result yet.</p>')
        else:
            rows = "".join(f"<tr><td class='nv-mono'>{escape(t['text'])}</td><td>{escape(t['kind'])}</td>"
                           f"<td>{escape(t['language'])}</td><td class='num'>{t['view']}</td>"
                           f"<td class='num'>{t['confidence']:.2f}</td></tr>" for t in r.get("texts", []))
            html(f"<table class='nv-table'><tr><th>Text</th><th>Kind</th><th>Lang</th><th>View</th><th>Conf</th></tr>"
                 f"{rows}</table>" if rows else '<p class="nv-muted">No text found.</p>')
    with tabs[3]:
        a = f.get("advocate")
        if not a or not a.get("objections"):
            html('<p class="nv-muted">No objections (the Critic is off or found nothing).</p>')
        else:
            mult = a.get("weight_multipliers", {})
            rows = "".join(f"<tr><td>{escape(o['hypothesis_label'])}</td>"
                           f"<td class='nv-sev-{o['severity']}'>{o['severity']}</td><td>{escape(o['issue'])}</td>"
                           f"<td class='num'>{'×%.2f' % mult[o['hypothesis_label']] if o['hypothesis_label'] in mult else ''}</td></tr>"
                           for o in a["objections"])
            html(f"<table class='nv-table'><tr><th>Hypothesis</th><th>Severity</th><th>Objection</th><th>Weight</th></tr>"
                 f"{rows}</table>")


def candidate_map(state: State, sub: Submission, height: int = 360) -> None:
    pts = [(c.lat, c.lng) for c in sub.candidates]
    m = folium.Map(location=pts[0], zoom_start=7, tiles=None, control_scale=True)
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
    for z in state.excluded_zones:
        folium.Circle((z.lat, z.lng), radius=z.radius_m, color=c["warning"], weight=2, dash_array="6",
                      fill=False, tooltip=f"Excluded: {z.label}").add_to(m)
    seen: dict[tuple[float, float], list[int]] = {}
    for cand in sub.candidates:
        seen.setdefault((cand.lat, cand.lng), []).append(cand.rank)
    for (lat, lng), ranks in seen.items():
        tag = f"{ranks[0]}" if len(ranks) == 1 else f"{ranks[0]}–{ranks[-1]}"
        icon = folium.DivIcon(html=f'<div style="font-family:{theme.FONTS["mono"]};font-size:12px;font-weight:600;'
                                   f'color:{c["on_accent"]};background:{c["accent"]};border-radius:999px;padding:2px 8px;'
                                   f'white-space:nowrap;transform:translate(-50%,-50%);display:inline-block">{tag}</div>')
        folium.Marker((lat, lng), icon=icon, tooltip=f"rank {tag}: {lat:.6f}, {lng:.6f}").add_to(m)
    if len(seen) + len(state.hypotheses) > 1:
        m.fit_bounds([*pts, *[(h.lat, h.lng) for h in state.hypotheses]], padding=(40, 40))
    st_folium(m, height=height, use_container_width=True, returned_objects=[], key=f"map-{state.pano_id}")


def candidate_list(sub: Submission) -> None:
    rows = "".join(f"<tr><td class='num'>{c.rank}</td><td class='num'>{c.lat:.6f}</td><td class='num'>{c.lng:.6f}</td></tr>"
                   for c in sub.candidates)
    html(f"<table class='nv-table'><tr><th>#</th><th>Lat</th><th>Lng</th></tr>{rows}</table>")


# ---------------------------------------------------------------- right column


def score_panel(state: State, expected: float) -> None:
    pf = multiplier(state)
    rows = "".join(f"<span class='k'>{name} ({n})</span><span class='v{' nv-warn' if n else ''}'>×{f:.3f}</span>"
                   for name, n, f in breakdown(state))
    special = "<span class='k'>Special One</span><span class='v nv-accent'>×3</span>" if state.special_one else ""
    nxt_cue, nxt_q = expected * penalty_factor("cue"), expected * penalty_factor("mcq")
    html(f"""
<p class="nv-label">Score</p>
<div class="nv-row"><div><div class="nv-muted">Penalty multiplier</div>
  <div class="nv-big{' nv-warn' if pf < 1 else ''}">×{pf:.3f}</div></div>
  <div style="text-align:right"><div class="nv-muted">Expected score</div>
  <div class="nv-big">{expected:,.0f}</div><div class="nv-muted nv-mono">of {max_score(state):,.0f}</div></div></div>
<div class="nv-kv" style="margin-top:12px">{rows}{special}
  <span class='k'>After one more cue</span><span class='v nv-warn'>{nxt_cue:,.0f}</span>
  <span class='k'>After one more answer</span><span class='v nv-warn'>{nxt_q:,.0f}</span></div>""")


def question_card(mcq: MCQ | None, state: State, enabled: bool, reason: str, note: str = "") -> str | None:
    """The agent's question with one button per option. Returns the clicked option id."""
    label("Question from the agent")
    if mcq is None:
        html(f'<p class="nv-muted">No question pending. {escape(reason)}</p>')
        return None
    pf = multiplier(state)
    html(f'<p class="nv-question">{escape(mcq.question)}</p>')
    clicked = None
    for o in mcq.options:
        key = f"opt-{mcq.mcq_id}-{o.id}" + ("-none" if o.cluster_id is None else "")
        if st.button(f"{o.id} · {o.text}", key=key, type="secondary", disabled=not enabled,
                     width="stretch"):
            clicked = o.id
    if any(o.cluster_id is None for o in mcq.options):
        html('<p class="nv-muted">"None of these" excludes the listed areas and the agent searches again.</p>')
    html(f'<p class="nv-cost">Answer costs ×{penalty_factor("mcq")} · ×{pf:.3f} → ×{pf * penalty_factor("mcq"):.3f}</p>')
    if reason:
        html(f'<p class="nv-muted">Why asked: {escape(reason)}</p>')
    if note:
        html(f'<p class="nv-muted nv-warn">{escape(note)}</p>')
    return clicked


def cue_cost(state: State) -> None:
    pf = multiplier(state)
    html(f'<p class="nv-cost">Cue costs ×{penalty_factor("cue")} on all candidates · ×{pf:.3f} → '
         f'×{pf * penalty_factor("cue"):.3f}</p>')


def quote(text: str) -> None:
    html(f'<div class="nv-quote">{escape(text)}</div>')


def result_card(sub: Submission, final: bool) -> None:
    label("Submission · 10 coordinates" if final else "Submission preview · not final yet")
    st.code(to_text(sub), language=None)
    html('<p class="nv-muted">Copy: icon in the top-right corner of the box. One "lat, lng" per line.</p>')


def downloads(pano_id: str, log_json: str, sub: Submission | None) -> None:
    c1, c2 = st.columns(2)
    c1.download_button("log.json", data=log_json, file_name=f"{pano_id}_log.json", mime="application/json",
                       width="stretch", key=f"dl-log-{pano_id}")
    c2.download_button("submission.json", data=sub.model_dump_json(indent=2) if sub else "", width="stretch",
                       file_name=f"{pano_id}_submission.json", mime="application/json", disabled=sub is None,
                       key=f"dl-sub-{pano_id}")


def run_meta(view: RunView, elapsed_s: float) -> None:
    s = view.state
    words = {"awaiting_human": "waiting for you", "running": "agent working", "done": "submitted", "error": "error"}
    html(f'<p class="nv-muted nv-mono">{escape(words.get(s.status, s.status))} · {fmt_mmss(elapsed_s)} on this '
         f'panorama · mode {escape(s.mode)}</p>')


def queue_pills(label_: str, status: str, special: bool) -> str:
    cls = {"done": "solid", "running": "accent", "awaiting_human": "warn", "error": "error"}.get(status, "")
    words = {"awaiting_human": "question"}.get(status, status)
    star = '<span class="nv-pill accent">×3</span> ' if special else ""
    short = "non-urb" if label_ == "non_urban" else "urban"
    return (f'<div class="nv-row" style="justify-content:flex-end">{star}<span class="nv-pill">{short}</span>'
            f'<span class="nv-pill {cls}">{escape(words)}</span></div>')


def kv(rows: dict[str, Any]) -> None:
    html("<div class='nv-kv'>" + "".join(f"<span class='k'>{escape(str(k))}</span><span class='v'>{escape(str(v))}</span>"
                                        for k, v in rows.items()) + "</div>")
