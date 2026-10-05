# common/excel_utils.py

import os
import re
import logging
from typing import List, Dict, Tuple, Any
from collections import defaultdict

import pandas as pd
from openpyxl import load_workbook
from shutil import copyfile

from common.config import TEMPLATE_PATH, TEMPLATE_SHEET
from common.normalize import normalize_row  # <-- NEW
from common.storage import atomic_output
from functools import lru_cache
from copy import copy
from openpyxl.worksheet.cell_range import CellRange

logger = logging.getLogger(__name__)

# If any of your OUTPUT_COLUMNS differ slightly from the template header text,
# map them here: left = your key, right = template header text.
ALIASES: Dict[str, str] = {
    # "Are-there-multiple_locations": "Are-there-multiple-locations",
}

# Keys that should be written as TEXT (to match strict dropdown/enums and avoid 3.0 issues)
TEXT_CONSTRAINED_KEYS_PREFIXES = (
    "Age-groups.",
    "Competition-level.",
)

TEXT_CONSTRAINED_KEYS_EXACT = {
    "Guaranteed-games",
    "Tournament-type",
    "Tournament-surface",
    "Hotel-required",
    "Are-there-multiple_locations",
}

def _norm(s: str) -> str:
    """Normalize header text for tolerant matching (hyphen/underscore, case, spaces, trailing period)."""
    if not isinstance(s, str):
        return ""
    s = s.strip()
    s = s.replace("_", "-")
    s = " ".join(s.split())
    if s.endswith("."):
        s = s[:-1]
    return s.lower()

def _should_write_as_text(key: str) -> bool:
    if key in TEXT_CONSTRAINED_KEYS_EXACT:
        return True
    return any(key.startswith(p) for p in TEXT_CONSTRAINED_KEYS_PREFIXES)

# --------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------

def create_workbook_from_template(output_file: str) -> None:
    """Copy the XLSX template to the output path (preserves formatting/validations/tables)."""
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with atomic_output(output_file, backup=False) as staged:
        copyfile(TEMPLATE_PATH, staged)
    logger.info(f"[Excel] Copied template to {output_file}")


def validate_template(column_order):
    """Fail before network/extraction work if the import contract is incompatible."""
    stat = os.stat(TEMPLATE_PATH)
    _validate_template(os.path.abspath(TEMPLATE_PATH), stat.st_mtime_ns, stat.st_size, tuple(column_order))


@lru_cache(maxsize=4)
def _validate_template(path, modified, size, column_order):
    wb = load_workbook(path)
    try:
        if TEMPLATE_SHEET not in wb.sheetnames:
            raise ValueError(f"Template is missing required sheet {TEMPLATE_SHEET!r}")
        _, headers, hits = _find_header_row(wb[TEMPLATE_SHEET], list(column_order))
        maps = _build_template_header_maps(headers)
        missing = [key for key in column_order if not _resolve_template_col(key, *maps)]
        if not hits or missing:
            raise ValueError(f"Template is missing required headers: {missing}")
    finally:
        wb.close()

def _find_header_row(ws, expected_cols: List[str], search_rows: int = 10) -> Tuple[int, List[str], int]:
    """
    Find the header row by maximum overlap with expected column names.
    Returns (row_index, header_values, hit_count).
    """
    best_row = None
    best_hits = -1
    best_headers = None
    expected_norm = {_norm(c) for c in expected_cols}

    for r in range(1, search_rows + 1):
        values = [c.value if c.value is not None else "" for c in ws[r]]
        hits = sum(1 for v in values if _norm(v) in expected_norm)
        if hits > best_hits:
            best_hits = hits
            best_row = r
            best_headers = values

    return best_row, (best_headers or []), best_hits

def _build_template_header_maps(headers: List[str]):
    """
    Build lookup maps for headers, including support for repeated names.
    Returns: exact, by_name, exact_norm, by_name_norm
    """
    exact = {}
    by_name = defaultdict(list)
    exact_norm = {}
    by_name_norm = defaultdict(list)

    for idx, name in enumerate(headers, start=1):
        if not name:
            continue
        exact.setdefault(name, idx)
        by_name[name].append(idx)

        n = _norm(name)
        exact_norm.setdefault(n, idx)
        by_name_norm[n].append(idx)

    return exact, by_name, exact_norm, by_name_norm

