"""Convert DAT's KMA market-area workbook into the CSVs that import_kma.py loads.

Developer tool, not part of the deployed service: it runs once when DAT reissues
the workbook, and its output (data/*.csv) is committed. That is why it reads the
xlsx with the standard library instead of adding openpyxl to requirements.txt,
which deploy.sh would then install into both systemd units for an annual task.

    python tools/convert_kma_xlsx.py [xlsx_path] [--out-dir data]

Two things about this workbook will silently corrupt the data if unhandled, so
they are asserted rather than trusted:

1. Excel stores prefixes as a mix of shared strings ("010") and numbers (60)
   *in the same column*, and the numeric ones have lost their leading zeros.
   Un-padded, the entire Northeast fails to map and nothing errors.
2. Canadian prefixes are postal-code stubs ("L4", "V0M"), not digits. Padding
   one with zfill produces "0L4" - junk that looks like data.
"""

import argparse
import csv
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_XLSX = r"C:\Users\MukundAgarwal\Downloads\KMA Zip RateView 3.0 (1).xlsx"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Sheet order in the workbook. Sheet 2 ("Market map") is a picture, not data.
SHEET_MARKETS = 1
SHEET_EXPANDED = 3
SHEET_REGIONS = 4

MARKET_ID_RE = re.compile(r"^[A-Z]{2}_[A-Z]{3}$")
CA_PREFIX_RE = re.compile(r"^[A-Z]\d[A-Z]?$")
CA_REGION_PREFIX_RE = re.compile(r"^[A-Z]$")

CA_PROVINCES = {"AB", "BC", "MB", "NB", "NF", "NL", "NS", "NT", "NU", "ON",
                "PE", "PQ", "QC", "SK", "YT"}

# Expected shape of the 2024 workbook. If DAT reissues and these move, look at
# the diff before relaxing them - a changed count is exactly what we want to see.
EXPECT_MARKETS = 149
EXPECT_US_PREFIXES = 905
EXPECT_CA_PREFIXES = 227
EXPECT_REGIONS = 18

# Prefixes whose leading zero Excel drops. Their presence proves the padding ran.
LEADING_ZERO_CANARY = {"005", "010", "060"}


# ---------------------------------------------------------------------------
# xlsx reading
# ---------------------------------------------------------------------------

def read_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(xml)
    # A string can be split across several <t> runs when parts are styled.
    return ["".join(t.text or "" for t in si.iter(f"{NS}t")) for si in root.iter(f"{NS}si")]


def read_sheet(zf: zipfile.ZipFile, sheet_index: int, shared: List[str]) -> List[List[str]]:
    """Return the sheet as a list of rows of cell text, blanks as empty strings.

    Rows are ragged: trailing empty cells are simply absent, and so are gaps in
    the middle, so cells are placed by their column reference rather than by
    their position in the XML.
    """
    root = ElementTree.fromstring(zf.read(f"xl/worksheets/sheet{sheet_index}.xml"))
    rows: List[List[str]] = []

    for row in root.iter(f"{NS}row"):
        cells: Dict[int, str] = {}
        for cell in row.iter(f"{NS}c"):
            ref = cell.get("r") or ""
            col = _column_index(ref)
            if col is None:
                continue

            if cell.get("t") == "s":
                value_el = cell.find(f"{NS}v")
                if value_el is None or value_el.text is None:
                    continue
                idx = int(value_el.text)
                text = shared[idx] if 0 <= idx < len(shared) else ""
            elif cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{NS}t"))
            else:
                # Numeric, or a formula whose cached <v> we read rather than
                # evaluate. Formula cells are how the sheet's footer rows look.
                value_el = cell.find(f"{NS}v")
                text = (value_el.text or "") if value_el is not None else ""

            cells[col] = text.strip()

        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])

    return rows


def _column_index(ref: str) -> Optional[int]:
    """'AB12' -> 27 (zero-based column number)."""
    letters = "".join(c for c in ref if c.isalpha())
    if not letters:
        return None
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


# ---------------------------------------------------------------------------
# Cell interpretation
# ---------------------------------------------------------------------------

