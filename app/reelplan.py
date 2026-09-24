"""Рилсы v5: вёрстка и анимация в Remotion (папка remotion/). Здесь бот готовит для него props:
камеру, время каждого слова, выноски, звуки, титр — и запускает рендер.

Картинки и голос Remotion берёт с маленького локального HTTP-сервера, который поднимается на время рендера
и отдаёт папку рилса. Если Remotion недоступен или упал, reels.py собирает видео старой вёрсткой (reelrender.py)."""
import functools
import hashlib
import http.server
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image

from app import config, reelrender

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent / "remotion"
BUNDLE = ROOT / "build"
FPS = 30
W, H = 1080, 1920
A = H / W
M = 72
SEG = float(os.getenv("REEL_SEG", "3.6"))
TITLE_HOLD = 3.0
RUBRIC = os.getenv("REEL_RUBRIC", "Paintings, closely")
# больше, чем ядер у машины, Remotion не принимает и падает
CONCURRENCY = str(max(1, min(int(os.getenv("REMOTION_CONCURRENCY", "2")), os.cpu_count() or 1)))


def available() -> bool:
    return BUNDLE.joinpath("index.html").exists() and bool(shutil.which("npx"))


def why_not() -> str:
    if not BUNDLE.joinpath("index.html").exists():
        return "нет сборки remotion/build (Dockerfile собирает её при деплое)"
    if not shutil.which("npx"):
        return "на сервере нет Node.js"
    return ""


# ======================= локальный сервер для картинок и голоса =======================

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class Serve:
    """with Serve(folder) as url: …  — отдаёт файлы папки по http://127.0.0.1:порт/"""

    def __init__(self, folder: Path):
        self.folder = folder

    def __enter__(self) -> str:
        handler = functools.partial(_Quiet, directory=str(self.folder))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def _url(base: str, folder: Path, path: Path | str) -> str:
    rel = Path(path).resolve().relative_to(folder.resolve())
    return f"{base}/{'/'.join(rel.parts)}"


# ======================= камера =======================
# Камера — [время, cx, cy, cw(, изгиб)] в пикселях картины: центр кадра и его ширина. На экране масштаб s = W / cw.
# Деталь ставится не в центр кадра, а в центр «чистого окна» — между верхней строкой и текстом внизу,
# и не приближается сильнее ZOOM_MAX экранных пикселей на пиксель картины (иначе мыло).

ZOOM_MAX = float(os.getenv("REEL_ZOOM_MAX", "1.25"))
WIN = (270, 1220)            # окно для детали под рассказом: от верхней строки до подписи
FULL_H = 1450                # общий план: картина не выше этого
HOLD_ZOOM = 1.04             # медленный наезд, пока камера стоит на детали
SLACK = (60, 220)            # насколько кадр может выйти за край картины (экранные px по x, y): деталь у самого
                             # края лучше показать с полоской тёмного фона, чем уводить под текст или отъезжать


def _size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def _clamp_cam(pw, ph, cx, cy, cw, slack=(0, 0)):
    """Кадр не выходит за картину (с запасом slack экранных px) — по тем осям, где он меньше картины."""
    ch = cw * A
    s = W / cw
    sx, sy = slack[0] / s, slack[1] / s
    if cw <= pw:
        cx = min(max(cx, cw / 2 - sx), pw - cw / 2 + sx)
    if ch <= ph:
        cy = min(max(cy, ch / 2 - sy), ph - ch / 2 + sy)
    return [cx, cy, cw]


def _s_full(pw, ph):
    return min(W / pw, FULL_H / ph)


def _full(pw, ph):
    """Вся картина: по ширине кадра (высокая — не выше FULL_H), центр — в середине окна над текстом."""
    s = _s_full(pw, ph)
    wy = (WIN[0] + WIN[1]) / 2
    top = max(WIN[0] - 40, wy - ph * s / 2)            # высокая картина начинается у верхней строки
    cy = ph / 2 - (top + ph * s / 2 - H / 2) / s
    return [pw / 2, cy, W / s]


