"""Desktop water / deep-river / bridge annotator for the 25 official maps.

Run from the project root:
    python tools/annotate_maps.py

Masks are written at 2048x2048:
    annotations/<slug>/water.png
    annotations/<slug>/bridge.png
    annotations/<slug>/meta.json

The build script downsamples these to the app's 1024 grid.

The 智能选区 (smart selection) tool grows a color-similar cluster outward from a
clicked pixel (8-connectivity) and turns that cluster into a polygon that can be
written into the current layer or handed to the manual polygon tool.

The 测高度 (height probe) tool reads terrain height out of the map's own
heightmap.png: Squad encodes it in the red/blue channels, decoded as
`(255 + R - B) * landscapeScale[2]`, which is metres relative to the map's
lowest point; adding the catalog's `zoff` gives the absolute world Z.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageTk


def _default_root() -> Path:
    """Where tools/, .tmp/ and annotations/ live.

    Frozen (PyInstaller) builds put __file__ inside a temporary _MEIPASS
    folder, so for the exe the meaningful root is the folder that holds it;
    a source run uses the repo root (the parent of tools/).
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        # The exe is commonly kept in dist/ or dropped in the project root;
        # walk up until the tool data (tools/maps_catalog.json) turns up.
        for cand in [here] + list(here.parents)[:3]:
            if (cand / "tools" / "maps_catalog.json").exists():
                return cand
        return here
    return Path(__file__).resolve().parents[1]


def _fatal(message: str) -> None:
    """Report a startup problem even before (or without) a Tk window."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "map-water-bridge-annotator", 0x10)
    except Exception:
        print(message)
    raise SystemExit(1)


ROOT = Path(os.environ.get("SQUAD_MAP_ROOT") or _default_root())
CATALOG_FILE = ROOT / "tools" / "maps_catalog.json"
try:
    CATALOG = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
except FileNotFoundError:
    _fatal(
        "找不到工具数据：\n"
        + str(CATALOG_FILE)
        + "\n\n请把 map-water-bridge-annotator.exe 放在仓库根目录（和 tools\\ 同级），\n"
        "或先运行 start-annotator.bat 启动。\n"
    )
except (OSError, ValueError) as exc:
    _fatal("无法读取 " + str(CATALOG_FILE) + "\n\n" + repr(exc))

SOURCE = ROOT / ".tmp" / "maps_source"
OUT = ROOT / "annotations"
SIZE = 2048
FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")

LAYERS = [
    ("水域", "water", (35, 110, 235), 0.36),
    ("桥梁", "bridge", (255, 170, 25), 0.72),
    ("地图边界内", "boundary", (35, 210, 120), 0.28),
    ("通畅道路", "clear", (60, 230, 100), 0.58),
    ("不可通行障碍", "obstacle", (255, 55, 110), 0.78),
    ("水面舰艇航道", "boat", (80, 190, 255), 0.48),
]
LABEL_BY_KEY = {k: label for label, k, _c, _a in LAYERS}
KEY_BY_LABEL = {label: k for label, k, _c, _a in LAYERS}
COLOR_BY_KEY = {k: (c, a) for _label, k, c, a in LAYERS}

TOOL_LABELS = {"polygon": "多边形", "brush": "画笔", "erase": "橡皮", "magic": "智能选区",
              "height": "测高度"}
SEL_COLOR = (56, 189, 248)
CURSOR_BRUSH = "#ffffff"   # brush outline core (erase gets its own colour below)
CURSOR_ERASE = "#ff3b30"
PROBE_RING = "#ffd60a"     # height probe marker + its readout
PROBE_TEXT = "#fff3b0"
SEL_OUTLINE = "#22d3ee"
SEL_REJECT = "#f87171"
MAX_SEEDS = 8
TOL_DEFAULT = 16
TOL_MAX = 120
SEL_EPS = 1.6          # polygon simplification distance, in map pixels
POLY_EPS = 3.0         # coarse simplification used when a selection becomes an editable polygon


# ---------------- smart selection (colour flood fill) ----------------
def color_distance(src: np.ndarray, rgb) -> np.ndarray:
    """Per-pixel RGB distance to rgb, normalised so the range is 0..255."""
    diff = src.astype(np.float32) - np.asarray(rgb, dtype=np.float32)
    return np.sqrt((diff * diff).mean(axis=2))


def _run_index(similar: np.ndarray):
    """Row runs of a boolean array, grouped by row: (starts, ends, offsets).

    One vectorised pass builds the index, so region_grow can then walk whole
    runs instead of single pixels.
    """
    height, width = similar.shape
    padded = np.zeros((height, width + 2), dtype=np.int8)
    padded[:, 1:-1] = similar
    delta = np.diff(padded, axis=1)
    starts = np.argwhere(delta == 1)
    ends = np.argwhere(delta == -1)
    if starts.size == 0:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty, np.zeros(height + 1, dtype=np.int64)
    counts = np.bincount(starts[:, 0], minlength=height)
    offsets = np.zeros(height + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])
    s_cols = starts[:, 1].astype(np.int64)
    e_cols = (ends[:, 1] - 1).astype(np.int64)
    return s_cols, e_cols, offsets


def region_grow(similar: np.ndarray, x: int, y: int, runs=None) -> np.ndarray:
    """8-connected flood fill of similar starting at pixel (x, y)."""
    height, width = similar.shape
    filled = np.zeros((height, width), dtype=bool)
    if not (0 <= x < width and 0 <= y < height) or not similar[y, x]:
        return filled
    if runs is None:
        runs = _run_index(similar)
    s_cols, e_cols, offsets = runs
    lo, hi = int(offsets[y]), int(offsets[y + 1])
    if hi <= lo:
        return filled
    i = lo + int(np.searchsorted(s_cols[lo:hi], x, side="right")) - 1
    if i < lo or e_cols[i] < x:
        return filled
    stack = [(y, int(s_cols[i]), int(e_cols[i]))]
    while stack:
        row, s, e = stack.pop()
        if filled[row, s]:
            continue
        filled[row, s:e + 1] = True
        for nrow in (row - 1, row + 1):
            if nrow < 0 or nrow >= height:
                continue
            nlo, nhi = int(offsets[nrow]), int(offsets[nrow + 1])
            if nhi <= nlo:
                continue
            ns = s_cols[nlo:nhi]
            ne = e_cols[nlo:nhi]
            a = nlo + int(np.searchsorted(ne, s - 1, side="left"))
            b = nlo + int(np.searchsorted(ns, e + 1, side="right"))
            for j in range(a, b):
                if not filled[nrow, s_cols[j]]:
                    stack.append((nrow, int(s_cols[j]), int(e_cols[j])))
    return filled


def _signed_area(points: np.ndarray) -> float:
    x = points[:, 0]
    y = points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _rdp(points: np.ndarray, eps: float) -> np.ndarray:
    """Douglas-Peucker kept points for an open polyline."""
    count = len(points)
    if count < 3:
        return points
    keep = np.zeros(count, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = points[j] - points[i]
        rel = points[i + 1:j] - points[i]
        length = float(np.hypot(seg[0], seg[1]))
        if length < 1e-9:
            dist = np.hypot(rel[:, 0], rel[:, 1])
        else:
            dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / length
        k = int(np.argmax(dist))
        if dist[k] > eps:
            mid = i + 1 + k
            keep[mid] = True
            stack.append((i, mid))
            stack.append((mid, j))
    return points[keep]


def _rotate_to_corner(points: np.ndarray) -> np.ndarray:
    """Rotate a closed ring so it starts on its sharpest corner.

    Douglas-Peucker always keeps the first and last point, so if the cut falls
    mid-edge the corner next to the seam gets flattened away.
    """
    count = len(points)
    if count < 4:
        return points
    prevs = points - np.roll(points, 1, axis=0)
    nexts = np.roll(points, -1, axis=0) - points
    len_a = np.hypot(prevs[:, 0], prevs[:, 1])
    len_b = np.hypot(nexts[:, 0], nexts[:, 1])
    len_a[len_a == 0] = 1.0
    len_b[len_b == 0] = 1.0
    cosang = (prevs[:, 0] * nexts[:, 0] + prevs[:, 1] * nexts[:, 1]) / (len_a * len_b)
    return np.roll(points, -int(np.argmax(1.0 - cosang)), axis=0)


def simplify_loop(loop, eps):
    """Douglas-Peucker on a closed ring, keeping at least three points."""
    points = np.asarray(loop, dtype=np.float64)
    if len(points) < 4:
        return points
    ring = _rotate_to_corner(points)
    ring = np.vstack([ring, ring[:1]])
    simple = _rdp(ring, eps)[:-1]
    if len(simple) < 3:
        return points
    return simple


def _boundary_edges(mask: np.ndarray):
    """Directed unit edges around the filled pixels, as integer arrays.

    Pixel sides facing empty space become edges on the integer lattice:
    up -> (x, y)-(x+1, y), right -> (x+1, y)-(x+1, y+1), and so on. Collecting
    them with array shifts keeps this exact (holes included) but fast.
    """
    height, width = mask.shape
    filled = np.zeros((height + 2, width + 2), dtype=bool)
    filled[1:-1, 1:-1] = mask
    body = filled[1:-1, 1:-1]
    empty = ~filled

    parts = []
    ys, xs = np.nonzero(body & empty[:-2, 1:-1])          # nothing above
    parts.append((xs, ys, xs + 1, ys))
    ys, xs = np.nonzero(body & empty[1:-1, 2:])           # nothing right
    parts.append((xs + 1, ys, xs + 1, ys + 1))
    ys, xs = np.nonzero(body & empty[2:, 1:-1])           # nothing below
    parts.append((xs + 1, ys + 1, xs, ys + 1))
    ys, xs = np.nonzero(body & empty[1:-1, :-2])          # nothing left
    parts.append((xs, ys + 1, xs, ys))
    sx = np.concatenate([p[0] for p in parts]).astype(np.int64)
    sy = np.concatenate([p[1] for p in parts]).astype(np.int64)
    ex = np.concatenate([p[2] for p in parts]).astype(np.int64)
    ey = np.concatenate([p[3] for p in parts]).astype(np.int64)
    return sx, sy, ex, ey


def _pick_next(prev, cur, outs, ends, stride):
    """At a self-touching vertex keep the turn that hugs the filled side."""
    if prev is None or len(outs) < 2:
        return outs[0]
    din = (cur % stride - prev % stride, cur // stride - prev // stride)
    best = None
    best_key = None
    for idx in outs:
        vid = int(ends[idx])
        dout = (vid % stride - cur % stride, vid // stride - cur // stride)
        key = (din[0] * dout[1] - din[1] * dout[0],
               din[0] * dout[0] + din[1] * dout[1])
        if best_key is None or key > best_key:
            best_key = key
            best = idx
    return best


def mask_loops(mask: np.ndarray, eps: float = SEL_EPS, min_area: float = 16.0,
               max_loops: int = 96):
    """Trace every closed boundary of a binary mask into simplified polygons.

    Boundaries are chained on the integer lattice, which gives exact outlines
    (holes and several disconnected parts included) with numpy only. Returns
    polygons sorted by |area|, largest first.
    """
    loops = []
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return loops
    height, width = mask.shape
    sx, sy, ex, ey = _boundary_edges(mask)
    if sx.size == 0:
        return loops
    stride = width + 1
    start_ids = sy * stride + sx
    end_ids = ey * stride + ex
    order = np.argsort(start_ids, kind="stable")
    starts = start_ids[order]
    ends = end_ids[order]
    used = np.zeros(len(order), dtype=bool)
    total = len(order)

    def coords(vid):
        return np.stack([vid % stride, vid // stride], axis=1).astype(np.float64)

    for first in range(total):
        if used[first]:
            continue
        start_v = int(starts[first])
        loop = [start_v]
        cur = start_v
        prev = None
        closed = False
        while True:
            lo = int(np.searchsorted(starts, cur, side="left"))
            hi = int(np.searchsorted(starts, cur, side="right"))
            cand = [k for k in range(lo, hi) if not used[k]]
            if not cand:
                break
            if len(cand) == 1:
                k = cand[0]
            else:
                k = _pick_next(prev, cur, cand, ends, stride)
            used[k] = True
            prev, cur = cur, int(ends[k])
            if cur == start_v:
                closed = True
                break
            loop.append(cur)
        if not closed or len(loop) < 4:
            continue
        ring = _rotate_to_corner(coords(np.asarray(loop, dtype=np.int64)))
        ring = np.vstack([ring, ring[:1]])
        simp = _rdp(ring, eps)[:-1]
        if len(simp) < 3:
            continue
        if abs(_signed_area(simp)) >= min_area:
            loops.append(simp)
            if len(loops) >= max_loops:
                break
    loops.sort(key=lambda p: -abs(_signed_area(p)))
    return loops


# ---------------- height probe ----------------
def decode_heightmap(path, zscale):
    """heightmap.png -> terrain height in metres, relative to the map's lowest point.

    Squad's exporter packs the height into the R/B channels of an 8-bit PNG:
        raw = 255 + R - B
        metres = raw * landscapeScale[2]
    which is the same decoding SquadCalc uses for its firing solutions
    (SquadCalc/src/js/squadHeightmaps.js).  The file is ~1 m per pixel, so it
    covers exactly the same extent as the minimap.
    """
    img = Image.open(path)
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        arr = np.array(img)
    finally:
        img.close()
    raw = arr[:, :, 0].astype(np.int16)
    raw -= arr[:, :, 2]
    raw += 255
    del arr
    out = raw.astype(np.float32)
    del raw
    out *= float(zscale)
    return out


def height_pixel(x, y, size, shape):
    """Map pixel -> (row, col) in the full-resolution heightmap.

    The heightmap spans the minimap extent inclusive on both edges, so the
    mapping is a plain proportional one; clamping keeps edge pixels valid.
    """
    height, width = shape
    col = int(round((x + 0.5) / size * (width - 1)))
    row = int(round((y + 0.5) / size * (height - 1)))
    col = min(max(col, 0), width - 1)
    row = min(max(row, 0), height - 1)
    return row, col


def height_at_pixel(field, x, y, size):
    """Nearest-sample height (relative metres) at a map pixel."""
    row, col = height_pixel(x, y, size, field.shape)
    return float(field[row, col])


def world_xz(m, x, y, size):
    """Map pixel -> world metres (x east, z south) inside the map's bounds."""
    spanx = m["maxx"] - m["minx"]
    spanz = m["maxz"] - m["minz"]
    return (m["minx"] + (x + 0.5) / size * spanx,
            m["minz"] + (y + 0.5) / size * spanz)


