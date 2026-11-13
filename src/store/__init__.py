"""Corpus document store: content-addressed documents with page-level dedup."""
from .store import (Store, DocumentInput, Page, IngestResult,
                    hash_page, hash_text, hash_images, combined_hash,
                    SOURCE_PDF, SOURCE_PPTX)

__all__ = ["Store", "DocumentInput", "Page", "IngestResult",
           "hash_page", "hash_text", "hash_images", "combined_hash",
           "SOURCE_PDF", "SOURCE_PPTX"]
