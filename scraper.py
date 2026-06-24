#!/usr/bin/env python3
"""
Taiwan Legislator Property Declaration Scraper
Parses PDFs from legislator-wealth.tw/declaration-pdfs/legislators/

Run: python3 scraper.py
Output: data/legislators_full.json
"""

import json
import re
import sys
import time
from pathlib import Path

import pdfplumber
import requests

API_BASE = "https://legislator-wealth.tw"
PDF_BASE = f"{API_BASE}/declaration-pdfs/legislators"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

PDF_CACHE = Path(__file__).parent / "data" / "_pdf_cache"
OUT_PATH = Path(__file__).parent / "data" / "legislators_full.json"

PDF_CACHE.mkdir(parents=True, exist_ok=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def ntd(s: str) -> int:
    """Parse '170,969,342' or '6,158.70' → int (round)."""
    return round(float(s.replace(",", "").replace(" ", "").strip()))


def get_api_legislators() -> list:
    r = requests.get(f"{API_BASE}/api/legislators.json", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def gazette_id_from_filename(filename: str) -> str | None:
    """'顏寬恒-2025-11-01-A0302-00421.json' → 'A0302-00421'"""
    m = re.search(r'(A\d{4}-\d{5})\.json$', filename)
    return m.group(1) if m else None


def declaration_date_from_filename(filename: str) -> str | None:
    m = re.search(r'-(\d{4}-\d{2}-\d{2})-A\d', filename)
    return m.group(1) if m else None


def download_pdf(gazette_id: str) -> Path | None:
    pdf_path = PDF_CACHE / f"{gazette_id}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 1000:
        return pdf_path
    url = f"{PDF_BASE}/{gazette_id}.pdf"
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 200 and "pdf" in r.headers.get("content-type", ""):
            pdf_path.write_bytes(r.content)
            return pdf_path
    except Exception as e:
        print(f"  [warn] download failed {gazette_id}: {e}", file=sys.stderr)
    return None


# ── PDF parsing ───────────────────────────────────────────────────────────────

# Patterns are matched line-by-line so they don't bleed across sections.
# Each tuple: (line-level regex, output key)
SECTION_TOTAL_RE = [
    (r'七[）\)].*?存款.*?總金額：新臺幣\s*([\d,]+)元', "deposits"),
    (r'六[）\)].*?現金.*?總金額：新臺幣\s*([\d,]+)元', "cash"),
    (r'八[）\)].*?有價證券.*?總價額：新臺幣\s*([\d,]+)元', "securities"),
    (r'1\.股票.*?總價額：新臺幣\s*([\d,]+)元', "stocks"),
    (r'2\.債券.*?總價額：新臺幣\s*([\d,]+)元', "bonds"),
    (r'3\.基金.*?總價額：新臺幣\s*([\d,]+)元', "funds"),
    (r'4\.其他有價證券.*?總價額：新臺幣\s*([\d,]+)元', "other_securities"),
    (r'珠寶.*?總價額：新臺幣\s*([\d,]+)元', "jewelry"),
    (r'十[）\)].*?債權.*?總金額：新臺幣\s*([\d,]+)元', "receivables"),
    (r'十一[）\)].*?債務.*?總金額：新臺幣\s*([\d,]+)元', "debts"),
    (r'十二[）\)].*?事業投資.*?總金額：新臺幣\s*([\d,]+)元', "investments"),
]

# Currency keywords used in deposits section
CURRENCIES = ("新臺幣", "美元", "日圓", "歐元", "英鎊", "港幣", "澳幣", "離岸人民幣", "人民幣")
# Account type keywords
ACCOUNT_TYPES = ("活期儲蓄存款", "定期儲蓄存款", "綜合存款", "定期存款", "活期存款",
                 "活儲證券戶", "活存證券戶", "定儲", "外幣存款", "保留款")


def extract_text_lines(pdf_path: Path) -> list[tuple[int, str]]:
    """Return list of (page_no, line_text), stripping the footer watermark."""
    lines = []
    footer_re = re.compile(r'監察院公報.*廉\s*政\s*專\s*刊')
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if footer_re.search(line):
                    continue
                lines.append((i + 1, line))
    return lines


def parse_totals(lines: list[tuple[int, str]]) -> dict:
    """Extract section-total amounts from headers like （總金額：新臺幣58,103,754元）."""
    totals = {}
    for _, line in lines:
        for pattern, key in SECTION_TOTAL_RE:
            if key in totals:
                continue
            m = re.search(pattern, line)
            if m:
                try:
                    totals[key] = ntd(m.group(1))
                except Exception:
                    pass
    return totals


def parse_deposits(lines: list[tuple[int, str]]) -> list[dict]:
    """
    Deposits format:
      {bank} {account_type} {currency} {owner} [{foreign_amount}] {ntd_amount}
    Some bank names wrap across lines, so we track the pending bank name.
    """
    # Find the deposit section
    in_section = False
    deposits = []
    pending_bank = ""
    deposit_section_re = re.compile(r'（七）存款')
    end_section_re = re.compile(r'（八）|（九）|（十）')

    currency_set = set(CURRENCIES)
    acct_re = re.compile(
        r'^(.+?)\s+(' + '|'.join(re.escape(a) for a in ACCOUNT_TYPES) + r')\s+(' +
        '|'.join(re.escape(c) for c in CURRENCIES) + r')\s+(\S+)\s+([\d,]+\.?\d*)(?:\s+([\d,]+\.?\d*))?'
    )
    # Pattern for the typical NTD-only line (no foreign amount)
    # Sometimes the account type is separated; handle by looking for lines
    # that begin with a known account type (wrapped bank-name case)
    wrapped_re = re.compile(
        r'^(' + '|'.join(re.escape(a) for a in ACCOUNT_TYPES) + r')\s+(' +
        '|'.join(re.escape(c) for c in CURRENCIES) + r')\s+(\S+)\s+([\d,]+\.?\d*)(?:\s+([\d,]+\.?\d*))?'
    )

    for _, line in lines:
        if not in_section:
            if deposit_section_re.search(line):
                in_section = True
            continue
        if end_section_re.search(line):
            break
        # Skip header rows
        if "存款種類" in line or "所有人" in line or "幣別" in line or "銀行" == line:
            continue

        # Try full match first (bank on same line)
        m = acct_re.match(line)
        if m:
            bank_name = m.group(1).strip()
            if pending_bank and not bank_name:
                bank_name = pending_bank
            acct_type = m.group(2)
            currency = m.group(3)
            owner = m.group(4)
            amount1 = m.group(5)
            amount2 = m.group(6)
            if currency == "新臺幣":
                ntd_amt = ntd(amount1)
                foreign_amt = None
            else:
                foreign_amt = float(amount1.replace(",", ""))
                ntd_amt = ntd(amount2) if amount2 else 0
            deposits.append({
                "bank": bank_name,
                "type": acct_type,
                "currency": currency,
                "owner": owner,
                "foreign": foreign_amt,
                "ntd": ntd_amt,
            })
            pending_bank = ""
            continue

        # Try wrapped match (bank name was on previous line)
        m = wrapped_re.match(line)
        if m and pending_bank:
            acct_type = m.group(1)
            currency = m.group(2)
            owner = m.group(3)
            amount1 = m.group(4)
            amount2 = m.group(5)
            if currency == "新臺幣":
                ntd_amt = ntd(amount1)
                foreign_amt = None
            else:
                foreign_amt = float(amount1.replace(",", ""))
                ntd_amt = ntd(amount2) if amount2 else 0
            deposits.append({
                "bank": pending_bank,
                "type": acct_type,
                "currency": currency,
                "owner": owner,
                "foreign": foreign_amt,
                "ntd": ntd_amt,
            })
            # The line after a wrapped row may be the tail of the bank name
            # — we keep pending_bank for one more iteration so the continuation
            # line (e.g. "平分社") can be collected if needed
            pending_bank = ""
            continue

        # Accumulate potential bank name fragment
        # (avoid treating continuation tails like "平分社" as a new bank)
        if not any(c in line for c in currency_set) and len(line) < 30:
            if pending_bank and not any(a in line for a in ACCOUNT_TYPES):
                # continuation fragment — discard (it came after the deposit row)
                pending_bank = ""
            else:
                pending_bank = line
        else:
            pending_bank = ""

    return deposits


def parse_debts(lines: list[tuple[int, str]]) -> list[dict]:
    """
    Debt rows are complex, but the header usually has the total.
    We also try to extract individual debt rows: creditor / amount / reason.
    Format is inconsistent across legislators so we do best-effort.
    """
    debts = []
    in_section = False
    debt_section_re = re.compile(r'（十一）債務')
    end_section_re = re.compile(r'（十二）|（十三）')
    # A row typically looks like: {type} {owner} {amount} {reason}
    # where amount is a large number like 70,000,000
    debt_row_re = re.compile(
        r'(授信|借款|抵押|貸款|透支|信用貸款|保證票據)\s+(\S+)\s+([\d,]+)\s*(.*)'
    )

    for _, line in lines:
        if not in_section:
            if debt_section_re.search(line):
                in_section = True
            continue
        if end_section_re.search(line):
            break
        m = debt_row_re.search(line)
        if m:
            try:
                debts.append({
                    "type": m.group(1),
                    "owner": m.group(2),
                    "amount": ntd(m.group(3)),
                    "note": m.group(4).strip(),
                })
            except Exception:
                pass
    return debts


def count_real_estate(lines: list[tuple[int, str]]) -> dict:
    """
    Count land parcels and buildings by counting ownership-fraction rows.
    Each data row (one parcel/building) has a share expression like 全部, N分之M, etc.
    """
    in_real_estate = False
    sub = None  # 'land' or 'building'
    land_count = 0
    building_count = 0
    # Ownership share pattern: 全部, 2分之1, 10分之5, 10000分之590, etc.
    share_re = re.compile(r'全部|\d+\s*分\s*之\s*\d+')
    skip_re = re.compile(r'所有權人|面積|土\s*地\s*坐|建\s*物\s*標|持\s*分|本欄空白')

    for _, line in lines:
        if "（二）不動產" in line:
            in_real_estate = True
            continue
        if not in_real_estate:
            continue
        if re.search(r'（三）|（四）|（五）', line):
            break
        if "1.土地" in line and "建物" not in line:
            sub = "land"
            continue
        if re.search(r'2\.建物', line):
            sub = "building"
            continue
        if skip_re.search(line):
            continue
        if sub and share_re.search(line):
            if sub == "land":
                land_count += 1
            else:
                building_count += 1

    return {"land": land_count, "building": building_count}


def parse_pdf(pdf_path: Path) -> dict:
    lines = extract_text_lines(pdf_path)
    return {
        "totals": parse_totals(lines),
        "deposits": parse_deposits(lines),
        "debts": parse_debts(lines),
        "real_estate": count_real_estate(lines),
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("Fetching legislator list from API…")
    legislators = get_api_legislators()
    print(f"  {len(legislators)} legislators found")

    results = []
    total = len(legislators)

    for idx, leg in enumerate(legislators):
        name = leg.get("name", "")
        meta = leg.get("meta", {}) or {}
        party = meta.get("party", "")
        district = leg.get("district", "")
        slug = leg.get("slug", "")

        # Find most recent declaration
        declarations = leg.get("declarations", []) or []
        if not declarations:
            print(f"[{idx+1}/{total}] {name} — no declarations, skip")
            results.append({
                "name": name, "party": party, "district": district,
                "slug": slug, "gazette_id": None,
                "totals": {}, "deposits": [], "debts": [], "real_estate": {},
            })
            continue

        # Use the first (most recent) declaration
        dec_filename = declarations[0] if isinstance(declarations[0], str) else declarations[0].get("filename", "")
        gazette_id = gazette_id_from_filename(dec_filename)
        dec_date = declaration_date_from_filename(dec_filename)

        if not gazette_id:
            print(f"[{idx+1}/{total}] {name} — cannot parse gazette ID from '{dec_filename}'")
            results.append({
                "name": name, "party": party, "district": district,
                "slug": slug, "gazette_id": None,
                "totals": {}, "deposits": [], "debts": [], "real_estate": {},
            })
            continue

        print(f"[{idx+1}/{total}] {name} ({party}) — {gazette_id}", end="", flush=True)

        pdf_path = download_pdf(gazette_id)
        if not pdf_path:
            print(" — PDF unavailable")
            results.append({
                "name": name, "party": party, "district": district,
                "slug": slug, "gazette_id": gazette_id, "declaration_date": dec_date,
                "totals": {}, "deposits": [], "debts": [], "real_estate": {},
            })
            time.sleep(0.5)
            continue

        try:
            parsed = parse_pdf(pdf_path)
            totals = parsed["totals"]
            print(f" — deposits:{totals.get('deposits',0):,} debts:{totals.get('debts',0):,}")
        except Exception as e:
            print(f" — parse error: {e}")
            parsed = {"totals": {}, "deposits": [], "debts": [], "real_estate": {}}

        results.append({
            "name": name,
            "party": party,
            "district": district,
            "slug": slug,
            "gazette_id": gazette_id,
            "declaration_date": dec_date,
            **parsed,
        })

        # polite rate limit
        time.sleep(0.3)

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. {len(results)} records → {OUT_PATH}")


if __name__ == "__main__":
    main()
