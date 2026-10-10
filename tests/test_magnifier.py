from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from navegador.tools import magnifier

W, H = 1440, 720  # 4 px per degree


def _pano(path: Path, squares: list[tuple[float, float]], half_deg: float = 5.0) -> Path:
    """Black equirect with white squares centred at the given (yaw, pitch) points."""
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for yaw, pitch in squares:
        x = (yaw / 360.0 + 0.5) * W
        y = (0.5 - pitch / 180.0) * H
        r = half_deg * W / 360.0
        img[int(y - r) : int(y + r), int(x - r) : int(x + r)] = 255
    Image.fromarray(img).save(path, quality=95)
    return path


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAVEGADOR_CACHE_DIR", str(tmp_path / "cache"))
    magnifier._decode.cache_clear()


def _white(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L")) > 200


def _centroid(mask: np.ndarray) -> tuple[float, float]:
    ys, xs = np.nonzero(mask)
    return float(xs.mean()), float(ys.mean())


def test_square_at_yaw90_centred_at_u90_absent_at_minus90(tmp_path: Path) -> None:
    pano = _pano(tmp_path / "p.jpg", [(90.0, 0.0)])
    right = _white(magnifier.zoom(pano, 90.0, 0.0, 90.0, size=256))
    assert right[128, 128]
    cx, cy = _centroid(right)
    assert abs(cx - 128) < 3 and abs(cy - 128) < 3
    left = _white(magnifier.zoom(pano, -90.0, 0.0, 90.0, size=256))
    assert not left.any()


def test_square_at_pitch30_centred_at_v30(tmp_path: Path) -> None:
    pano = _pano(tmp_path / "p.jpg", [(0.0, 30.0)])
    up = _white(magnifier.zoom(pano, 0.0, 30.0, 90.0, size=256))
    cx, cy = _centroid(up)
    assert abs(cx - 128) < 3 and abs(cy - 128) < 3
    level = _white(magnifier.zoom(pano, 0.0, 0.0, 90.0, size=256))
    assert _centroid(level)[1] < 128 - 30  # above centre when looking level


def test_wrap_yaw() -> None:
    assert [magnifier.wrap_yaw(a) for a in range(0, 360, 45)] == [0, 45, 90, 135, 180, -135, -90, -45]
    assert magnifier.wrap_yaw(-180.0) == 180.0
    assert magnifier.wrap_yaw(450.0) == 90.0


def test_views_names_sizes_and_angles(tmp_path: Path) -> None:
    pano = _pano(tmp_path / "p.jpg", [(90.0, 0.0)])
    out = tmp_path / "views"
    vs = magnifier.views(pano, out)

    assert len(vs) == 17
    names = [v.name for v in vs]
    assert names[:8] == [f"h{a:03d}" for a in range(0, 360, 45)]
    assert names[8:12] == ["g000", "g090", "g180", "g270"]
    assert names[12:16] == ["u000", "u090", "u180", "u270"]
    assert names[16] == "overview"
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{n}.jpg" for n in names)

    by_name = {v.name: v for v in vs}
    assert by_name["h090"].yaw == 90.0 and by_name["h180"].yaw == 180.0
    assert by_name["h270"].yaw == -90.0 and by_name["g270"].yaw == -90.0
    assert all(v.pitch == 0.0 and v.fov == 90.0 for v in vs[:8])
    assert all(v.pitch == -50.0 and v.fov == 90.0 for v in vs[8:12])
    assert all(v.pitch == 50.0 and v.fov == 90.0 for v in vs[12:16])
    assert [v.yaw for v in vs[12:16]] == [0.0, 90.0, 180.0, -90.0]

    for v in vs[:16]:
        assert v.path.parent == out
        assert Image.open(v.path).size == (1024, 1024)
    assert Image.open(by_name["overview"].path).size == (2048, 1024)
    assert _white(by_name["h090"].path)[512, 512]
    assert not _white(by_name["h270"].path).any()


def _directions(n: int = 20_000, seed: int = 0) -> np.ndarray:
    """Uniform unit vectors on the sphere; x = right, y = up, z = forward (yaw 0, pitch 0)."""
    d = np.random.default_rng(seed).normal(size=(n, 3))
    return d / np.linalg.norm(d, axis=1, keepdims=True)


def _margin_deg(d: np.ndarray, yaw: float, pitch: float, half_fov: float = 45.0) -> np.ndarray:
    """Angular margin to the frame edge of a perspective view (py360convert: u > 0 right, v > 0 up).

    Positive = inside the frame (|x/z| ≤ 1, |y/z| ≤ 1, z > 0 for FOV 90); −inf behind the camera.
    """
    u, v = np.radians(yaw), np.radians(pitch)
    fwd = np.array([np.sin(u) * np.cos(v), np.sin(v), np.cos(u) * np.cos(v)])
    right = np.array([np.cos(u), 0.0, -np.sin(u)])
    up = np.cross(fwd, right)
    x, y, z = d @ right, d @ up, d @ fwd
    with np.errstate(divide="ignore", invalid="ignore"):
        ax = np.degrees(np.arctan(np.abs(x / z)))
        ay = np.degrees(np.arctan(np.abs(y / z)))
    return np.where(z > 0, half_fov - np.maximum(ax, ay), -np.inf)


def _best_margin(d: np.ndarray, rigs: list[tuple[float, float]]) -> np.ndarray:
    return np.max([_margin_deg(d, yaw, pitch) for yaw, pitch in rigs], axis=0)


def test_full_sphere_coverage() -> None:
    d = _directions()
    h = [(magnifier.wrap_yaw(a), 0.0) for a in range(0, 360, 45)]
    g = [(magnifier.wrap_yaw(a), magnifier.GROUND_PITCH) for a in range(0, 360, 90)]
    u = [(magnifier.wrap_yaw(a), magnifier.UP_PITCH) for a in range(0, 360, 90)]

    # sanity: u=90 looks right, v=30 looks up (same signs as the image tests above)
    assert _margin_deg(np.array([[1.0, 0.0, 0.0]]), 90.0, 0.0)[0] == pytest.approx(45.0)
    up30 = np.array([[0.0, np.sin(np.radians(30)), np.cos(np.radians(30))]])
    assert _margin_deg(up30, 0.0, 30.0)[0] == pytest.approx(45.0)

    full = _best_margin(d, h + g + u)
    print(f"\nworst-covered direction: {full.min():.2f}° inside the nearest frame edge")
    assert (full >= 0).all()

    without_up = _best_margin(d, h + g)
    holes = int((without_up < 0).sum())
    print(f"without u-views: {holes} / {len(d)} directions uncovered")
    assert holes > 0


def test_pano_decoded_once(tmp_path: Path) -> None:
    pano = _pano(tmp_path / "p.jpg", [])
    magnifier.views(pano, tmp_path / "views")
    magnifier.zoom(pano, 10.0, 5.0, 30.0, size=128)
    info = magnifier._decode.cache_info()
    assert info.misses == 1 and info.hits >= 1


@pytest.mark.parametrize(("asked", "used"), [(3.0, 8.0), (120.0, 90.0), (45.0, 45.0)])
def test_zoom_clamps_fov(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asked: float, used: float
) -> None:
    pano = _pano(tmp_path / "p.jpg", [])
    seen: list[float] = []
    real = magnifier._render

    def spy(img: np.ndarray, fov: float, yaw: float, pitch: float, size: int) -> np.ndarray:
        seen.append(fov)
        return real(img, fov, yaw, pitch, size)

    monkeypatch.setattr(magnifier, "_render", spy)
    path = magnifier.zoom(pano, 0.0, 0.0, asked, size=128)
    assert seen == [used]
    assert path == magnifier.zoom_cache_path(pano, 0.0, 0.0, used, size=128)


def test_zoom_cached_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pano = _pano(tmp_path / "p.jpg", [(30.0, 10.0)])
    calls: list[int] = []
    real = magnifier._render

    def spy(img: np.ndarray, fov: float, yaw: float, pitch: float, size: int) -> np.ndarray:
        calls.append(1)
        return real(img, fov, yaw, pitch, size)

    monkeypatch.setattr(magnifier, "_render", spy)
    first = magnifier.zoom(pano, 30.0, 10.0, 20.0, size=128)
    mtime = first.stat().st_mtime_ns
    second = magnifier.zoom(pano, 30.0, 10.0, 20.0, size=128)
    assert first == second and first.exists()
    assert first.parent == tmp_path / "cache" / "zoom"
    assert len(calls) == 1 and second.stat().st_mtime_ns == mtime
    magnifier.zoom(pano, 31.0, 10.0, 20.0, size=128)
    assert len(calls) == 2  # different parameters → new crop
