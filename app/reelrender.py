"""Сборка рилса: кадры рисует Pillow, склеивает ffmpeg (бинарник приезжает пакетом imageio-ffmpeg).

Кадр 1080×1920, 30 к/с, без звука — музыку автор кладёт сам в Instagram.
Камера — прямоугольник 9:16 внутри «сцены» (картинки): плавно едет и приближается, координаты дробные,
поэтому движение без дрожи. Для скорости у сцены есть пирамида уменьшенных копий.

Два вида:
  подборка — по кадру на работу, у каждой свой медленный наезд или проезд, сверху название и автор;
             первый кадр ещё и с названием подборки;
  детали   — одна картина: общий план, затем камера по очереди переезжает к деталям, под каждой фраза,
             в конце снова общий план и подпись «название, автор, музей».
"""
import logging
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = int(os.getenv("REEL_FPS", "30"))
ASPECT = H / W
FONTS = Path(__file__).resolve().parent.parent / "data" / "fonts"
FADE = 0.35                     # появление и исчезновение текста, с
LOGO_BOTTOM = int(os.getenv("REEL_LOGO_BOTTOM", "380"))   # знак выше подписи Instagram, иначе её плашка его закроет
MAX_SIDE = 4200                 # больше не нужно даже для крупных деталей


# ======================= ffmpeg =======================

def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def ffmpeg_ok() -> bool:
    exe = ffmpeg_exe()
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-version"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


# ======================= шрифты и текст =======================

_fonts: dict = {}


def font(size: int, medium: bool = True, bold: bool = False):
    key = (size, medium, bold)
    if key not in _fonts:
        name = ("IBMPlexSans-Bold.ttf" if bold else "IBMPlexSans-Medium.ttf" if medium
                else "IBMPlexSans-Regular.ttf")
        try:
            _fonts[key] = ImageFont.truetype(str(FONTS / name), size)
        except OSError:
            _fonts[key] = ImageFont.load_default(size=size)
    return _fonts[key]


def _wrap(text: str, f, width: int, max_lines: int = 4) -> list[str]:
    d = ImageDraw.Draw(Image.new("L", (10, 10)))
    out: list[str] = []
    for para in str(text).split("\n"):
        line = ""
        for w in para.split():
            test = f"{line} {w}".strip()
            if d.textlength(test, font=f) <= width or not line:
                line = test
            else:
                out.append(line)
                line = w
        if line:
            out.append(line)
    if len(out) > max_lines:
        out = out[:max_lines]
        out[-1] = out[-1].rstrip(".,;: ") + "…"
    return out


def _balanced(text: str, f, width: int) -> list[str]:
    """Перенос без «висящего» слова: столько же строк, но самая узкая ширина, при которой их не больше."""
    best = _wrap(text, f, width)
    if len(best) < 2:
        return best
    lo, hi = width // 2, width
    while hi - lo > 8:
        mid = (lo + hi) // 2
        if len(_wrap(text, f, mid)) <= len(best):
            hi = mid
        else:
            lo = mid
    return _wrap(text, f, hi)


