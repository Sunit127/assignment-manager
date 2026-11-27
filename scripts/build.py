#!/usr/bin/env python3
"""
Build distributables into ./dist

  dist/assignment_manager.py         single-file app (UI embedded)
  dist/Assignment Manager.app        macOS app bundle (with icon)
  dist/AssignmentManager-macOS.zip   zipped app bundle for sharing
  dist/assignment-manager.mcpb       Claude Desktop extension

Usage:  python3 scripts/build.py
"""
import os
import shutil
import stat
import subprocess
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
APP_SRC = os.path.join(ROOT, "app", "assignment_manager.py")
UI_SRC = os.path.join(ROOT, "app", "ui", "index.html")
VERSION = "3.0.0"

LAUNCHER = r"""#!/bin/bash
# Assignment Manager launcher: finds Python 3 and starts the app.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
DATA="$HOME/.assignment_manager"; mkdir -p "$DATA"
export PATH="/Library/Frameworks/Python.framework/Versions/Current/bin:$HOME/.homebrew/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ -x "$DATA/venv/bin/python" ] && "$DATA/venv/bin/python" -c "import webview" >/dev/null 2>&1; then
  exec "$DATA/venv/bin/python" "$RES/assignment_manager.py" >>"$DATA/launcher.log" 2>&1
fi
for p in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 "$HOME/.homebrew/bin/python3" \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sqlite3, venv" >/dev/null 2>&1; then
    exec "$p" "$RES/assignment_manager.py" >>"$DATA/launcher.log" 2>&1
  fi
done
osascript -e 'display dialog "Assignment Manager needs Python 3.\n\nInstall it from python.org, then open the app again." buttons {"Open python.org","Cancel"} default button 1 with title "Assignment Manager"' \
  -e 'if button returned of result is "Open python.org" then open location "https://www.python.org/downloads/macos/"'
exit 1
"""

PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Assignment Manager</string>
  <key>CFBundleDisplayName</key><string>Assignment Manager</string>
  <key>CFBundleIdentifier</key><string>com.sunit.assignmentmanager</string>
  <key>CFBundleVersion</key><string>{VERSION}</string>
  <key>CFBundleShortVersionString</key><string>{VERSION}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
"""


def build_single_file():
    with open(APP_SRC, encoding="utf-8") as fh:
        src = fh.read()
    with open(UI_SRC, encoding="utf-8") as fh:
        ui = fh.read()
    marker = "EMBEDDED_UI = None  # filled in by scripts/build.py for the single-file build"
    assert marker in src, "embed marker missing"
    out = src.replace(marker, "EMBEDDED_UI = " + repr(ui))
    path = os.path.join(DIST, "assignment_manager.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    return path


def make_icon(dest_icns):
    """Gradient rounded-square icon with a sparkle, rendered with Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return False
    S = 1024
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    px = grad.load()
    a, b = (139, 92, 246), (6, 182, 212)
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([90, 90, S - 90, S - 90], radius=200, fill=255)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([100, 120, S - 80, S - 60], radius=200, fill=(20, 10, 60, 140))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(28)))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # glossy highlight
    gloss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse([-200, -520, S + 200, 420], fill=(255, 255, 255, 46))
    gm = Image.new("L", (S, S), 0)
    gm.paste(mask, (0, 0))
    img = Image.composite(Image.alpha_composite(img, gloss), img, gm)
    d = ImageDraw.Draw(img)
    # four-point sparkle
    c = S // 2
    pts = []
    import math
    for i in range(16):
        ang = math.pi / 2 - i * math.pi / 8
        r = 300 if i % 4 == 0 else (70 if i % 2 else 110)
        pts.append((c + r * math.cos(ang), c - r * math.sin(ang) + 10))
    d.polygon(pts, fill=(255, 255, 255, 255))
    d.ellipse([c + 150, c - 250, c + 230, c - 170], fill=(255, 255, 255, 220))
    img.save(dest_icns, format="ICNS")
    img.resize((512, 512)).save(os.path.join(DIST, "icon.png"))
    return True


def build_mac_app(single):
    app = os.path.join(DIST, "Assignment Manager.app")
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    os.makedirs(os.path.join(app, "Contents", "Resources"))
    with open(os.path.join(app, "Contents", "Info.plist"), "w") as fh:
        fh.write(PLIST)
    launcher = os.path.join(app, "Contents", "MacOS", "launcher")
    with open(launcher, "w") as fh:
        fh.write(LAUNCHER)
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copy(single, os.path.join(app, "Contents", "Resources", "assignment_manager.py"))
    make_icon(os.path.join(app, "Contents", "Resources", "AppIcon.icns"))
    zpath = os.path.join(DIST, "AssignmentManager-macOS.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(app):
            for f in files:
                full = os.path.join(base, f)
                info = zipfile.ZipInfo.from_file(full, os.path.relpath(full, DIST))
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as fh:
                    z.writestr(info, fh.read())
    return zpath


def build_extension():
    src = os.path.join(ROOT, "claude-extension")
    out = os.path.join(DIST, "assignment-manager.mcpb")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(src, "manifest.json"), "manifest.json")
        z.write(os.path.join(src, "server", "main.py"), "server/main.py")
    return out


if __name__ == "__main__":
    shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(DIST)
    single = build_single_file()
    print("built", single)
    print("built", build_mac_app(single))
    print("built", build_extension())
