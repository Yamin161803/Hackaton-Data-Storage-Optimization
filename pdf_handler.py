"""
pdf_to_markdown.py

Load a PDF, extract its images into a folder, and save the text
(with links to the extracted images) into a Markdown file.

Images are named after the SHA-256 hash of their bytes, so identical
images (within one PDF, or across PDFs sharing the same output folder)
are stored only once. Images whose hash is already in the folder are skipped.

Requirements:
    pip install pypdf pillow

Usage:
    python pdf_to_markdown.py input.pdf
    python pdf_to_markdown.py input.pdf --out output_folder

ToDo:
 - Large systems can have billios of unique images, so we want tags to separate them and speed up search.
 - Move hashes to metadata and try to keep original file name

"""

import argparse
import hashlib
from pathlib import Path

from pypdf import PdfReader


def load_pdf(pdf_path):
    """Open a PDF file and return a PdfReader."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    return PdfReader(str(path))


def hash_image(data):
    """Return the SHA-256 hex digest of the image bytes."""
    return hashlib.sha256(data).hexdigest()


def existing_hashes(images_dir):
    """
    Return {hash: file_name} for images already saved in images_dir.
    Files are named <hash><extension>, so the stem is the hash.
    """
    return {p.stem: p.name for p in Path(images_dir).iterdir() if p.is_file()}


def extract_images(reader, images_dir):
    """
    Save every unique image in the PDF into images_dir, named by its hash.
    Images whose hash is already saved are skipped (but still linked).
    Returns (images_per_page, saved_count, skipped_count) where
    images_per_page is {page_number: [image_file_name, ...]}.
    """
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)

    known = existing_hashes(images_dir)
    saved_count = 0
    skipped_count = 0

    images_per_page = {}
    for page_number, page in enumerate(reader.pages, start=1):
        page_files = []
        for image in page.images:
            digest = hash_image(image.data)
            if digest in known:
                skipped_count += 1
            else:
                extension = Path(image.name).suffix or ".png"
                file_name = f"{digest}{extension}"
                (images_dir / file_name).write_bytes(image.data)
                known[digest] = file_name
                saved_count += 1
            page_files.append(known[digest])
        images_per_page[page_number] = page_files
    return images_per_page, saved_count, skipped_count


def extract_text(reader):
    """Return a list with the text of each page."""
    return [(page.extract_text() or "").strip() for page in reader.pages]


def save_markdown(md_path, title, page_texts, images_per_page, images_folder_name):
    """Write page text and image links into a Markdown file."""
    lines = [f"# {title}", ""]
    for page_number, text in enumerate(page_texts, start=1):
        lines.append(f"## Page {page_number}")
        lines.append("")
        if text:
            lines.append(text)
            lines.append("")
        for file_name in images_per_page.get(page_number, []):
            lines.append(f"![{file_name}]({images_folder_name}/{file_name})")
            lines.append("")
    Path(md_path).write_text("\n".join(lines), encoding="utf-8")


def convert(pdf_path, out_dir=None):
    """Run the full pipeline: load -> extract images -> save Markdown."""
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir) if out_dir else pdf_path.parent / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    reader = load_pdf(pdf_path)
    images_per_page, saved, skipped = extract_images(reader, out_dir / "images")
    page_texts = extract_text(reader)

    md_path = out_dir / f"{pdf_path.stem}.md"
    save_markdown(md_path, pdf_path.stem, page_texts, images_per_page, "images")

    print(f"Pages:  {len(reader.pages)}")
    print(f"Images: {saved} saved, {skipped} skipped (already saved) in {out_dir / 'images'}")
    print(f"Markdown: {md_path}")
    return md_path


if __name__ == "__main__":
    # parser = argparse.ArgumentParser(description="Extract images and text from a PDF into Markdown.")
    # parser.add_argument("pdf", help="Path to the PDF file")
    # parser.add_argument("--out", help="Output folder (default: folder named after the PDF)")
    # args = parser.parse_args()

    filename =  r"C:\Users\pwoll\Desktop\Python\AI\Hackathon2026\Hackaton-Data-Storage-Optimization\data\test_merged.pdf" \
    ""
    out_folder = "dump"

    convert(filename, out_folder)
    # convert(args.pdf, args.out)
