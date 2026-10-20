#!/usr/bin/env python3
"""Render the calabamagate.org teaser: text cards that push toward the viewer
and vanish in a quarter of a second. Frames are drawn with Pillow and piped
to ffmpeg. Edit CARDS and rerun.

    python3 teaser/make_teaser.py [-o out.mp4] [--height 2160] [--no-url]
        [--music FILE [--preset NAME] [--start SECONDS] [--bar SECONDS]]

--start is the position in the music file at video time 0. --bar snaps the
cards to a grid: card i appears at LEAD_IN + i * bar. --preset takes the
sync from PRESETS, e.g.:

    python3 teaser/make_teaser.py --height 2160 --music "02 East.flac" --preset east
"""
import argparse
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30  # W, H set from --height
R = 1.0   # output pixels per layout unit (layout is in 1080p units)
SS = 1.5  # supersampling of the pre-rendered cards
FONT = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"
WHITE = (245, 242, 250)
PURPLE = (130, 60, 220)
GREY = (170, 165, 180)
ACCENT = (190, 145, 255)  # album titles, marked _LIKE THIS_ in CARDS

LEAD_IN = 0.8   # black before the first card
GAP = 0.5       # black between cards
APPEAR = 0.10   # fade-in, "in one go"
EXIT = 0.25     # zoom toward the viewer and vanish
HIT_LEAD = 0.04  # a card appears this long before its musical hit

# Music sync. "hits" are the positions in the music file (seconds) where each
# card lands; "bar" puts card i at LEAD_IN + i * bar.
PRESETS = {
    # N.E.W.S., "East": one card on every second bass-drum hit (about 4.08 s),
    # the end card on the guitar riff. Hits measured in the FLAC.
    "east": {"start": 305.437,
             "hits": [306.045, 310.197, 314.299, 318.395, 322.489, 326.550, 330.607, 334.682]},
    # lotusflow3r.com "Loading Site" loop: 3.871 s cycle, drums enter on the first fact.
    "lotusflow3r": {"start": 38.01, "bar": 3.8713},
}

# (kind, text, seconds on screen)
CARDS = [
    ("intro", "DID YOU KNOW…", 2.8),
    ("fact", "THE 2022 REISSUE OF _COME_\nGETS PRINCE’S BIRTH YEAR WRONG\nON ITS COVER", 3.4),
    ("fact", "MOST TRACKS ON _ORIGINALS_\nARE POSTHUMOUS\n“FRANKENSTEIN” MIXES", 3.4),
    ("fact", "21 SECONDS OF SHEILA E.’S DRUM SOLO\nWERE CUT FROM THE\nUTRECHT 1987 SHOW", 3.6),
    ("fact", "PRINCE SPED UP\n“MONEY DON’T MATTER 2 NIGHT”.\nTHE REISSUE SLOWED IT DOWN", 3.6),
    ("fact", "THE NEW 12\" OF “GIRL” PLAYS TOO SLOW…\nUNTIL THE VERY LAST NOTE", 3.4),
    ("fact", "“CALABAMA” WAS RECORDED IN 2003.\nITS HORNS, IN 2026", 3.8),
    ("end", "CALABAMAGATE\nTHE ESTATE FILES\ncalabamagate.org", 4.5),
]


def px(v):
    """Layout units to pre-render pixels."""
    return v * SS * R


def font(size):
    return ImageFont.truetype(FONT, px(size))


def tracked_line(draw, xy, text, fnt, fill, tracking):
    """Draw text with extra letter spacing; _segments_ use ACCENT."""
    x, y = xy
    accent = False
    for ch in text:
        if ch == "_":
            accent = not accent
            continue
        draw.text((x, y), ch, font=fnt, fill=(ACCENT if accent else fill[:3]) + (255,))
        x += fnt.getlength(ch) + tracking


def tracked_width(text, fnt, tracking):
    text = text.replace("_", "")
    return sum(fnt.getlength(c) for c in text) + tracking * (len(text) - 1)