def text_block(lines: list[tuple[str, object, int]], width: int = 900, gap: int = 14,
               shadow: bool = True) -> Image.Image:
    """Блок строк по центру: [(текст, шрифт, отступ сверху)] → RGBA с мягкой тенью."""
    d = ImageDraw.Draw(Image.new("L", (10, 10)))
    rows, h = [], 0
    for text, f, top in lines:
        for i, ln in enumerate(_balanced(text, f, width)):
            asc, desc = f.getmetrics()
            rows.append((ln, f, h + (top if i == 0 else 0)))
            h += (top if i == 0 else 0) + asc + desc + gap
    h = max(h - gap, 1)
    pad = 40
    im = Image.new("RGBA", (width + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ink = Image.new("L", im.size, 0)
    di = ImageDraw.Draw(ink)
    for ln, f, y in rows:
        x = pad + (width - d.textlength(ln, font=f)) / 2
        di.text((x, pad + y), ln, font=f, fill=255)
    if shadow:
        sh = ink.filter(ImageFilter.GaussianBlur(10)).point(lambda a: min(255, int(a * 1.5)))
        im.paste((0, 0, 0, 150), (0, 0), sh.point(lambda a: int(a * 0.55)))
    im.paste((255, 255, 255, 255), (0, 0), ink)
    return im


def gradient(top: bool, height: int = 760, strength: float = 0.62) -> Image.Image:
    """Затемнение сверху или снизу, чтобы белый текст читался на любом фоне."""
    g = Image.new("L", (1, height))
    for y in range(height):
        t = 1 - y / height if top else y / height
        g.putpixel((0, y), int(255 * strength * (t ** 1.6)))
    g = g.resize((W, height))
    im = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    im.putalpha(g)
    return im


# ======================= сцена и камера =======================

class Stage:
    """Картинка сцены с пирамидой уменьшенных копий."""

    def __init__(self, im: Image.Image):
        self.levels = [im.convert("RGB")]
        while self.levels[-1].width > 2 * W:
            last = self.levels[-1]
            self.levels.append(last.reduce(2))
        self.w, self.h = im.size

    def view(self, cx: float, cy: float, w: float) -> Image.Image:
        h = w * ASPECT
        k = 0
        while k + 1 < len(self.levels) and w / (2 ** (k + 1)) >= W:
            k += 1
        s = 2 ** k
        lv = self.levels[k]
        x0, y0 = max(0.0, (cx - w / 2) / s), max(0.0, (cy - h / 2) / s)
        x1, y1 = min(float(lv.width), (cx + w / 2) / s), min(float(lv.height), (cy + h / 2) / s)
        return lv.resize((W, H), Image.BILINEAR, box=(x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))


def clamp_rect(stage: Stage, cx: float, cy: float, w: float) -> tuple[float, float, float]:
    """Прямоугольник камеры целиком внутри сцены."""
    w = min(w, stage.w, stage.h / ASPECT)
    h = w * ASPECT
    cx = min(max(cx, w / 2), stage.w - w / 2)
    cy = min(max(cy, h / 2), stage.h - h / 2)
    return cx, cy, w


def full_rect(stage: Stage) -> tuple[float, float, float]:
    return clamp_rect(stage, stage.w / 2, stage.h / 2, 1e9)


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _at(keys: list, t: float, eased: bool = True) -> tuple[float, float, float]:
    """Камера в момент t по опорным точкам [(время, (cx, cy, w))]: между ними плавно, ширина — по логарифму."""
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, a), (t1, b) in zip(keys, keys[1:]):
        if t <= t1:
            u = (t - t0) / max(t1 - t0, 1e-6)
            u = _ease(u) if eased else u
            return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u,
                    math.exp(math.log(a[2]) + (math.log(b[2]) - math.log(a[2])) * u))
    return keys[-1][1]


# ======================= знак =======================

_logo = None


def _logo_layer():
    global _logo
    if _logo is None:
        from app import brand
        lg = brand._logo("white", brand.LOGO_W, brand.LOGO_H)
        alpha = lg.getchannel("A").point(lambda a: round(a * brand.OPACITY))
        _logo = (lg.convert("RGB"), alpha, (brand.MARGIN_LEFT, H - LOGO_BOTTOM - brand.LOGO_H))
    return _logo


# ======================= кадры и склейка =======================

def _alpha_cache(im: Image.Image, steps: int = 10) -> list:
    a = im.getchannel("A")
    rgb = im.convert("RGB")
    return [(rgb, a.point(lambda v, k=k: int(v * k / steps))) for k in range(steps + 1)]


def _paste(frame: Image.Image, cache: list, xy: tuple[int, int], k: float) -> None:
    i = round(max(0.0, min(1.0, k)) * (len(cache) - 1))
    if i:
        rgb, a = cache[i]
        frame.paste(rgb, xy, a)


