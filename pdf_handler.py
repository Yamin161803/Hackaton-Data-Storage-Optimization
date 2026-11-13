"""
pdf_to_markdown.py

Load a PDF, extract its images into a folder, and save the text
(with links to the extracted images) into a Markdown file.

Requirements:
    pip install pypdf pillow

Usage:
    python pdf_to_markdown.py input.pdf
    python pdf_to_markdown.py input.pdf --out output_folder
"""

import argparse
from pathlib import Path

from pypdf import PdfReader


def load_pdf(pdf_path):
    """Open a PDF file and return a PdfReader."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    return PdfReader(str(path))


def extract_images(reader, images_dir):
    """
    Save every image in the PDF into images_dir.
    Returns a dict: {page_number: [image_file_name, ...]}
    """
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)

    images_per_page = {}
    for page_number, page in enumerate(reader.pages, start=1):
        saved = []
        for index, image in enumerate(page.images, start=1):
            extension = Path(image.name).suffix or ".png"
            file_name = f"page{page_number}_img{index}{extension}"
            (images_dir / file_name).write_bytes(image.data)
            saved.append(file_name)
        images_per_page[page_number] = saved
    return images_per_page

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
    images_per_page = extract_images(reader, out_dir / "images")
    page_texts = extract_text(reader)

    md_path = out_dir / f"{pdf_path.stem}.md"
    save_markdown(md_path, pdf_path.stem, page_texts, images_per_page, "images")

    total_images = sum(len(v) for v in images_per_page.values())
    print(f"Pages:  {len(reader.pages)}")
    print(f"Images: {total_images} saved to {out_dir / 'images'}")
    print(f"Markdown: {md_path}")
    return md_path


if __name__ == "__main__":
    # parser = argparse.ArgumentParser(description="Extract images and text from a PDF into Markdown.")
    # parser.add_argument("pdf", help="Path to the PDF file")
    # parser.add_argument("--out", help="Output folder (default: folder named after the PDF)")
    # args = parser.parse_args()

    filename =  r"C:\Users\pwoll\Desktop\Python\AI\Hackathon2026\Hackaton-Data-Storage-Optimization\data\Ericsson Response Introduction[64] - Read-Only.pdf"
    out_folder = "dump"

    convert(filename, out_folder)
    # convert(args.pdf, args.out)

