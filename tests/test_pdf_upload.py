"""PDF uploads must become usable evidence, never binary text on the source shelf."""

import base64
import io
import json
import threading
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ar.studio.asgi import app
from ar.studio.server import SlidingWindowLimiter, Studio, StudioHTTPServer, make_handler


def make_pdf(*, blank=False, encrypted=False, padding=0):
    writer = PdfWriter()
    for text in ("Introduction: This study measures learning across two student groups.",
                 "Results: Recall improved after practice compared with reading alone."):
        page = writer.add_blank_page(width=612, height=792)
        if not blank:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    if padding:
        writer.add_metadata({"/Subject": "x" * padding})
    if encrypted:
        writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(params=["local", "asgi"])
def api(request, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_PASSWORD", "")
    monkeypatch.setattr("ar.studio.server._RATE_LIMITER", SlidingWindowLimiter())
    studio = Studio(tmp_path, fake=True)
    if request.param == "asgi":
        monkeypatch.setattr("ar.studio.asgi.vercel_studio", lambda: studio)
        with TestClient(app) as client:
            def post(path, body):
                response = client.post(path, json=body)
                return response.status_code, response.json()
            yield studio, post
    else:
        server = StudioHTTPServer(("127.0.0.1", 0), make_handler(studio))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}",
                                         json.dumps(body).encode(), {"Content-Type": "application/json"})
            try:
                response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            yield studio, post
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


def test_pdf_upload_preserves_pages_and_can_restore_a_lost_session(api):
    studio, post = api
    _, created = post("/api/session", {})
    body = {"id": created["id"], "name": "Research.PDF", "after": 0,
            "pdf_base64": base64.b64encode(make_pdf(padding=800_000)).decode()}
    # A real PDF exceeding the old 1 MB JSON request cap works on both adapters.
    assert len(json.dumps(body)) > 1_000_000
    status, result = post("/api/source/pdf", body)
    assert status == 200
    assert result["id"] == "research"
    text = result["text"]
    assert text.startswith("[Page 1]\nIntroduction:")
    assert "\n\n[Page 2]\nResults:" in text
    assert "%PDF-" not in text
    assert studio.get(created["id"]).sources == {"research": text}
    assert any(e["type"] == "sources" for e in result["events"])
    assert post("/api/source/pdf", body)[1]["id"] == "research"
    _, restored = post("/api/session", {"restore": {"sources": {"research": text}}})
    assert studio.get(restored["id"]).sources == {"research": text}


@pytest.mark.parametrize("content, message", [
    (None, "missing"),
    ("%%%", "invalid file data"),
    (base64.b64encode(b"not a PDF").decode(), "not a PDF"),
    (base64.b64encode(b"%PDF-1.7\nbroken").decode(), "could not extract"),
    (base64.b64encode(make_pdf(blank=True)).decode(), "OCR"),
    (base64.b64encode(make_pdf(encrypted=True)).decode(), "could not extract"),
    ("A" * 4_000_004, "larger than 3 MB"),
], ids=["missing", "bad-base64", "wrong-format", "corrupt", "scanned", "encrypted", "too-large"])
def test_invalid_pdf_is_rejected_without_adding_a_source(api, content, message):
    studio, post = api
    _, created = post("/api/session", {})
    status, result = post("/api/source/pdf", {"id": created["id"], "name": "paper.pdf", "pdf_base64": content})
    assert status == 400
    assert message in result["error"]
    assert studio.get(created["id"]).sources == {}


def test_text_upload_still_works_and_rejects_raw_pdf_bytes(api):
    studio, post = api
    _, created = post("/api/session", {})
    status, result = post("/api/source", {"id": created["id"], "name": "notes.md", "text": "# Notes\nEvidence"})
    assert status == 200 and result["id"] == "notes"
    status, result = post("/api/source", {"id": created["id"], "name": "bad.pdf", "text": "%PDF-1.7\nbinary"})
    assert status == 400 and "extract PDF text" in result["error"]
    assert list(studio.get(created["id"]).sources) == ["notes"]
