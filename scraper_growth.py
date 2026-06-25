#!/usr/bin/env python3
"""
Growth scraper: parse 2024 declarations for legislators who have both 2024 and 2025 filings.
Outputs data/legislators_growth.json for the wealth-change ranking feature.

Run: python3 scraper_growth.py
Output: data/legislators_growth.json
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
FULL_PATH = Path(__file__).parent / "data" / "legislators_full.json"
OUT_PATH = Path(__file__).parent / "data" / "legislators_growth.json"

PDF_CACHE.mkdir(parents=True, exist_ok=True)


def ntd(s: str) -> int:
    return round(float(s.replace(",", "").replace(" ", "").strip()))


def gazette_id_from_filename(filename: str):
    m = re.search(r'(A\d{4}-\d{5})\.json$', filename)
    return m.group(1) if m else None


def download_pdf(gazette_id: str):
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


SECTION_TOTAL_RE = [
    (r'七[）\)].*?存款.*?總金額：新臺幣\s*([\d,]+)元', "deposits"),
    (r'六[）\)].*?現金.*?總金額：新臺幣\s*([\d,]+)元', "cash"),
    (r'八[）\)].*?有價證券.*?總價額：新臺幣\s*([\d,]+)元', "securities"),
    (r'1\.股票.*?總價額：新臺幣\s*([\d,]+)元', "stocks"),
    (r'2\.債券.*?總價額：新臺幣\s*([\d,]+)元', "bonds"),
    (r'3\.基金.*?總價額：新臺幣\s*([\d,]+)元', "funds"),
    (r'珠寶.*?總價額：新臺幣\s*([\d,]+)元', "jewelry"),
    (r'十一[）\)].*?債務.*?總金額：新臺幣\s*([\d,]+)元', "debts"),
    (r'十二[）\)].*?事業投資.*?總金額：新臺幣\s*([\d,]+)元', "investments"),
]


def extract_text_lines(pdf_path):
    lines = []
    footer_re = re.compile(r'監察院公報.*廉\s*政\s*專\s*刊')
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line or footer_re.search(line):
                    continue
                lines.append((i + 1, line))
    return lines


def parse_totals(lines):
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


def liquidity(totals):
    """deposits + securities - debts as a proxy for liquid net worth."""
    return (totals.get("deposits", 0) + totals.get("securities", 0)
            - totals.get("debts", 0))


def main():
    # Load existing 2025 data
    if not FULL_PATH.exists():
        print("ERROR: data/legislators_full.json not found — run scraper.py first", file=sys.stderr)
        sys.exit(1)
    full_2025 = {r["slug"]: r for r in json.loads(FULL_PATH.read_text())}
    print(f"Loaded {len(full_2025)} 2025 records from {FULL_PATH.name}")

    # Fetch API for legislators with both 2024 and 2025 declarations
    r = requests.get(f"{API_BASE}/api/legislators.json", headers=HEADERS, timeout=20)
    r.raise_for_status()
    all_legs = r.json()

    targets = []
    for lg in all_legs:
        decs = lg.get("declarations", []) or []
        dec_2024 = next((d for d in decs if "-2024-" in d), None)
        dec_2025 = next((d for d in decs if "-2025-" in d), None)
        if dec_2024 and dec_2025:
            gid2024 = gazette_id_from_filename(dec_2024)
            if gid2024:
                targets.append({
                    "name": lg["name"],
                    "slug": lg.get("slug", ""),
                    "meta": (lg.get("meta") or {}),
                    "gid2024": gid2024,
                    "gid2025": gazette_id_from_filename(dec_2025),
                })

    print(f"{len(targets)} legislators with both 2024 and 2025 filings")

    results = []
    for idx, t in enumerate(targets):
        name = t["name"]
        slug = t["slug"]
        party = t["meta"].get("party", "")
        gid2024 = t["gid2024"]
        gid2025 = t["gid2025"]

        print(f"[{idx+1}/{len(targets)}] {name} ({party}) — 2024={gid2024}", end="", flush=True)

        # Get 2025 totals from existing data
        rec2025 = full_2025.get(slug, {})
        totals2025 = rec2025.get("totals", {})

        # Scrape 2024 PDF
        pdf_path = download_pdf(gid2024)
        if not pdf_path:
            print(" — PDF unavailable")
            continue

        try:
            lines = extract_text_lines(pdf_path)
            totals2024 = parse_totals(lines)
            liq2024 = liquidity(totals2024)
            liq2025 = liquidity(totals2025)
            delta = liq2025 - liq2024
            print(f" — liq2024:{liq2024:+,} → liq2025:{liq2025:+,} Δ{delta:+,}")
        except Exception as e:
            print(f" — parse error: {e}")
            totals2024 = {}
            delta = None

        results.append({
            "name": name,
            "party": party,
            "slug": slug,
            "gid2024": gid2024,
            "gid2025": gid2025,
            "totals2024": totals2024,
            "totals2025": totals2025,
            "delta": {
                "deposits": totals2025.get("deposits", 0) - totals2024.get("deposits", 0),
                "securities": totals2025.get("securities", 0) - totals2024.get("securities", 0),
                "debts": totals2025.get("debts", 0) - totals2024.get("debts", 0),
                "liquidity": (liquidity(totals2025) - liquidity(totals2024))
                              if totals2024 else None,
            },
        })

        time.sleep(0.3)

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. {len(results)} records → {OUT_PATH}")

    # Quick summary
    ranked = [r for r in results if r["delta"]["liquidity"] is not None]
    ranked.sort(key=lambda x: x["delta"]["liquidity"], reverse=True)
    print("\n=== 流動性財富成長 TOP 10 ===")
    for i, r in enumerate(ranked[:10]):
        print(f"  {i+1}. {r['name']}（{r['party']}）: {r['delta']['liquidity']:+,}")
    print("\n=== 流動性財富縮減 TOP 10 ===")
    for i, r in enumerate(ranked[-10:][::-1]):
        print(f"  {i+1}. {r['name']}（{r['party']}）: {r['delta']['liquidity']:+,}")


if __name__ == "__main__":
    main()
