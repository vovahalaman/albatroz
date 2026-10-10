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

    assert len(vs) == 13
    names = [v.name for v in vs]
    assert names[:8] == [f"h{a:03d}" for a in range(0, 360, 45)]
    assert names[8:12] == ["g000", "g090", "g180", "g270"]
    assert names[12] == "overview"
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{n}.jpg" for n in names)

    by_name = {v.name: v for v in vs}
    assert by_name["h090"].yaw == 90.0 and by_name["h180"].yaw == 180.0
    assert by_name["h270"].yaw == -90.0 and by_name["g270"].yaw == -90.0
    assert all(v.pitch == 0.0 and v.fov == 90.0 for v in vs[:8])
    assert all(v.pitch == -50.0 and v.fov == 90.0 for v in vs[8:12])

    for v in vs[:12]:
        assert v.path.parent == out
        assert Image.open(v.path).size == (1024, 1024)
    assert Image.open(by_name["overview"].path).size == (2048, 1024)
    assert _white(by_name["h090"].path)[512, 512]
    assert not _white(by_name["h270"].path).any()


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
