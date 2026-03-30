(async () => {
  const STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"];
  const MAX_RETRIES = 20;
  const FORCE_SPORT_ID = 1;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  function findAjaxSeed() {
    try {
      const entries = performance.getEntriesByType("resource")
        .map(e => e.name)
        .filter(u => u && u.includes("/ajax-events"));

      if (entries.length) {
        const preferred =
          entries.find(u => u.includes("season_ids=") && u.includes("page=1")) ||
          entries.find(u => u.includes("page=1")) ||
          entries[entries.length - 1];

        const url = new URL(preferred);
        return {
          ajaxUrl: `${url.origin}${url.pathname}`,
          params: Object.fromEntries(url.searchParams.entries())
        };
      }
    } catch (e) {
      console.log("Could not inspect ajax-events request from performance:", e);
    }

    try {
      const fallbackUrl = `${location.origin}/ajax-events`;
      const params = Object.fromEntries(new URLSearchParams(location.search).entries());
      return {
        ajaxUrl: fallbackUrl,
        params
      };
    } catch (e) {
      console.log("Could not build fallback ajax URL from current page:", e);
      return null;
    }
  }

  const seed = findAjaxSeed();
  if (!seed) {
    alert(
      "Could not detect /ajax-events parameters.\n\n" +
      "Open the target site page first, wait until listings load, " +
      "apply any season/year filter you want, then run this script again."
    );
    return;
  }

  console.log("Using ajax endpoint:", seed.ajaxUrl);
  console.log("Seed params:", seed.params);

  async function fetchState(state) {
    const params = new URLSearchParams(seed.params || {});

    params.set("layout", params.get("layout") || "medium");
    params.set("past_events", params.get("past_events") || "false");
    params.set("events_exits", "");
    params.set("state", state);

    if (FORCE_SPORT_ID !== null) {
      params.set("sport_id", String(FORCE_SPORT_ID));
    }

    const allPages = [];
    let page = 1;
    let retries = 0;

    while (true) {
      params.set("page", String(page));
      const url = `${seed.ajaxUrl}?${params.toString()}`;

      let resp;
      try {
        resp = await fetch(url, {
          credentials: "include",
          headers: {
            "x-requested-with": "XMLHttpRequest"
          }
        });
      } catch (e) {
        console.log(`[${state}] Network error on page ${page}:`, e);
        retries++;
        if (retries > MAX_RETRIES) break;
        await sleep(3000);
        continue;
      }

      if (resp.status === 403) {
        console.log(`[${state}] 403 on page ${page}. Solve any challenge in this tab, then wait...`);
        retries++;
        if (retries > MAX_RETRIES) break;
        await sleep(5000);
        continue;
      }

      retries = 0;

      const text = await resp.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        console.log(`[${state}] Non-JSON on page ${page}. Stopping.`);
        break;
      }

      let eventCount = 0;
      if (Array.isArray(data)) {
        eventCount = data.length;
      } else if (Array.isArray(data?.events)) {
        eventCount = data.events.length;
      } else if (Array.isArray(data?.data)) {
        eventCount = data.data.length;
      } else if (Array.isArray(data?.eventlist)) {
        eventCount = data.eventlist.length;
      } else if (Array.isArray(data?.payload)) {
        eventCount = data.payload.length;
      } else if (Array.isArray(data?.payload?.events)) {
        eventCount = data.payload.events.length;
      } else if (Array.isArray(data?.payload?.data)) {
        eventCount = data.payload.data.length;
      } else if (Array.isArray(data?.payload?.eventlist)) {
        eventCount = data.payload.eventlist.length;
      } else if (typeof data?.html === "string") {
        eventCount = (data.html.match(/data-event-id=/g) || []).length;
      } else if (typeof data?.payload?.html === "string") {
        eventCount = (data.payload.html.match(/data-event-id=/g) || []).length;
      }

      if (eventCount === 0) {
        console.log(`[${state}] No more events at page ${page}. Stopping.`);
        break;
      }

      console.log(`[${state}] Saved page ${page} (${eventCount} events)`);

      allPages.push({
        state,
        page,
        url,
        fetchedAt: new Date().toISOString(),
        payload: data
      });

      page++;
      await sleep(500);
    }

    return allPages;
  }

  const dump = {
    generated_at: new Date().toISOString(),
    source: location.href,
    domain: location.origin,
    forced_sport_id: FORCE_SPORT_ID,
    states: STATES,
    seed_params: seed.params || {},
    pages: []
  };

  for (const state of STATES) {
    console.log(`Starting export for ${state}...`);
    const pages = await fetchState(state);
    dump.pages.push(...pages);
    console.log(`Done with ${state}. Added ${pages.length} page(s).`);
    await sleep(1000);
  }

  const out = JSON.stringify(dump, null, 2);
  const blob = new Blob([out], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "fivetoolyouth_dump.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);

  console.log("All states complete. Downloaded fivetoolyouth_dump.json");
})();
