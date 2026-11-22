"""生成应用图标 packaging/AppIcon.icns（用 AppKit 绘制，再用 iconutil 打包）。

    .venv/bin/python scripts/make_icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSCompositingOperationSourceOver,
                    NSGradient, NSGraphicsContext, NSPNGFileType)
from Foundation import NSMakeRect

OUT = Path(__file__).resolve().parent.parent / "packaging" / "AppIcon.icns"


def rgb(h: str, a: float = 1.0) -> NSColor:
    h = h.lstrip("#")
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)), a)


def draw(px: int) -> NSBitmapImageRep:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setCompositingOperation_(NSCompositingOperationSourceOver)
    s = px / 1024
    # macOS 图标网格：824×824 的圆角矩形，四周留白
    body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(100 * s, 100 * s, 824 * s, 824 * s), 185 * s, 185 * s)
    NSGradient.alloc().initWithStartingColor_endingColor_(rgb("#2A2F38"), rgb("#14171C")).drawInBezierPath_angle_(body, -90)
    # 看板图形：四块圆角矩形（与界面里的 logo 一致），其中一块用「执行中」的蓝色
    blocks = [(3, 3, 7, 9, "#ECEEF1"), (14, 3, 7, 5, "#ECEEF1"), (14, 12, 7, 9, "#5B93FF"), (3, 16, 7, 5, "#ECEEF1")]
    u, ox, oy = 26 * s, 200 * s, 200 * s   # 24 单位的图标放大到 624px
    for x, y, w, h, c in blocks:
        r = NSMakeRect(ox + x * u, oy + (24 - y - h) * u, w * u, h * u)
        rgb(c).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 1.6 * u, 1.6 * u).fill()
    NSGraphicsContext.restoreGraphicsState()
    return rep


def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "AppIcon.iconset"
    tmp.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            draw(px).representationUsingType_properties_(NSPNGFileType, None).writeToFile_atomically_(str(tmp / name), True)
    OUT.parent.mkdir(exist_ok=True)
    subprocess.run(["iconutil", "-c", "icns", str(tmp), "-o", str(OUT)], check=True)
    shutil.copy(tmp / "icon_512x512@2x.png", OUT.with_suffix(".png"))
    shutil.rmtree(tmp.parent)
    print(f"已生成 {OUT}")


if __name__ == "__main__":
    main()
