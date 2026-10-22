import json
import os
from pathlib import Path

PATH = Path(os.environ.get("BOOKSTORE_SETTINGS", Path(__file__).parent.parent / "settings.json"))
SETTINGS = json.loads(PATH.read_text())