def layout(lines):
    """lines: [(text, size, colour, tracking)] -> RGBA image, centred block."""
    gap = 0.18
    metrics = []
    for text, size, colour, tracking in lines:
        f = font(size)
        w = tracked_width(text, f, px(tracking))
        asc, desc = f.getmetrics()
        metrics.append((text, f, colour, px(tracking), w, asc + desc, px(size)))
    total_h = sum(m[5] for m in metrics) + sum(m[6] * gap for m in metrics[1:])
    max_w = max(m[4] for m in metrics)
    pad = int(px(80))
    img = Image.new("RGBA", (int(max_w) + 2 * pad, int(total_h) + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = pad
    for i, (text, f, colour, tr, w, lh, sz) in enumerate(metrics):
        if i:
            y += sz * gap
        tracked_line(d, ((img.width - w) / 2, y), text, f, colour + (255,), tr)
        y += lh
    return img


def with_glow(img, radius=22, strength=0.9):
    alpha = img.getchannel("A")
    glow_a = alpha.filter(ImageFilter.GaussianBlur(px(radius))).point(lambda v: int(min(255, v * strength * 2)))
    glow = Image.new("RGBA", img.size, PURPLE + (0,))
    glow.putalpha(glow_a)
    return Image.alpha_composite(glow, img)


def fit_size(lines, base, max_w=1700):
    f = font(base)
    widest = max(f.getlength(l.replace("_", "")) for l in lines) / px(1)
    return min(base, base * max_w / widest)


def render_card(kind, text):
    lines = text.split("\n")
    if kind == "intro":
        img = layout([(lines[0], 190, WHITE, 6)])
        return with_glow(img, 30, 1.0)
    if kind == "end":
        styles = [(250, WHITE, 7), (132, ACCENT, 16), (84, (205, 200, 215), 3)]
        img = layout([(l,) + s for l, s in zip(lines, styles)])
        return with_glow(img, 26, 0.6)
    size = fit_size(lines, 150)
    img = layout([(l, size, WHITE, 1) for l in lines])
    return with_glow(img, 18, 0.7)


def ease_out(u):
    return 1 - (1 - u) ** 3


def card_state(kind, t, dur):
    """Scale and opacity of a card t seconds after it appears."""
    s0, s1 = 0.94, 1.06
    hold_end = dur - (0.6 if kind == "end" else EXIT)
    if t < APPEAR:
        u = t / APPEAR
        return 0.90 + (s0 - 0.90) * ease_out(u), u
    if t < hold_end:
        u = (t - APPEAR) / (hold_end - APPEAR)
        return s0 + (s1 - s0) * u, 1.0
    u = (t - hold_end) / (dur - hold_end)
    if kind == "end":
        return s1 + 0.02 * u, 1 - u
    return s1 * (1 + 1.1 * u * u), (1 - u) ** 1.5


def main():
    global W, H, R
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", type=Path, default=Path(__file__).with_name("teaser.mp4"))
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--music", type=Path)
    ap.add_argument("--preset", choices=PRESETS)
    ap.add_argument("--start", type=float)
    ap.add_argument("--fade-out", type=float, default=2.0)
    ap.add_argument("--bar", type=float)
    ap.add_argument("--no-url", action="store_true", help="drop the URL from the end card")
    args = ap.parse_args()
    if args.no_url:
        kind, text, dur = CARDS[-1]
        CARDS[-1] = (kind, text.rsplit("\n", 1)[0], dur)
    R = args.height / 1080
    W, H = round(1920 * R), args.height
    preset = PRESETS.get(args.preset, {})
    start = args.start if args.start is not None else preset.get("start", 0.0)
    bar = args.bar or preset.get("bar")
    hits = None if args.bar else preset.get("hits")

    if hits:
        assert len(hits) == len(CARDS)
        starts = [h - start - HIT_LEAD for h in hits]
    elif bar:
        starts = [LEAD_IN + i * bar for i in range(len(CARDS))]
    else:
        starts, t = [], LEAD_IN
        for _, _, dur in CARDS:
            starts.append(t)
            t += dur + GAP
    timeline = []
    for i, (kind, text, dur) in enumerate(CARDS):
        if (hits or bar) and kind != "end":
            dur = starts[i + 1] - GAP - starts[i]
        timeline.append((starts[i], dur, kind, render_card(kind, text)))
    total = starts[-1] + timeline[-1][1] + 0.5
    n_frames = int(total * FPS)
    total = n_frames / FPS

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    if args.music:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{total:.3f}", "-i", str(args.music),
                "-map", "0:v:0", "-map", "1:a:0",
                "-af", f"afade=t=in:d=0.05,afade=t=out:st={total - args.fade_out:.3f}:d={args.fade_out}",
                "-c:a", "aac", "-b:a", "320k"]
    cmd += ["-vf", "noise=alls=3:allf=t,vignette=PI/5",
            "-c:v", "libx264", "-crf", "21", "-preset", "slow", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(args.output)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    for i in range(n_frames):
        now = i / FPS
        frame = Image.new("RGB", (W, H), (0, 0, 0))
        for start, dur, kind, card in timeline:
            dt = now - start
            if not 0 <= dt < dur:
                continue
            scale, alpha = card_state(kind, dt, dur)
            # faint purple flash when a card lands
            if dt < 0.2:
                k = 1 - dt / 0.2
                frame = Image.new("RGB", (W, H), tuple(int(c * 0.10 * k) for c in PURPLE))
            f = scale / SS
            sw, sh = max(1, int(card.width * f)), max(1, int(card.height * f))
            img = card.resize((sw, sh), Image.BICUBIC)
            if alpha < 1:
                img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
            frame.paste(img, ((W - sw) // 2, (H - sh) // 2), img)
        ff.stdin.write(frame.tobytes())
    ff.stdin.close()
    ff.wait()
    print(f"{args.output} ({W}x{H}, {total:.2f} s)")


if __name__ == "__main__":
    main()