def normalize_prefix_cell(cell: str) -> Optional[Tuple[str, str]]:
    """A prefix cell -> (prefix, country), or None if it isn't a prefix at all.

    US prefixes are the first three ZIP digits, zero-padded because Excel drops
    the leading zero on the numeric ones. Canadian prefixes are postal-code
    stubs of two or three characters and must never be padded.
    """
    value = (cell or "").strip().upper()
    if not value:
        return None
    if value.isdigit():
        return (value.zfill(3), "US") if len(value) <= 3 else None
    if CA_PREFIX_RE.match(value):
        return value, "CA"
    return None


def normalize_region_cell(cell: str) -> Optional[Tuple[str, str]]:
    """As normalize_prefix_cell, but the Regions sheet is coarser.

    Its three Canadian regions are listed by the single leading letter of the
    postal code ("A", "G", "V") rather than by a two- or three-character stub.
    """
    value = (cell or "").strip().upper()
    if CA_REGION_PREFIX_RE.match(value):
        return value, "CA"
    return normalize_prefix_cell(value)


def _iter_prefix_cells(row: List[str], start_col: int, normalizer=normalize_prefix_cell):
    """Prefix columns run from start_col to the end of a ragged row."""
    for cell in row[start_col:]:
        parsed = normalizer(cell)
        if parsed:
            yield parsed


# ---------------------------------------------------------------------------
# Sheet parsers
# ---------------------------------------------------------------------------

def parse_markets(rows: List[List[str]]) -> Tuple[List[dict], List[dict]]:
    """Sheet 1: market definitions plus their own prefixes.

    Columns: A count, B ref city, C ref state, D market id, E name, F.. prefixes.

    The sheet ends with a 'US Zips Count' footer whose column D holds a formula,
    so a row is only accepted when column A is an integer AND column D looks
    like a market id. Filtering on "column D is non-empty" lets the footer in.
    """
    markets: List[dict] = []
    prefixes: List[dict] = []

    for row in rows:
        if len(row) < 5:
            continue
        count, ref_city, ref_state, market_id, name = (row[0], row[1], row[2], row[3], row[4])
        if not count.strip().isdigit() or not MARKET_ID_RE.match(market_id.strip()):
            continue

        market_id = market_id.strip()
        ref_state = ref_state.strip().upper()
        markets.append({
            "market_id": market_id,
            "market_name": (name or market_id).replace(" Mkt", "").strip(),
            "ref_city": ref_city.strip(),
            "ref_state": ref_state,
            "country": "CA" if ref_state in CA_PROVINCES else "US",
        })

        for prefix, country in _iter_prefix_cells(row, 5):
            prefixes.append({"prefix": prefix, "country": country, "market_id": market_id})

    return markets, prefixes


def parse_expanded(rows: List[List[str]]) -> List[dict]:
    """Sheet 3: each market's expanded 'X-Mkt' geography - itself plus neighbours.

    Market ids carry a '+' suffix here (AL_BIR+) which is stripped so the rows
    join to kma_markets. Unused by the current pipeline; loaded because it is
    free now and market-adjacency matching will need it.
    """
    out: List[dict] = []
    for row in rows:
        if len(row) < 5:
            continue
        market_id = row[3].strip().rstrip("+")
        if not row[0].strip().isdigit() or not MARKET_ID_RE.match(market_id):
            continue
        for prefix, _country in _iter_prefix_cells(row, 5):
            out.append({"market_id": market_id, "prefix": prefix})
    return out


