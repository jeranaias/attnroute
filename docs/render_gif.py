#!/usr/bin/env python3
"""
Render terminal-style GIFs from Python demo scripts.

Captures ANSI-colored output from demo scripts and renders each
"scene" as a frame in an animated GIF with a dark terminal background.
"""
import io
import os
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Terminal theme ──────────────────────────────────────────
BG_COLOR = (30, 30, 46)       # Dark purple-gray (catppuccin mocha)
FG_COLOR = (205, 214, 244)    # Light text
TITLE_BG = (49, 50, 68)      # Title bar
BORDER = (69, 71, 90)

ANSI_COLORS = {
    "30": (69, 71, 90),       # black/dark
    "31": (243, 139, 168),    # red
    "32": (166, 227, 161),    # green
    "33": (249, 226, 175),    # yellow
    "34": (137, 180, 250),    # blue
    "35": (245, 194, 231),    # magenta
    "36": (148, 226, 213),    # cyan
    "37": (205, 214, 244),    # white
}

# ── Font setup ──────────────────────────────────────────────
def get_font(size=16):
    """Get a monospace font, with fallbacks."""
    font_paths = [
        "C:/Windows/Fonts/consola.ttf",     # Consolas (Windows)
        "C:/Windows/Fonts/cascadiacode.ttf", # Cascadia Code
        "C:/Windows/Fonts/lucon.ttf",        # Lucida Console
        "C:/Windows/Fonts/cour.ttf",         # Courier New
    ]
    for p in font_paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def get_bold_font(size=16):
    """Get bold variant."""
    bold_paths = [
        "C:/Windows/Fonts/consolab.ttf",
        "C:/Windows/Fonts/cascadiacodeb.ttf",
        "C:/Windows/Fonts/courbd.ttf",
    ]
    for p in bold_paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return get_font(size)


# ── ANSI parser ─────────────────────────────────────────────
def parse_ansi(text):
    """Parse ANSI escape codes into (text, color, bold) segments."""
    segments = []
    current_color = FG_COLOR
    current_bold = False
    current_dim = False

    parts = re.split(r'(\033\[[0-9;]*m)', text)
    for part in parts:
        if part.startswith('\033['):
            codes = part[2:-1].split(';')
            for code in codes:
                if code == '0':
                    current_color = FG_COLOR
                    current_bold = False
                    current_dim = False
                elif code == '1':
                    current_bold = True
                elif code == '2':
                    current_dim = True
                elif code in ANSI_COLORS:
                    current_color = ANSI_COLORS[code]
        elif part:
            color = current_color
            if current_dim:
                color = tuple(c // 2 for c in color)
            segments.append((part, color, current_bold))

    return segments


# ── Frame renderer ──────────────────────────────────────────
def render_frame(lines, width=1400, title="attnroute", font_size=22):
    """Render a list of text lines into a terminal-style PIL image."""
    font = get_font(font_size)
    bold_font = get_bold_font(font_size)

    line_height = font_size + 8
    padding_x = 32
    padding_y = 24
    title_height = 52
    content_height = len(lines) * line_height + padding_y * 2
    height = title_height + content_height + 16  # bottom margin

    img = Image.new('RGB', (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rounded_rectangle(
        [0, 0, width, title_height],
        radius=12, fill=TITLE_BG
    )

    # Traffic light dots
    for i, color in enumerate([(243, 139, 168), (249, 226, 175), (166, 227, 161)]):
        draw.ellipse([20 + i * 32, 15, 38 + i * 32, 33], fill=color)

    # Title text
    title_font = get_font(18)
    tw = draw.textlength(title, font=title_font)
    draw.text(((width - tw) / 2, 14), title, fill=(150, 150, 170), font=title_font)

    # Content area border
    draw.rounded_rectangle(
        [0, title_height, width - 1, height - 1],
        radius=0, fill=BG_COLOR, outline=BORDER
    )

    # Render lines
    y = title_height + padding_y
    for line in lines:
        segments = parse_ansi(line)
        x = padding_x
        for text, color, bold in segments:
            f = bold_font if bold else font
            draw.text((x, y), text, fill=color, font=f)
            x += draw.textlength(text, font=f)
        y += line_height

    return img


# ── GIF assembly ────────────────────────────────────────────
def capture_output(script_path):
    """Run a demo script and capture its ANSI output."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Force color output
    env["FORCE_COLOR"] = "1"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, env=env, timeout=30,
        encoding='utf-8', errors='replace',
    )
    return result.stdout


def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return re.sub(r'\033\[[0-9;]*m', '', text)


def split_into_scenes(output):
    """Split output into scenes (separated by the ─ header lines)."""
    lines = output.split('\n')
    scenes = []
    current_scene = []

    for line in lines:
        # Strip ANSI codes before checking for separator
        plain = strip_ansi(line)
        if '─' * 20 in plain and current_scene:
            # Finish previous scene
            scenes.append(current_scene)
            current_scene = []
        current_scene.append(line)

    if current_scene:
        scenes.append(current_scene)

    # If no scenes detected, treat whole output as one scene
    if not scenes:
        scenes = [lines]

    return scenes


def make_gif(script_path, output_path, title="attnroute", frame_duration=3000):
    """Generate an animated GIF from a demo script."""
    print(f"  Capturing: {script_path.name}")
    output = capture_output(script_path)

    if not output.strip():
        print(f"  ERROR: No output from {script_path}")
        return

    # Split into scenes for animation
    scenes = split_into_scenes(output)

    # Also make a full-output single frame
    all_lines = output.split('\n')
    # Remove empty trailing lines
    while all_lines and not all_lines[-1].strip():
        all_lines.pop()

    frames = []

    # Progressive reveal: show more lines with each frame
    # Start with first scene, then add each scene
    accumulated = []
    for scene in scenes:
        accumulated.extend(scene)
        # Remove trailing empty lines
        clean = list(accumulated)
        while clean and not clean[-1].strip():
            clean.pop()
        if clean:
            frame = render_frame(clean, title=title)
            frames.append(frame)

    if not frames:
        print(f"  ERROR: No frames generated for {script_path}")
        return

    # Add a longer pause on the final frame
    # Save as animated GIF
    if len(frames) == 1:
        frames[0].save(str(output_path))
    else:
        durations = [frame_duration] * len(frames)
        durations[-1] = frame_duration * 2  # Pause longer on final frame
        frames[0].save(
            str(output_path),
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
        )

    size_kb = output_path.stat().st_size / 1024
    print(f"  Saved: {output_path} ({len(frames)} frames, {size_kb:.0f}KB)")


# ── Main ────────────────────────────────────────────────────
def main():
    docs_dir = Path(__file__).parent
    output_dir = docs_dir

    demos = [
        ("demo_hero.py",         "demo.gif",              "attnroute"),
        ("demo_verifyfirst.py",  "demo_verifyfirst.gif",  "VerifyFirst v2.1"),
        ("demo_loopbreaker.py",  "demo_loopbreaker.gif",  "LoopBreaker v3.0"),
        ("demo_burnrate.py",     "demo_burnrate.gif",     "BurnRate v1.0"),
        ("demo_contextguard.py", "demo_contextguard.gif", "ContextGuard v1.0"),
    ]

    print("Rendering GIFs...")
    for script_name, gif_name, title in demos:
        script_path = docs_dir / script_name
        output_path = output_dir / gif_name
        if script_path.exists():
            make_gif(script_path, output_path, title=title)
        else:
            print(f"  SKIP: {script_name} not found")

    print("\nDone! GIFs saved to docs/")


if __name__ == "__main__":
    main()
