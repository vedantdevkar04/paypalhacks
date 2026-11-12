"""Assemble the final video from the video kit (_kit/): your voice notes, the clips and slides, under 3:00."""
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

K = Path(__file__).resolve().parent / "_kit"
W = K / "_build"
W.mkdir(exist_ok=True)
FF = imageio_ffmpeg.get_ffmpeg_exe()
TARGET = 177.5  # seconds, leaves room under 3:00
VF = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x14182b,fps=30,format=yuv420p"


def ff(*a):
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *map(str, a)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.stderr[-1500:])


def dur(p):
    r = subprocess.run([FF, "-hide_banner", "-i", str(p), "-f", "null", "-"], capture_output=True, text=True)
    h, m, s = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


TRIM = ("silenceremove=start_periods=1:start_threshold=-40dB:start_silence=0.1,"
        "areverse,silenceremove=start_periods=1:start_threshold=-40dB:start_silence=0.15,areverse,"
        "silenceremove=stop_periods=-1:stop_threshold=-40dB:stop_duration=0.45:stop_silence=0.3")


def voice(name, tempo):
    out = W / f"v_{name}_{tempo:.3f}.wav"
    if not out.exists():
        ff("-i", K / "voice" / f"{name}.ogg", "-af", f"{TRIM},atempo={tempo},apad=pad_dur=0.25",
           "-ar", "48000", "-ac", "2", out)
    return out


# kit names were shifted: 10a = tracking, 10b = complaint, 12 = dispatch (with Bryntum), 11 = a dashboard retake (unused)
LINES = ["line_01", "line_02", "line_03", "line_04", "line_05", "line_06", "line_07", "line_08",
         "line_12", "line_10a", "line_10b", "line_13", "line_wa", "line_14", "line_15-17"]

act1 = K / "clips" / "00_act1_intro_with_voice.mp4"
budget = TARGET - dur(act1)
tempo = 1.12
while True:
    total = sum(dur(voice(n, tempo)) for n in LINES)
    print(f"tempo {tempo:.2f}: act2+3 voice {total:.1f}s (budget {budget:.1f})")
    if total <= budget or tempo >= 1.45:
        break
    tempo = round(tempo * total / budget + 0.005, 3)
V = {n: voice(n, tempo) for n in LINES}

seg_n = 0


def segment(picture, audio, start=None, length=None, still=False, mute_src=True):
    """One piece: picture (clip part or still) stretched/squeezed to the audio's length."""
    global seg_n
    seg_n += 1
    out = W / f"seg{seg_n:02d}.mp4"
    a = dur(audio)
    if still:
        ff("-loop", "1", "-i", picture, "-i", audio, "-t", f"{a:.3f}", "-vf", VF, "-c:v", "libx264", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-map", "0:v", "-map", "1:a", out)
    else:
        src_len = length if length is not None else dur(picture) - (start or 0)
        k = a / src_len  # <1 speeds the picture up
        pre = ["-ss", f"{start:.3f}"] if start else []
        ff(*pre, "-t", f"{src_len:.3f}", "-i", picture, "-i", audio,
           "-filter_complex", f"[0:v]setpts={k:.5f}*(PTS-STARTPTS),{VF}[v]", "-map", "[v]", "-map", "1:a",
           "-t", f"{a:.3f}", "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k", out)
    return out


def cat_audio(names, label):
    out = W / f"cat_{label}.wav"
    lst = W / f"cat_{label}.txt"
    lst.write_text("".join(f"file '{V[n].as_posix()}'\n" for n in names))
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out)
    return out


def split_audio(src, cuts, label):
    edges = [0.0, *cuts, dur(src)]
    outs = []
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        o = W / f"{label}_{i}.wav"
        ff("-i", src, "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-c", "copy", o)
        outs.append(o)
    return outs


def pauses(src, n):
    r = subprocess.run([FF, "-hide_banner", "-i", str(src), "-af", "silencedetect=noise=-40dB:d=0.2", "-f", "null", "-"],
                       capture_output=True, text=True)
    s = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    e = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    gaps = sorted(((b - a, (a + b) / 2) for a, b in zip(s, e)), reverse=True)[:n]
    return sorted(m for _, m in gaps)


C = K / "clips"
S = K / "slides"
parts = []
# Act 1, re-encoded to the same format
a1 = W / "act1.mp4"
ff("-i", act1, "-vf", VF, "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", a1)
parts.append(a1)
parts.append(segment(S / "01_title_card.png", V["line_01"], still=True))
parts.append(segment(C / "02_jacket_post.mp4", V["line_02"]))
parts.append(segment(C / "03_photo_match_BLURRED.mp4", V["line_03"]))
long = C / "04-09_haggle_trick_checkout_fakepay_paypal_paid.mp4"
for name, (a, b) in zip(["line_04", "line_05", "line_06", "line_07", "line_08"],
                        [(8, 22), (22, 30), (30, 38), (38, 59), (59, dur(long))]):
    parts.append(segment(long, V[name], start=a, length=b - a))
parts.append(segment(C / "10-11_dispatch_bryntum.mp4", cat_audio(["line_12", "line_10a"], "10_11")))
parts.append(segment(C / "12_complaint.mp4", V["line_10b"]))
l13 = V["line_13"]
h = dur(l13) / 2
x, y = split_audio(l13, [h], "l13")
parts.append(segment(C / "13a_dashboard_owner_needed.mp4", x))
parts.append(segment(C / "13b_dashboard_orders_market_prices.mp4", y))
parts.append(segment(S / "16_whatsapp.png", V["line_wa"], still=True))
parts.append(segment(S / "14_under_the_hood.png", V["line_14"], still=True))
l15 = V["line_15-17"]
L15 = dur(l15)
cuts = sorted(c for c in pauses(l15, 8) if 0.45 * L15 < c < L15 - 1.8)
cuts = [cuts[0], cuts[-1]] if len(cuts) >= 2 and cuts[-1] - cuts[0] > 2 else []
if len(cuts) < 2:
    L = dur(l15)
    cuts = [L * 0.5, L * 0.8]
print("line 15-17 cuts", cuts)
p15, p16, p17 = split_audio(l15, cuts, "l15")
parts.append(segment(S / "15_benchmark.png", p15, still=True))
parts.append(segment(act1, p16, start=11, length=10))
parts.append(segment(S / "17_end_card.png", p17, still=True))

lst = W / "final.txt"
lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
final = K.parent / "HiClose_demo_final.mp4"
ff("-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
   "-movflags", "+faststart", final)
print("wrote", final, f"{dur(final):.1f}s")
