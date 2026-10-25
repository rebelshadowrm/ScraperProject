"""Read published event JSON-LD without treating navigation schema as event data."""
import json


def sports_events(soup):
    result=[]
    def walk(value):
        if isinstance(value,list):
            for item in value:walk(item)
        elif isinstance(value,dict):
            kinds=value.get('@type',[])
            if isinstance(kinds,str):kinds=[kinds]
            if any(kind in ('Event','SportsEvent') for kind in kinds):result.append(value)
            if '@graph' in value:walk(value['@graph'])
    for script in soup.select('script[type="application/ld+json"]'):
        try:walk(json.loads(script.get_text()))
        except (TypeError,ValueError):continue
    return result
