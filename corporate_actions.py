"""Conservative readers for SBI dividends, splits and balance statements."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from ledger import LedgerError, decimal_text, parse_date
from pdf_common import compact_text, load_pdf, normalize_text


def number(value):
    return decimal_text(normalize_text(value))


def japanese_date(value):
    return parse_date(compact_text(value))


@dataclass
class Document:
    kind: str
    filename: str
    raw: bytes
    digest: str
    owner: str
    item: dict


def _read(path):
    data = load_pdf(path, max_pages=50)
    return data.path, data.raw, [page.best_text for page in data.pages]


def _owner(text):
    lines = [normalize_text(line).strip() for line in text.splitlines() if line.strip()]
    for exact in (True, False):
        for line in lines:
            pattern = r"^(.{2,50}?)\s+様\s*$" if exact else r"^(.{2,50}?)\s+様(?:\s|$)"
            match = re.search(pattern, line)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip()
    raise LedgerError("名義を読み取れません。原本を確認してください。")


def parse_notice(path):
    path, raw, pages = _read(path)
    if len(pages) != 1:
        raise LedgerError("この通知は1ページのPDFにのみ対応します。")
    text = pages[0]
    compact = compact_text(text)
    owner = _owner(text)

    if "株式等配当金のお知らせ" in compact or "株式等利益剰余金配当金のお知らせ" in compact:
        # One issue followed by payment date, per-share dividend and shares;
        # then gross, withholding fields and net receipt.
        lines = [normalize_text(line) for line in text.splitlines()]
        row = next(
            (
                index
                for index, line in enumerate(lines)
                if re.search(r"\d{4}年\s*\d+月\s*\d+日\s+[\d,.]+\s+[\d,]+\s*$", line)
            ),
            None,
        )
        if row is None or row < 1 or row + 3 >= len(lines):
            raise LedgerError("配当の日付・株数・金額欄を読み取れません。")
        date_line = re.search(
            r"(\d{4}年\s*\d+月\s*\d+日)\s+([\d,.]+)\s+([\d,]+)\s*$", lines[row]
        )
        amounts = re.findall(r"[\d,]+", lines[row + 2])
        if not date_line or len(amounts) != 5:
            raise LedgerError("配当の税額または受取額の欄が異なります。")
        name = lines[row - 1].strip()
        code_match = re.search(r"\(\s*([0-9A-Za-z-]{4,20})\s*\)", lines[row + 1])
        if not name or not code_match:
            raise LedgerError("配当の銘柄・コードを読み取れません。")
        gross, tax1, tax2, tax3, net = map(number, amounts)
        withholding = sum(map(Decimal, (tax1, tax2, tax3)))
        if Decimal(gross) - withholding != Decimal(net):
            raise LedgerError("配当の税引前金額と税額・受取額が一致しません。")
        event = {
            "type": "配当",
            "date": japanese_date(date_line.group(1)),
            "name": name,
            "code": code_match.group(1),
            "quantity": number(date_line.group(3)),
            "per_share": number(date_line.group(2)),
            "gross": gross,
            "tax": number(str(withholding)),
            "net": net,
            "custody": "NISA" if "NISA" in compact_text(lines[row + 3]) else "",
            "currency": "JPY",
        }
        if Decimal(event["quantity"]) * Decimal(event["per_share"]) != Decimal(gross):
            raise LedgerError("配当単価と株数から計算した金額が通知と一致しません。")
        return Document(
            "dividend_pdf", path.name, raw, hashlib.sha256(raw).hexdigest(), owner, event
        )

    if ("割当株式等のお知らせ" in compact or "割当株券のお知らせ" in compact) and "株式分割" in compact:
        lines = [normalize_text(line) for line in text.splitlines()]
        line = next((value for value in lines if "株式分割" in value and re.search(r"\d{4}年", value)), None)
        if line is None:
            raise LedgerError("株式分割の効力発生日を読み取れません。")
        match = re.fullmatch(
            r"(.+?)\s+株式分割\s+(\d{4}年\s*\d+月\s*\d+日)\s+(\d+)\s*:\s*(\d+)",
            line.strip(),
        )
        code = next(
            (value.strip() for value in lines if re.fullmatch(r"\s*[0-9A-Za-z-]{4,20}\s*", value)),
            None,
        )
        if not match or not code:
            raise LedgerError("分割の銘柄・比率を読み取れません。")
        holders = [
            value
            for value in lines
            if re.search(r"^.+?\s+[\d,]+\s+[\d,]+\s+[\d,]+\s*$", value) and "年" not in value
        ]
        if len(holders) != 1:
            raise LedgerError("分割の株数の明細は1人分のみ読み取れます。")
        quantities = re.search(r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$", holders[0])
        before, added, after = map(lambda value: int(value.replace(",", "")), quantities.groups())
        old, new = map(int, match.group(3, 4))
        if (
            old <= 0
            or new <= old
            or before <= 0
            or before * new % old
            or after != before + added
            or after != before * new // old
        ):
            raise LedgerError("通知の分割比率と株数が一致しません。")
        event = {
            "type": "株式分割",
            "date": japanese_date(match.group(2)),
            "name": match.group(1),
            "code": code,
            "ratio_old": old,
            "ratio_new": new,
            "before": before,
            "added": added,
            "after": after,
            "currency": "JPY",
        }
        allocations = {}
        for value in lines:
            allocation = re.search(
                r"うち\s*(NISA成長投資枠|特定対象)\s*\(\s*([\d,]+)\s*\)\s*"
                r"\(\s*([\d,]+)\s*\)\s*\(\s*([\d,]+)\s*\)",
                value,
            )
            if allocation:
                label = "NISA(成)" if allocation.group(1) == "NISA成長投資枠" else "特定"
                allocations[label] = [int(item.replace(",", "")) for item in allocation.groups()[1:]]
        if allocations:
            event["allocations"] = allocations
        return Document("split_pdf", path.name, raw, hashlib.sha256(raw).hexdigest(), owner, event)

    if "譲渡益税徴収・還付のお知らせ" in compact:
        if re.search(r'\d{12}STWHAB020P#',compact):
            return parse_tax_table(path,owner)
        # SBI has changed this notice several times.  Import only when a labeled
        # refund amount is unambiguous; otherwise the UI offers a manual cash
        # entry instead of guessing from unrelated tax figures on the page.
        amount_patterns = (
            r"(?:譲渡益税)?還付金額(?:\(円\))?[:：]?([\d,]+)",
            r"還付額(?:\(円\))?[:：]?([\d,]+)",
            r"還付金(?:\(円\))?[:：]?([\d,]+)",
        )
        amounts = []
        for pattern in amount_patterns:
            amounts.extend(re.findall(pattern, compact))
        amounts = list(dict.fromkeys(value for value in amounts if Decimal(value.replace(',', '')) > 0))
        if len(amounts) != 1:
            raise LedgerError("税還付額を1つに確定できません。原本を確認し、手動追加してください。")
        labeled_dates = []
        for pattern in (
            r"(?:還付日|受渡日|支払日)[:：]?(20\d{2}年\d{1,2}月\d{1,2}日)",
            r"作成日[:：]?(20\d{2}年\d{1,2}月\d{1,2}日)",
        ):
            labeled_dates = re.findall(pattern, compact)
            if labeled_dates:
                break
        if len(set(labeled_dates)) != 1:
            raise LedgerError("税還付の日付を確定できません。原本を確認し、手動追加してください。")
        amount = number(amounts[0])
        event = {
            "type": "税還付", "date": japanese_date(labeled_dates[0]),
            "name": "譲渡益税還付", "description": "譲渡益税還付", "code": "",
            "amount": amount, "gross": amount, "tax": "0", "net": amount,
            "currency": "JPY",
        }
        return Document("tax_refund_pdf", path.name, raw, hashlib.sha256(raw).hexdigest(), owner, event)

    raise LedgerError("このPDFは対応する配当・株式分割・税還付通知ではありません。")


def parse_tax_table(path,owner):
    """Verified SBI AB020 layout. H is the cash tax movement, never dividend receipts."""
    pdf=load_pdf(path,words=True,max_pages=1);page=pdf.pages[0]
    sx=597.6/page.width;sy=324/page.height
    def cell(x0,x1,y0,y1):
        words=sorted((w for w in page.words if x0 <= (w['x0']+w['x1'])*sx/2 < x1 and y0 <= w['top']*sy < y1),key=lambda w:w['x0'])
        return compact_text(''.join(w['text'] for w in words)).replace('−','-').replace('―','-')
    def value(x0,x1,y):
        text=cell(x0,x1,y-2,y+10)
        if not re.fullmatch(r'-?[\d,]+',text):raise LedgerError('税通知の表の数値を読み取れません。')
        return Decimal(text.replace(',',''))
    A,B,C=[value(x0,x1,104) for x0,x1 in ((218,345),(346,460),(461,578))]
    D,E,F,G=[value(x0,x1,140) for x0,x1 in ((25,216),(218,345),(346,460),(461,578))]
    H,I,J=[value(x0,x1,176) for x0,x1 in ((218,345),(346,460),(461,578))]
    if A+D!=E or F-B!=I or G-C!=J or I+J!=H:raise LedgerError('税通知の年間損益・所得税・地方税の整合性が取れません。')
    day=japanese_date(cell(120,215,102,115))
    if H==0:raise LedgerError('解析済み：徴収・還付額0円。入金は発生していないため配当シートへ登録しません。')
    if H>0:raise LedgerError('解析済み：税金の徴収通知。還付入金ではないため配当シートへ登録しません。')
    amount=number(-H)
    event=dict(type='税還付',date=day,name='譲渡益税還付',description='譲渡益税還付',code='',amount=amount,gross=amount,tax='0',net=amount,currency='JPY')
    return Document('tax_refund_pdf',pdf.path.name,pdf.raw,hashlib.sha256(pdf.raw).hexdigest(),owner,event)


def _balance_sections(page):
    headers = ("【国内株式等の残高】", "【保護預り証券および代用証券等の残高】")
    for header in headers:
        if header not in page:
            continue
        section = page.split(header, 1)[1]
        section = section.split("(取引残高報告書)", 1)[0]
        yield section


def parse_balance(path):
    path, raw, pages = _read(path)
    all_text = "\n".join(pages)
    compact = compact_text(all_text)
    if not any(
        marker in compact
        for marker in ("取引残高報告書", "お預り残高等の明細", "STWHZB", "FR00011M01")
    ):
        raise LedgerError("対応する取引残高報告書ではありません。")

    match = re.search(r"(20\d{2}年\d{1,2}月\d{1,2}日)現在", compact)
    if not match:
        raise LedgerError("残高の基準日を読み取れません。")
    as_of = japanese_date(match.group(1))
    holdings = []
    for page in pages:
        for section in _balance_sections(page):
            for line in section.splitlines():
                normalized = normalize_text(line)
                holding = None if normalized.startswith("[") else re.match(
                    r"(.+?)\s+\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\s+([\d,]+)株\s+", normalized
                )
                if holding:
                    holdings.append(
                        {
                            "name": holding.group(1).strip(),
                            "quantity": int(holding.group(2).replace(",", "")),
                            "code": "",
                        }
                    )
                if not holdings:
                    continue
                code = re.search(r"\[\s*([0-9A-Za-z-]{4,20})\s*\]", normalized)
                if code:
                    holdings[-1]["code"] = code.group(1)
                addition = re.search(
                    r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\s+([\d,]+)株.*株式分割追加発行",
                    normalized,
                )
                if addition:
                    effective = (
                        f"{addition.group(1)}-{int(addition.group(2)):02}-{int(addition.group(3)):02}"
                    )
                    if effective <= as_of:
                        holdings[-1]["quantity"] += int(addition.group(4).replace(",", ""))
                total = re.search(r"\(銘柄計\)\s*([\d,]+)株", normalize_text(normalized))
                if total:
                    holdings[-1]["quantity"] = int(total.group(1).replace(",", ""))

    costs = []
    for page in pages:
        if "NISA預り証券の取得コスト等" not in compact_text(page):
            continue
        lines = page.splitlines()
        for index, line in enumerate(lines):
            cost = re.match(r"国内株式等\s+(.+?)\s+([\d,]+)株\s+([\d,]+)円\s+", normalize_text(line))
            if not cost:
                continue
            next_line = normalize_text(lines[index + 1] if index + 1 < len(lines) else "")
            code = re.search(r"\[\s*([0-9A-Za-z-]{4,20})\s*\]", next_line)
            if not code:
                continue
            physical = next((item for item in holdings if item["code"] == code.group(1)), None)
            quantity = int(cost.group(2).replace(",", ""))
            costs.append(
                {
                    "name": cost.group(1),
                    "code": code.group(1),
                    "quantity": quantity,
                    "per_share": int(cost.group(3).replace(",", "")),
                    "physical_match": bool(physical and physical["quantity"] == quantity),
                }
            )

    empty_markers = (
        "残高はございません",
        "ご報告の対象となる残高はございません",
        "報告の対象となる残高はございません",
    )
    if holdings:
        status = "parsed"
        warning = ""
    elif any(marker in compact for marker in empty_markers):
        status = "empty_confirmed"
        warning = "報告書に国内株式の残高なしと記載されています。"
    else:
        # Keep the old statement as evidence without inventing holdings.  The UI
        # makes this explicit so an unreadable table is never treated as zero.
        status = "not_extracted"
        warning = "古い様式のため国内株式の残高明細を抽出できません。原本で確認してください。"
    item = {
        "as_of": as_of,
        "holdings": holdings,
        "reference_costs": costs,
        "parse_status": status,
        "warning": warning,
    }
    return Document("balance_pdf", path.name, raw, hashlib.sha256(raw).hexdigest(), _owner(pages[0]), item)