# ---------------- GitHub data-pack upload ----------------
GH_OWNER = "copperbookworm"
GH_REPO = "Yehorivka-MapData"
GH_BRANCH = "main"
GH_DIR = "maps-data"
GH_API = "https://api.github.com"
GH_WEB = "https://github.com/%s/%s" % (GH_OWNER, GH_REPO)
GH_TIMEOUT = 120
GH_CONFIG = ROOT / "tools" / "github-upload.json"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def human_bytes(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def read_gh_config() -> dict:
    try:
        data = json.loads(GH_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _root_candidates():
    """Possible project roots, most explicit first."""
    out = []
    for key in ("SQUAD_PROJECT_ROOT", "SQUAD_MAP_PROJECT", "YEHO_PROJECT_ROOT"):
        val = os.environ.get(key)
        if val:
            out.append(Path(val))
    cfg = read_gh_config().get("project_root")
    if cfg:
        out.append(Path(cfg))
    cur = ROOT
    for _ in range(4):
        out.append(cur)
        cur = cur.parent
    return out


def project_root() -> Path:
    """The folder that holds tools/build_maps.py and dist/maps-data.

    The annotator itself may live in dist/ or in a checkout of its own repo, so
    the project is found by looking for the build script and the data folder
    instead of assuming anything about where the exe sits.
    """
    best, best_score = ROOT, -1
    seen = set()
    for cand in _root_candidates():
        try:
            cand = cand.resolve()
        except OSError:
            continue
        if cand in seen:
            continue
        seen.add(cand)
        score = 0
        if (cand / "dist" / "maps-data" / "manifest.json").exists():
            score += 2
        if (cand / "tools" / "build_maps.py").exists():
            score += 1
        if score > best_score:
            best, best_score = cand, score
    return best


def data_dir() -> Path:
    return project_root() / "dist" / "maps-data"


def data_files() -> list:
    """The files the app downloads: every pack plus the manifest."""
    d = data_dir()
    if not d.exists():
        return []
    return sorted((p for p in d.glob("*.json") if p.is_file()), key=lambda p: p.name)


def stale_maps() -> list:
    """Maps whose annotation files are newer than the built packs."""
    root = project_root()
    man = data_dir() / "manifest.json"
    ann = root / "annotations"
    if not man.exists() or not ann.exists():
        return []
    ref = man.stat().st_mtime
    out = []
    for d in sorted(ann.iterdir()):
        if not d.is_dir():
            continue
        newest = 0.0
        for f in d.glob("*"):
            try:
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
        if newest > ref:
            out.append(d.name)
    return out


def find_token():
    """(token, where) — env, then tools/github-upload.json, then git credential."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val, "环境变量 " + key
    val = (read_gh_config().get("token") or "").strip()
    if val:
        return val, "配置文件 " + str(GH_CONFIG)
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=25,
            cwd=str(project_root()), creationflags=_NO_WINDOW,
        )
        for line in (proc.stdout or "").splitlines():
            if line.startswith("password="):
                tok = line[len("password="):].strip()
                if tok:
                    return tok, "Windows 凭据管理器（git credential）"
    except (OSError, subprocess.SubprocessError):
        pass
    return "", ""


def blob_sha(data: bytes) -> str:
    """git's blob object id for these bytes — the same sha the contents API returns."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GhError(Exception):
    def __init__(self, text, status=None):
        super().__init__(text)
        self.status = status


def gh_call(method: str, path: str, token: str, payload=None, on_retry=None):
    """One GitHub API call with backoff. Returns (json, headers)."""
    url = GH_API + path
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    last = ""
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "map-water-bridge-annotator")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=GH_TIMEOUT) as resp:
                raw = resp.read()
                headers = dict(resp.headers.items())
                return (json.loads(raw.decode("utf-8")) if raw else {}), headers
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            last = ("HTTP %d %s" % (exc.code, detail)).strip()
            if exc.code in (500, 502, 503, 504) and attempt < 2:
                if on_retry:
                    on_retry(attempt + 1, last)
                time.sleep(1.5 * (attempt + 1))
                continue
            raise GhError(last, exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last = "网络不通：%s" % reason
            if attempt < 2:
                if on_retry:
                    on_retry(attempt + 1, last)
                time.sleep(1.5 * (attempt + 1))
                continue
            raise GhError(last)
    raise GhError(last or "未知网络错误")


class UploadDialog(tk.Toplevel):
    """比对本地数据包与 GitHub，只上传变过的文件，全程显示进度。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("上传地图数据到 GitHub — %s/%s" % (GH_OWNER, GH_REPO))
        self.configure(bg="#0d1117")
        self.geometry("820x640")
        self.minsize(760, 560)
        self.transient(master)
        self.q = queue.Queue()
        self.running = False
        self.rebuilding = False
        self.phase = "idle"
        self.total_bytes = 0
        self.sent_bytes = 0
        self.done_bytes = 0
        self.samples = []
        self.started = 0.0
        self._build()
        self._refresh_info()
        self.after(150, self._poll)

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 12}
        info = tk.Frame(self, bg="#161b22")
        info.pack(fill="x", **pad, pady=(12, 6))
        tk.Label(info, text="要上传的数据（App 的「在线更新」就是读这里）", bg="#161b22",
                 fg="#8b949e", anchor="w", font=FONT_B).pack(fill="x", padx=10, pady=(8, 2))
        self.info_text = tk.Label(info, text="", bg="#161b22", fg="#c9d1d9", anchor="w",
                                  justify="left", font=FONT, wraplength=760)
        self.info_text.pack(fill="x", padx=10)

        row = tk.Frame(info, bg="#161b22")
        row.pack(fill="x", padx=10, pady=(6, 2))
        tk.Label(row, text="GitHub 登录：", bg="#161b22", fg="#8b949e").pack(side="left")
        self.token_label = tk.Label(row, text="", bg="#161b22", fg="#c9d1d9")
        self.token_label.pack(side="left")
        tk.Button(row, text="更新 token…", command=self._ask_token, bg="#21262d", fg="#c9d1d9",
                  relief="flat", activebackground="#30363d", padx=6, pady=1,
                  font=FONT).pack(side="right")
        self.target_label = tk.Label(info, text="目标：" + GH_WEB + "/tree/" + GH_BRANCH + "/" + GH_DIR,
                                     bg="#161b22", fg="#6e7681", anchor="w")
        self.target_label.pack(fill="x", padx=10, pady=(2, 8))

        self.net_label = tk.Label(self, text="网络：待机", bg="#0d1117", fg="#8b949e", anchor="w",
                                  font=FONT_B)
        self.net_label.pack(fill="x", **pad, pady=(2, 2))

        box = tk.Frame(self, bg="#0d1117")
        box.pack(fill="x", **pad, pady=(4, 0))
        self.all_label = tk.Label(box, text="整体：等待开始", bg="#0d1117", fg="#c9d1d9", anchor="w")
        self.all_label.pack(fill="x")
        self.all_bar = ttk.Progressbar(box, mode="determinate", maximum=1000)
        self.all_bar.pack(fill="x", pady=(2, 6))
        self.cur_label = tk.Label(box, text="当前文件：—", bg="#0d1117", fg="#8b949e", anchor="w")
        self.cur_label.pack(fill="x")
        self.cur_bar = ttk.Progressbar(box, mode="determinate", maximum=1000)
        self.cur_bar.pack(fill="x", pady=(2, 6))
        self.speed_label = tk.Label(box, text="速度：—   剩余：—", bg="#0d1117", fg="#8b949e",
                                    anchor="w")
        self.speed_label.pack(fill="x")

        logf = tk.Frame(self, bg="#0d1117")
        logf.pack(fill="both", expand=True, **pad, pady=(8, 0))
        self.log = tk.Text(logf, bg="#0b0f14", fg="#c9d1d9", relief="flat", wrap="word",
                           height=10, font=("Consolas", 9), insertbackground="#c9d1d9",
                           highlightthickness=0)
        sb = tk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bar = tk.Frame(self, bg="#11161d")
        bar.pack(fill="x", side="bottom")
        self.btn_rebuild = tk.Button(bar, text="先重建数据包", command=self._start_rebuild,
                                     bg="#21262d", fg="#c9d1d9", relief="flat",
                                     activebackground="#30363d", padx=10, pady=4, font=FONT)
        self.btn_rebuild.pack(side="left", padx=(10, 4), pady=8)
        self.btn_go = tk.Button(bar, text="开始上传", command=self._start_upload, bg="#1f6feb",
                                fg="#fff", relief="flat", activebackground="#388bfd", padx=14,
                                pady=4, font=FONT_B)
        self.btn_go.pack(side="left", padx=4, pady=8)
        self.btn_close = tk.Button(bar, text="关闭", command=self._on_close, bg="#21262d",
                                   fg="#c9d1d9", relief="flat", activebackground="#30363d",
                                   padx=12, pady=4, font=FONT)
        self.btn_close.pack(side="right", padx=10, pady=8)

    # ---------- small helpers ----------
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _net(self, text, color="#8b949e"):
        self.net_label.configure(text="网络：" + text, fg=color)

    def _refresh_info(self):
        root = project_root()
        files = data_files()
        total = sum(p.stat().st_size for p in files)
        man = data_dir() / "manifest.json"
        built = time.strftime("%Y-%m-%d %H:%M", time.localtime(man.stat().st_mtime)) if man.exists() else "还没有"
        stale = stale_maps()
        lines = [
            "工程目录：%s" % root,
            "数据目录：%s" % data_dir(),
            "文件：%d 个，共 %s（清单 %s）" % (
                len(files), human_bytes(total),
                human_bytes(man.stat().st_size) if man.exists() else "缺失"),
            "最近构建：%s" % built,
        ]
        if stale:
            lines.append("⚠ 有 %d 张图的标注比数据包新（%s）：先点「先重建数据包」，"
                         "否则上传的是旧数据" % (len(stale), "、".join(stale[:6]) + ("…" if len(stale) > 6 else "")))
        elif files:
            lines.append("✓ 数据包比标注新，可以直接上传")
        else:
            lines.append("✗ 找不到 dist/maps-data，先跑一次 tools/build_maps.py")
        self.info_text.configure(text="\n".join(lines))
        tok, where = find_token()
        if tok:
            self.token_label.configure(text="已就绪（%s）" % where, fg="#3fb950")
        else:
            self.token_label.configure(text="缺少 token，点右边「更新 token…」", fg="#f85149")
        self.token = tok
        return tok

    def _ask_token(self):
        win = tk.Toplevel(self)
        win.title("GitHub token")
        win.configure(bg="#0d1117")
        win.transient(self)
        tk.Label(win, text="粘贴 GitHub Personal Access Token（需要本仓库 Contents 读写权限）：",
                 bg="#0d1117", fg="#c9d1d9").pack(padx=12, pady=(12, 4), anchor="w")
        entry = tk.Entry(win, width=64, show="•", bg="#161b22", fg="#c9d1d9",
                         insertbackground="#c9d1d9", relief="flat")
        entry.pack(padx=12, pady=4)
        tk.Label(win, text="存到 %s（不要提交进 git）" % GH_CONFIG, bg="#0d1117",
                 fg="#6e7681").pack(padx=12, anchor="w")
        def save():
            val = entry.get().strip()
            win.destroy()
            if not val:
                return
            cfg = read_gh_config()
            cfg["token"] = val
            try:
                GH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                GH_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc), parent=self)
                return
            self._refresh_info()
            self._log("已保存 token 到 " + str(GH_CONFIG))
        tk.Button(win, text="保存", command=save, bg="#1f6feb", fg="#fff", relief="flat",
                  padx=16, pady=3).pack(pady=(4, 12))
        entry.focus_set()

    def _python(self):
        cand = []
        if not getattr(sys, "frozen", False):
            cand.append([sys.executable])
        cand.append(["py", "-3"])
        cand.append(["python"])
        for cmd in cand:
            try:
                probe = subprocess.run(cmd + ["-c", "import numpy, PIL; print(1)"],
                                       capture_output=True, text=True, timeout=25,
                                       creationflags=_NO_WINDOW)
                if probe.returncode == 0:
                    return cmd
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    # ---------- rebuild ----------
    def _start_rebuild(self):
        if self.running or self.rebuilding:
            return
        root = project_root()
        script = root / "tools" / "build_maps.py"
        if not script.exists():
            messagebox.showwarning("找不到构建脚本",
                                   "没有找到 %s\n\n这个功能需要在完整工程目录里运行。" % script,
                                   parent=self)
            return
        py = self._python()
        if py is None:
            messagebox.showwarning("找不到 Python",
                                   "重建数据包要跑 tools/build_maps.py，但本机没有可用的 python"
                                   "（需要 numpy + Pillow）。\n\n可以手动在工程目录执行：\n"
                                   "python tools\\build_maps.py",
                                   parent=self)
            return
        self.rebuilding = True
        self.btn_rebuild.configure(state="disabled")
        self.btn_go.configure(state="disabled")
        self._net("重建中（读标注 → 算坡度/树冠 → 打包），约 4–5 分钟", "#f0b429")
        self.all_label.configure(text="重建：启动 %s tools/build_maps.py" % " ".join(py))
        self._log("开始重建：" + " ".join(py + ["tools/build_maps.py"]) + "  (cwd=%s)" % root)
        threading.Thread(target=self._rebuild_worker, args=(py, root, script), daemon=True).start()

    def _rebuild_worker(self, py, root, script):
        started = time.time()
        try:
            proc = subprocess.Popen(py + [str(script)], cwd=str(root), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", creationflags=_NO_WINDOW)
        except OSError as exc:
            self.q.put(("rebuild_done", False, str(exc)))
            return
        lines = 0
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                lines += 1
                self.q.put(("rebuild_line", line))
        proc.wait()
        self.q.put(("rebuild_done", proc.returncode == 0,
                    "退出码 %s，用时 %.0f 秒，%d 行输出" % (proc.returncode, time.time() - started, lines)))

    # ---------- upload ----------
    def _start_upload(self):
        if self.running or self.rebuilding:
            return
        files = data_files()
        if not files:
            messagebox.showwarning("没有数据", "dist/maps-data 里没有 json，先重建数据包。", parent=self)
            return
        tok = self._refresh_info()
        if not tok:
            messagebox.showwarning("缺少 token",
                                   "没有找到 GitHub token。\n\n可以：\n"
                                   "1) 点「更新 token…」粘贴一个；\n"
                                   "2) 或设置环境变量 GITHUB_TOKEN；\n"
                                   "3) 或在本机用 git 登录过 github.com（会自动读取）。",
                                   parent=self)
            return
        self.running = True
        self.phase = "probe"
        self.total_bytes = 0
        self.total_files = 0
        self.sent_bytes = 0
        self.done_bytes = 0
        self.samples = []
        self.started = time.time()
        self.btn_go.configure(state="disabled")
        self.btn_rebuild.configure(state="disabled")
        self.all_bar["value"] = 0
        self.cur_bar["value"] = 0
        self._net("检查仓库连通性…", "#f0b429")
        self._log("开始：%d 个文件，本地共 %s" % (len(files), human_bytes(sum(p.stat().st_size for p in files))))
        threading.Thread(target=self._upload_worker, args=(tok, files), daemon=True).start()

    def _remote_sha(self, token, name):
        try:
            data, _h = gh_call("GET", "/repos/%s/%s/contents/%s/%s?ref=%s"
                               % (GH_OWNER, GH_REPO, GH_DIR, name, GH_BRANCH), token)
            return (data or {}).get("sha")
        except GhError as exc:
            if exc.status == 404:
                return None
            raise

    def _upload_worker(self, token, files):
        started = time.time()
        try:
            self.q.put(("net", "连接 github.com…", "#f0b429"))
            t0 = time.time()
            info, headers = gh_call("GET", "/repos/%s/%s" % (GH_OWNER, GH_REPO), token,
                                    on_retry=lambda n, why: self.q.put(
                                        ("log", "  · 重试 %d/%d：%s" % (n, 2, why))))
            rtt = (time.time() - t0) * 1000
            left = headers.get("X-RateLimit-Remaining", "?")
            self.q.put(("net", "已连上 %s（RTT %.0f ms，API 配额剩余 %s）"
                        % (GH_WEB, rtt, left), "#3fb950"))
            default_branch = (info or {}).get("default_branch")
            if default_branch and default_branch != GH_BRANCH:
                self.q.put(("log", "  注意：仓库默认分支是 %s，本次写入 %s"
                            % (default_branch, GH_BRANCH)))
            self.q.put(("phase", "compare", "正在比对 %d 个文件（只读，不改远端）…" % len(files)))

            # one request tells us every blob sha under GH_DIR; the Contents API
            # would take one round trip per file (~2s each on a slow link).
            remote_map = None
            t0 = time.time()
            try:
                tree, _h = gh_call("GET", "/repos/%s/%s/git/trees/%s?recursive=1"
                                   % (GH_OWNER, GH_REPO, GH_BRANCH), token,
                                   on_retry=lambda n, why: self.q.put(
                                       ("log", "  · 重试 %d/%d：%s" % (n, 2, why))))
                remote_map = {}
                prefix = GH_DIR + "/"
                for ent in (tree or {}).get("tree", []):
                    ep = ent.get("path") or ""
                    if ent.get("type") == "blob" and ep.startswith(prefix):
                        remote_map[ep[len(prefix):]] = ent.get("sha")
                self.q.put(("log", "  远端清单：%d 个文件（一次请求，用时 %.1f 秒）"
                            % (len(remote_map), time.time() - t0)))
            except GhError as exc:
                self.q.put(("log", "  · 远端清单读取失败（%s），改成逐个文件比对，会慢一些"
                            % (exc.status or exc)))

            plan = []
            for i, path in enumerate(files):
                if not path.exists():
                    continue
                data = path.read_bytes()
                sha = blob_sha(data)
                if remote_map is not None:
                    remote = remote_map.get(path.name)
                else:
                    remote = self._remote_sha(token, path.name)
                changed = remote != sha
                plan.append((path, data, sha, remote, changed))
                self.q.put(("compare", i + 1, len(files), path.name, changed))

            todo = [p for p in plan if p[4]]
            skipped = len(plan) - len(todo)
            total = sum(len(p[1]) for p in todo)
            self.q.put(("plan", len(todo), total, skipped))
            if not todo:
                self.q.put(("done", 0, skipped, time.time() - started))
                return

            # packs first, manifest last: the app must never see a manifest
            # that points at packs which are not there yet.
            todo.sort(key=lambda p: 1 if p[0].name == "manifest.json" else 0)
            sent = 0
            for idx, (path, data, _sha, remote, _changed) in enumerate(todo):
                self.q.put(("file", path.name, len(data), idx + 1, len(todo)))
                payload = {
                    "message": "更新地图数据 %s/%s" % (GH_DIR, path.name),
                    "content": base64.b64encode(data).decode("ascii"),
                    "branch": GH_BRANCH,
                }
                if remote:
                    payload["sha"] = remote
                t0 = time.time()
                gh_call("PUT", "/repos/%s/%s/contents/%s/%s"
                        % (GH_OWNER, GH_REPO, GH_DIR, path.name), token, payload,
                        on_retry=lambda n, why, nm=path.name: self.q.put(
                            ("log", "  · %s 重试 %d/%d：%s" % (nm, n, 2, why))))
                sent += len(data)
                self.q.put(("bytes", len(data), time.time() - t0))
                self.q.put(("log", "  ✓ %s  %s  %.1f 秒" % (path.name, human_bytes(len(data)),
                                                            time.time() - t0)))
            self.q.put(("done", len(todo), skipped, time.time() - started))
        except GhError as exc:
            self.q.put(("error", self._explain(exc)))
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            self.q.put(("error", "%s: %s" % (type(exc).__name__, exc)))

    def _explain(self, exc) -> str:
        st = exc.status
        if st == 401:
            return ("GitHub 拒绝了这个 token（HTTP 401）。token 可能过期或被撤销，"
                    "点「更新 token…」换一个。")
        if st == 403:
            return ("被 GitHub 拒绝（HTTP 403）。多半是 token 没有这个仓库的 Contents 写权限，"
                    "或者触发了限流，稍等几分钟再试。")
        if st == 404:
            return ("仓库或分支不存在（HTTP 404）。检查 %s 是否可访问、分支 %s 是否存在。"
                    % (GH_WEB, GH_BRANCH))
        if st == 413:
            return "文件太大，GitHub 拒绝（HTTP 413）。"
        if st == 422:
            return ("GitHub 不接受这次写入（HTTP 422）。常见原因：远端文件在此期间被改过"
                    "（重新点一次上传即可），或分支 %s 不存在。" % GH_BRANCH)
        return str(exc)

    # ---------- event pump ----------
    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        if self.running and self.phase == "upload":
            self._tick()
        self.after(150, self._poll)

    def _tick(self):
        now = time.time()
        self.samples.append((now, self.sent_bytes))
        self.samples = [(t, b) for t, b in self.samples if now - t <= 4.0]
        if len(self.samples) >= 2:
            t0, b0 = self.samples[0]
            dt = now - t0
            if dt > 0.3:
                speed = (self.sent_bytes - b0) / dt
                self.speed_label.configure(
                    text="速度：%s/s   已传 %s / %s   剩余约 %s"
                         % (human_bytes(speed), human_bytes(self.sent_bytes),
                            human_bytes(self.total_bytes),
                            ("%d 秒" % max(0, int((self.total_bytes - self.sent_bytes) / speed)))
                            if speed > 1 else "—"))
        if self.total_bytes:
            self.all_bar["value"] = int(1000 * self.sent_bytes / self.total_bytes)
            if getattr(self, "total_files", 0):
                self.all_label.configure(text="上传：%s / %s（%d 个文件）"
                                         % (human_bytes(self.sent_bytes),
                                            human_bytes(self.total_bytes), self.total_files))

    def _handle(self, ev):
        kind = ev[0]
        if kind == "log":
            self._log(ev[1])
        elif kind == "net":
            self._net(ev[1], ev[2])
        elif kind == "phase":
            self.phase = ev[1]
            self.all_label.configure(text=ev[2])
        elif kind == "compare":
            _k, i, n, name, changed = ev
            self.phase = "compare"
            self.all_label.configure(text="比对 %d/%d：%s %s" % (i, n, name, "有变化" if changed else "无变化"))
            self.all_bar["value"] = int(1000 * i / max(1, n))
        elif kind == "plan":
            _k, n, total, skipped = ev
            self.phase = "upload"
            self.total_bytes = total
            self.total_files = n
            self.sent_bytes = 0
            self.samples = [(time.time(), 0)]
            self._log("比对完成：%d 个有变化（共 %s），%d 个无变化跳过" % (n, human_bytes(total), skipped))
            if n:
                self.all_label.configure(text="上传：0 / %s（%d 个文件）" % (human_bytes(total), n))
                self._net("开始上传 %d 个文件，共 %s" % (n, human_bytes(total)), "#f0b429")
            else:
                self.all_label.configure(text="无需上传：%d 个文件都和远端一致" % skipped)
        elif kind == "file":
            _k, name, size, i, n = ev
            self.cur_label.configure(text="当前文件 %d/%d：%s（%s）" % (i, n, name, human_bytes(size)))
            self.cur_bar["value"] = 0
            self._net("正在上传 %s（%d/%d）" % (name, i, n), "#f0b429")
        elif kind == "bytes":
            self.sent_bytes += ev[1]
            self.done_bytes += ev[1]
            self.cur_bar["value"] = 1000
        elif kind == "done":
            _k, uploaded, skipped, secs = ev
            self.running = False
            self.phase = "idle"
            self.btn_go.configure(state="normal")
            self.btn_rebuild.configure(state="normal")
            self.all_bar["value"] = 1000
            self.cur_bar["value"] = 1000
            if uploaded:
                self._net("完成：上传 %d 个文件，跳过 %d 个，用时 %.0f 秒" % (uploaded, skipped, secs),
                          "#3fb950")
                self._log("全部完成 ✓  上传 %d，跳过 %d，用时 %.0f 秒" % (uploaded, skipped, secs))
                self._log("玩家这边：App → 地图数据 · 在线更新 → 检查更新 → 下载有更新的")
                messagebox.showinfo("上传完成",
                                    "已上传 %d 个文件（跳过 %d 个没变化的），用时 %.0f 秒。\n\n"
                                    "%s" % (uploaded, skipped, secs, GH_WEB), parent=self)
            else:
                self._net("完成：所有文件都和远端一致，没有需要上传的", "#3fb950")
                self._log("所有文件都和远端一致，未上传任何内容。")
        elif kind == "error":
            self.running = False
            self.phase = "idle"
            self.btn_go.configure(state="normal")
            self.btn_rebuild.configure(state="normal")
            self._net("失败：" + ev[1].splitlines()[0], "#f85149")
            self._log("失败：" + ev[1])
            messagebox.showerror("上传失败", ev[1], parent=self)
        elif kind == "rebuild_line":
            self._log("  " + ev[1])
        elif kind == "rebuild_done":
            self.rebuilding = False
            self.btn_rebuild.configure(state="normal")
            self.btn_go.configure(state="normal")
            ok, detail = ev[1], ev[2]
            self._log(("重建完成 ✓  " if ok else "重建失败 ✗  ") + detail)
            self._net("重建完成，可以上传了" if ok else "重建失败：" + detail,
                      "#3fb950" if ok else "#f85149")
            self._refresh_info()
            if not ok:
                messagebox.showerror("重建失败", detail, parent=self)

    def _on_close(self):
        if self.running or self.rebuilding:
            if not messagebox.askyesno("还在进行中", "上传/重建还没结束，确定关闭窗口吗？", parent=self):
                return
        master = self.master
        if master is not None and getattr(master, "_upload_win", None) is self:
            master._upload_win = None
        self.destroy()



class Annotator(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Squad 地图区域标注器（水域 / 桥梁 / 边界 / 道路 / 障碍）")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1340, sw - 80), min(800, sh - 80)
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1080, 700)
        self.configure(bg="#0d1117")
        self.option_add("*Font", FONT)

        self.maps = CATALOG
        self.slug = None
        self.base = None
        self.topo = None
        self.water = Image.new("L", (SIZE, SIZE), 0)
        self.bridge = Image.new("L", (SIZE, SIZE), 0)
        self.boundary = Image.new("L", (SIZE, SIZE), 0)
        self.clear = Image.new("L", (SIZE, SIZE), 0)
        self.obstacle = Image.new("L", (SIZE, SIZE), 0)
        self.boat = Image.new("L", (SIZE, SIZE), 0)
        self.layer = "water"
        self.tool = "polygon"
        self.brush = 4
        self.zoom = 1.0
        self.cx = SIZE / 2
        self.cy = SIZE / 2
        self.dirty = False
        self.poly = []
        self.pan = None
        self.last_brush = None
        self._brush_pos = None     # last pointer position (canvas px) for the brush outline
        self.undo_stack = []
        self.redo_stack = []
        self._render_job = None
        # smart selection state
        self.tol = TOL_DEFAULT
        self.sel_ops = []          # [{"x","y","op","dist"}], op=+1 add / -1 subtract
        self.sel_mask = None       # np.bool_ (SIZE, SIZE) or None
        self.sel_image = None      # PIL "L" preview of sel_mask
        self.sel_loops = []        # simplified polygons, largest first
        self.sel_pixels = 0
        self.sel_bbox = None
        self.sel_written = ""
        self._sel_job = None
        self._src_key = None
        self._src_np = None
        # height probe state
        self.hm_field = None       # np.float32 metres (relative), cached per map
        self.hm_slug = None
        self.probe = None          # {"x","y","rel","world"} of the pinned reading

        self._build_ui()
        self._bind_keys()
        self._fill_map_list()
        first = "yehorivka" if any(m["slug"] == "yehorivka" for m in self.maps) else self.maps[0]["slug"]
        self.select_map(first, force=True)
        self.after(250, self.fit)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI ----------------
    def _build_ui(self):
        header = tk.Frame(self, bg="#161b22", height=46)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self.map_label = tk.Label(header, text="", bg="#161b22", fg="#f0f6fc", font=("Microsoft YaHei UI", 13, "bold"))
        self.map_label.pack(side="left", padx=(14, 8))

        tk.Label(header, text="底图", bg="#161b22", fg="#8b949e").pack(side="left", padx=(4, 2))
        self.topo_var = tk.BooleanVar(value=False)
        tk.Checkbutton(header, text="地形图", variable=self.topo_var, command=self.render,
                       bg="#161b22", fg="#c9d1d9", selectcolor="#161b22",
                       activebackground="#161b22", activeforeground="#fff").pack(side="left")

        tk.Frame(header, bg="#30363d", width=1, height=26).pack(side="left", padx=10, fill="y")

        tk.Label(header, text="图层", bg="#161b22", fg="#8b949e").pack(side="left", padx=(2, 2))
        self.layer_var = tk.StringVar(value="水域")
        self.layer_combo = ttk.Combobox(header, textvariable=self.layer_var, state="readonly",
                                        values=[label for label, _k, _c, _a in LAYERS],
                                        width=13, font=FONT)
        self.layer_combo.set("水域")
        self.layer_combo.pack(side="left", padx=2)
        self.layer_combo.bind("<<ComboboxSelected>>", self.on_layer_combo)

        tk.Frame(header, bg="#30363d", width=1, height=26).pack(side="left", padx=10, fill="y")

        tk.Label(header, text="工具", bg="#161b22", fg="#8b949e").pack(side="left", padx=(2, 2))
        self.tool_var = tk.StringVar(value="polygon")
        for text, value in (("多边形", "polygon"), ("画笔", "brush"), ("橡皮", "erase"),
                            ("智能选区", "magic"), ("测高度", "height")):
            tk.Radiobutton(header, text=text, value=value, variable=self.tool_var,
                           command=lambda v=value: self.set_tool(v), bg="#161b22", fg="#c9d1d9",
                           selectcolor="#161b22", activebackground="#161b22",
                           activeforeground="#fff", indicatoron=False, width=6,
                           font=FONT).pack(side="left", padx=2)

        tk.Label(header, text="笔刷", bg="#161b22", fg="#8b949e").pack(side="left", padx=(10, 2))
        self.brush_scale = tk.Scale(header, from_=1, to=40, orient="horizontal", length=120,
                                    showvalue=False, command=self.set_brush,
                                    bg="#161b22", fg="#c9d1d9", highlightthickness=0,
                                    troughcolor="#30363d")
        self.brush_scale.set(4)
        self.brush_scale.pack(side="left")

        actions = tk.Frame(self, bg="#11161d")
        actions.pack(fill="x", side="top")
        for text, cmd, fg in (("保存当前地图", self.save_current, "#0d1117"),
                              ("上传数据到 GitHub", self.open_upload, "#ffffff"),
                              ("撤销", self.undo, "#c9d1d9"),
                              ("重做", self.redo, "#c9d1d9"),
                              ("完成多边形", self.close_poly, "#0d1117"),
                              ("取消多边形", self.cancel_poly, "#c9d1d9"),
                              ("水域→舰艇", self.copy_water_to_boat, "#c9d1d9"),
                              ("适应窗口", self.fit, "#c9d1d9"),
                              ("清空当前图层", self.clear_layer, "#f85149")):
            bg = ("#f0b429" if text == "保存当前地图"
                  else "#3fb950" if text == "完成多边形"
                  else "#1f6feb" if text == "上传数据到 GitHub"
                  else "#21262d")
            tk.Button(actions, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground="#30363d", activeforeground="#fff",
                      relief="flat", padx=9, pady=3, font=FONT).pack(side="left", padx=(8 if text == "保存当前地图" else 3, 3), pady=3)
        tk.Label(actions, text="多边形：逐点左键；撤销删上一个点；双击/Enter/完成按钮闭合。",
                 bg="#11161d", fg="#6e7681").pack(side="left", padx=10)

        body = tk.Frame(self, bg="#0d1117")
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg="#161b22", width=230)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="地图（✓ 已标记）", bg="#161b22", fg="#8b949e",
                 anchor="w", font=FONT_B).pack(fill="x", padx=10, pady=(10, 4))
        self.listbox = tk.Listbox(left, bg="#0d1117", fg="#c9d1d9",
                                  selectbackground="#1f6feb", selectforeground="#fff",
                                  activestyle="none", exportselection=False,
                                  relief="flat", highlightthickness=0, font=FONT)
        self.listbox.pack(fill="both", expand=True, padx=8, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self.on_list_select)
        self.progress = tk.Label(left, text="", bg="#161b22", fg="#8b949e", anchor="w")
        self.progress.pack(fill="x", padx=10, pady=(0, 6))

        tk.Frame(left, bg="#30363d", height=1).pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(left, text="高度查询", bg="#161b22", fg="#8b949e",
                 anchor="w", font=FONT_B).pack(fill="x", padx=10)
        self.height_info = tk.Label(left, text="", bg="#161b22", fg="#6e7681", anchor="w",
                                    justify="left", wraplength=206)
        self.height_info.pack(fill="x", padx=10, pady=(2, 6))

        tk.Frame(left, bg="#30363d", height=1).pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(left, text="智能选区（颜色成簇）", bg="#161b22", fg="#8b949e",
                 anchor="w", font=FONT_B).pack(fill="x", padx=10)
        self.sel_info = tk.Label(left, text="", bg="#161b22", fg="#6e7681", anchor="w",
                                 justify="left", wraplength=206)
        self.sel_info.pack(fill="x", padx=10, pady=(2, 4))
        self.tol_label = tk.Label(left, text=f"颜色容差 {self.tol}", bg="#161b22", fg="#8b949e",
                                  anchor="w")
        self.tol_label.pack(fill="x", padx=10)
        self.tol_scale = tk.Scale(left, from_=0, to=TOL_MAX, orient="horizontal",
                                  showvalue=False, command=self.on_tol_change,
                                  bg="#161b22", fg="#c9d1d9", highlightthickness=0,
                                  troughcolor="#30363d", sliderlength=18)
        self.tol_scale.set(self.tol)
        self.tol_scale.pack(fill="x", padx=8)
        sel_row1 = tk.Frame(left, bg="#161b22")
        sel_row1.pack(fill="x", padx=8, pady=(2, 0))
        for text, cmd, bg, fg in (("写入图层", self.apply_selection, "#3fb950", "#0d1117"),
                                  ("转多边形", self.selection_to_polygon, "#21262d", "#c9d1d9")):
            tk.Button(sel_row1, text=text, command=cmd, bg=bg, fg=fg, relief="flat",
                      activebackground="#30363d", activeforeground="#fff",
                      padx=4, pady=3, font=FONT).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(left, text="取消选区（Esc）", command=self.clear_selection, bg="#21262d",
                  fg="#c9d1d9", relief="flat", activebackground="#30363d",
                  activeforeground="#fff", padx=4, pady=3, font=FONT).pack(fill="x", padx=10, pady=(4, 8))

        right = tk.Frame(body, bg="#0d1117")
        right.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(right, bg="#0b0f14", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<ButtonPress-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Return>", lambda e: self.on_enter())
        self.canvas.bind("<ButtonPress-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        self.canvas.bind("<ButtonPress-3>", self.on_pan_start)
        self.canvas.bind("<B3-Motion>", self.on_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self.on_pan_end)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: self.hide_brush_cursor())

        self.status = tk.Label(self, text="", bg="#161b22", fg="#8b949e", anchor="w", padx=10)
        self.status.pack(fill="x", side="bottom")
        self.hint = tk.Label(self, text="图层：W水域 B桥梁 N边界内 R通畅道路 O障碍 V舰艇航道 · Ctrl+U 上传数据到 GitHub · 多边形左键逐点，双击/Enter/完成按钮闭合，撤销删点 · 智能选区 S：单击成簇，Shift+单击并入，Ctrl+单击挖掉，Enter 写入 · 测高度 H：单击读数 · 中键/右键拖动 · 滚轮缩放 · Ctrl+S 保存",
                             bg="#0d1117", fg="#6e7681", anchor="w", padx=10, pady=3)
        self.hint.pack(fill="x", side="bottom")

    def _bind_keys(self):
        self.bind("<Control-s>", lambda e: self.save_current())
        self.bind("<Control-u>", lambda e: self.open_upload())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Escape>", lambda e: self.on_escape())
        self.bind("<Return>", lambda e: self.on_enter())
        self.bind("w", lambda e: self.set_layer("water"))
        self.bind("b", lambda e: self.set_layer("bridge"))
        self.bind("n", lambda e: self.set_layer("boundary"))
        self.bind("r", lambda e: self.set_layer("clear"))
        self.bind("o", lambda e: self.set_layer("obstacle"))
        self.bind("v", lambda e: self.set_layer("boat"))
        self.bind("p", lambda e: self.set_tool("polygon"))
        self.bind("g", lambda e: self.set_tool("brush"))
        self.bind("e", lambda e: self.set_tool("erase"))
        self.bind("s", lambda e: self.set_tool("magic"))
        self.bind("h", lambda e: self.set_tool("height"))

    # ---------------- maps ----------------
    def _fill_map_list(self):
        self.listbox.delete(0, tk.END)
        names = ("water.png", "bridge.png", "boundary.png", "clear_road.png", "obstacle.png", "boat.png")
        def done(slug):
            return any((OUT / slug / n).exists() for n in names)
        for m in self.maps:
            self.listbox.insert(tk.END, ("✓ " if done(m["slug"]) else "  ") + m["label"])
        self.progress.config(text=f"已标记 {sum(1 for m in self.maps if done(m['slug']))} / {len(self.maps)}")

    def on_list_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        slug = self.maps[sel[0]]["slug"]
        if slug == self.slug:
            return
        self.select_map(slug)

    def select_map(self, slug, force=False):
        if not force:
            if not self.save_current(silent=True):
                return
        m = next((x for x in self.maps if x["slug"] == slug), None)
        if not m:
            return
        src = SOURCE / slug
        try:
            base = Image.open(src / "basemap.webp").convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
            topo_path = src / "topomap.webp"
            topo = Image.open(topo_path).convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS) if topo_path.exists() else base
        except Exception as exc:
            messagebox.showerror("地图加载失败", f"{slug}\n{exc}\n\n缺少地图源数据：先双击 start-annotator.bat，"
                                 f"或运行 tools\\download_maps.ps1 下载。")
            return

        self.slug = slug
        self.base, self.topo = base, topo
        self.water = Image.new("L", (SIZE, SIZE), 0)
        self.bridge = Image.new("L", (SIZE, SIZE), 0)
        self.boundary = Image.new("L", (SIZE, SIZE), 0)
        self.clear = Image.new("L", (SIZE, SIZE), 0)
        self.obstacle = Image.new("L", (SIZE, SIZE), 0)
        mp = OUT / slug
        for name, target in (("water.png", "water"), ("bridge.png", "bridge"),
                             ("boundary.png", "boundary"), ("clear_road.png", "clear"),
                             ("obstacle.png", "obstacle"), ("boat.png", "boat")):
            p = mp / name
            if p.exists():
                im = Image.open(p).convert("L")
                if im.size != (SIZE, SIZE):
                    im = im.resize((SIZE, SIZE), Image.Resampling.NEAREST)
                setattr(self, target, im)
        self.poly = []
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self._src_key = None
        self.hm_field = None
        self.hm_slug = None
        self.clear_selection()
        self.clear_probe()
        self.hide_brush_cursor()
        self.map_label.config(text=m["label"])
        self.zoom = 1.0
        self.cx = self.cy = SIZE / 2
        self.fit()
        self.listbox.selection_clear(0, tk.END)
        idx = self.maps.index(m)
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self.update_status()

    # ---------------- view ----------------
    def on_resize(self, _event=None):
        if self._render_job:
            self.after_cancel(self._render_job)
        self._render_job = self.after(40, self.render)

    def fit(self):
        w = max(200, self.canvas.winfo_width())
        h = max(200, self.canvas.winfo_height())
        self.zoom = max(0.35, min(2.0, min((w - 30) / SIZE, (h - 30) / SIZE)))
        self.cx = self.cy = SIZE / 2
        self.render()

    def view_rect(self):
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        vw, vh = w / self.zoom, h / self.zoom
        return self.cx - vw / 2, self.cy - vh / 2, w, h

    def map_to_screen(self, x, y):
        x0, y0, _w, _h = self.view_rect()
        return (x - x0) * self.zoom, (y - y0) * self.zoom

    def screen_to_map(self, sx, sy):
        x0, y0, _w, _h = self.view_rect()
        return x0 + sx / self.zoom, y0 + sy / self.zoom

    def render(self):
        self._render_job = None
        if self.slug is None:
            return
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        x0, y0, _w, _h = self.view_rect()
        src = self.topo if self.topo_var.get() else self.base
        box = (x0, y0, x0 + w / self.zoom, y0 + h / self.zoom)
        try:
            view = src.transform((w, h), Image.Transform.EXTENT, box, Image.Resampling.BILINEAR).convert("RGB")
        except Exception:
            view = Image.new("RGB", (w, h), (11, 15, 20))

        for _label, key, color, alpha in LAYERS:
            mask = getattr(self, key)
            if mask.getbbox() is None:
                continue
            m = mask.transform((w, h), Image.Transform.EXTENT, box, Image.Resampling.NEAREST)
            solid = Image.new("RGB", (w, h), color)
            a = m.point(lambda v: int(v * alpha))
            view = Image.composite(solid, view, a)

        if self.sel_image is not None:
            sel = self.sel_image.transform((w, h), Image.Transform.EXTENT, box, Image.Resampling.NEAREST)
            solid = Image.new("RGB", (w, h), SEL_COLOR)
            a = sel.point(lambda v: int(v * 0.34))
            view = Image.composite(solid, view, a)

        self.photo = ImageTk.PhotoImage(view)
        self.canvas.delete("map")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="map")
        self.canvas.tag_lower("map")
        self.draw_selection_outline()
        self.draw_poly_preview()
        self.draw_probe()
        self.redraw_brush_cursor()

    def draw_poly_preview(self):
        self.canvas.delete("poly")
        if not self.poly:
            return
        pts = [self.map_to_screen(x, y) for x, y in self.poly]
        outline = list(pts)
        if len(pts) > 2:
            outline.append(pts[0])          # show the shape that Enter would fill
        if len(outline) > 1:
            self.canvas.create_line(*[c for p in outline for c in p], fill="#67e8f9",
                                    width=2, tags="poly", joinstyle="round")
        # Dense selections would turn their vertex dots into one solid blob, so
        # only hand-placed (short) polygons get the big handles.
        radius = 3 if len(pts) <= 60 else 1
        for x, y in pts:
            self.canvas.create_oval(x-radius, y-radius, x+radius, y+radius,
                                    fill="#67e8f9", outline="", tags="poly")

    def draw_selection_outline(self):
        self.canvas.delete("sel")
        for item in self.sel_ops:
            sx, sy = self.map_to_screen(item["x"], item["y"])
            color = SEL_OUTLINE if item["op"] > 0 else SEL_REJECT
            self.canvas.create_oval(sx - 5, sy - 5, sx + 5, sy + 5,
                                    outline=color, width=2, tags="sel")
            self.canvas.create_line(sx - 8, sy, sx + 8, sy, fill=color, tags="sel")
            self.canvas.create_line(sx, sy - 8, sx, sy + 8, fill=color, tags="sel")
        for loop in self.sel_loops:
            if len(loop) < 3:
                continue
            pts = [self.map_to_screen(float(px), float(py)) for px, py in loop]
            pts.append(pts[0])
            flat = [c for p in pts for c in p]
            self.canvas.create_line(*flat, fill=SEL_OUTLINE, width=2,
                                    tags="sel", joinstyle="round")

    # ---------------- tools ----------------
    def set_layer(self, layer):
        self.layer = layer
        label = LABEL_BY_KEY.get(layer, "水域")
        self.layer_var.set(label)
        try:
            self.layer_combo.set(label)
        except Exception:
            pass
        self.update_status()

    def on_layer_combo(self, _event=None):
        label = self.layer_var.get()
        self.set_layer(KEY_BY_LABEL.get(label, "water"))

    def set_tool(self, tool):
        previous = self.tool
        self.tool = tool
        self.tool_var.set(tool)
        if tool != "polygon":
            self.cancel_poly()
        if tool != "magic" and previous == "magic":
            self.clear_selection()
        if tool != "height":
            self.clear_probe()          # the reading belongs to its tool
        self.redraw_brush_cursor()      # shows for brush/erase, drops otherwise
        self.update_status()

    def set_brush(self, value):
        self.brush = max(1, int(float(value)))
        self.redraw_brush_cursor()

    # ---------------- brush cursor ----------------
    # The brush stamps a disc of self.brush *map* pixels (paint_at), so on
    # screen it is a circle of brush * zoom — that is what the outline shows.
    def brush_cursor_active(self):
        return self.slug is not None and self.tool in ("brush", "erase")

    def hide_brush_cursor(self):
        self._brush_pos = None
        canvas = getattr(self, "canvas", None)      # may run before the UI exists
        if canvas is not None:
            canvas.delete("cursor")

    def redraw_brush_cursor(self):
        """Repaint the outline after a zoom, a size change or a re-render."""
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        if self._brush_pos is None or not self.brush_cursor_active():
            canvas.delete("cursor")
            return
        self.draw_brush_cursor(*self._brush_pos)

    def update_brush_cursor(self, sx, sy):
        if not self.brush_cursor_active():
            self.hide_brush_cursor()
            return
        mx, my = self.screen_to_map(sx, sy)
        if not (0 <= mx < SIZE and 0 <= my < SIZE):
            self.hide_brush_cursor()      # pointer is off the map image
            return
        self._brush_pos = (sx, sy)
        self.draw_brush_cursor(sx, sy)

    def draw_brush_cursor(self, sx, sy):
        self.canvas.delete("cursor")
        r = self.brush * self.zoom
        erasing = self.tool == "erase"
        core = CURSOR_ERASE if erasing else CURSOR_BRUSH
        if r < 3:
            # Too small to show as a ring: mark the exact stamp centre.
            self.canvas.create_oval(sx - 2, sy - 2, sx + 2, sy + 2,
                                    fill=core, outline="#000000", tags="cursor")
        else:
            # Black band under the coloured ring stays readable on any basemap;
            # the erase ring is red so "取走像素" never looks like "涂上像素".
            self.canvas.create_oval(sx - r, sy - r, sx + r, sy + r,
                                    outline="#000000", width=3, tags="cursor")
            self.canvas.create_oval(sx - r, sy - r, sx + r, sy + r,
                                    outline=core, width=2 if erasing else 1, tags="cursor")
        self.canvas.tag_raise("cursor")

    # ---------------- height probe ----------------
    # heightmap.png is the map's own elevation raster (~1 m/px, encoded in R/B).
    # A click pins a reading: absolute world Z (decoded + the catalog's zoff) and
    # the same height relative to the map's lowest point, which is what the game
    # and SquadCalc's firing solutions work with.
    def height_ready(self):
        return self.slug is not None and self.base is not None

    def height_field(self):
        """Decoded height raster for the current map (relative metres), cached."""
        if self.hm_field is not None and self.hm_slug == self.slug:
            return self.hm_field
        self.hm_field = None
        self.hm_slug = self.slug
        if not self.slug:
            return None
        m = next((mm for mm in self.maps if mm["slug"] == self.slug), None)
        path = SOURCE / self.slug / "heightmap.png"
        if m is None or not path.exists():
            return None
        try:
            self.hm_field = decode_heightmap(path, m.get("zscale") or 1.0)
        except Exception as exc:
            print(f"[height] {self.slug}: {exc}")
            self.hm_field = None
        return self.hm_field

    def height_reading(self, x, y):
        """(relative, absolute) metres at map pixel (x, y), or None if unknown."""
        if not self.height_ready():
            return None
        field = self.height_field()
        m = next((mm for mm in self.maps if mm["slug"] == self.slug), None)
        if field is None or m is None:
            return None
        rel = height_at_pixel(field, x, y, SIZE)
        return rel, rel + (m.get("zoff") or 0.0)

    def probe_click(self, e):
        if not self.height_ready():
            return
        x, y = self.screen_to_map(e.x, e.y)
        x, y = int(round(x)), int(round(y))
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            return
        if self.hm_field is None:
            # First click on a map decodes its ~4000x4000 raster (~0.3 s).
            self.canvas.config(cursor="watch")
            self.status.config(text="正在读取 heightmap.png…")
            self.update_idletasks()
        reading = self.height_reading(x, y)
        self.canvas.config(cursor="crosshair")
        if reading is None:
            self.status.config(text="这张地图没有 heightmap.png，读不到高度（先在 tools/download_maps.py 里补齐）。")
            return
        rel, world = reading
        self.probe = {"x": x, "y": y, "rel": rel, "world": world}
        self.draw_probe()
        self._refresh_height_info()
        self.update_status(x, y)
        self.status.config(text=f"该点地形高度 {rel:.1f} m（相对地图最低点；世界 Z {world:+.1f} m）"
                                f" · 像素 {x}, {y}。再单击可换点，Esc 清除标记。")

    def draw_probe(self):
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        canvas.delete("probe")
        if not self.probe:
            return
        sx, sy = self.map_to_screen(self.probe["x"], self.probe["y"])
        canvas.create_oval(sx - 7, sy - 7, sx + 7, sy + 7,
                           outline=PROBE_RING, width=2, tags="probe")
        canvas.create_line(sx - 14, sy, sx + 14, sy, fill=PROBE_RING, tags="probe")
        canvas.create_line(sx, sy - 14, sx, sy + 14, fill=PROBE_RING, tags="probe")
        width = max(100, canvas.winfo_width())
        height = max(100, canvas.winfo_height())
        tx, ty = sx + 16, sy - 14
        anchor = "sw"
        if sx > width - 130:            # keep the label inside the viewport
            tx, anchor = sx - 16, "se"
        if sy < 34:
            ty = sy + 14
            anchor = "nw" if anchor == "sw" else "ne"
        tid = canvas.create_text(tx, ty, text=f"{self.probe['rel']:.1f} m",
                                 fill=PROBE_TEXT, anchor=anchor, tags="probe",
                                 font=("Microsoft YaHei UI", 11, "bold"))
        box = canvas.bbox(tid)
        if box:
            plate = canvas.create_rectangle(box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2,
                                            fill="#0d1117", outline=PROBE_RING, tags="probe")
            canvas.tag_lower(plate, tid)
        canvas.tag_raise("probe")

    def clear_probe(self):
        self.probe = None
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.delete("probe")
        self._refresh_height_info()

    def _refresh_height_info(self):
        label = getattr(self, "height_info", None)
        if label is None:
            return
        if not self.probe:
            label.config(text="工具切到「测高度」后单击地图：\n"
                              "按 heightmap.png（约 1 米/像素）\n"
                              "读出该点地形高度（相对最低点）\n"
                              "与游戏世界 Z。")
            return
        m = next((mm for mm in self.maps if mm["slug"] == self.slug), None)
        wx, wz = world_xz(m, self.probe["x"], self.probe["y"], SIZE) if m else (0.0, 0.0)
        label.config(text=f"地形高度 {self.probe['rel']:.1f} m\n"
                          f"（相对地图最低点）\n"
                          f"世界 Z {self.probe['world']:+.1f} m\n"
                          f"像素 {self.probe['x']}, {self.probe['y']}\n"
                          f"世界坐标 x {wx:.0f} / z {wz:.0f}")

    def current_mask(self):
        return getattr(self, self.layer)

    def snapshot(self):
        layer = self.layer
        self.undo_stack.append((layer, self.current_mask().tobytes()))
        if len(self.undo_stack) > 40:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        # While drawing a polygon, undo removes the last vertex instead of
        # restoring a mask snapshot (there is no mask yet).
        if self.tool == "polygon" and self.poly:
            self.poly.pop()
            self.draw_poly_preview()
            self.update_status()
            return
        # While building a smart selection, undo drops the last seed.
        if self.tool == "magic" and self.sel_ops:
            self.sel_ops.pop()
            if self.sel_ops:
                self.rebuild_selection()
            else:
                self.clear_selection()
            self.update_status()
            return
        if not self.undo_stack:
            return
        layer, raw = self.undo_stack.pop()
        self.redo_stack.append((layer, getattr(self, layer).tobytes()))
        setattr(self, layer, Image.frombytes("L", (SIZE, SIZE), raw))
        self.dirty = True
        self.render()

    def redo(self):
        if not self.redo_stack:
            return
        layer, raw = self.redo_stack.pop()
        self.undo_stack.append((layer, getattr(self, layer).tobytes()))
        setattr(self, layer, Image.frombytes("L", (SIZE, SIZE), raw))
        self.dirty = True
        self.render()

    def on_left_down(self, e):
        self.canvas.focus_set()
        if self.tool == "height":
            self.probe_click(e)
            return
        if self.tool == "magic":
            self.magic_click(e)
            return
        if self.tool == "polygon":
            self.poly.append(self.screen_to_map(e.x, e.y))
            self.draw_poly_preview()
            self.update_status()
        else:
            self.snapshot()
            self.dirty = True
            self.last_brush = (e.x, e.y)
            self.update_brush_cursor(e.x, e.y)
            self.paint_at(e.x, e.y)

    def on_left_move(self, e):
        if self.tool in ("brush", "erase") and self.last_brush is not None:
            self.paint_at(e.x, e.y, self.last_brush)
            self.last_brush = (e.x, e.y)
        self.update_brush_cursor(e.x, e.y)

    def on_left_up(self, _e):
        self.last_brush = None

    def paint_at(self, sx, sy, prev=None):
        mask = self.current_mask()
        draw = ImageDraw.Draw(mask)
        x, y = self.screen_to_map(sx, sy)
        value = 0 if self.tool == "erase" else 255
        r = self.brush
        if prev is not None:
            px, py = self.screen_to_map(*prev)
            draw.line([(px, py), (x, y)], fill=value, width=max(1, r * 2))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=value)
        self.render()
        self.update_status(x, y)

    def on_double(self, e):
        if self.tool == "polygon":
            # A double click also generates two Button-1 events; drop the
            # duplicate last vertex before closing.
            if len(self.poly) >= 2:
                x, y = self.screen_to_map(e.x, e.y)
                lx, ly = self.poly[-1]
                if (x - lx) ** 2 + (y - ly) ** 2 < 25:
                    self.poly.pop()
            self.close_poly()

    def close_poly(self, _event=None):
        if self.tool != "polygon":
            return
        if len(self.poly) < 3:
            self.status.config(text="多边形至少需要 3 个点")
            return
        self.snapshot()
        ImageDraw.Draw(self.current_mask()).polygon(self.poly, fill=255)
        self.dirty = True
        self.poly = []
        self.render()
        self.update_status()
        self.status.config(text="多边形已闭合并写入当前图层；可撤销。")

    def cancel_poly(self):
        self.poly = []
        self.draw_poly_preview()

    # ---------------- smart selection ----------------
    def on_tol_change(self, value):
        self.tol = max(0, int(round(float(value))))
        self.tol_label.config(text=f"颜色容差 {self.tol}")
        if self.sel_ops:
            self._schedule_selection_rebuild()

    def _schedule_selection_rebuild(self):
        if self._sel_job is not None:
            try:
                self.after_cancel(self._sel_job)
            except Exception:
                pass
        self._sel_job = self.after(180, self.rebuild_selection)

    def source_array(self):
        """The RGB array the cluster scan runs on (current basemap / topomap)."""
        key = (self.slug, bool(self.topo_var.get()))
        if self._src_np is None or self._src_key != key:
            src = self.topo if self.topo_var.get() else self.base
            self._src_np = np.asarray(src.convert("RGB"), dtype=np.uint8)
            self._src_key = key
        return self._src_np

    def magic_click(self, e):
        if self.slug is None or self.base is None:
            return
        x, y = self.screen_to_map(e.x, e.y)
        x, y = int(round(x)), int(round(y))
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            return
        src = self.source_array()
        rgb = src[y, x]
        subtract = bool(e.state & 0x0004)     # Ctrl+click digs a hole in the selection
        self.sel_ops.append({"x": x, "y": y, "op": -1 if subtract else 1,
                             "dist": color_distance(src, rgb).astype(np.float16)})
        if len(self.sel_ops) > MAX_SEEDS:
            self.sel_ops = self.sel_ops[-MAX_SEEDS:]
        self.update_status(x, y)
        self.rebuild_selection()

    def rebuild_selection(self):
        """Re-grow every seed with the current tolerance, then re-trace polygons."""
        self._sel_job = None
        if self.slug is None or not self.sel_ops:
            return
        self.status.config(text="正在把颜色相近的相邻像素扩成一簇…")
        self.canvas.config(cursor="watch")
        self.update_idletasks()
        tol = self.tol
        mask = np.zeros((SIZE, SIZE), dtype=bool)
        for item in self.sel_ops:
            part = region_grow(item["dist"] <= tol, item["x"], item["y"])
            if item["op"] > 0:
                mask |= part
            else:
                mask &= ~part
        self.sel_mask = mask
        self.sel_written = ""
        if mask.any():
            ys, xs = np.nonzero(mask)
            self.sel_pixels = int(mask.sum())
            self.sel_bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
            self.sel_image = Image.fromarray(mask.astype(np.uint8) * 255, "L")
            loops = mask_loops(mask)
            # Speckle holes are noise, not regions: keep the outlines that
            # actually carry area, biggest first.
            cutoff = max(64.0, 0.002 * self.sel_pixels)
            significant = [l for l in loops if abs(_signed_area(l)) >= cutoff]
            self.sel_loops = (significant or loops)[:6]
            if self.sel_loops:
                area = int(abs(_signed_area(self.sel_loops[0])))
                extra = f"，另有 {len(self.sel_loops) - 1} 块显著轮廓" if len(self.sel_loops) > 1 else ""
                self.status.config(
                    text=f"选区 {self.sel_pixels:,} 像素，主轮廓约 {area:,} px²{extra}（容差 {tol}）。"
                         f"Enter 或「写入图层」写入当前图层。")
            else:
                self.status.config(
                    text=f"选区 {self.sel_pixels:,} 像素，但没有闭合轮廓（簇太细）。调大容差或换位置单击。")
        else:
            self.sel_pixels = 0
            self.sel_bbox = None
            self.sel_image = None
            self.sel_loops = []
            self.status.config(text="这一簇没有像素：容差太小或点到边界，调大容差或换个位置单击。")
        self.canvas.config(cursor="crosshair")
        self.render()
        self._refresh_sel_info()

    def _refresh_sel_info(self):
        if not hasattr(self, "sel_info"):
            return
        if not self.sel_ops or not self.sel_pixels:
            text = ("工具切到「智能选区」后单击地图：\n"
                    "自动把颜色相近的相邻像素扩成一簇，\n"
                    "再写入当前图层或转成多边形。\n"
                    "Shift+单击并入、Ctrl+单击挖掉。")
        else:
            x0, y0, x1, y1 = self.sel_bbox
            biggest = len(self.sel_loops[0]) if self.sel_loops else 0
            text = (f"选区 {self.sel_pixels:,} 像素\n"
                    f"包围盒 {x1 - x0 + 1}×{y1 - y0 + 1}\n"
                    f"多边形 {len(self.sel_loops)} 个，最大 {biggest} 点\n"
                    f"颜色容差 {self.tol}")
        if self.sel_written:
            text += f"\n{self.sel_written}"
        self.sel_info.config(text=text)

    def apply_selection(self):
        if self.sel_mask is None or not self.sel_pixels:
            self.status.config(text="没有可写入的选区：先切到「智能选区」并单击地图。")
            return
        self.snapshot()
        merged = np.maximum(np.asarray(self.current_mask(), dtype=np.uint8),
                            self.sel_mask.astype(np.uint8) * 255)
        setattr(self, self.layer, Image.fromarray(merged, "L"))
        self.dirty = True
        label = LABEL_BY_KEY.get(self.layer, self.layer)
        self.sel_written = f"已写入「{label}」"
        self.render()
        self.update_status()
        self._refresh_sel_info()
        self.status.config(text=f"选区已写入「{label}」；可撤销，也可以换图层再写入一次。")

    def selection_to_polygon(self):
        if not self.sel_loops:
            self.status.config(text="没有可转换的选区：先单击地图生成一簇像素。")
            return
        outer = simplify_loop(self.sel_loops[0], POLY_EPS)
        count = len(outer)
        self.clear_selection()
        self.set_tool("polygon")
        self.poly = [(float(px), float(py)) for px, py in outer]
        self.draw_poly_preview()
        self.update_status()
        self.status.config(text=f"已把选区外轮廓转成 {count} 点多边形：可继续逐点编辑，Enter 闭合写入图层。")

    def clear_selection(self):
        if self._sel_job is not None:
            try:
                self.after_cancel(self._sel_job)
            except Exception:
                pass
            self._sel_job = None
        had = bool(self.sel_ops) or self.sel_mask is not None
        self.sel_ops = []
        self.sel_mask = None
        self.sel_image = None
        self.sel_loops = []
        self.sel_pixels = 0
        self.sel_bbox = None
        self.sel_written = ""
        if had:
            self.canvas.delete("sel")
            self.render()
        self._refresh_sel_info()

    def on_enter(self):
        if self.tool == "magic":
            self.apply_selection()
        else:
            self.close_poly()

    def on_escape(self):
        if self.tool == "height" and self.probe is not None:
            self.clear_probe()
            self.update_status()
        elif self.tool == "magic" or self.sel_ops:
            self.clear_selection()
        else:
            self.cancel_poly()

    def copy_water_to_boat(self):
        if not messagebox.askyesno("生成舰艇航道", "用当前水域掩膜覆盖水面舰艇航道？\n覆盖后可以继续增删修改。"):
            return
        self.set_layer("boat")
        self.snapshot()
        self.boat = self.water.copy()
        self.dirty = True
        self.render()
        self.status.config(text="已从水域复制到舰艇航道；现在可以继续修改。")

    def clear_layer(self):
        if not messagebox.askyesno("清空图层", f"清空当前地图的{'水域' if self.layer == 'water' else '桥梁'}标记？"):
            return
        self.snapshot()
        setattr(self, self.layer, Image.new("L", (SIZE, SIZE), 0))
        self.dirty = True
        self.render()

    # ---------------- pan / zoom ----------------
    def on_pan_start(self, e):
        self.pan = (e.x, e.y, self.cx, self.cy)
        self.canvas.config(cursor="fleur")
        self.hide_brush_cursor()

    def on_pan_move(self, e):
        if not self.pan:
            return
        sx, sy, cx0, cy0 = self.pan
        self.cx = cx0 - (e.x - sx) / self.zoom
        self.cy = cy0 - (e.y - sy) / self.zoom
        self.render()
        self.update_brush_cursor(e.x, e.y)

    def on_pan_end(self, _e):
        self.pan = None
        self.canvas.config(cursor="crosshair")
        self.redraw_brush_cursor()

    def on_wheel(self, e):
        if not self.slug:
            return
        mx, my = self.screen_to_map(e.x, e.y)
        factor = 1.18 if e.delta > 0 else 1 / 1.18
        self.zoom = max(0.35, min(8.0, self.zoom * factor))
        self.cx = mx - (e.x - self.canvas.winfo_width() / 2) / self.zoom
        self.cy = my - (e.y - self.canvas.winfo_height() / 2) / self.zoom
        self.render()

    # ---------------- upload to GitHub ----------------
    def open_upload(self):
        """Publish the built data packs to the MapData repo, no AI in the loop."""
        self.save_current(silent=True)
        win = getattr(self, "_upload_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    win.focus_force()
                    return
            except tk.TclError:
                pass
        self._upload_win = UploadDialog(self)

    # ---------------- save ----------------
    def save_current(self, silent=False):
        if self.slug is None:
            return True
        if not self.dirty:
            if not silent:
                self.update_status()
            return True
        mp = OUT / self.slug
        try:
            mp.mkdir(parents=True, exist_ok=True)
            for name, img in (("water.png", self.water), ("bridge.png", self.bridge),
                              ("clear_road.png", self.clear), ("obstacle.png", self.obstacle),
                              ("boat.png", self.boat)):
                tmp = mp / (name + ".tmp")
                img.save(tmp, format="PNG", optimize=True)
                tmp.replace(mp / name)
            # Boundary is optional: no file means "entire map is inside".
            bp = mp / "boundary.png"
            if self.boundary.getbbox() is not None:
                tmp = mp / "boundary.png.tmp"
                self.boundary.save(tmp, format="PNG", optimize=True)
                tmp.replace(bp)
            elif bp.exists():
                bp.unlink()
            meta = {
                "slug": self.slug,
                "label": next(m["label"] for m in self.maps if m["slug"] == self.slug),
                "size": SIZE,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": "annotate_maps.py/2",
                "layers": ["water", "bridge", "boundary", "clear", "obstacle", "boat"],
            }
            (mp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            self.dirty = False
            self._fill_map_list()
            if not silent:
                self.status.config(text=f"已保存 {mp}  ({time.strftime('%H:%M:%S')})")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

    def on_close(self):
        if not self.save_current(silent=True):
            return
        self.destroy()

    def update_status(self, x=None, y=None):
        if not self.slug:
            return
        m = next((m for m in self.maps if m["slug"] == self.slug), None)
        pos = ""
        if x is not None and y is not None:
            pos = f" · 像素 {int(x)}, {int(y)}"
        if self.tool == "height" and x is not None and y is not None:
            reading = self.height_reading(int(x), int(y))
            if reading is not None:
                pos += f" · 高度 {reading[0]:.1f} m"
        if self.tool in ("brush", "erase") and self.slug:
            # At fit zoom the outline can be only a few pixels wide, so repeat
            # the size as text: self.brush is the radius in map pixels.
            pos += f" · 笔刷半径 {self.brush}px（直径 {self.brush * 2}px）"
        self.status.config(text=f"{m['label'] if m else ''} · {SIZE}×{SIZE} · 缩放 {self.zoom:.2f} · "
                                f"图层 {LABEL_BY_KEY.get(self.layer, self.layer)} · 工具 {self.tool}{pos}")

    def on_motion(self, e):
        if self.slug:
            x, y = self.screen_to_map(e.x, e.y)
            self.update_status(x, y)
        self.update_brush_cursor(e.x, e.y)


if __name__ == "__main__":
    app = Annotator()
    app.mainloop()
