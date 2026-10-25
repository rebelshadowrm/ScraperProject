"""Public source contact facts; publication permission remains a receiver policy."""
import re
from urllib.parse import urljoin, urlsplit, parse_qs


def cloudflare_email(node):
    """Decode the provider's public email rendering, rejecting malformed bytes."""
    value = node.get('data-cfemail', '') if node else ''
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}){4,321}', value):
        return ''
    raw = bytes.fromhex(value)
    try:
        email = bytes(byte ^ raw[0] for byte in raw[1:]).decode('utf-8')
    except UnicodeDecodeError:
        return ''
    return email if re.fullmatch(r'[^\s@?&#]+@[^\s@?&#]+\.[^\s@?&#]+', email) else ''


def pg_contact_facts(soup, url):
    """PG identifies its director and contact destination through stable field IDs."""
    director = soup.select_one('[id$="fvEventContact_lblDirectorName"]')
    contact = soup.select_one('[id$="EventHeader1_hlSideMenuContact"][href]')
    facts = {}
    if director:
        facts['directorName'] = director.get_text(' ', strip=True)
    if contact:
        destination = urljoin(url, contact['href'])
        if destination.startswith('https://www.perfectgame.org/Events/Contact.aspx?event='):
            facts['organizer_contact'] = {'form': {'page_url': destination, 'action_label': 'Contact event director'}}
    return facts


def pg_division_facts(soup, url):
    """Only this event's labelled division link, never the event's freeform name."""
    event_id = parse_qs(urlsplit(url).query).get('event')
    for link in soup.select('[id*="rptDivisions_hlDivisions"][href]'):
        if parse_qs(urlsplit(urljoin(url, link['href'])).query).get('event') != event_id:
            continue
        label = link.get_text(' ', strip=True)
        match = re.fullmatch(r'(\d{1,2}(?:/\d{1,2})*U|HS|High School)\s*(?:\((Open|Invite|A|AA|B|BB|C|Rec|All-Star|Gold)\))?', label, re.I)
        if match:
            from common.age_groups import age_tokens
            return dict(age_groups=age_tokens(match[1]), competition_class=match[2] or '', classification_source_label=label)
    return {}
