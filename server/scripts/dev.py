"""Local development server with generated demo media.

    python scripts/dev.py            # http://localhost:8600 (TLS off for the dev preview)

Data lives in server/.devdata and demo media in server/.devmedia (both git-ignored).
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.environ.setdefault("PADDOCK_DATA_DIR", str(HERE / ".devdata"))
os.environ.setdefault("PADDOCK_MEDIA_ROOTS", str(HERE / ".devmedia"))
os.environ.setdefault("PADDOCK_TLS", "0")
os.environ.setdefault("PADDOCK_DEV", "1")
os.environ.setdefault("PADDOCK_PORT", "8600")
os.environ.setdefault("PADDOCK_HOST", "127.0.0.1")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

media = Path(os.environ["PADDOCK_MEDIA_ROOTS"])
if not (media / "movies").exists():
    import media_fixtures

    print("Generating demo media…")
    media_fixtures.build(media)

from paddock.__main__ import main  # noqa: E402

main()
