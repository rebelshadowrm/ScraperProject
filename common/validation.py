# common/validation.py

import re
from datetime import datetime
from typing import Dict, Tuple
from urllib.parse import urlparse

from common.normalize import normalize_required_address

_PLACEHOLDER_STRINGS = {
    "", "N/A", "NA", "NONE", "NULL", "-", "--", "NIL", "NOT AVAILABLE", "NOTAPPLICABLE", "NOT APPLICABLE",
}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _normalize_optional_str(value) -> str:
    """
    Normalize optional string fields:
    - None -> ""
    - Trim whitespace
    - Common placeholders ("N/A", "-", etc.) -> ""
    """
    if value is None:
        return ""
    v = str(value).strip()
    if v.upper() in _PLACEHOLDER_STRINGS:
        return ""
    return v


def normalize_zip(zip_value) -> str:
    """
    Zip is OPTIONAL.
    - If missing/placeholder/unusable -> ""
    - If usable -> return 5-digit ZIP as a string

    Examples:
      "N/A" -> ""
      "" -> ""
      "75201" -> "75201"
      "TX 75201" -> "75201"  (extracts 5-digit ZIP if present)
      "75201-1234" -> "75201" (keeps first 5)
      "ABCDE" -> ""
    """
    z = _normalize_optional_str(zip_value)
    if not z:
        return ""

    # Extract candidates
    digits = re.sub(r"\D", "", z)

    # Zip+4 -> keep first 5
    if len(digits) >= 5:
        first5 = digits[:5]
        if first5.isdigit() and len(first5) == 5:
            return first5

    return ""


def validate_email(email: str) -> bool:
    """Basic email format check (keeps only clearly-valid emails)."""
    return bool(_EMAIL_RE.fullmatch((email or "").strip()))


def normalize_email(email_value) -> str:
    """
    Email is OPTIONAL.
    - If missing/placeholder/unusable -> ""
    - If present but invalid -> ""
    - If multiple emails are present, keep the first valid one.
    - Strips "mailto:" if present.
    """
    e = _normalize_optional_str(email_value)
    if not e:
        return ""

    # Strip mailto:
    if e.lower().startswith("mailto:"):
        e = e[7:].strip()

    # Sometimes scraped content contains multiple emails in one field
    # (comma/semicolon/space separated). Keep the first valid one.
    parts = re.split(r"[,\s;]+", e)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if validate_email(part):
            return part.lower()

    return ""

def normalize_phone_number(phone_value) -> str:
    """
    Phone is OPTIONAL.
    - Missing/placeholder/unusable -> ""
    - Accepts common US forms:
        (505) 730-6979
        5057306979
        505.730.6979
        505 730 6979
        +1 505 730 6979
    - Returns canonical xxx-xxx-xxxx
    - If unusable -> ""
    """
    p = _normalize_optional_str(phone_value)
    if not p:
        return ""

    digits = re.sub(r"\D", "", p)

    # Handle leading US country code
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

    return ""


def validate_event_data(data: Dict) -> Tuple[bool, str | None]:
    """
    Validate the extracted event data according to custom rules.

    Required fields:
      - Tournament-name
      - Start-date
      - End-date
      - State-FullName
      - Website-url

    Optional-but-normalized fields:
      - Zip: if present but invalid -> set to ""
      - Email: if present but invalid -> set to ""
      - Phone-number: if present must be xxx-xxx-xxxx (still hard-fails)
    """

    # Tournament name
    tournament_name = _normalize_optional_str(data.get("Tournament-name"))
    if not tournament_name:
        return False, "Tournament name is missing or invalid."
    data["Tournament-name"] = tournament_name

    # Dates
    for date_field in ["Start-date", "End-date"]:
        date_value = _normalize_optional_str(data.get(date_field))
        if not date_value:
            return False, f"{date_field} is required."
        if not validate_date_format(date_value):
            return False, f"{date_field} '{date_value}' has an invalid format."
        data[date_field] = date_value

    # State (ensure this key matches your pipeline exactly)
    state_fullname = _normalize_optional_str(data.get("State-FullName"))
    if not state_fullname:
        return False, "State-FullName is required."
    data["State-FullName"] = state_fullname

    # Website URL
    website_url = _normalize_optional_str(data.get("Website-url"))
    if not website_url:
        return False, "Website URL is required."
    if not validate_url(website_url):
        return False, f"Website URL '{website_url}' is invalid."
    data["Website-url"] = website_url

    # Address is required by downstream upload process; normalize blanks/placeholders to N/A
    data["Address"] = normalize_required_address(data.get("Address"))

    # Zip (OPTIONAL): normalize and never hard-fail
    data["Zip"] = normalize_zip(data.get("Zip"))

    # Email (OPTIONAL): normalize and never hard-fail
    data["Email"] = normalize_email(data.get("Email"))

    # Optional phone (still validated if present)
    phone = normalize_phone_number(data.get("Phone-number"))
    if _normalize_optional_str(data.get("Phone-number")) and not phone:
        return False, f"Phone number '{data.get('Phone-number')}' is invalid."
    data["Phone-number"] = phone

    return True, None


def validate_date_format(date_str: str) -> bool:
    """
    Accept common formats: %m/%d/%Y, %B %d, %Y, %b %d, %Y, %b %d %Y, and ISO %Y-%m-%d.
    """
    date_formats = [
        "%m/%d/%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%Y-%m-%d",  # ISO
    ]
    for fmt in date_formats:
        try:
            datetime.strptime(date_str, fmt)
            return True
        except ValueError:
            continue
    return False


def validate_phone_number(phone: str) -> bool:
    """Validate phone number pattern: xxx-xxx-xxxx."""
    return bool(re.fullmatch(r"\d{3}-\d{3}-\d{4}", phone))


def validate_url(url: str) -> bool:
    """Accept http(s) URLs with a host; allow any path/query/fragment."""
    try:
        u = urlparse((url or "").strip())
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False
