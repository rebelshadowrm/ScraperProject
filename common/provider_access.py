"""Report each unavailable softball source without suppressing permitted ones."""


def softball_classes(manifest):
    from scrapers.ncs_fastpitch_scraper import NCSFastpitchScraper
    from scrapers.topgun_fastpitch_scraper import TopGunFastpitchScraper
    result = []
    for cls in (NCSFastpitchScraper, TopGunFastpitchScraper):
        try:
            cls.require_permission()
            result.append(cls)
        except RuntimeError as exc:
            manifest.provider(cls.__name__, "queued", error=str(exc))
    return result
