"""Install requirements.txt again when it changed since the last run (stamp stored inside the virtual environment)."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
req = ROOT / "requirements.txt"
stamp = Path(sys.prefix) / "uvs_requirements.sha1"
digest = hashlib.sha1(req.read_bytes()).hexdigest()
try:
    if stamp.read_text().strip() == digest:
        sys.exit(0)
except OSError:
    pass
print("[update] Installing updated dependencies ...", flush=True)
rc = subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(req)])
if rc == 0:
    stamp.write_text(digest)
sys.exit(rc)
