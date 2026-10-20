"""Tiny software renderer for mesh thumbnails (STL / OBJ / 3MF). numpy + Pillow only."""

from __future__ import annotations

import re
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

MAX_TRIANGLES = 250_000


# ---- loaders: all return float32 array of shape (N, 3, 3) ------------------

def load_stl(path: Path) -> np.ndarray:
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(84)
        if len(head) == 84:
            (n,) = struct.unpack("<I", head[80:84])
            if 84 + n * 50 == size:
                dt = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
                data = np.fromfile(f, dtype=dt, count=n)
                return data["v"].astype(np.float32)
    # ASCII
    text = path.read_text(errors="ignore")
    nums = re.findall(r"vertex\s+([-+\deE.]+)\s+([-+\deE.]+)\s+([-+\deE.]+)", text)
    arr = np.array(nums, dtype=np.float32)
    n = len(arr) // 3
    return arr[: n * 3].reshape(n, 3, 3)


def load_obj(path: Path) -> np.ndarray:
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    with path.open("r", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = [int(tok.split("/")[0]) for tok in line.split()[1:]]
                idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
    if not tris:
        return np.zeros((0, 3, 3), np.float32)
    v = np.array(verts, dtype=np.float32)
    return v[np.array(tris)]


def load_3mf(path: Path) -> np.ndarray:
    out = []
    with zipfile.ZipFile(path) as z:
        models = [n for n in z.namelist() if n.lower().endswith(".model")]
        for name in models:
            with z.open(name) as fh:
                verts: list = []
                for ev, el in ET.iterparse(fh, events=("end",)):
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag == "vertex":
                        verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                        el.clear()
                    elif tag == "triangle":
                        out.append((verts[int(el.get("v1"))], verts[int(el.get("v2"))], verts[int(el.get("v3"))]))
                        el.clear()
                    elif tag == "mesh":
                        verts = []
                    if len(out) > MAX_TRIANGLES * 2:
                        break
    return np.array(out, dtype=np.float32).reshape(-1, 3, 3)


def load_mesh(path: Path) -> np.ndarray:
    ext = path.suffix.lower()
    if ext == ".stl":
        return load_stl(path)
    if ext == ".obj":
        return load_obj(path)
    if ext == ".3mf":
        return load_3mf(path)
    raise ValueError(f"unsupported mesh: {ext}")


# ---- facts -----------------------------------------------------------------

def mesh_facts(tris: np.ndarray) -> dict:
    if len(tris) == 0:
        return {"triangles": 0}
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(0), pts.max(0)
    # signed tetrahedron volume — correct for closed, consistently wound meshes
    v0, v1, v2 = tris[:, 0].astype(np.float64), tris[:, 1].astype(np.float64), tris[:, 2].astype(np.float64)
    vol = abs(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0)
    area = (np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) / 2).sum()
    dims = (hi - lo).tolist()
    return {
        "triangles": int(len(tris)),
        "size_x": round(dims[0], 2), "size_y": round(dims[1], 2), "size_z": round(dims[2], 2),
        "volume_cm3": round(vol / 1000.0, 2),       # assuming millimetres, as slicers do
        "surface_cm2": round(float(area) / 100.0, 1),
    }


# ---- renderer ----------------------------------------------------------------

def render(tris: np.ndarray, size: tuple[int, int] = (640, 360), color=(255, 122, 69)) -> Image.Image:
    W, H = size
    ss = 2  # supersample
    img = Image.new("RGB", (W * ss, H * ss))
    # vertical gradient background
    top, bot = np.array([42, 45, 52]), np.array([20, 21, 25])
    grad = (top[None, :] + (bot - top)[None, :] * np.linspace(0, 1, H * ss)[:, None]).astype(np.uint8)
    img.paste(Image.fromarray(np.repeat(grad[:, None, :], W * ss, axis=1)))
    if len(tris) == 0:
        return img.resize(size, Image.LANCZOS)

    tris = tris.astype(np.float64)
    if len(tris) > MAX_TRIANGLES:  # keep the triangles that matter visually
        e1 = tris[:, 1] - tris[:, 0]
        e2 = tris[:, 2] - tris[:, 0]
        area = np.linalg.norm(np.cross(e1, e2), axis=1)
        keep = np.argpartition(area, -MAX_TRIANGLES)[-MAX_TRIANGLES:]
        tris = tris[keep]

    pts = tris.reshape(-1, 3)
    center = (pts.min(0) + pts.max(0)) / 2
    tris = tris - center
    # Z-up (CAD convention) → camera: yaw 35°, pitch 30° looking down
    yaw, pitch = np.radians(-35), np.radians(-60)
    rz = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    t = tris @ (rx @ rz).T

    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    norm = np.linalg.norm(n, axis=1, keepdims=True)
    norm[norm == 0] = 1
    n = n / norm
    light = np.array([0.35, 0.55, 0.75])
    light /= np.linalg.norm(light)
    # two-sided lighting: STL winding is often inconsistent
    diffuse = np.abs(n @ light)
    rim = np.clip(1 - np.abs(n[:, 2]), 0, 1) ** 3 * 0.25
    shade = np.clip(0.28 + 0.72 * diffuse + rim, 0, 1.15)

    xy = t[:, :, :2]
    lo, hi = xy.reshape(-1, 2).min(0), xy.reshape(-1, 2).max(0)
    span = np.maximum(hi - lo, 1e-9)
    scale = min((W * ss * 0.82) / span[0], (H * ss * 0.82) / span[1])
    xy = (xy - (lo + hi) / 2) * scale
    xy[:, :, 0] += W * ss / 2
    xy[:, :, 1] = H * ss / 2 - xy[:, :, 1]

    # soft contact shadow
    shadow = Image.new("L", img.size, 0)
    sd = ImageDraw.Draw(shadow)
    base_y = xy[:, :, 1].max()
    cx, w = W * ss / 2, (xy[:, :, 0].max() - xy[:, :, 0].min()) * 0.55
    sd.ellipse([cx - w, base_y - 18 * ss, cx + w, base_y + 10 * ss], fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * ss))
    img.paste(Image.new("RGB", img.size, (8, 8, 10)), mask=shadow)

    order = np.argsort(t[:, :, 2].mean(1))  # far → near
    draw = ImageDraw.Draw(img)
    base = np.array(color, dtype=np.float64)
    cols = np.clip(base[None, :] * shade[:, None], 0, 255).astype(np.uint8)
    for i in order:
        p = xy[i]
        c = tuple(int(v) for v in cols[i])
        draw.polygon([(p[0, 0], p[0, 1]), (p[1, 0], p[1, 1]), (p[2, 0], p[2, 1])], fill=c)
    return img.resize(size, Image.LANCZOS)
