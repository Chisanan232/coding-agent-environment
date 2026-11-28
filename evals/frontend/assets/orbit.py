"""Render an original layered orbit, encode VP9, and measure its decoded loop seam."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess


WIDTH, HEIGHT, FRAMES = 96, 64, 8


def center(frame: int) -> tuple[float, float]:
    phase = 2 * math.pi * frame / FRAMES
    return 48 + 17 * math.cos(phase), 32 + 9 * math.sin(phase)


def illustration(frame: int) -> str:
    x, y = center(frame)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="64" viewBox="0 0 96 64">'
        '<title>Relay orbit</title><g id="ground"><rect width="96" height="64" fill="#20263a"/></g>'
        '<g id="track"><ellipse cx="48" cy="32" rx="17" ry="9" fill="none" stroke="#b7a1c8" stroke-width="2"/></g>'
        f'<g id="orbiter"><circle cx="{x:.3f}" cy="{y:.3f}" r="5" fill="#efb36e"/></g>'
        '<g id="foreground"><path d="M5 57H91" stroke="#90899c"/></g></svg>'
    )


def convert(tool: str, source: Path, target: Path) -> None:
    if Path(tool).name == 'sips':
        command = [tool, '-s', 'format', {'jpg': 'jpeg', 'bmp': 'bmp'}[target.suffix[1:]],
                   str(source), '--out', str(target)]
    else:
        output = 'BMP3:' + str(target) if target.suffix == '.bmp' else str(target)
        command = [tool, str(source), '-alpha', 'remove', '-type', 'TrueColor', output]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, timeout=30)


def centroid(bmp: Path) -> tuple[float, float]:
    data = bmp.read_bytes()
    if data[:2] != b'BM':
        raise ValueError('Expected a BMP image')
    offset = struct.unpack_from('<I', data, 10)[0]
    _, width, height, planes, bits, compression = struct.unpack_from('<IiiHHI', data, 14)
    if (width, abs(height), planes, bits, compression) != (WIDTH, HEIGHT, 1, 24, 0):
        raise ValueError('Expected a 96 by 64 pixel uncompressed 24-bit BMP')
    # Each 96-pixel row has 288 bytes and requires no padding.
    mask = []
    for y in range(HEIGHT):
        row = y if height < 0 else HEIGHT - 1 - y
        for x in range(WIDTH):
            pixel = offset + 3 * (row * WIDTH + x)
            blue, green, red = data[pixel:pixel + 3]
            if red > 170 and red - green > 25 and green - blue > 24:
                mask.append((x + .5, y + .5))
    if not 40 <= len(mask) <= 110:
        raise ValueError('The decoded amber orb has an unexpected area')
    return tuple(sum(axis) / len(mask) for axis in zip(*mask))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='A new private directory for the output')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='Local FFmpeg with MJPEG, VP9 and PNG support')
    parser.add_argument('--converter', choices=('sips', 'magick'), default='sips')
    args = parser.parse_args()
    encoder = shutil.which(args.ffmpeg)
    converter = shutil.which(args.converter)
    if not encoder or not converter:
        parser.error('Select locally installed FFmpeg and image conversion tools')
    out = args.output.resolve()
    out.mkdir(mode=0o700, exist_ok=False)
    jpegs = bytearray()
    for frame in range(FRAMES):
        svg = out / f'frame-{frame}.svg'
        svg.write_text(illustration(frame), encoding='utf-8')
        jpeg = svg.with_suffix('.jpg')
        convert(converter, svg, jpeg)
        jpegs.extend(jpeg.read_bytes())
    if illustration(0) != illustration(FRAMES):
        raise ValueError('Source poses differ at the cycle boundary')
    ffmpeg = [encoder, '-v', 'error', '-threads', '1']
    subprocess.run([*ffmpeg, '-f', 'image2pipe', '-framerate', '8', '-c:v', 'mjpeg', '-i', 'pipe:0',
                    '-an', '-c:v', 'libvpx-vp9', '-threads', '1', '-cpu-used', '8', '-deadline', 'realtime',
                    '-f', 'webm', 'orbit.webm'], cwd=out, input=jpegs, check=True, timeout=30)
    subprocess.run([*ffmpeg, '-c:v', 'libvpx-vp9', '-i', 'orbit.webm', '-c:v', 'png', '-f', 'image2',
                    'decoded-%02d.png'], cwd=out, check=True, timeout=30)
    frames = sorted(out.glob('decoded-*.png'))
    if len(frames) != FRAMES:
        raise ValueError(f'Expected eight decoded frames, got {len(frames)}')
    poses = []
    for image in frames:
        bitmap = image.with_suffix('.bmp')
        convert(converter, image, bitmap)
        poses.append(centroid(bitmap))
    errors = []
    for i, here in enumerate(poses):
        following = poses[(i + 1) % FRAMES]
        motion = tuple(b - a for a, b in zip(here, following))
        expected = tuple(b - a for a, b in zip(center(i), center(i + 1)))
        errors.append(math.dist(motion, expected))
    video = (out / 'orbit.webm').read_bytes()
    result = {'frames': len(frames), 'centroids': poses, 'seam_error_px': errors[-1],
              'max_step_error_px': max(errors), 'pass': max(errors) < 1,
              'video_bytes': len(video), 'jpeg_stream_bytes': len(jpegs),
              'sha256': hashlib.sha256(video).hexdigest()}
    print(json.dumps(result, indent=2))
    if not result['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
