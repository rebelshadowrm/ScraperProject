"""Explicit source division eligibility, independent of output adapters."""
import re


def age_tokens(label, explicit_age=None):
    # Only labelled U ages or a provider's explicit numeric age field.
    raw=str(label or '')
    ages=[]
    for match in re.finditer(r'(?<!\d)((?:\d{1,2}\s*/\s*)*\d{1,2})\s*u(?=$|[\s,;/&()]|(?:AA|BB|[abcogr])\b|-(?:AA|BB|[abcogr])\b|_Combined\b)',raw,re.I):
        ages.extend(int(v) for v in re.findall(r'\d+',match[1]))
    if not ages and re.fullmatch(r'\d{1,2}',str(explicit_age or '')):
        ages=[int(explicit_age)]
    result = list(dict.fromkeys(f'{age}u' for age in ages if 6<=age<=18))
    if re.search(r'(?<![A-Za-z])(?:HS|High\s*School)(?![A-Za-z])', raw, re.I):
        result.append('HS')
    return result
