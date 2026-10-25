"""Map source-backed softball observations to the existing 56-column contract."""
from datetime import datetime
from decimal import Decimal,InvalidOperation
import re

from us import states
from common.config import OUTPUT_COLUMNS
from common.source_exclusions import SourceEventIncomplete
from common.validation import normalize_email,normalize_phone_number,validate_event_data


def source_date(value):
    raw=str(value or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T.*)?',raw):
        return datetime.strptime(raw[:10],'%Y-%m-%d').strftime('%m/%d/%Y')
    for fmt in ('%m/%d/%Y','%b %d %Y','%B %d %Y','%b %d, %Y','%B %d, %Y'):
        try:return datetime.strptime(raw,fmt).strftime('%m/%d/%Y')
        except ValueError:pass
    raise SourceEventIncomplete('Source has no verifiable explicit-year event date')


def source_amount(value):
    raw=str(value if value is not None else '').strip()
    if not re.fullmatch(r'\$?\d[\d,]*(?:\.\d{1,2})?',raw):
        return ''
    try:amount=Decimal(raw.lstrip('$').replace(',',''))
    except InvalidOperation:return ''
    return format(amount,'f')


def source_state(value):
    raw=str(value or '').strip()
    state=states.DC if raw.casefold() in {'dc','district of columbia'} else states.lookup(raw)
    if not state:raise SourceEventIncomplete('No verified US state in source event/venue')
    return state.name


def city_state(label):
    raw=str(label or '').strip()
    match=re.fullmatch(r'(.+),\s*([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?',raw)
    return (match[1].strip(),source_state(match[2])) if match else ('','')


from common.age_groups import age_tokens


def source_address(venue, address, *, state=''):
    """Observed US comma-separated or line-separated postal addresses."""
    raw=str(address or '').strip()
    match=re.fullmatch(r'(.+?)[,\n]+\s*([^,\n]+?)[,\n]+\s*([A-Za-z ]+?)\s+(\d{5}(?:-\d{4})?)',raw)
    if match:
        return dict(venue=venue,address=match[1].strip(),city=match[2].strip(),
                    state=source_state(match[3]),zip=match[4])
    tail=re.search(r',\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$',raw)
    if tail:
        before=raw[:tail.start()]
        boundaries=list(re.finditer(r'\b(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Blvd|Boulevard|Pkwy|Parkway|Ln|Lane|Ct|Court)\.?\s+(?=[A-Za-z])',before,re.I))
        if len(boundaries)==1 and re.match(r'^\d',before):
            split=boundaries[0].end()
            return dict(venue=venue,address=before[:split].strip(),city=before[split:].strip(),state=source_state(tail[1]),zip=tail[2])
        return dict(venue=venue,address=raw,city='',state=source_state(tail[1]),zip=tail[2])
    return dict(venue=venue,address=raw,city='',state=state,zip='')


def make_row(*,title,start,end,url,state,city='',divisions=(),locations=(),
             organizer=None,notes='',guaranteed_games='',hotel='NA'):
    row={field:'' for field in OUTPUT_COLUMNS}
    row.update({'Tournament-name':title,'Start-date':source_date(start),'End-date':source_date(end),
                'Website-url':url,'State-FullName':source_state(state),'City':city,
                'Tournament-type':'Team','Gate-fees':'NA','Hotel-required':hotel,'Tournament-surface':'NA',
                'Are-there-multiple_locations':'Yes' if len(locations)>1 else 'No'})
    if locations:
        first=locations[0]
        for field,key in [('Venue','venue'),('Address','address'),('City','city'),('Zip','zip')]:
            if first.get(key):row[field]=first[key]
        if first.get('state'):row['State-FullName']=source_state(first['state'])
        if not re.fullmatch(r'\d{5}(?:-\d{4})?',str(first.get('zip') or '').strip()):row['Zip']=''
    organizer=organizer or {}
    row['Contact-name']=organizer.get('name','')
    row['Email']=normalize_email(organizer.get('email',''))
    row['Phone-number']=normalize_phone_number(organizer.get('phone',''))
    pairs=[]
    raw_labels=[]
    for division in divisions:
        label=division.get('label','')
        raw_labels.append(f"{label}: {division.get('fee_label',division.get('entry_fee',''))}")
        mapped=age_tokens(division.get('age_labels') or label,division.get('age'))
        if not mapped:raise SourceEventIncomplete('Source division has unresolved eligibility: '+str(label))
        for age in mapped:
            # Different division fees on one U age remain distinct source pairs.
            pair=(age,source_amount(division.get('entry_fee')))
            if pair not in pairs:pairs.append(pair)
    if not pairs:raise SourceEventIncomplete('No source-backed numeric U eligibility; preserve labels for further mapping')
    if len(pairs) > 13:
        row['source_details'] = {'divisions': [dict(division, age_groups=age_tokens(division.get('age_labels') or division.get('label', ''), division.get('age'))) for division in divisions]}
    for index,(age,fee) in enumerate(pairs[:13],1):
        row[f'Age-groups.{index}']=age;row[f'Entry-fee.{index}']=fee
    if str(guaranteed_games).isdigit() and 1<=int(guaranteed_games)<=9:
        row['Guaranteed-games']=str(guaranteed_games)
    extra_locations=['; '.join(str(value) for value in place.values() if value is not None) for place in locations]
    row['More-information-and-additional-tournament-locations']='\n\n'.join(
        [text for text in [notes,*raw_labels,*extra_locations] if text])
    valid,error=validate_event_data(row)
    if not valid:raise SourceEventIncomplete(error)
    return row