def _resolve_template_col(
    name: str,
    exact: Dict[str, int],
    by_name: Dict[str, List[int]],
    exact_norm: Dict[str, int],
    by_name_norm: Dict[str, List[int]],
) -> int:
    """
    Resolve an OUTPUT_COLUMNS key to a concrete template column index.
    Strategy:
      0) apply ALIASES
      1) exact match
      2) 'Base.N' -> Nth occurrence of Base (exact), else Nth of normalized(Base)
      3) normalized exact match
    Returns 0 if not found.
    """
    name = ALIASES.get(name, name)

    c = exact.get(name)
    if c:
        return c

    if "." in name:
        base, suffix = name.rsplit(".", 1)
        if suffix.isdigit():
            n = int(suffix)
            cols = by_name.get(base)
            if cols and 1 <= n <= len(cols):
                return cols[n - 1]
            cols_n = by_name_norm.get(_norm(base))
            if cols_n and 1 <= n <= len(cols_n):
                return cols_n[n - 1]

    return exact_norm.get(_norm(name), 0)

def _get_table_bounds(ws):
    """
    If a table exists, return (start_col_letters, end_col_letters, current_bottom_row).
    Otherwise return None.
    """
    if not ws._tables:
        return None
    tbl = next(iter(ws.tables.values()))
    start_cell, end_cell = tbl.ref.split(":")
    start_col = "".join(filter(str.isalpha, start_cell))
    end_col = "".join(filter(str.isalpha, end_cell))
    current_bottom = int("".join(filter(str.isdigit, end_cell)))
    return start_col, end_col, current_bottom

def _first_resolved_column_index(
    column_order: List[str],
    exact: Dict[str, int],
    by_name: Dict[str, List[int]],
    exact_norm: Dict[str, int],
    by_name_norm: Dict[str, List[int]],
) -> int:
    """Pick a real column index to probe for the first empty row."""
    for key in column_order:
        c = _resolve_template_col(key, exact, by_name, exact_norm, by_name_norm)
        if c:
            return c
    return 1  # fallback

def _find_first_empty_row(ws, header_row_idx: int, key_col: int) -> int:
    """
    Scan downward from the first data row until a truly empty cell is found in key_col.
    Ignores ws.max_row inflation from formatting.
    """
    r = header_row_idx + 1
    hard_stop = max(ws.max_row + 5000, r + 5000)  # safety
    while r <= hard_stop:
        v = ws.cell(r, key_col).value
        if v is None or v == "":
            return r
        r += 1
    return r

# --------------------------------------------------------------------
# Normalization helpers (NEW)
# --------------------------------------------------------------------

def _preflight_log_dropdown_issues(original: Dict[str, Any], normalized: Dict[str, Any]) -> None:
    """
    Warn if a dropdown-constrained field becomes empty after normalization.
    This helps catch bad scraped tokens early.
    """
    # Age groups
    for k, v in original.items():
        if k.startswith("Age-groups."):
            before = str(v).strip() if v is not None else ""
            after = str(normalized.get(k, "")).strip()
            if before and not after:
                logger.warning("[Normalize] Age group '%s' -> '' for key %s", before, k)

        if k.startswith("Competition-level."):
            before = str(v).strip() if v is not None else ""
            after = str(normalized.get(k, "")).strip()
            if before and not after:
                logger.warning("[Normalize] Comp level '%s' -> '' for key %s", before, k)

    # Guaranteed games: we expect it to always normalize to a token (usually 'None' or digit)
    if "Guaranteed-games" in original:
        before = str(original.get("Guaranteed-games") or "").strip()
        after = str(normalized.get("Guaranteed-games") or "").strip()
        if before and not after:
            logger.warning("[Normalize] Guaranteed-games '%s' -> ''", before)

# --------------------------------------------------------------------
# Main appender (values only; template supplies formatting/DV/table)
# --------------------------------------------------------------------

def append_rows_to_template(
    output_file: str,
    rows: List[Dict],
    column_order: List[str],
    event_keys: List[str] | None = None,
) -> None:
    """
    Append dict rows (keys = OUTPUT_COLUMNS) into the template sheet.

    - Detect header row dynamically.
    - Map numbered columns (e.g., 'Age-groups.3') to repeated headers.
    - Find the first truly empty row under the header.
    - Write VALUES ONLY (styles/data validation come from pre-formatted template rows).
    - Normalize dropdown-constrained fields to canonical tokens before writing (NEW).
    - Force dropdown-constrained fields to be written as TEXT to avoid 3.0 vs "3" mismatches (NEW).
    - Expand the first table to include newly written rows (if a table exists).
    """
    if not rows:
        logger.info("[Excel] No rows to append for %s", output_file)
        return

    if event_keys is not None and (len(event_keys) != len(rows) or any(not k for k in event_keys)):
        raise ValueError("Each exported row requires a nonempty event identity.")
    wb = load_workbook(output_file, data_only=False)
    try:
        _write_rows(wb, output_file, rows, column_order, event_keys)
    finally:
        wb.close()


