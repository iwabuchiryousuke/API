"""Shared, defensive PDF reading helpers for SBI documents.

SBI has used several PDF generators.  Some insert ordinary/full-width spaces
between every date component, while older statements expose Japanese text only
through a different PDF text extractor.  This module keeps those differences
out of the individual document parsers.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ledger import LedgerError


_TRANSLATION = str.maketrans({
    "\u2212": "-",  # minus sign
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\ufe63": "-",
    "\uff0d": "-",
    "\uff1a": ":",
    "\uff0c": ",",
    "\uff08": "(",
    "\uff09": ")",
    "\uff3b": "[",
    "\uff3d": "]",
    "\u200b": "",
    "\ufeff": "",
})


def normalize_text(value: str | None) -> str:
    """Normalize glyph variants but retain line breaks and meaningful spaces."""
    return unicodedata.normalize("NFKC", value or "").translate(_TRANSLATION)


def compact_text(value: str | None) -> str:
    """Return text suitable for labels/dates that may contain arbitrary spaces."""
    return re.sub(r"\s+", "", normalize_text(value))


def japanese_date_tokens(value: str | None) -> list[str]:
    """Find Japanese calendar dates after removing all PDF-inserted whitespace."""
    return re.findall(r"20\d{2}年\d{1,2}月\d{1,2}日", compact_text(value))


def _text_score(value: str) -> tuple[int, int]:
    # Prefer the extractor that recovered Japanese labels, then the longer text.
    japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", value))
    return japanese, len(compact_text(value))


@dataclass(frozen=True)
class PageData:
    width: float
    height: float
    text: str
    best_text: str
    words: tuple[dict, ...]

    @property
    def compact(self) -> str:
        return compact_text(self.text)

    @property
    def best_compact(self) -> str:
        return compact_text(self.best_text)


@dataclass(frozen=True)
class PdfData:
    path: Path
    raw: bytes
    pages: tuple[PageData, ...]

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def best_text(self) -> str:
        return "\n".join(page.best_text for page in self.pages)

    @property
    def compact(self) -> str:
        return compact_text(self.text)

    @property
    def best_compact(self) -> str:
        return compact_text(self.best_text)


def _fallback_texts(path: Path, count: int) -> list[str]:
    """Use pypdf only as a fallback for PDFs whose embedded font fools pdfplumber."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return [""] * count

    # A few old SBI fonts trigger harmless decoder warnings.  They do not help
    # an end user and would otherwise flood the Windows console.
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    try:
        reader = PdfReader(str(path))
        result = []
        for page in reader.pages[:count]:
            try:
                result.append(normalize_text(page.extract_text(extraction_mode="layout") or ""))
            except Exception:
                result.append("")
        return result + [""] * (count - len(result))
    except Exception:
        return [""] * count


def load_pdf(path, *, words: bool = False, max_pages: int = 50) -> PdfData:
    """Read a PDF once, with normalized words and an old-font text fallback."""
    try:
        import pdfplumber
    except ImportError as error:
        raise LedgerError(
            "PDF機能の準備が必要です。「setup_pdf_windows.bat」を1回実行してください。"
        ) from error

    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LedgerError("PDFファイルを開けません。場所とアクセス権を確認してください。") from error
    if len(raw) > 25_000_000:
        raise LedgerError("PDFは25MB以下にしてください。")

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise LedgerError("ページのないPDFです。")
            if len(pdf.pages) > max_pages:
                raise LedgerError(f"PDFのページ数が多すぎます（上限{max_pages}ページ）。")
            plumber = []
            for page in pdf.pages:
                text = normalize_text(page.extract_text() or "")
                extracted = ()
                if words:
                    extracted = tuple(
                        dict(word, text=normalize_text(word.get("text", "")))
                        for word in page.extract_words()
                    )
                plumber.append((float(page.width), float(page.height), text, extracted))
    except LedgerError:
        raise
    except Exception as error:
        raise LedgerError(
            "PDFを読み取れません。暗号化・画像PDF・ファイル破損の可能性があります。"
        ) from error

    # Only invoke the second extractor when at least one page recovered almost
    # no Japanese.  This keeps recent PDFs fast while rescuing older SBI forms.
    needs_fallback = any(_text_score(text)[0] < 5 for _, _, text, _ in plumber)
    fallback = _fallback_texts(path, len(plumber)) if needs_fallback else [""] * len(plumber)
    pages = []
    for (width, height, text, extracted), other in zip(plumber, fallback):
        best = other if _text_score(other) > _text_score(text) else text
        pages.append(PageData(width, height, text, best, extracted))

    if not any(page.best_compact or page.words for page in pages):
        raise LedgerError("文字を読み取れません。画像だけのPDFには対応していません。")
    return PdfData(path, raw, tuple(pages))
