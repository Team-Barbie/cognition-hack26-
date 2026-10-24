"""Bounded public-web retrieval and deterministic document extraction.

DNS is validated at connection time and the socket connects to that exact address.
Redirects are checked afresh; credentials never cross a redirect boundary.
"""
from __future__ import annotations

import http.client
import ipaddress
import io
import json
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


class RetrievalError(RuntimeError):
    pass


def public_url(url: str) -> str:
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise ValueError
        if p.port not in {None, 80 if p.scheme == "http" else 443}:
            raise ValueError
        if any(ord(ch) < 33 for ch in url) or "\\" in url:
            raise ValueError
        host = p.hostname.encode("idna").decode("ascii")
        if host.casefold().rstrip(".") in {"localhost", "metadata.google.internal"}:
            raise ValueError
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        raise RetrievalError("only public HTTP(S) URLs on standard ports are allowed") from None


def _public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise RetrievalError("destination resolves to a private or reserved address")
    last_error = None
    for family, socktype, proto, _, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise RetrievalError("could not connect to destination") from last_error


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    body: bytes

    def json(self) -> dict:
        try:
            value = json.loads(self.body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise RetrievalError("endpoint returned invalid JSON") from None


class PublicWeb:
    def get(self, url: str, *, headers: dict | None = None, max_bytes: int = 5_000_000,
            timeout: float = 20) -> Fetched:
        current = public_url(url)
        for redirect in range(5):
            p = urlsplit(current)
            conn = (http.client.HTTPSConnection(p.hostname, timeout=timeout, context=ssl.create_default_context())
                    if p.scheme == "https" else http.client.HTTPConnection(p.hostname, timeout=timeout))
            conn._create_connection = _public_connection
            try:
                conn.request("GET", urlunsplit(("", "", p.path, p.query, "")),
                             headers={"User-Agent": "ar-literature/0.1", "Accept-Encoding": "identity",
                                      **(headers or {})})
                res = conn.getresponse()
                if res.status in {301, 302, 303, 307, 308}:
                    location = res.getheader("Location")
                    if not location or redirect == 4:
                        raise RetrievalError("redirect limit reached or missing destination")
                    next_url = public_url(urljoin(current, location))
                    if p.scheme == "https" and urlsplit(next_url).scheme != "https":
                        raise RetrievalError("HTTPS downgrade redirect refused")
                    current, headers = next_url, None
                    continue
                if res.status != 200:
                    raise RetrievalError(f"remote server returned HTTP {res.status}")
                length = res.getheader("Content-Length")
                if length and (not length.isdigit() or int(length) > max_bytes):
                    raise RetrievalError("response exceeds the download size limit")
                if res.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise RetrievalError("unsupported compressed response")
                body = res.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise RetrievalError("response exceeds the download size limit")
                return Fetched(current, res.getheader("Content-Type", "").lower(), body)
            except (OSError, http.client.HTTPException) as exc:
                raise RetrievalError(f"document fetch failed ({type(exc).__name__})") from exc
            finally:
                conn.close()
        raise RetrievalError("redirect limit reached")


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.skip = 0
        self.citation_urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "noscript"}:
            self.skip += 1
        if tag in {"p", "div", "section", "br", "h1", "h2", "h3", "li", "tr"}:
            self.text.append("\n")
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name", "").lower() in {"citation_pdf_url", "citation_fulltext_html_url"}:
            if attrs.get("content"):
                self.citation_urls.append(attrs["content"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "noscript"} and self.skip:
            self.skip -= 1
        if tag in {"p", "div", "section", "h1", "h2", "h3", "li", "tr"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def html_text(text: str) -> str:
    parser = _HTML()
    parser.feed(text)
    return "\n".join(line for raw in "".join(parser.text).splitlines() if (line := " ".join(raw.split())))


@dataclass(frozen=True)
class Extracted:
    text: str
    kind: str  # full_text, web_page, or abstract; never inferred from model output
    parser: str
    citation_urls: tuple[str, ...] = ()


def extract_document(fetched: Fetched) -> Extracted:
    body, kind = fetched.body, fetched.content_type.split(";", 1)[0]
    if kind == "application/pdf" or body.startswith(b"%PDF-"):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(body))
            if len(reader.pages) > 500:
                raise RetrievalError("PDF exceeds the 500-page extraction limit")
            pages = [f"[Page {n}]\n{page.extract_text() or ''}" for n, page in enumerate(reader.pages, 1)]
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError("could not extract the PDF text") from exc
        if not any(len(page.split()) > 8 for page in pages):
            raise RetrievalError("PDF has no usable text; OCR is required")
        return Extracted("\n\n".join(pages), "full_text", "pypdf")
    if kind in {"application/xml", "text/xml"} or body.lstrip().startswith(b"<?xml"):
        from defusedxml.ElementTree import fromstring
        try:
            root = fromstring(body)
            nodes = root.findall(".//abstract") + root.findall(".//body")
            if not nodes:
                raise RetrievalError("XML document has no article body")
            paragraphs = [" ".join("".join(p.itertext()).split()) for node in nodes
                          for p in node.iter() if p.tag in {"p", "title", "table"}]
            return Extracted("\n".join(paragraphs), "full_text" if root.find(".//body") is not None else "abstract",
                             "jats-xml")
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError("could not parse article XML") from exc
    if kind in {"text/html", "application/xhtml+xml"}:
        parser = _HTML()
        parser.feed(body.decode("utf-8", errors="replace"))
        return Extracted(html_text(body.decode("utf-8", errors="replace")), "web_page", "html",
                         tuple(urljoin(fetched.url, link) for link in parser.citation_urls))
    if kind in {"text/plain", "text/markdown"}:
        return Extracted(body.decode("utf-8", errors="replace"), "web_page", "plain-text")
    raise RetrievalError("unsupported document content type")
