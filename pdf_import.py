"""Read SBI domestic stock and exchange/fund transaction reports safely."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from ledger import Snapshot, LedgerError, decimal_text, parse_date, trade_key
from pdf_common import compact_text, japanese_date_tokens, load_pdf, normalize_text


REFERENCE_WIDTH = Decimal("597.6")
REFERENCE_HEIGHT = Decimal("324")


def _scaled_words(page):
    """Normalize a landscape page to SBI's reference coordinate system."""
    sx = float(REFERENCE_WIDTH) / page.width
    sy = float(REFERENCE_HEIGHT) / page.height
    result = []
    for original in page.words:
        word = dict(original)
        word["text"] = normalize_text(word.get("text", ""))
        for key in ("x0", "x1"):
            word[key] = float(word[key]) * sx
        for key in ("top", "bottom"):
            word[key] = float(word[key]) * sy
        result.append(word)
    return result


def _region_lines(words, x0, x1, y0, y1):
    selected = []
    for word in words:
        cx = (word["x0"] + word["x1"]) / 2
        cy = (word["top"] + word["bottom"]) / 2
        if x0 <= cx < x1 and y0 <= cy < y1:
            selected.append(word)
    selected.sort(key=lambda word: (word["top"], word["x0"]))
    lines = []
    for word in selected:
        if not lines or abs(word["top"] - lines[-1][0]["top"]) > 2.5:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [" ".join(word["text"] for word in line).strip() for line in lines]


def _region(words, x0, x1, y0, y1):
    return "\n".join(_region_lines(words, x0, x1, y0, y1))


def _owner(text):
    lines = [normalize_text(line).strip() for line in text.splitlines() if line.strip()]
    for exact in (True, False):
        for line in lines:
            pattern = r"^(.{2,50}?)\s+様\s*$" if exact else r"^(.{2,50}?)\s+様(?:\s|$)"
            match = re.search(pattern, line)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip()
    raise LedgerError("PDFの名義を読み取れません。")


def _date_pair(words, text, page_no):
    # The broad fallback handles a few-points print shift; the header region is
    # preferred so the document creation date is not mistaken for the trade date.
    header = _region(words, 355, 595, 45, 88)
    found = japanese_date_tokens(header)
    if len(found) != 2:
        all_dates = japanese_date_tokens(text)
        if len(all_dates) >= 3:
            found = all_dates[-2:]
        elif len(all_dates) == 2:
            found = all_dates
    if len(found) != 2:
        raise LedgerError(f"{page_no}ページ目の約定日・受渡日を確定できません。")
    pair = tuple(parse_date(value) for value in found)
    if pair[1] < pair[0]:
        raise LedgerError("受渡日が約定日より前です。")
    return pair


def _optional_number(value, label):
    cleaned = compact_text(value).replace("円", "")
    if cleaned in ("", "-", "--"):
        return "0"
    try:
        return decimal_text(cleaned)
    except LedgerError as error:
        raise LedgerError(f"{label}を読み取れません。") from error


def _custody(details):
    if "NISA成長投資枠" in details:
        return "NISA(成)", "非課税"
    if "NISAつみたて投資枠" in details or "つみたてNISA" in details:
        return "NISA(積)", "非課税"
    if "NISA預り" in details or "一般NISA" in details:
        return "NISA", "非課税"
    if "特定預り" in details or "特定口座預り" in details:
        return "特定", "課税"
    if "一般預り" in details or "一般口座預り" in details:
        return "一般", "課税"
    raise LedgerError("預り区分（NISA・特定・一般）を確認できません。")


def _market(details):
    if "取引:自己" in details and "単元未満取引" in details:
        return "--"
    if "取引:委託" not in details:
        return ""
    if "市場:私設(X)" in details and "普通取引" in details:
        return "PTS(X)"
    if "市場:私設(O)" in details and "普通取引" in details:
        return "PTS(O)"
    exchanges = {
        "東京": "東証",
        "名古屋": "名証",
        "福岡": "福証",
        "札幌": "札証",
        "大阪": "大証",
    }
    for label, output in exchanges.items():
        if f"市場:{label}" in details:
            if "立会外取引" in details:
                return output + "(外)"
            if "普通取引" in details:
                return output
    return ""


def _instrument(quantity, price, gross, report_type, details, code):
    q = Decimal(quantity)
    p = Decimal(price)
    g = Decimal(gross)
    direct = q * p
    per_ten_thousand = direct / Decimal(10_000)
    listed_market = _market(details)

    if direct == g:
        if not listed_market:
            raise LedgerError("上場商品の市場・取引条件を確認できません。")
        if not code:
            raise LedgerError("上場商品の銘柄コードを読み取れません。")
        return "国内株式", "株", "円/株", listed_market

    # Ordinary mutual funds quote a price per 10,000 units.  Settlement is in
    # whole yen, so tolerate at most the one-yen rounding difference.
    if report_type == "fund" and abs(per_ten_thousand - g) <= Decimal("1"):
        return "投資信託", "口", "円/1万口", "--"
    raise LedgerError("数量×単価と約定金額が一致しません。")


