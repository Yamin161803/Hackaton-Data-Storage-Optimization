from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SAMPLES_DIR = ROOT_DIR / "samples"
OUT_DIR = ROOT_DIR / "out"
DEFAULT_CATALOG_PATH = OUT_DIR / "catalog.sqlite3"

PHASH_HASH_SIZE = 8
DUPLICATE_JACCARD_THRESHOLD = 0.9