def encode(shots: list[dict], out: Path, logo: bool = True) -> float:
    """shots: [{stage, keys: [(t, rect)], dur, eased, layers: [(RGBA, (x, y), t0, t1, fade_in, fade_out)]}].
    → длительность ролика, с."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("не найден ffmpeg")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", os.getenv("REEL_PRESET", "veryfast"),
           "-crf", os.getenv("REEL_CRF", "22"), "-maxrate", "9M", "-bufsize", "18M", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    lg = _logo_layer() if logo else None
    total = 0.0
    try:
        for shot in shots:
            layers = [(_alpha_cache(ly[0]), *ly[1:6], ly[6] if len(ly) > 6 else FADE) for ly in shot.get("layers", [])]
            n = max(1, round(shot["dur"] * FPS))
            for i in range(n):
                t = i / FPS
                frame = shot["stage"].view(*_at(shot["keys"], t, shot.get("eased", True)))
                for cache, xy, t0, t1, fi, fo, fd in layers:
                    if t0 <= t < t1:
                        k = min(1.0, (t - t0) / fd if fi else 1.0, (t1 - t) / fd if fo else 1.0)
                        _paste(frame, cache, xy, k)
                if lg:
                    frame.paste(lg[0], lg[2], lg[1])
                proc.stdin.write(frame.tobytes())
            total += n / FPS
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="ignore")
        if proc.wait(timeout=600) != 0:
            raise RuntimeError(f"ffmpeg: {err[-300:]}")
    except BrokenPipeError:
        err = proc.stderr.read().decode(errors="ignore")
        raise RuntimeError(f"ffmpeg оборвался: {err[-300:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
    return total


def load(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return im


def padded_stage(im: Image.Image) -> tuple[Stage, tuple[int, int]]:
    """Картина целиком в кадре 9:16: вокруг — она же, размытая и затемнённая. → (сцена, смещение картины)."""
    w, h = im.size
    if h / w >= ASPECT:
        sw, sh = w, h
    else:
        sw, sh = w, round(w * ASPECT)
    # фон — растянутая, сильно размытая и тёмная копия
    small = ImageOps.fit(im, (max(1, sw // 12), max(1, sh // 12)))
    bg = small.filter(ImageFilter.GaussianBlur(6)).resize((sw, sh), Image.BILINEAR)
    bg = ImageEnhance.Brightness(bg).enhance(0.32)
    ox, oy = (sw - w) // 2, (sh - h) // 2
    bg.paste(im, (ox, oy))
    return Stage(bg), (ox, oy)


# ======================= подборка =======================

SEG = float(os.getenv("REEL_SEG", "3.6"))        # секунд на работу
TITLE_HOLD = 2.6                                 # сколько на первом кадре держится название подборки


def collection(title: str, items: list[dict], out: Path) -> float:
    """items: [{path, label, sub}] — label крупно (название работы), sub мельче (автор)."""
    shots = []
    top = gradient(True)
    for n, it in enumerate(items):
        im = load(Path(it["path"]))
        st = Stage(im)
        full = full_rect(st)
        fx, fy = (it.get("focus") or [0.5, 0.5])[:2]
        landscape = im.width / im.height > W / H * 1.25
        if landscape:   # широкая картина — камера медленно проезжает мимо главного, туда или обратно
            w = full[2]
            span = min(st.w * 0.16, (st.w - w) / 2)
            a = clamp_rect(st, fx * st.w - span / 2, st.h / 2, w)
            b = clamp_rect(st, fx * st.w + span / 2, st.h / 2, w)
            if n % 2:
                a, b = b, a
        else:           # вертикальная — медленный наезд к главному
            a = full
            b = clamp_rect(st, full[0] + (fx * st.w - full[0]) * 0.35, full[1] + (fy * st.h - full[1]) * 0.35,
                           full[2] / 1.1)
        dur = SEG + (TITLE_HOLD if n == 0 else 0)
        layers = [(top, (0, 0), 0, dur, n == 0, False)]
        lab = text_block([(it["label"].upper(), font(52), 0), (it.get("sub") or "", font(30, False), 14)])
        lx = (W - lab.width) // 2
        if n == 0:
            head = text_block([(title.upper(), font(92), 0)], width=920)
            layers.append((head, ((W - head.width) // 2, int(H * 0.40) - head.height // 2), 0, TITLE_HOLD, False, True))
            layers.append((lab, (lx, 150), TITLE_HOLD, dur, True, False))
        else:
            layers.append((lab, (lx, 150), 0, dur, False, False))
        shots.append({"stage": st, "keys": [(0, a), (dur, b)], "dur": dur, "eased": False, "layers": layers})
    # последний кадр чуть дольше, с плавным уходом в чёрный не заморачиваемся: Instagram зацикливает ролик
    return encode(shots, out)


# ======================= слова по одному =======================

WORD_SIZE = int(os.getenv("REEL_WORD_SIZE", "70"))
WORD_FADE = 0.09
WORD_STEP = 0.32                # без голоса: пауза между словами, с
CAPTION_Y = float(os.getenv("REEL_CAPTION_Y", "0.63"))   # центр подписи по высоте кадра


def _word_img(word: str, f) -> Image.Image:
    """Одно слово: белое, жирное, с мягкой тенью и тонкой тёмной обводкой — читается на любом фоне."""
    d = ImageDraw.Draw(Image.new("L", (10, 10)))
    asc, desc = f.getmetrics()
    w = int(d.textlength(word, font=f)) + 1
    pad = 24
    ink = Image.new("L", (w + 2 * pad, asc + desc + 2 * pad), 0)
    ImageDraw.Draw(ink).text((pad, pad), word, font=f, fill=255)
    edge = Image.new("L", ink.size, 0)
    ImageDraw.Draw(edge).text((pad, pad), word, font=f, fill=255, stroke_width=3, stroke_fill=255)
    sh = edge.filter(ImageFilter.GaussianBlur(9))
    im = Image.new("RGBA", ink.size, (0, 0, 0, 0))
    im.paste((0, 0, 0, 255), (0, 0), sh.point(lambda a: int(a * 0.62)))
    im.paste((0, 0, 0, 255), (0, 0), edge.point(lambda a: int(a * 0.35)))
    im.paste((255, 255, 255, 255), (0, 0), ink)
    return im


def _chunks(words: list[str], f, width: int, max_lines: int = 2) -> list[list[int]]:
    """Слова → группы, которые помещаются в две строки; группа заканчивается и на конце предложения."""
    out, cur = [], []
    for i, w in enumerate(words):
        test = cur + [i]
        if cur and len(_wrap(" ".join(words[k] for k in test), f, width, 99)) > max_lines:
            out.append(cur)
            test = [i]
        cur = test
        if re.search(r"[.!?…:;]$", w) and len(cur) >= 3:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def word_layers(text: str, starts: list[float], t_end: float, width: int = 920, size: int | None = None,
                fade: float = WORD_FADE) -> list[tuple]:
    """Слова появляются по одному в моменты starts; группа из двух строк держится до первого слова следующей."""
    f = font(size or WORD_SIZE, bold=True)
    words = str(text).split()
    if not words:
        return []
    starts = (list(starts) + [starts[-1] if starts else 0.0] * len(words))[:len(words)]
    d = ImageDraw.Draw(Image.new("L", (10, 10)))
    asc, desc = f.getmetrics()
    lh = asc + desc + 6
    groups = _chunks(words, f, width)
    layers = []
    for g, idx in enumerate(groups):
        t_off = starts[groups[g + 1][0]] - 0.02 if g + 1 < len(groups) else t_end
        lines = _balanced(" ".join(words[k] for k in idx), f, width)
        top = int(H * CAPTION_Y) - (lh * len(lines)) // 2
        k = 0
        for ln_no, line in enumerate(lines):
            parts = line.split()
            x0 = (W - d.textlength(line, font=f)) / 2
            for n, part in enumerate(parts):
                wi = idx[min(k, len(idx) - 1)]
                k += 1
                x = x0 + (d.textlength(" ".join(parts[:n]) + " ", font=f) if n else 0)
                img = _word_img(part, f)
                t0 = min(starts[wi], t_off - 0.05)
                layers.append((img, (int(x) - 24, top + ln_no * lh - 24), t0, t_off, fade > 0, False, max(fade, 0.01)))
    return layers


def _timed(text: str, start: float, voice: dict | None) -> tuple[list[float], float]:
    """→ (время каждого слова от начала ролика, длительность речи)."""
    n = len(str(text).split())
    if voice and voice.get("starts"):
        return [start + x for x in voice["starts"]], float(voice["dur"])
    return [start + i * WORD_STEP for i in range(n)], n * WORD_STEP + 0.3


def mux(video: Path, audio: list[tuple[float, str]], dur: float, out: Path) -> None:
    """Видео + фразы голоса в своих местах → out. Громкость выравнивается под соцсети."""
    exe = ffmpeg_exe()
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(video)]
    for _, path in audio:
        cmd += ["-i", path]
    parts = [f"[{n + 1}:a]aresample=44100,adelay={int(t * 1000)}:all=1[a{n}]" for n, (t, _) in enumerate(audio)]
    mix = "".join(f"[a{n}]" for n in range(len(audio)))
    graph = ";".join(parts) + f";{mix}amix=inputs={len(audio)}:normalize=0,apad,loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-ar", "44100", "-t", f"{dur:.2f}", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg (звук): {r.stderr[-300:]}")


# ======================= детали =======================

HOLD = float(os.getenv("REEL_HOLD", "3.4"))      # минимум на деталь, с (с голосом — сколько длится фраза)
MOVE = 1.3                                       # переезд между деталями


def _detail_rect(stage: Stage, off: tuple[int, int], size: tuple[int, int], box: list[float]):
    """box — доли картины [x0, y0, x1, y1] → прямоугольник камеры 9:16 вокруг детали с полями."""
    x0, y0, x1, y1 = [max(0.0, min(1.0, float(v))) for v in box]
    if x1 <= x0 or y1 <= y0:
        x0, y0, x1, y1 = 0.3, 0.3, 0.7, 0.7
    pw, ph = size
    cx, cy = off[0] + (x0 + x1) / 2 * pw, off[1] + (y0 + y1) / 2 * ph
    bw, bh = (x1 - x0) * pw * 1.25, (y1 - y0) * ph * 1.25
    w = max(bw, bh / ASPECT, pw * 0.16)          # не ближе, чем шестая часть ширины картины: иначе мыло
    if w <= pw and w * ASPECT <= ph:             # помещается в картину — не заезжаем на тёмные поля
        h = w * ASPECT
        cx = min(max(cx, off[0] + w / 2), off[0] + pw - w / 2)
        cy = min(max(cy, off[1] + h / 2), off[1] + ph - h / 2)
    return clamp_rect(stage, cx, cy, w)


HOOK_SIZE = int(os.getenv("REEL_HOOK_SIZE", "84"))
PAUSE_AFTER_HOOK = 0.75
PAUSE_BEFORE_CLIMAX = 0.9


def story(image: Path, beats: list[dict], end_lines: list[str], out: Path, voices: list | None = None) -> float:
    """Рилс по сюжету: beats — [{kind: hook|context|reveal|climax|final, text, box}] по порядку,
    box — доли картины [x0, y0, x1, y1] или None (вся картина). voices — tts.speak() на каждую фразу или None.

    Хук: рилс открывается сразу на крупной детали, слова крупнее и без проявления, после — пауза.
    Перед кульминацией — пауза, после неё кадр держится чуть дольше. Потом финальная фраза и титр."""
    if isinstance(voices, dict) and voices.get("one_take"):
        return _story_one_take(image, beats, end_lines, out, voices)
    vs = list(voices or []) + [None] * len(beats)
    im = load(image)
    st, off = padded_stage(im)
    full = full_rect(st)
    keys, layers, audio = [], [], []
    t = 0.0
    for n, (b, v) in enumerate(zip(beats, vs)):
        kind, text = b.get("kind"), b.get("text") or ""
        target = _detail_rect(st, off, im.size, b["box"]) if b.get("box") else full
        if n == 0:
            s0 = 0.12 if kind == "hook" else 0.4
            keys.append((0.0, target))
            arrive = 0.0
        else:
            arrive = t + MOVE
            keys.append((arrive, target))
            s0 = arrive - 0.25 + (PAUSE_BEFORE_CLIMAX if kind == "climax" else 0.0)
        starts, speech = _timed(text, s0, v)
        tail = PAUSE_AFTER_HOOK if kind == "hook" else 1.0 if kind == "climax" else 0.55
        end = max(arrive + (2.2 if kind == "hook" else 2.8), s0 + speech + tail)
        if target is full:
            drift = (full[0], full[1], full[2] / 1.05)
        else:
            drift = clamp_rect(st, target[0], target[1] - target[2] * 0.03, target[2] / 1.05)
        keys.append((end, drift))
        if kind == "hook":
            layers += word_layers(text, starts, end - 0.05, size=HOOK_SIZE, fade=0.0)
        else:
            layers += word_layers(text, starts, end - 0.05)
        if v:
            audio.append((s0, v["audio"]))
        t = end
    # титр: вся картина, название, автор, музей
    fin = 3.0
    keys += [(t + MOVE + 0.2, full), (t + MOVE + 0.2 + fin, full)]
    endb = text_block([(end_lines[0], font(58, bold=True), 0)]
                      + [(ln, font(36, False), 16) for ln in end_lines[1:] if ln], width=900)
    layers.append((gradient(False, 900, 0.7), (0, H - 900), t + MOVE, t + MOVE + 0.2 + fin, True, False))
    layers.append((endb, ((W - endb.width) // 2, int(H * 0.70) - endb.height // 2), t + MOVE + 0.3,
                   t + MOVE + 0.2 + fin, True, False))
    dur = t + MOVE + 0.2 + fin
    shots = [{"stage": st, "keys": keys, "dur": dur, "layers": layers}]
    if not audio:
        return encode(shots, out)
    silent = out.with_name(out.stem + "_silent.mp4")
    total = encode(shots, silent)
    try:
        mux(silent, audio, total, out)
    finally:
        silent.unlink(missing_ok=True)
    return total


def _end_card(keys: list, layers: list, full, t: float) -> float:
    """Титр после рассказа: камера на всю картину, название, автор, музей. → длительность ролика."""
    fin = 3.0
    keys += [(t + MOVE + 0.2, full), (t + MOVE + 0.2 + fin, full)]
    return t + MOVE + 0.2 + fin


def _story_one_take(image: Path, beats: list[dict], end_lines: list[str], out: Path, take: dict) -> float:
    """Весь рассказ — один дубль голоса; камера и слова подстраиваются под него: к каждой детали камера
    приезжает к началу её фразы, слова идут вместе с голосом, паузы — те, что сделал сам голос."""
    im = load(image)
    st, off = padded_stage(im)
    full = full_rect(st)
    tb = list(take.get("beats") or []) + [{"start": None, "starts": []}] * len(beats)
    total = float(take.get("dur") or 0)
    starts = [b.get("start") for b in tb[:len(beats)]]
    # время начала каждой части; если чего-то нет — ставим между соседями
    for i in range(len(starts)):
        if starts[i] is None:
            starts[i] = (starts[i - 1] + 2.5) if i else 0.0
    keys, layers = [], []
    prev_arrive = 0.0
    for n, b in enumerate(beats):
        target = _detail_rect(st, off, im.size, b["box"]) if b.get("box") else full
        if n == 0:
            keys.append((0.0, target))
            arrive = 0.0
        else:
            arrive = max(starts[n] - 0.1, prev_arrive + 0.8)
            move_from = max(arrive - MOVE, prev_arrive + 0.5)
            prev_target = keys[-1][1]
            keys.append((move_from, prev_target))
            keys.append((arrive, target))
        nxt = starts[n + 1] if n + 1 < len(beats) else total + 0.4
        hold_end = max(arrive + 0.6, nxt - MOVE)
        drift = (full[0], full[1], full[2] / 1.04) if target is full else \
            clamp_rect(st, target[0], target[1] - target[2] * 0.02, target[2] / 1.04)
        keys.append((hold_end, drift))
        words = tb[n].get("starts") or [starts[n] + i * WORD_STEP for i in range(len(b["text"].split()))]
        layers += word_layers(b["text"], words, nxt - 0.05, size=HOOK_SIZE if b.get("kind") == "hook" else None,
                              fade=0.0 if b.get("kind") == "hook" else WORD_FADE)
        prev_arrive = arrive
    t = max(total, keys[-1][0]) + 0.2
    endb = text_block([(end_lines[0], font(58, bold=True), 0)]
                      + [(ln, font(36, False), 16) for ln in end_lines[1:] if ln], width=900)
    dur = _end_card(keys, layers, full, t)
    layers.append((gradient(False, 900, 0.7), (0, H - 900), t + MOVE, dur, True, False))
    layers.append((endb, ((W - endb.width) // 2, int(H * 0.70) - endb.height // 2), t + MOVE + 0.1, dur, True, False))
    keys.sort(key=lambda k: k[0])
    shots = [{"stage": st, "keys": keys, "dur": dur, "layers": layers}]
    silent = out.with_name(out.stem + "_silent.mp4")
    total_v = encode(shots, silent)
    try:
        mux(silent, [(0.0, take["audio"])], total_v, out)
    finally:
        silent.unlink(missing_ok=True)
    return total_v


def cover(video_frame_src: Path, dest: Path) -> Path:
    """Обложка для карточки в боте — первый кадр по центру картинки (превью, без текста)."""
    im = load(video_frame_src)
    ImageOps.fit(im, (540, 960)).save(dest, "JPEG", quality=88)
    return dest
