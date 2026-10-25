"""Source-labelled RegisterPlay division facts; unknown values stay unknown."""
from decimal import Decimal
import re

from common.softball_rows import age_tokens, source_amount


def division_label(label):
    match = re.fullmatch(r'((?:\d{1,2}/)*\d{1,2})\s*u?\s*(O|A|B|C|G|R|A/B|B/C|C/Rec)',
                         str(label or '').strip(),re.I)
    if not match:
        return '', None
    token = match[2].upper()
    return match[1]+'u', {'O':'Open','R':'Rec','G':'Gold','C/REC':'C/Rec'}.get(token,token)


def division_metadata(item, *, cc_fee_extra):
    label = str(item.get('name') or '').strip()
    age_label, competition = division_label(label)
    ages = age_tokens(age_label or label, item.get('age'))
    base, total = source_amount(item.get('divisionFee')), source_amount(item.get('fee'))
    pricing = {'base_fee': base or None, 'advertised_fee': total or None,
               'credit_card_fee_extra': cc_fee_extra, 'currency': 'USD'}
    if base and total:
        pricing['observed_range'] = [format(min(Decimal(base), Decimal(total)), 'f'),
                                     format(max(Decimal(base), Decimal(total)), 'f')]
        if cc_fee_extra is True and Decimal(total) >= Decimal(base):
            pricing['additional_card_fee'] = format(Decimal(total) - Decimal(base), '.2f')
    spots = item.get('spotsRemaining')
    spots = spots if isinstance(spots, dict) else {}
    message = str(spots.get('message') or '')
    waitlist = item.get('waitlist')
    # Never copy registration/participant arrays from a source response.
    if isinstance(waitlist, dict):
        waitlist = {key: waitlist[key] for key in ('showEnter', 'spotsRemaining', 'message')
                    if key in waitlist and isinstance(waitlist[key], (str, int, bool, type(None)))}
    elif not isinstance(waitlist, (str, int, bool, type(None))):
        waitlist = None
    status = 'unknown'
    if re.search(r'\b(closed|registration ended|sold out)\b', message, re.I):
        status = 'closed'
    elif re.search(r'\bwait\s*list\b', message, re.I) or waitlist is True:
        status = 'waitlist'
    elif re.search(r'\bfull\b', message, re.I):
        status = 'full'
    elif spots.get('showEnter') is True:
        status = 'open'
    remaining = spots.get('spotsRemaining')
    registration = {'deadline_label': item.get('enterByCutDate'),
                    'deadline_iso': None, 'message': message or None,
                    'status': status, 'action_available': spots.get('showEnter'),
                    'source_show_enter': item.get('showEnter'),
                    'spots_remaining': remaining if type(remaining) is int and remaining >= 0 else None,
                    'waitlist': waitlist}
    # A yearless button label is useful evidence, but cannot supply an ISO date.
    deadline = str(item.get('enterByCutDate') or '')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T.*)?', deadline):
        from datetime import date
        try:
            registration['deadline_iso'] = date.fromisoformat(deadline[:10]).isoformat()
        except ValueError:
            pass
    return {'age_groups': ages, 'competition_class': competition,
            'pricing': pricing, 'registration': registration}
