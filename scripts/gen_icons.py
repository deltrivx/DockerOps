#!/usr/bin/env python3
"""Generate DockerOps brand icons (PNG) without external deps."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path: Path, rgba: list[list[tuple[int, int, int, int]]]) -> None:
    h = len(rgba)
    w = len(rgba[0])
    raw = bytearray()
    for row in rgba:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(v: int) -> int:
    return 0 if v < 0 else 255 if v > 255 else v


def gen(size: int) -> list[list[tuple[int, int, int, int]]]:
    """Cyber-dark rounded tile with cyan→purple hex mark + DO monogram vibe."""
    px: list[list[tuple[int, int, int, int]]] = [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]
    cx = cy = (size - 1) / 2.0
    r_outer = size * 0.46
    r_inner = size * 0.40
    corner = size * 0.18

    def in_rounded_rect(x: float, y: float, half: float, rad: float) -> bool:
        ax, ay = abs(x - cx), abs(y - cy)
        if ax <= half - rad and ay <= half:
            return True
        if ay <= half - rad and ax <= half:
            return True
        dx = max(ax - (half - rad), 0)
        dy = max(ay - (half - rad), 0)
        return dx * dx + dy * dy <= rad * rad

    half = r_outer
    for y in range(size):
        for x in range(size):
            # soft outer glow
            dx, dy = x - cx, y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            glow = max(0.0, 1.0 - dist / (size * 0.55))
            glow = glow ** 2 * 40

            if not in_rounded_rect(x, y, half, corner):
                if glow > 1:
                    px[y][x] = (clamp(int(glow * 0.4)), clamp(int(glow * 0.7)), clamp(int(glow)), clamp(int(glow * 2.5)))
                continue

            # panel fill gradient
            t = (x + y) / (2 * size)
            r = int(lerp(12, 28, t))
            g = int(lerp(16, 24, t))
            b = int(lerp(32, 48, t))
            a = 255

            # border ring
            edge = in_rounded_rect(x, y, half, corner) and not in_rounded_rect(x, y, r_inner, corner * 0.9)
            if edge:
                et = x / size
                r = int(lerp(0, 108, et))
                g = int(lerp(243, 92, et))
                b = int(lerp(255, 231, et))

            # hexagon mark (container)
            hx, hy = (x - cx) / size, (y - cy) / size
            # hex distance
            ax, ay = abs(hx), abs(hy)
            hex_d = max(ax * 0.866 + ay * 0.5, ay)
            if 0.12 < hex_d < 0.22:
                et = (hy + 0.5)
                r = int(lerp(0, 167, et))
                g = int(lerp(243, 139, et))
                b = int(lerp(255, 250, et))
            elif hex_d <= 0.12:
                # inner core
                r, g, b = 18, 22, 36

            # three container "blocks" inside
            for bx, by, bw, bh in (
                (-0.08, -0.06, 0.07, 0.05),
                (0.01, -0.06, 0.07, 0.05),
                (-0.035, 0.02, 0.07, 0.05),
            ):
                if abs(hx - bx) < bw and abs(hy - by) < bh:
                    r, g, b = 0, 243, 255

            # accent dot
            if (hx - 0.14) ** 2 + (hy + 0.14) ** 2 < 0.004:
                r, g, b = 251, 63, 98

            px[y][x] = (clamp(r), clamp(g), clamp(b), a)

    return px


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    rgba256 = gen(256)
    rgba128 = gen(128)
    rgba64 = gen(64)

    write_png(root / "unraid" / "icon.png", rgba256)
    write_png(root / "fnos" / "fpk" / "ICON_256.PNG", rgba256)
    write_png(root / "fnos" / "fpk" / "ICON.PNG", rgba128)
    write_png(root / "fnos" / "fpk" / "ui" / "images" / "icon_256.png", rgba256)
    write_png(root / "fnos" / "fpk" / "ui" / "images" / "icon_64.png", rgba64)
    print("icons written")
    for p in [
        root / "unraid" / "icon.png",
        root / "fnos" / "fpk" / "ICON_256.PNG",
        root / "fnos" / "fpk" / "ICON.PNG",
        root / "fnos" / "fpk" / "ui" / "images" / "icon_256.png",
        root / "fnos" / "fpk" / "ui" / "images" / "icon_64.png",
    ]:
        print(p.relative_to(root), p.stat().st_size)


if __name__ == "__main__":
    main()
