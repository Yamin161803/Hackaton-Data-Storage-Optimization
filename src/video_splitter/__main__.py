"""CLI: python -m video_splitter input.mp4 [-o out_dir] [--fps N] [--image-format png] [--audio-format mp3]"""

import argparse
import sys
from pathlib import Path

from .splitter import AUDIO_FORMATS, IMAGE_FORMATS, split_video


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="video_splitter", description="Split a video into image frames and an audio file."
    )
    parser.add_argument("video", type=Path, help="input video, e.g. clip.mp4")
    parser.add_argument("-o", "--output", type=Path,
                        help="output folder (default: <video name>_split next to the video)")
    parser.add_argument("--fps", type=float, help="frames per second to extract (default: all frames)")
    parser.add_argument("--image-format", choices=IMAGE_FORMATS, default="jpg")
    parser.add_argument("--audio-format", choices=tuple(AUDIO_FORMATS), default="wav")
    args = parser.parse_args(argv)

    output = args.output or args.video.with_name(f"{args.video.stem}_split")
    try:
        result = split_video(args.video, output, args.fps, args.image_format, args.audio_format)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{result.frame_count} frames -> {result.frames_dir}")
    print(f"audio -> {result.audio_path}" if result.audio_path else "no audio track in video")
    return 0


if __name__ == "__main__":
    sys.exit(main())
