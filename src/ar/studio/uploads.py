"""Decode uploaded PDFs into the page-labelled text used by the evidence pipeline."""

import base64
import binascii

from ar.retrieval import Fetched, extract_document

MAX_PDF_BYTES = 3_000_000
MAX_PDF_BODY = 4_010_000  # Base64 plus JSON metadata, below the hosted request limit.


def pdf_text(encoded: object) -> str:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("PDF upload is missing its file data")
    if len(encoded) > 4 * ((MAX_PDF_BYTES + 2) // 3):
        raise ValueError("PDF is larger than 3 MB")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("PDF upload contains invalid file data") from exc
    if len(body) > MAX_PDF_BYTES:
        raise ValueError("PDF is larger than 3 MB")
    if not body.startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a PDF")
    return extract_document(Fetched("", "application/pdf", body)).text