def _hook_win(text: str) -> tuple[int, int]:
    """Хук набран крупно сверху: деталь — под ним."""
    lines = max(1, -(-len(text) // 18))
    return (min(250 + lines * 108 + 60, 900), 1480)


def _fit(pw, ph, box, win):
    """Кадр, в котором деталь box (доли) целиком в окне win (y экрана) с полями. → [cx, cy, cw]."""
    x0, y0, x1, y1 = [max(0.0, min(1.0, float(v))) for v in box]
    bx0, by0, bx1, by1 = x0 * pw, y0 * ph, x1 * pw, y1 * ph
    bw, bh = max(bx1 - bx0, pw * 0.02), max(by1 - by0, ph * 0.02)
    bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
    wy0, wy1 = win
    wyc = (wy0 + wy1) / 2
    s_min = _s_full(pw, ph)
    # деталь — около двух третей окна: вокруг остаётся картина, видно, в каком она месте
    s = max(min(0.70 * 960 / bw, 0.62 * (wy1 - wy0) / bh, ZOOM_MAX), s_min)
    for _ in range(12):
        cx, cy, cw = _clamp_cam(pw, ph, bcx, bcy - (wyc - H / 2) / s, W / s, SLACK)
        # у края картины камера упирается — проверяем, что деталь всё ещё в окне и не под текстом
        top = H / 2 + (by0 - cy) * s
        bot = H / 2 + (by1 - cy) * s
        left = W / 2 + (bx0 - cx) * s
        right = W / 2 + (bx1 - cx) * s
        if top >= wy0 - 60 and bot <= wy1 + 60 and left >= -10 and right <= W + 10:
            return [cx, cy, cw]
        if s <= s_min * 1.001:
            break
        s = max(s * 0.9, s_min)
    return _full(pw, ph)


def _hold(pw, ph, r, box, k=HOLD_ZOOM):
    """Медленный наезд на месте: приблизить в k раз так, чтобы центр детали остался в той же точке экрана."""
    cx, cy, cw = r
    if box:
        x0, y0, x1, y1 = [max(0.0, min(1.0, float(v))) for v in box]
        px, py = (x0 + x1) / 2 * pw, (y0 + y1) / 2 * ph
    else:
        px, py = pw / 2, ph / 2
    s, s2 = W / cw, W / cw * k
    sx, sy = W / 2 + (px - cx) * s, H / 2 + (py - cy) * s
    return _clamp_cam(pw, ph, px - (sx - W / 2) / s2, py - (sy - H / 2) / s2, cw / k, SLACK if box else (0, 0))


# Движение: плавный разгон и торможение (синус), скорость ограничена — на телефоне быстрый проезд дёргается.
# Камера всегда едет от детали к детали — зритель видит, где на картине эта часть. На быстром участке проезда
# картинка чуть смазывается по направлению движения (как у настоящей камеры), поэтому проезд не стробит.
PAN_MAX = float(os.getenv("REEL_PAN_MAX", "30"))     # пик скорости проезда, экранных px за кадр
ZOOM_RATE = 0.025                                    # пик скорости наезда: 2,5% масштаба за кадр
MOVE_MIN, MOVE_MAX = 1.8, 4.5                        # переезд, с
MIN_HOLD = 1.2                                       # сколько камера стоит на детали до следующего переезда, с
CUT_OVER = float(os.getenv("REEL_CUT_OVER", "99"))   # переезд дольше — растворением (по умолчанию — никогда)
DISSOLVE = 0.7


def _need(a, b):
    """Сколько секунд нужно на переезд a → b, чтобы не превысить скорость проезда и наезда."""
    s = W / (a[2] * b[2]) ** 0.5
    d = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 * s
    t_pan = d * (math.pi / 2) / (PAN_MAX * FPS)
    t_zoom = abs(math.log(a[2] / b[2])) * (math.pi / 2) / (ZOOM_RATE * FPS)
    return min(MOVE_MAX, max(MOVE_MIN, t_pan, t_zoom))


# ======================= слова =======================

def _words(raw: str, starts: list[float]) -> list[dict]:
    """*слово* — акцент (курсив), ^слово — на нём камера приезжает к детали (на экране знака нет)."""
    toks = raw.split()
    starts = (list(starts) + [starts[-1] if starts else 0.0] * len(toks))[:len(toks)]
    out = []
    for tok, t in zip(toks, starts):
        em = "*" in tok
        out.append({"w": tok.replace("*", "").replace("^", ""), "em": em, "t": round(float(t), 3)})
    return out


def _anchor(raw: str) -> int | None:
    for n, tok in enumerate(raw.split()):
        if "^" in tok:
            return n
    return None


# ======================= звук =======================

SFX_VOL = {"page": 0.3, "move": 0.22, "close": 0.28}
SFX_DIR = config.DATA_DIR / "sfx"


def _sfx_pool(name: str) -> list:
    """Свои звуки из /data/sfx (move*.wav, page*.wav, close*.wav), иначе встроенные — студийные записи Mixkit
    (remotion/public/SFX-SOURCES.txt)."""
    own = []
    if SFX_DIR.exists():
        own = sorted(SFX_DIR.glob(f"{name}*.wav")) + sorted(SFX_DIR.glob(f"{name}*.mp3"))
    return own or sorted((ROOT / "public").glob(f"sfx_{name}_*.wav"))


_peaks: dict = {}


def _sfx_peak(f: Path) -> float:
    """Где у звука самая громкая точка, с — чтобы пик прохода воздуха пришёлся на середину переезда."""
    if f not in _peaks:
        try:
            import numpy as np
            exe = reelrender.ffmpeg_exe()
            raw = subprocess.run([exe, "-v", "error", "-i", str(f), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                                 capture_output=True, timeout=30).stdout
            x = np.abs(np.frombuffer(raw, "<i2").astype(np.float32))
            k = 400                                  # огибающая по 50 мс
            env = np.convolve(x, np.ones(k) / k, "same")
            _peaks[f] = float(np.argmax(env)) / 8000 if len(env) else 0.0
        except Exception:
            _peaks[f] = 0.0
    return _peaks[f]


def _sfx(folder: Path, events: list[tuple], seed: str) -> list[dict]:
    """События (t, имя[, по пику]) → [{t, src, vol}]. По пику — t означает момент самой громкой точки звука.
    Одинаковые события берут разные варианты по кругу, начало круга — от seed."""
    out, used = [], {}
    base = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    for e in events:
        t, name = e[0], e[1]
        pool = _sfx_pool(name)
        if not pool:
            continue
        k = used.get(name, 0)
        used[name] = k + 1
        f = pool[(base + k) % len(pool)]
        if len(e) > 2 and e[2]:
            t = t - _sfx_peak(f)
        if f.parent == ROOT / "public":
            src = f"static:{f.name}"
        else:
            dest = folder / "sfx" / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy(f, dest)
            src = str(dest)
        out.append({"t": round(max(0.0, t), 3), "src": src, "vol": SFX_VOL[name]})
    return out


def _mix_voice(clips: list[tuple[float, str, float]], dur: float, dest: Path) -> Path:
    """Фразы голоса в своих местах (t, файл, сколько срезать тишины в начале) → одна дорожка wav,
    громкость голоса выровнена отдельно, в два прохода: звуки потом не вытягиваются нормализацией."""
    exe = reelrender.ffmpeg_exe()
    raw = dest.with_name(dest.stem + "_raw.wav")
    cmd = [exe, "-y", "-loglevel", "error"]
    for _, path, _ in clips:
        cmd += ["-i", path]
    parts = [f"[{n}:a]aresample=44100,atrim=start={trim:.3f},asetpts=PTS-STARTPTS,adelay={int(max(0, t) * 1000)}:all=1[a{n}]"
             for n, (t, _, trim) in enumerate(clips)]
    graph = ";".join(parts) + ";" + "".join(f"[a{n}]" for n in range(len(clips))) + \
        f"amix=inputs={len(clips)}:normalize=0,apad[a]"
    cmd += ["-filter_complex", graph, "-map", "[a]", "-t", f"{dur:.2f}", "-ac", "1", "-ar", "44100", str(raw)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    target = "I=-16:TP=-2:LRA=9"
    r = subprocess.run([exe, "-hide_banner", "-i", str(raw), "-af", f"loudnorm={target}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    try:
        m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
        af = (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
              f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    except (ValueError, KeyError):
        af = f"loudnorm={target}"
    subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(raw), "-af", af, "-ar", "44100", str(dest)],
                   check=True, capture_output=True, timeout=300)
    raw.unlink(missing_ok=True)
    return dest


# ======================= «детали картины» =======================

def story_props(folder: Path, image: Path, beats: list[dict], voice, pt: dict, sfx: bool = True) -> dict:
    """beats — reels.beats(d) (с raw — текст с *акцентами* и ^якорем); voice — дубль OpenAI (dict), фразы по одной
    (list) или None. → props для композиции Story (пути — относительно folder, их заменит render())."""
    pw, ph = _size(image)
    full = _full(pw, ph)
    rects = []
    for b in beats:
        if not b.get("box"):
            rects.append((full, _hold(pw, ph, full, None, 1.03)))
            continue
        r0 = _fit(pw, ph, b["box"], _hook_win(b["text"]) if b["kind"] == "hook" else WIN)
        rects.append((r0, _hold(pw, ph, r0, b["box"])))
    anchors = [_anchor(b["raw"]) for b in beats]
    plan: list[tuple[float, float, int]] = []          # (уезжает, приезжает, 1 — растворение) для каждой части

    def place(i, ws, s0, prev_arrive, end=None):
        """Когда камере уехать с прошлой части и приехать к этой. Общий план — медленный отъезд на всю фразу;
        деталь — проезд, который кончается на слове-якоре (или чуть позже, если переезд длинный: скорость важнее)."""
        if i == 0:
            return 0.0, 0.0, 0
        a, b = rects[i - 1][1], rects[i][0]
        need = _need(a, b)
        if not beats[i].get("box"):
            leave = max(s0 - 0.15, prev_arrive + MIN_HOLD)
            return leave, leave + min(max(need, 2.4), MOVE_MAX), 0
        if anchors[i] is not None and anchors[i] < len(ws):
            want = ws[anchors[i]] + 0.1
        else:
            want = s0 + 0.5
        if need > CUT_OVER:
            leave = max(want - DISSOLVE / 2, prev_arrive + MIN_HOLD)
            return leave, leave + DISSOLVE, 1
        arrive = max(want, prev_arrive + MIN_HOLD + need)
        if end is not None and arrive > end - 1.2:
            # не успевает к концу фразы — приезжаем раньше, проезд короче (но не короче MOVE_MIN)
            arrive = max(end - 1.2, prev_arrive + MIN_HOLD + MOVE_MIN)
        leave = max(prev_arrive + MIN_HOLD, arrive - need)
        return leave, arrive, 0

    out, clips, arrive, ends = [], [], [], []
    one = voice if isinstance(voice, dict) and voice.get("one_take") else None
    per = voice if isinstance(voice, list) else [None] * len(beats)
    per = list(per) + [None] * len(beats)

    if one:
        tb = list(one.get("beats") or []) + [{"start": None, "starts": []}] * len(beats)
        # тишина перед первым словом срезается: хук звучит с первого кадра
        first = (tb[0].get("starts") or [0.0])[0] if tb else 0.0
        lead = max(0.0, float(first or 0.0) - 0.06)
        starts, wss = [], []
        for i, b in enumerate(beats):
            st = tb[i].get("start")
            st = float(st) - lead if st is not None else (starts[-1] + 2.5 if starts else 0.0)
            n = len(b["raw"].split())
            ws = [float(x) - lead for x in tb[i].get("starts") or []] or [st + k * 0.32 for k in range(n)]
            starts.append(st)
            wss.append(ws)
        voice_end = float(one["dur"]) - lead
        for i in range(len(beats)):
            end_i = starts[i + 1] - 0.05 if i + 1 < len(beats) else voice_end + 0.3
            plan.append(place(i, wss[i], starts[i], arrive[-1] if arrive else 0.0, end_i))
            arrive.append(plan[-1][1])
        voice_dur = float(one["dur"]) - lead
        ends = [starts[i + 1] - 0.05 if i + 1 < len(beats) else voice_dur + 0.3 for i in range(len(beats))]
        ends = [max(e, a + 1.0) for e, a in zip(ends, arrive)]
        for i, b in enumerate(beats):
            out.append({"kind": b["kind"], "arrive": arrive[i], "start": starts[i], "end": ends[i],
                        "words": _words(b["raw"], wss[i])})
        clips = [(0.0, one["audio"], lead)]
    else:
        t = 0.0
        for i, b in enumerate(beats):
            v = per[i]
            n = len(b["raw"].split())
            trim = max(0.0, float(v["starts"][0]) - 0.06) if v and v.get("starts") else 0.0
            if i == 0:
                s0 = 0.0
            else:
                s0 = t + (0.7 if b["kind"] == "climax" else 0.2)
            speech = (float(v["dur"]) - trim) if v else n * 0.32 + 0.3
            ws = [s0 + x - trim for x in v["starts"]] if v and v.get("starts") else [s0 + k * 0.32 for k in range(n)]
            plan.append(place(i, ws, s0, arrive[-1] if arrive else 0.0))
            a = plan[-1][1]
            tail = 0.5 if b["kind"] == "hook" else 1.0 if b["kind"] == "climax" else 0.3
            e = max(a + 1.6, s0 + speech + tail)
            out.append({"kind": b["kind"], "arrive": a, "start": s0, "end": e, "words": _words(b["raw"], ws)})
            if v:
                clips.append((s0, v["audio"], trim))
            arrive.append(a)
            ends.append(e)
            t = e
        voice_dur = ends[-1]

    # камера: [время, cx, cy, cw, растворение]. Приехать к arrive, медленно наезжать, уехать к следующей части
    cam = []
    for i, (r0, r1) in enumerate(rects):
        if i == 0:
            cam.append([0.0, *r0, 0])
        else:
            leave, a, cut = plan[i]
            cam.append([leave, *rects[i - 1][1], 0])
            cam.append([a, *r0, cut])
        if i + 1 == len(rects):
            cam.append([max(ends[i], cam[-1][0] + 0.5), *r1, 0])

    reveal_n = 0
    for b, o in zip(beats, out):
        if b["kind"] == "reveal":
            reveal_n += 1
            o["label"] = b.get("label") or ""
            o["n"] = reveal_n
    end_start = max(ends[-1], voice_dur) + 0.3
    # титр: картина вписывается в поле 936×760 слева сверху, камера сама приводит её туда
    s_end = min(936 / pw, 760 / ph)
    cwe = W / s_end
    end_start = max(end_start, cam[-1][0] + 0.1)
    cam.append([end_start, *cam[-1][1:4], 0])
    cam.append([end_start + 1.6, cwe / 2 - M / s_end, cwe * A / 2 - 300 / s_end, cwe, 0])
    dur = end_start + 4.0
    # звуки: мягкий проход воздуха на каждом переезде — его пик на середине переезда, где камера быстрее всего;
    # на титре — перелистнутая страница
    ev = [((pl[0] + pl[1]) / 2, "move", True) for pl in plan[1:] if pl[1] - pl[0] >= 1.0]
    events = _sfx(folder, ev + [(end_start + 0.15, "close", False)], str(image)) if sfx else []

    voice_path = None
    if clips:
        voice_path = folder / "voice_mix.wav"
        _mix_voice(clips, dur, voice_path)
    year = str(pt.get("year") or "").strip()
    meta = [[k, v] for k, v in (("Medium", pt.get("medium")), ("Size", pt.get("size")),
                                ("Collection", pt.get("museum"))) if v]
    return {"fps": FPS, "duration": round(dur * FPS), "pw": pw, "ph": ph,
            "cam": [[round(x, 3) for x in k] for k in cam],
            "beats": out, "sfx": events, "slack": list(SLACK), "endStart": round(end_start, 3), "labelTop": round(300 + ph * s_end + 60),
            "title": pt.get("title") or "", "sub": ", ".join(x for x in (pt.get("author"), year) if x),
            "rubric": RUBRIC, "series": " · ".join(x for x in (pt.get("title"), year) if x), "meta": meta,
            "image": str(image), "voice": str(voice_path) if voice_path else None, "_dur": dur}


# ======================= подборка =======================
# Все работы подборки показываются одинаково: либо все на весь кадр (подборка вертикальных работ), либо все
# целиком, как на стене. Без наездов на детали. Смена работ — через короткое затемнение, кадры не накладываются.

BLEED = float(os.getenv("REEL_BLEED_MAX", "0.72"))   # все работы уже этого (ширина / высота) — подборка на весь кадр
BOX_W, BOX_TOP, BOX_BOTTOM = 936, 260, 1126          # поле картины; низ картины — на одной линии у всех работ
TEXT_TOP = 1190                                       # подпись — на одном месте у всех работ
RHYTHM = [1.0, 0.85, 1.15, 0.9, 1.1, 0.95]


def collection_mode(sizes: list[tuple[int, int]]) -> str:
    return "bleed" if sizes and all(w / h <= BLEED for w, h in sizes) else "frame"


def collection_props(folder: Path, d: dict, sfx: bool = True) -> dict:
    sizes = [_size(Path(it["path"])) for it in d["items"]]
    mode = collection_mode(sizes)
    items, t = [], 0.0
    n_all = len(d["items"])
    for n, (it, (pw, ph)) in enumerate(zip(d["items"], sizes)):
        dur = SEG * (RHYTHM[(n - 1) % len(RHYTHM)] if n else 1.0) + (TITLE_HOLD if n == 0 else 0) \
            + (0.8 if n == n_all - 1 else 0)
        row = {"image": it["path"], "pw": pw, "ph": ph, "start": round(t, 3), "dur": round(dur, 3),
               "title": it.get("title") or "", "author": it.get("author") or "", "year": str(it.get("year") or "")}
        if mode == "frame":
            k = min(BOX_W / pw, (BOX_BOTTOM - BOX_TOP) / ph)
            fw, fh = pw * k, ph * k
            row["frame"] = [round((W - fw) / 2, 1), round(BOX_BOTTOM - fh, 1), round(fw, 1), round(fh, 1)]
        items.append(row)
        t += dur
    cap = re.split(r"(?<=[.!?])\s", (d.get("caption") or "").strip())[0] if d.get("caption") else ""
    ev = [(0.0, "page")] + [(it["start"] - 0.15, "page") for it in items[1:]]
    events = _sfx(folder, ev, d.get("title") or "") if sfx else []
    dur = t + 0.3
    return {"fps": FPS, "duration": round(dur * FPS), "items": items, "mode": mode, "textTop": TEXT_TOP,
            "titleEnd": TITLE_HOLD, "title": d.get("title_em") or d.get("title") or "",
            "subtitle": cap if len(cap) <= 90 else "", "series": (d.get("title") or "").replace("*", ""),
            "sfx": events, "_dur": dur}


# ======================= рендер =======================

def render(comp: str, props: dict, folder: Path, out: Path) -> float:
    """Рендер композиции Remotion → out (mp4 до 50 МБ, громкость под соцсети). → длительность, с."""
    props = json.loads(json.dumps(props))
    with Serve(folder) as base:
        def fix(v):
            return _url(base, folder, v) if isinstance(v, str) and v.startswith(str(folder)) else v
        if props.get("image"):
            props["image"] = fix(props["image"])
        if props.get("voice"):
            props["voice"] = fix(props["voice"])
        for it in props.get("items") or []:
            it["image"] = fix(it["image"])
        for e in props.get("sfx") or []:
            e["src"] = fix(e["src"])
        pfile = folder / f"props_{comp}.json"
        pfile.write_text(json.dumps(props, ensure_ascii=False))
        raw = out.with_name(out.stem + "_raw.mp4")
        cmd = ["npx", "remotion", "render", str(BUNDLE), comp, str(raw), f"--props={pfile}",
               f"--concurrency={CONCURRENCY}", "--crf=20", "--log=error"]
        if os.getenv("REMOTION_BROWSER"):
            cmd.append(f"--browser-executable={os.getenv('REMOTION_BROWSER')}")
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=int(os.getenv("REMOTION_TIMEOUT", "1800")))
        if r.returncode != 0 or not raw.exists():
            raise RuntimeError(f"Remotion: {(r.stderr or r.stdout)[-400:]}")
    # сжать под лимит Telegram и выровнять громкость
    exe = reelrender.ffmpeg_exe()
    has_audio = "Audio:" in subprocess.run([exe, "-hide_banner", "-i", str(raw)], capture_output=True, text=True).stderr
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(raw), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-maxrate", "9M", "-bufsize", "18M", "-pix_fmt", "yuv420p"]
    # голос уже выровнен в _mix_voice; здесь только ограничитель пиков — тихие звуки не вытягиваются
    cmd += (["-af", "alimiter=limit=0.89:level=disabled", "-c:a", "aac", "-b:a", "160k", "-ar", "44100"] if has_audio else ["-an"])
    cmd += ["-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    raw.unlink(missing_ok=True)
    return float(props.get("_dur") or 0)
