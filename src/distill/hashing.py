import hashlib
from pathlib import Path

import cv2
import imagehash
from PIL import Image

from .config import PHASH_HASH_SIZE


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def phash_image(path: str | Path) -> str:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Could not read image: {path}")
    rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return str(imagehash.phash(Image.fromarray(rgb_image), hash_size=PHASH_HASH_SIZE))


def text_hash(text: str) -> str:
    normalized = " ".join(text.split()).strip().lower()
    return sha256_bytes(normalized.encode("utf-8"))
