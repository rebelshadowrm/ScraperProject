"""Preliminary FASA adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
from common.sport import payload_revision
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from common.softball_html import labelled_table_values,labelled_table_rows
from scrapers.triplecrown_scraper import _extract_emails
from datetime import datetime
import re
from common.softball_rows import make_row,city_state,source_amount
from common.softball_scaffold import fetch_listing
from common.source_exclusions import SourceEventOutOfScope
from common.softball_description import description


class FASAScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "FASA"
    DISCOVERY_URL = "https://www.playfasa.com/Search.asp"
    BLOCKER = "Bounded profile rows verified; filtered search coverage, additional complexes and conditional fees remain"
    EVENT_HOST = "www.playfasa.com"
    EVENT_PATH = "/profile\\.asp"
    EVENT_QUERY_ID = "ID"
    DISCOVERY_SELECTOR = "a[href^=\"profile.asp?ID=\"]"
    REUSE = ""
    DETAIL_HTML_VERIFIED = True

    def configure_filters(self,*,state,on_date):
        from us import states
        if not states.lookup(state or '') or states.lookup(state).abbr!=state:raise ValueError('FASA filter needs a US state abbreviation')
        datetime.strptime(on_date,'%Y-%m-%d')
        self._search_form=dict(sortcol='',sortdir='asc',STARTDATE=on_date,DIRECTOR='',TRNYCITY='',STATE=state,TAGE='',TYPE='All',action='Search')

    def collect_event_links(self,driver,base_url,**kwargs):
        if not hasattr(self,'_search_form'):return super().collect_event_links(driver,base_url,**kwargs)
        if base_url!=self.DISCOVERY_URL:raise ValueError('Use the observed FASA search form endpoint')
        links=self.parse_listing(fetch_listing(base_url,method='POST',data=self._search_form),base_url,max_events=kwargs.get('max_events',5))
        links.search_filters=dict(self._search_form)
        links.error='Bounded state/exact-start-date form search; no pagination link was present in inspected response'
        return links

    def parse_listing(self, html, base_url, **kwargs):
        links = super().parse_listing(html, base_url, **kwargs)
        for anchor in BeautifulSoup(html, 'html.parser').select(self.DISCOVERY_SELECTOR):
            url = self.canonical_detail_url(urljoin(base_url, anchor['href']))
            if url in links and anchor.find_parent('tr'):
                # Discovery change marker, never an identity or a complete detail row.
                cells = [cell.get_text(' ', strip=True) for cell in anchor.find_parent('tr').find_all('td', recursive=False)]
                # Entered teams/visit counters change without tournament changes.
                self.discovery_revisions[url] = payload_revision(cells[1:15])
        return links

    def inspect_detail_html(self, url, html):
        identity = self.get_event_identity(url)
        soup = BeautifulSoup(html, 'html.parser')
        titles = [cell.get_text(' ', strip=True) for cell in soup.select('th')
                  if cell.get_text(' ', strip=True).endswith(' Tournament Profile')]
        if len(titles) != 1:
            raise ValueError('FASA profile title is missing or ambiguous')
        title = titles[0].removesuffix(' Tournament Profile')
        fields = labelled_table_values(soup, ('Age Groups','Cost','Start Date','End Date','Location','GG','Format'))
        values = {key: cell.get_text(' ', strip=True) for key, cell in fields.items()}
        for key in ('Start Date', 'End Date'):
            if not re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}',values[key]):
                raise ValueError('FASA date needs an explicit year')
            datetime.strptime(values[key],'%m/%d/%Y')
        venue_cells = labelled_table_values(soup, ('Complex','Complex Address'))
        venues=[{key:cell.get_text(' ',strip=True) for key,cell in cells.items()} for cells in
                labelled_table_rows(soup,('Complex','Complex Address'))]
        contact_cells = labelled_table_values(soup, ('Name','Cell','E-Mail Address'))
        # Organizer block only; participant/team tables are outside inspection.
        contact = {key: cell.get_text(' ',strip=True) for key,cell in contact_cells.items()}
        email_soup = BeautifulSoup(str(contact_cells['E-Mail Address']), 'html.parser')
        contact['emails'] = _extract_emails(email_soup)
        return dict(source_id=identity,source_url=url,sport='softball',title=title,
                    cancelled=title.upper().startswith('CANCELLED'),
                    start=values['Start Date'],end=values['End Date'],
                    age_labels=values['Age Groups'],fee_label=values['Cost'],
                    location=values['Location'],guaranteed_games=values['GG'],format=values['Format'],
                    venue={key:cell.get_text(' ',strip=True) for key,cell in venue_cells.items()},
                    venues=venues,description=description(str(fields['Format']),base_url=url),
                    organizer=contact,ingestion_ready=False)

    def extract_event_details(self,url,driver=None,**kwargs):
        details=self.inspect_event(url)
        if not hasattr(self,'source_details'):self.source_details={}
        self.source_details[url]=details
        if details['cancelled']:raise SourceEventOutOfScope('Cancelled by FASA source')
        city,state=city_state(details['location'])
        contact=details['organizer']
        venue=details['venue']
        divisions=[dict(label=details['age_labels'],entry_fee=source_amount(details['fee_label']),fee_label=details['fee_label'])]
        details['data_gaps']=[] if source_amount(details['fee_label']) else ['Fee has source qualifiers; raw label retained without a guessed numeric fee']
        return make_row(title=details['title'],start=details['start'],end=details['end'],url=url,state=state,city=city,
            divisions=divisions,locations=[dict(venue=place.get('Complex',''),address=place.get('Complex Address',''),city=city,state=state)
                                           for place in details['venues']],
            organizer=dict(name=contact.get('Name',''),email=next(iter(contact.get('emails',[])),''),phone=contact.get('Cell','')),
            guaranteed_games=details['guaranteed_games'],notes='Source fee: '+details['fee_label']+'\n\nFormat: '+details['description']['text'])
