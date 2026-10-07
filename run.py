"""Entry point: python run.py  (works without installing the package)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qqbridge.server import main  # noqa: E402

if __name__ == "__main__":
    main()