def _write_rows(wb, output_file, rows, column_order, event_keys):
    if TEMPLATE_SHEET not in wb.sheetnames:
        ws = wb[wb.sheetnames[0]]
        logger.warning(
            "Configured TEMPLATE_SHEET '%s' not found in '%s'. Falling back to first sheet: '%s'.",
            TEMPLATE_SHEET, output_file, ws.title
        )
    else:
        ws = wb[TEMPLATE_SHEET]
    template_bottom = ws.max_row

    # 1) Header row + maps
    header_row_idx, headers, hits = _find_header_row(ws, column_order, search_rows=10)
    if not header_row_idx or hits == 0:
        raise ValueError("Could not locate a header row that matches template columns.")
    headers = [h if isinstance(h, str) else "" for h in headers]
    exact, by_name, exact_norm, by_name_norm = _build_template_header_maps(headers)

    # Log coverage
    resolved = sum(
        1 for name in column_order
        if _resolve_template_col(name, exact, by_name, exact_norm, by_name_norm)
    )
    logger.info("[Excel] Resolved %d/%d headers into template columns.", resolved, len(column_order))
    if resolved != len(column_order):
        raise ValueError(f"Template resolves only {resolved}/{len(column_order)} required columns.")

    # 2) First truly empty row under header
    key_col = _first_resolved_column_index(column_order, exact, by_name, exact_norm, by_name_norm)
    occupied_rows = [r for r in range(header_row_idx + 1, ws.max_row + 1)
                     if any(ws.cell(r, c).value not in (None, "") for c in range(1, ws.max_column + 1))]
    next_row = max(occupied_rows, default=header_row_idx) + 1
    column_map = {key: _resolve_template_col(key, exact, by_name, exact_norm, by_name_norm)
                  for key in column_order}
    url_col = column_map.get("Website-url")
    index = None
    indexed = {}
    index_rows = {}
    indexed_data_rows = set()
    if event_keys is not None:
        index = wb["_ScraperExportIndex"] if "_ScraperExportIndex" in wb.sheetnames else wb.create_sheet("_ScraperExportIndex")
        index.sheet_state = "hidden"
        if index.cell(1, 1).value is None:
            for c, value in enumerate(("Event identity", "Import row", "Website URL", "Tournament name"), 1):
                index.cell(1, c).value = value
        for index_row in range(2, index.max_row + 1):
            identity, data_row, previous_url = [index.cell(index_row, c).value for c in range(1, 4)]
            if not identity:
                continue
            if not isinstance(data_row, int) or data_row <= header_row_idx:
                raise ValueError("Invalid export index row; restore a valid workbook before replay.")
            if url_col and ws.cell(data_row, url_col).value != previous_url:
                raise ValueError("Export index disagrees with the import sheet; review edited/sorted rows before replay.")
            previous_name = index.cell(index_row, 4).value
            if previous_name is not None and ws.cell(data_row, key_col).value != previous_name:
                raise ValueError("Export index disagrees with tournament names; review edited/sorted rows before replay.")
            indexed[str(identity)] = data_row
            index_rows[str(identity)] = index_row
            indexed_data_rows.add(data_row)
    legacy_urls = {}
    if url_col:
        for data_row in occupied_rows:
            value = ws.cell(data_row, url_col).value
            if value and data_row not in indexed_data_rows:
                legacy_urls.setdefault(str(value), data_row)

    # 3) Table bounds (for expansion)
    tbl_bounds = _get_table_bounds(ws)
    if tbl_bounds:
        start_col, end_col, current_bottom = tbl_bounds
    else:
        start_col, end_col, current_bottom = "A", None, header_row_idx

    # 4) Normalize + write values only
    rptr = next_row
    written_rows = []
    for row_number, row in enumerate(rows):
        normed = normalize_row(row)
        _preflight_log_dropdown_issues(row, normed)
        identity = event_keys[row_number] if event_keys is not None else None
        target_row = indexed.get(identity) if identity else None
        if target_row is None and identity:
            target_row = legacy_urls.pop(str(normed.get("Website-url", "")), None)
        if target_row is None:
            target_row = rptr
            rptr += 1

        for key in column_order:
            col_idx = column_map[key]
            if not col_idx:
                continue

            value = normed.get(key, "")

            cell = ws.cell(target_row, col_idx)
            if target_row > template_bottom:
                cell._style = copy(ws.cell(header_row_idx + 1, col_idx)._style)

            # Force TEXT for dropdown-constrained fields (prevents 3.0, type coercion issues)
            if _should_write_as_text(key):
                cell.number_format = "@"
                # Always write as string (including None -> "")
                cell.value = "" if value is None else str(value)
            else:
                cell.value = value if value is not None else ""

        written_rows.append(target_row)
        if identity:
            indexed[identity] = target_row
            index_row = index_rows.get(identity)
            if index_row is None:
                index_row = index.max_row + 1
                index_rows[identity] = index_row
            for c, value in enumerate((identity, target_row, normed.get("Website-url", ""), normed.get(column_order[0], "")), 1):
                index.cell(index_row, c).value = value

    last_written_row = max(written_rows)
    if last_written_row > template_bottom:
        for validation in ws.data_validations.dataValidation:
            for original in list(validation.ranges.ranges):
                if original.min_row == header_row_idx + 1 and original.max_row >= template_bottom:
                    expanded = CellRange(str(original))
                    expanded.max_row = last_written_row
                    validation.ranges.remove(original)
                    validation.add(expanded)
    logger.info("[Excel] Wrote rows %d..%d (key col %d)", next_row, last_written_row, key_col)

    # 5) Expand table if needed (banded rows etc. will extend automatically)
    if tbl_bounds and last_written_row > current_bottom:
        next(iter(ws.tables.values())).ref = f"{start_col}{header_row_idx}:{end_col}{last_written_row}"

    # 6) Save
    with atomic_output(output_file) as staged:
        wb.save(staged)
    logger.info("[Excel] Appended %d rows. New bottom row: %d.", len(rows), last_written_row)

