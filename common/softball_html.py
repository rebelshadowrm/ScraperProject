"""Small helpers for source-labelled table contracts; no positional guessing."""


def labelled_table_values(soup, required_labels):
    matches = []
    for table in soup.find_all('table'):
        container = table.find('tbody', recursive=False) or table
        rows = container.find_all('tr', recursive=False)
        for index, row in enumerate(rows[:-1]):
            labels = [cell.get_text(' ', strip=True).rstrip(':').strip()
                      for cell in row.find_all(['td', 'th'], recursive=False)]
            if not set(required_labels).issubset(labels):
                continue
            cells = rows[index+1].find_all(['td', 'th'], recursive=False)
            if len(labels) != len(cells) or len(set(labels)) != len(labels):
                raise ValueError('Labelled source table has inconsistent columns')
            matches.append(dict(zip(labels, cells)))
    if len(matches) != 1:
        raise ValueError('Expected exactly one source-labelled table section')
    return matches[0]


def labelled_table_rows(soup,required_labels):
    """All consecutive data rows after one verified labelled header."""
    sections=[]
    for table in soup.find_all('table'):
        container=table.find('tbody',recursive=False) or table
        rows=container.find_all('tr',recursive=False)
        for index,row in enumerate(rows):
            labels=[cell.get_text(' ',strip=True).rstrip(':').strip() for cell in row.find_all(['td','th'],recursive=False)]
            if not set(required_labels).issubset(labels):continue
            if len(labels)!=len(set(labels)):raise ValueError('Duplicate source section labels')
            values=[]
            for data in rows[index+1:]:
                cells=data.find_all(['td','th'],recursive=False)
                if len(cells)!=len(labels):break
                values.append(dict(zip(labels,cells)))
            sections.append(values)
    if len(sections)!=1 or not sections[0]:raise ValueError('Expected one populated source section')
    return sections[0]