def parse_regions(rows: List[List[str]]) -> List[dict]:
    """Sheet 4: 18 macro regions. Columns A name, B states, C ranges, D.. prefixes.

    Trailing rows are prose footnotes with no prefixes; they fall out naturally
    because nothing in them parses as a prefix.
    """
    out: List[dict] = []
    for row in rows:
        if len(row) < 4:
            continue
        region = row[0].strip()
        if not region or not row[1].strip():
            continue
        for prefix, _country in _iter_prefix_cells(row, 3, normalize_region_cell):
            out.append({"region_name": region, "prefix": prefix})
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(markets: List[dict], prefixes: List[dict],
             expanded: List[dict], regions: List[dict]) -> None:
    """Fail loudly before writing anything, so a bad parse never reaches CSV."""
    problems: List[str] = []

    if len(markets) != EXPECT_MARKETS:
        problems.append(f"expected {EXPECT_MARKETS} markets, parsed {len(markets)}")

    ids = [m["market_id"] for m in markets]
    if len(set(ids)) != len(ids):
        problems.append("duplicate market ids")

    us = {p["prefix"] for p in prefixes if p["country"] == "US"}
    ca = {p["prefix"] for p in prefixes if p["country"] == "CA"}
    if len(us) != EXPECT_US_PREFIXES:
        problems.append(f"expected {EXPECT_US_PREFIXES} US prefixes, parsed {len(us)}")
    if len(ca) != EXPECT_CA_PREFIXES:
        problems.append(f"expected {EXPECT_CA_PREFIXES} CA prefixes, parsed {len(ca)}")

    if bad := sorted(p for p in us if not re.fullmatch(r"\d{3}", p)):
        problems.append(f"malformed US prefixes: {bad[:10]}")
    if bad := sorted(p for p in ca if not CA_PREFIX_RE.match(p)):
        problems.append(f"malformed CA prefixes: {bad[:10]}")

    if missing := sorted(LEADING_ZERO_CANARY - us):
        problems.append(f"leading-zero padding did not run, missing {missing}")

    owner: Dict[str, str] = {}
    for p in prefixes:
        prior = owner.get(p["prefix"])
        if prior and prior != p["market_id"]:
            problems.append(f"prefix {p['prefix']} claimed by {prior} and {p['market_id']}")
        owner[p["prefix"]] = p["market_id"]

    # Canadian lookup falls back from a 3-character key to its 2-character
    # parent, so the two must never point at different markets.
    for prefix in ca:
        if len(prefix) == 3 and (parent := owner.get(prefix[:2])) and parent != owner[prefix]:
            problems.append(f"CA prefix {prefix} ({owner[prefix]}) conflicts with {prefix[:2]} ({parent})")

    known = set(ids)
    if orphans := {e["market_id"] for e in expanded} - known:
        problems.append(f"expanded rows reference unknown markets: {sorted(orphans)[:5]}")

    region_count = len({r["region_name"] for r in regions})
    if region_count != EXPECT_REGIONS:
        problems.append(f"expected {EXPECT_REGIONS} regions, parsed {region_count}")

    if problems:
        for p in problems:
            logger.error("VALIDATION FAILED | %s", p)
        sys.exit(1)


# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: List[dict], fieldnames: List[str]) -> None:
    seen = set()
    unique = []
    for row in rows:
        key = tuple(row[f] for f in fieldnames[:2])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)
    logger.info("Wrote | file=%s rows=%d", path.name, len(unique))


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert the DAT KMA workbook to CSV")
    parser.add_argument("xlsx_path", nargs="?", default=DEFAULT_XLSX)
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Reading workbook | path=%s", args.xlsx_path)
    with zipfile.ZipFile(args.xlsx_path) as zf:
        shared = read_shared_strings(zf)
        markets, prefixes = parse_markets(read_sheet(zf, SHEET_MARKETS, shared))
        expanded = parse_expanded(read_sheet(zf, SHEET_EXPANDED, shared))
        regions = parse_regions(read_sheet(zf, SHEET_REGIONS, shared))

    logger.info(
        "Parsed | markets=%d prefixes=%d expanded=%d regions=%d",
        len(markets), len(prefixes), len(expanded),
        len({r["region_name"] for r in regions}),
    )
    validate(markets, prefixes, expanded, regions)

    write_csv(out_dir / "kma_markets.csv", markets,
              ["market_id", "market_name", "ref_city", "ref_state", "country"])
    write_csv(out_dir / "kma_postal_prefixes.csv", prefixes,
              ["prefix", "country", "market_id"])
    write_csv(out_dir / "kma_expanded_prefixes.csv", expanded,
              ["market_id", "prefix"])
    write_csv(out_dir / "kma_regions.csv", regions,
              ["region_name", "prefix"])

    logger.info("Done | out_dir=%s", out_dir)
    logger.info("Do NOT open these CSVs in Excel and re-save - it strips the leading zeros back out.")


if __name__ == "__main__":
    main()