def _parse_row(page_no, words, side_word, dates, report_type):
    y = side_word["top"]
    name_lines = _region_lines(words, 12, 148, y - 17, y + 2)
    name = "".join(name_lines).strip()
    code_text = compact_text(_region(words, 12, 148, y + 1, y + 15))
    codes = re.findall(r"\(([0-9A-Z-]{4,20})\)", code_text)
    if not name or len(codes) > 1:
        raise LedgerError(f"{page_no}ページ目の銘柄・コードを読み取れません。")
    code = codes[0] if codes else ""
    if report_type == "stock" and not code:
        raise LedgerError(f"{page_no}ページ目の銘柄コードを読み取れません。")

    try:
        quantity = decimal_text(compact_text(_region(words, 168, 335, y - 14, y + 2)))
        price = decimal_text(compact_text(_region(words, 335, 455, y - 14, y + 2)))
        gross = decimal_text(compact_text(_region(words, 455, 595, y - 14, y + 2)))
    except LedgerError as error:
        raise LedgerError(f"{page_no}ページ目の数量・単価・約定金額を読み取れません。") from error
    if Decimal(quantity) <= 0:
        raise LedgerError("数量は0より大きい必要があります。")

    fees = _optional_number(_region(words, 168, 335, y + 1, y + 14), "手数料")
    tax = _optional_number(_region(words, 335, 455, y + 1, y + 14), "消費税等")
    cash_text = compact_text(_region(words, 455, 595, y + 1, y + 15)).replace("円", "")
    side = compact_text(side_word["text"])
    expected_sign = "-" if side == "買" else "+"
    if not cash_text.startswith(expected_sign):
        raise LedgerError("売買と受渡金額の符号が一致しません。")
    amount = decimal_text(cash_text[1:])

    details = compact_text(_region(words, 160, 595, y + 9, y + 26))
    custody, tax_class = _custody(details)
    asset_type, quantity_unit, price_unit, market = _instrument(
        quantity, price, gross, report_type, details, code
    )
    settlement = Decimal(gross) + Decimal(fees) + Decimal(tax)
    if side == "売":
        settlement = Decimal(gross) - Decimal(fees) - Decimal(tax)
    if settlement < 0 or settlement != Decimal(amount):
        raise LedgerError("約定金額・手数料・税額と受渡金額が一致しません。")

    transaction = {
        "trade_date": dates[0],
        "name": name,
        "code": code,
        "market": market,
        "side": side,
        "custody": custody,
        "tax_class": tax_class,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "tax": tax,
        "settlement_date": dates[1],
        "settlement_amount": amount,
        "asset_type": asset_type,
        "quantity_unit": quantity_unit,
        "price_unit": price_unit,
        "currency": "JPY",
    }
    transaction["signature"] = trade_key(transaction)
    return transaction


def parse_report(path):
    data = load_pdf(path, words=True, max_pages=20)
    combined = data.best_compact + data.compact
    if "特定口座年間取引報告書" in combined or "STWHGB" in combined:
        raise LedgerError("年間取引報告書は売買明細の取り込み対象外です。")
    if "外国株式等取引報告書" in combined:
        raise LedgerError("外国株式の取引報告書は現在の取り込み対象外です。")
    if "外国債券取引報告書" in combined:
        raise LedgerError("外国債券の取引報告書は取り込み対象外です。")

    first = data.pages[0].best_compact + data.pages[0].compact
    if "投資信託取引報告書" in first:
        report_type = "fund"
    elif "株式取引報告書" in first:
        report_type = "stock"
    else:
        raise LedgerError("SBIの国内株式・投資信託の取引報告書と確認できません。")
    owner = _owner(data.pages[0].best_text)

    transactions = []
    date_pair = None
    identifiers = set()
    for page_no, page in enumerate(data.pages, 1):
        ratio = page.width / page.height
        if page.width <= page.height or not 1.65 <= ratio <= 2.05:
            raise LedgerError(f"{page_no}ページ目の用紙方向・縦横比が確認済み様式と異なります。")
        words = _scaled_words(page)
        current = _date_pair(words, page.best_text or page.text, page_no)
        if date_pair is not None and current != date_pair:
            raise LedgerError("報告書のページ間で約定日・受渡日が異なります。")
        date_pair = current

        page_compact = page.best_compact or page.compact
        found_ids = re.findall(r"\d{12}STWHAB[0-9A-Z]+P#\d{10,}", page_compact)
        if len(set(found_ids)) != 1:
            raise LedgerError(f"{page_no}ページ目の報告書識別情報を読み取れません。")
        identifiers.add(found_ids[0])

        sides = []
        for word in words:
            cx = (word["x0"] + word["x1"]) / 2
            cy = (word["top"] + word["bottom"]) / 2
            if 130 <= cx <= 190 and 98 <= cy <= 235 and compact_text(word["text"]) in ("買", "売"):
                sides.append(word)
        sides.sort(key=lambda word: word["top"])
        if not sides or len(sides) > 4:
            raise LedgerError(f"{page_no}ページ目の取引行数を確認できません。")
        for side_word in sides:
            transactions.append(_parse_row(page_no, words, side_word, current, report_type))

    if len(identifiers) != 1:
        raise LedgerError("複数ページの報告書識別情報が一致しません。")
    if not transactions or date_pair is None:
        raise LedgerError("取引明細がありません。")
    raw = data.raw
    trade_date = date_pair[0]
    return Snapshot(
        raw,
        data.path.name,
        hashlib.sha256(raw).hexdigest(),
        trade_date,
        trade_date,
        transactions,
        kind="pdf",
        owner=owner,
        document_key=hashlib.sha256(next(iter(identifiers)).encode()).hexdigest(),
    )