# --------------------------------------------------------------------
# File naming & consolidation
# --------------------------------------------------------------------

def get_output_filename(location: str, base_folder: str = "Sheets", run_date: str | None = None) -> str:
    """
    Generate a location-specific Excel file name with the current date.
    If the location is 'Region (Org)', create Sheets/<date>/<Org>/Region_<date>.xlsx
    """
    from datetime import datetime
    date_str = run_date or datetime.now().strftime("%Y-%m-%d")

    match = re.match(r'^(.*?)\s*\((.*?)\)\s*$', location.strip())
    if match:
        region = match.group(1).strip()
        org = match.group(2).strip()
    else:
        region = location.strip()
        org = "DefaultOrg"

    region_clean = re.sub(r'[^\w-]', '_', region)
    org_clean = re.sub(r'[^\w-]', '_', org)

    os.makedirs(base_folder, exist_ok=True)

    date_folder = os.path.join(base_folder, date_str)
    os.makedirs(date_folder, exist_ok=True)

    org_folder = os.path.join(date_folder, org_clean)
    os.makedirs(org_folder, exist_ok=True)

    filename = f"{region_clean}_{date_str}.xlsx"
    return os.path.join(org_folder, filename)


def write_snapshot(output_file, rows, column_order, event_keys):
    """Replace a location snapshot from canonical rows, removing stale output rows."""
    wb = load_workbook(TEMPLATE_PATH, data_only=False)
    try:
        if rows:
            _write_rows(wb, output_file, rows, column_order, event_keys)
        else:
            with atomic_output(output_file) as staged:
                wb.save(staged)
    finally:
        wb.close()

def consolidate_org_files(org_folder: str, org: str, date_str: str) -> str:
    """
    Consolidate all Excel files in org_folder into one file saved in the parent (date) folder:
      <org>_consolidation_<date_str>.xlsx
    """
    xlsx_files = [f for f in os.listdir(org_folder) if f.endswith('.xlsx')]
    if not xlsx_files:
        logger.info(f"No Excel files found in {org_folder}. Nothing to consolidate.")
        return ""

    consolidated_df = pd.DataFrame()
    for file in xlsx_files:
        file_path = os.path.join(org_folder, file)
        try:
            df = pd.read_excel(file_path, sheet_name=TEMPLATE_SHEET if TEMPLATE_SHEET else 0, header=1)
            consolidated_df = pd.concat([consolidated_df, df], ignore_index=True)
        except Exception as e:
            logger.warning(f"Failed to read {file_path}: {e}")

    if consolidated_df.empty:
        logger.info(f"No data consolidated from {org_folder}.")
        return ""

    parent_folder = os.path.dirname(org_folder)
    output_file = os.path.join(parent_folder, f"{org}_consolidation_{date_str}.xlsx")
    consolidated_df.to_excel(output_file, sheet_name=org, index=False)
    logger.info(f"Consolidation complete for {org}. Output: {output_file}")
    return output_file
