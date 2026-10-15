# Softball source assessment

Research date: 2026-10-14. Priority is an engineering assessment of demonstrated
public listings and adapter reuse, not a claim of complete US coverage or a
redistribution license. Search-index results establish candidate sources only.
No undocumented sport IDs or nationwide completeness assumptions are adopted.
No access restriction is bypassed; logins, private rosters and player contacts
are outside acquisition scope. Preserve only published organizer contacts.

| Priority | Provider / official source | Coverage and feasibility | Current decision |
| --- | --- | --- | --- |
| 1 | [NCS](https://www.playncs.com/fastpitch) | State-searchable fastpitch events; same Details/Divisions/Locations/Contacts platform as existing baseball adapter. Public Texas listing returned 200; observed state filter is countryState=US-TX, not state=TX. | Implemented behind written-permission gate. [Terms](https://www.playncs.com/terms-of-use) restrict public/commercial reuse without written consent. Captured date labels omit the year; the candidate rejects these rather than guessing from season titles/current year. A permitted dated feed is also needed. |
| 1 | [USSSA Fastpitch](https://v10.usssa.com/fastpitch/searchEvents) | National state/season search plus state-operated sites. Existing baseball HTML/JSON-LD parsing is reusable only after host/filter verification. | Investigate separate fastpitch sport/season fields, not baseball sport 11. [Terms](https://usssa.com/terms-of-use) and any feed permission must be reviewed for intended redistribution. No fastpitch rollout yet. |
| 1 | [Triple Crown Fastpitch](https://www.triplecrownfastpitch.com/) | Official site describes over 100 events, with regional and national event families. Public Webflow event pages contain divisions, fees and venue data. | Reuse schedule/detail parsers after verifying fastpitch component filters and new host families; do not reuse baseball sportID=1. Homepage returned 200, robots empty. |
| 2 | [PGF](https://www.premiergirlsfastpitch.com/regional_championships) | Official regional schedule and registration systems; [TournamentConnect](https://softball.tournamentconnect.premiergirlsfastpitch.com/events/register/tournaments/provider/premiergirlsfastpitch) publishes additional events. Multiple systems need deduplication. | Direct robots request returned 403. No further automated probe; obtain permitted feed/access and stable registration IDs. Indexed listings are not live extraction verification. |
| 2 | [USA Softball](https://www.usasoftball.com/tournaments-national-championships/national-championships/) | National championships plus affiliate qualification networks. [Events](https://www.usasoftball.com/events/) is broader than youth fastpitch. | Assess national/affiliate sources separately and distinguish slowpitch/adult events. No evidence of one complete youth tournament API. |
| 2 | [Perfect Game Softball](https://www.perfectgame.org/softball/schedule.aspx?Type=Tournaments) | Public schedule exposes ages, A/B/C/Open classifications and directors. Existing baseball adapter hardcodes Baseball and group categories. | New observed softball schedule/feed contract needed. Preserve B/C eligibility outside legacy dropdowns; never relabel it as baseball AA/AAA. |
| 2 | [USFA](https://usfastpitch.com/) / [event search](https://usfaevents.com/usaRankEvents.aspx) | Public multi-state event listing, fees and dates visible in search index. | Verify official frontend linkage, postback pagination, stable IDs and reuse permission. Not a nationwide completeness claim. |
| 2 | [Alliance Fastpitch](https://unified.thealliancefastpitch.com/tournaments/) | League-linked events, qualifiers and national/regional championships. | Federation/league events may duplicate PGF, USSSA or regional hosts. Preserve source identities; avoid fuzzy cross-provider merges. |
| 3 | [NSA](https://playnsa.com/) | Separate Fastpitch and Slowpitch tournament navigation and state directors. | Follow official Fastpitch navigation; verify registration backend and state coverage before estimating event counts. |
| Regional | [TopGun](https://topgunstats.com/tournaments) | Existing two-feed adapter; bounded response contained 128 SportID=2 records and 84 SportID=1 records. TournamentID is present. These are raw feed counts, not valid future coverage. | Fastpitch subclass reuses that parser with SportID=2 and a separate permission gate. robots.txt explicitly disallows all other bots; no further automated probes or new softball acquisition until permission is obtained. Existing baseball path is retained. |
| Regional | [USFA](https://usfaevents.com/usaRankEvents.aspx), NCS state directors, Triple Crown regional hosts and USA Softball affiliates | Regional sources fill different geography, age and skill gaps. Their overlaps and freshness need measurement by state/month/division. | Start with source-backed adapters and measure unique valid events; do not assume any four organizations suffice. Consider regional provider additions based on measured gaps. |

## Update/access policy

Daily refresh is a local desired cadence, not evidence of provider update frequency.
Use serial provider acquisition, existing bounded calls/pagination and conservative
delays. Recheck source terms and robots before enabling a new source. Publicly
reachable structured data is not by itself an API license. Prefer an officially
permitted feed when offered. Access denials are unresolved coverage, never empty
success. Keep source captures local and omit credentials/private participant data.

Current historical baseball validation is in LIVE_FULL_PULL_RUN_REPORT.md. New
softball verification and exact sample limitations are recorded in the handoff.
