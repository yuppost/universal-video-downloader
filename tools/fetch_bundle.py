"""Build helper: download FFmpeg and Deno once into ./ffmpeg and ./deno so build.bat can bundle them."""
import sys, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import dependency_service as dep  # noqa: E402


def prog(pct, msg, _speed):
    print(f"  {msg}" + (f" {pct:.0f}%" if pct is not None else ""), flush=True)


def main() -> int:
    ev = threading.Event()
    ok = True
    if not (ROOT / "ffmpeg" / "ffmpeg.exe").is_file() and not (ROOT / "ffmpeg" / "bin" / "ffmpeg.exe").is_file():
        print("Downloading FFmpeg for the bundle...")
        try:
            dep.install_ffmpeg(prog, ev, dest_dir=ROOT / "ffmpeg")
        except Exception as exc:
            print("FFmpeg download failed:", exc); ok = False
    if not (ROOT / "deno" / "deno.exe").is_file():
        print("Downloading Deno for the bundle...")
        try:
            dep.install_deno(prog, ev, dest_dir=ROOT / "deno")
        except Exception as exc:
            print("Deno download failed:", exc); ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
