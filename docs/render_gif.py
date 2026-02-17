#!/usr/bin/env python3
"""
Render terminal-style animated GIFs with typewriter effect.

Line-by-line reveal with character-by-character typing for user input.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Terminal theme (Catppuccin Mocha) ─────────────────────
BG_COLOR = (30, 30, 46)
FG_COLOR = (205, 214, 244)
TITLE_BG = (49, 50, 68)
BORDER = (69, 71, 90)
CURSOR_COLOR = (166, 227, 161)  # Green cursor

ANSI_COLORS = {
    "30": (69, 71, 90),
    "31": (243, 139, 168),
    "32": (166, 227, 161),
    "33": (249, 226, 175),
    "34": (137, 180, 250),
    "35": (245, 194, 231),
    "36": (148, 226, 213),
    "37": (205, 214, 244),
}


# ── Font setup ────────────────────────────────────────────
def get_font(size=16):
    for p in [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cascadiacode.ttf",
        "C:/Windows/Fonts/lucon.ttf",
        "C:/Windows/Fonts/cour.ttf",
    ]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def get_bold_font(size=16):
    for p in [
        "C:/Windows/Fonts/consolab.ttf",
        "C:/Windows/Fonts/cascadiacodeb.ttf",
        "C:/Windows/Fonts/courbd.ttf",
    ]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return get_font(size)


# ── ANSI helpers ──────────────────────────────────────────
def parse_ansi(text):
    """Parse ANSI escape codes into (text, color, bold) segments."""
    segments = []
    color = FG_COLOR
    bold = False
    dim = False

    for part in re.split(r'(\033\[[0-9;]*m)', text):
        if part.startswith('\033['):
            for code in part[2:-1].split(';'):
                if code == '0':
                    color, bold, dim = FG_COLOR, False, False
                elif code == '1':
                    bold = True
                elif code == '2':
                    dim = True
                elif code in ANSI_COLORS:
                    color = ANSI_COLORS[code]
        elif part:
            c = tuple(v // 2 for v in color) if dim else color
            segments.append((part, c, bold))
    return segments


def strip_ansi(text):
    return re.sub(r'\033\[[0-9;]*m', '', text)


def visible_len(ansi_str):
    """Count visible (non-ANSI) characters."""
    return len(strip_ansi(ansi_str))


def truncate_ansi(ansi_str, n):
    """Return first n visible characters, preserving ANSI codes."""
    result = []
    count = 0
    i = 0
    s = ansi_str
    while i < len(s) and count < n:
        if s[i] == '\033':
            j = s.index('m', i) + 1
            result.append(s[i:j])
            i = j
        else:
            result.append(s[i])
            count += 1
            i += 1
    result.append('\033[0m')
    return ''.join(result)


# ── Frame renderer ────────────────────────────────────────
def render_frame(lines, width=1400, title="attnroute", font_size=22,
                 min_height=0, cursor_pos=None):
    """Render lines into a terminal-style image.

    cursor_pos: if set, (line_index, x_pixel) to draw a block cursor.
    """
    font = get_font(font_size)
    bold_font = get_bold_font(font_size)

    line_height = font_size + 8
    padding_x = 32
    padding_y = 24
    title_height = 52
    content_height = len(lines) * line_height + padding_y * 2
    height = max(title_height + content_height + 16, min_height)

    img = Image.new('RGB', (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rounded_rectangle([0, 0, width, title_height], radius=12, fill=TITLE_BG)

    # Traffic lights
    for i, c in enumerate([(243, 139, 168), (249, 226, 175), (166, 227, 161)]):
        draw.ellipse([20 + i * 32, 15, 38 + i * 32, 33], fill=c)

    # Title text
    tf = get_font(18)
    tw = draw.textlength(title, font=tf)
    draw.text(((width - tw) / 2, 14), title, fill=(150, 150, 170), font=tf)

    # Content border
    draw.rounded_rectangle(
        [0, title_height, width - 1, height - 1],
        radius=0, fill=BG_COLOR, outline=BORDER
    )

    # Render lines
    y = title_height + padding_y
    for idx, line in enumerate(lines):
        segments = parse_ansi(line)
        x = padding_x
        for text, color, bold in segments:
            f = bold_font if bold else font
            draw.text((x, y), text, fill=color, font=f)
            x += draw.textlength(text, font=f)

        # Draw cursor on this line if requested
        if cursor_pos and cursor_pos[0] == idx:
            cx = cursor_pos[1]
            draw.rectangle(
                [cx, y, cx + font_size * 0.6, y + font_size],
                fill=CURSOR_COLOR
            )

        y += line_height

    return img


# ── Capture ───────────────────────────────────────────────
def capture_output(script_path):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["FORCE_COLOR"] = "1"
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, env=env, timeout=30,
        encoding='utf-8', errors='replace',
    )
    return result.stdout


# ── Typewriter GIF builder ────────────────────────────────
def is_typing_line(plain):
    """Lines starting with >> or $ get typewriter effect."""
    stripped = plain.lstrip()
    return stripped.startswith('>> ') or stripped.startswith('$ ')


def make_gif(script_path, output_path, title="attnroute"):
    """Build an animated GIF with typewriter effect."""
    print(f"  Capturing: {script_path.name}")
    output = capture_output(script_path)

    if not output.strip():
        print(f"  ERROR: No output from {script_path}")
        return

    all_lines = output.split('\n')
    # Clean trailing blanks
    while all_lines and not all_lines[-1].strip():
        all_lines.pop()
    # Clean leading blanks
    while all_lines and not all_lines[0].strip():
        all_lines.pop(0)

    if not all_lines:
        print(f"  ERROR: No content from {script_path}")
        return

    # Pre-calculate the target height (all lines visible)
    target_height = render_frame(all_lines, title=title).size[1]

    # Build frames: line-by-line reveal with typewriter for prompts
    frames = []
    durations = []
    visible = []

    # Initial empty frame (just the terminal chrome) — brief pause
    frames.append(render_frame([], title=title, min_height=target_height))
    durations.append(800)

    font = get_font(22)
    padding_x = 32

    for line in all_lines:
        plain = strip_ansi(line)

        if is_typing_line(plain):
            # Typewriter: reveal chars in chunks of 3
            total_chars = visible_len(line)
            chunk = 3
            for n in range(chunk, total_chars + 1, chunk):
                partial = truncate_ansi(line, n)
                # Calculate cursor x position
                partial_plain = strip_ansi(partial)
                cursor_x = padding_x + font.getlength(partial_plain)

                display = visible + [partial]
                frame = render_frame(
                    display, title=title, min_height=target_height,
                    cursor_pos=(len(visible), cursor_x)
                )
                frames.append(frame)
                durations.append(60)

            # Full line without cursor
            visible.append(line)
            frames.append(render_frame(
                visible, title=title, min_height=target_height
            ))
            durations.append(600)  # Pause after typing completes

        elif not plain.strip():
            # Blank line — tiny pause
            visible.append(line)
            frames.append(render_frame(
                visible, title=title, min_height=target_height
            ))
            durations.append(100)

        elif '─' * 10 in plain:
            # Separator line — skip rendering a separate frame,
            # it'll appear with the next line
            visible.append(line)

        else:
            # Regular output line — appears instantly
            visible.append(line)
            frames.append(render_frame(
                visible, title=title, min_height=target_height
            ))
            # Longer pause for "important" lines (colored/bold)
            if '\033[1m' in line and ('\033[31m' in line or '\033[32m' in line):
                durations.append(1200)  # Important result — linger
            else:
                durations.append(250)

    # Final frame: long hold so viewer can absorb
    durations[-1] = 4000

    # Save animated GIF
    if len(frames) == 1:
        frames[0].save(str(output_path))
    else:
        frames[0].save(
            str(output_path),
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
        )

    size_kb = output_path.stat().st_size / 1024
    total_ms = sum(durations)
    print(f"  Saved: {output_path} ({len(frames)} frames, {size_kb:.0f}KB, {total_ms/1000:.1f}s)")


# ── Main ──────────────────────────────────────────────────
def main():
    docs_dir = Path(__file__).parent
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
        output_path = docs_dir / gif_name
        if script_path.exists():
            make_gif(script_path, output_path, title=title)
        else:
            print(f"  SKIP: {script_name} not found")

    print("\nDone!")


if __name__ == "__main__":
    main()
