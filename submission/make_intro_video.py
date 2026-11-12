"""Turn the Act 1 voice-over into a finished intro clip: intro.html, timed to the audio, recorded in Chrome.

    python submission/make_intro_video.py path/to/act1-audio.m4a [out.mp4]

Finds the five longest pauses in the audio (between the six lines of Act 1), gives each scene the length
of its line, records intro.html at 1280x720 with Playwright (using the installed Chrome), and muxes the
audio on. Needs: pip install playwright imageio-ffmpeg
"""

import re
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
LINES = 6
# Rough word counts of the six lines, used only if the pauses can't be found.
WORDS = [22, 27, 24, 17, 25, 14]


def run(args: list[str]) -> str:
    done = subprocess.run([FFMPEG, "-hide_banner", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise SystemExit(done.stderr[-2000:])
    return done.stderr


def audio_length(audio: Path) -> float:
    out = run(["-i", str(audio), "-f", "null", "-"])
    h, m, s = re.findall(r"time=(\d+):(\d+):([\d.]+)", out)[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def scene_lengths(audio: Path, total: float) -> list[float]:
    """Cut points in the middle of the five longest pauses; the last scene runs a little past the voice."""
    out = run(["-i", str(audio), "-af", "silencedetect=noise=-32dB:d=0.3", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    gaps = [(e - s, (s + e) / 2) for s, e in zip(starts, ends) if 1.0 < s and e < total - 0.5]
    if len(gaps) >= LINES - 1:
        cuts = sorted(mid for _, mid in sorted(gaps, reverse=True)[: LINES - 1])
        print("pauses found at", ", ".join(f"{c:.1f}s" for c in cuts))
    else:
        print(f"only {len(gaps)} clear pauses; splitting by word count instead")
        acc, cuts = 0, []
        for w in WORDS[:-1]:
            acc += w
            cuts.append(total * acc / sum(WORDS))
    edges = [0.0, *cuts, total + 1.5]
    return [round(b - a, 2) for a, b in zip(edges, edges[1:])]


def record(lengths: list[float], workdir: Path) -> tuple[Path, float]:
    url = (HERE / "intro.html").as_uri() + "?rec=1&d=" + ",".join(map(str, lengths))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1,
                                      record_video_dir=str(workdir), record_video_size={"width": 1280, "height": 720})
        page = context.new_page()
        t0 = time.monotonic()
        page.goto(url, wait_until="load")
        lead = time.monotonic() - t0  # blank frames before the animation starts
        page.wait_for_timeout(int(sum(lengths) * 1000) + 800)
        video = Path(page.video.path())
        context.close()
        browser.close()
    return video, lead


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    audio = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else HERE / "act1_intro.mp4"
    total = audio_length(audio)
    lengths = scene_lengths(audio, total)
    print(f"audio {total:.1f}s; scene lengths {lengths}")
    workdir = out.parent / "_intro_recording"
    workdir.mkdir(exist_ok=True)
    video, lead = record(lengths, workdir)
    run(["-y", "-ss", f"{lead:.2f}", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a",
         "-t", f"{sum(lengths):.2f}", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
         "-c:a", "aac", "-b:a", "192k", "-af", "apad", str(out)])
    video.unlink(missing_ok=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
