(async () => {
  const STATES = ["TX", "OH", "AZ", "GA", "LA", "MI", "MO", "NM", "NV", "OK"];
  const MAX_RETRIES = 20;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  function findSeasonIdsFromPerformance() {
    try {
      const entries = performance.getEntriesByType("resource")
        .map(e => e.name)
        .filter(u => u && u.includes("/ajax-events") && u.includes("season_ids="));

      if (!entries.length) return "";

      const page1 = entries.find(u => u.includes("page=1")) || entries[0];
      const url = new URL(page1);
      return url.searchParams.get("season_ids") || "";
    } catch (e) {
      console.log("Unable to read season_ids from performance entries:", e);
      return "";
    }
  }

  const seasonIds = findSeasonIdsFromPerformance();
  if (!seasonIds) {
    alert(
      "Could not detect current season_ids.\n\n" +
      "Open the current Five Tool Youth baseball season page first " +
      "(for example /season/2026-baseball), wait for it to fully load, " +
      "then run this script again."
    );
    return;
  }

  console.log("Detected season_ids:", seasonIds);

  async function fetchState(state) {
    const params = new URLSearchParams({
      layout: "medium",
      past_events: "false",
      season_ids: seasonIds,
      region: "",
      name: "",
      event_label_id: "",
      division: "",
      state,
      sport_id: "1",
      director: "",
      events_exits: "",
      organization_id: ""
    });

    const allPages = [];
    let page = 1;
    let retries = 0;

    while (true) {
      params.set("page", String(page));
      const url = `${location.origin}/ajax-events?${params.toString()}`;

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
        console.log(`[${state}] 403 on page ${page}. Solve Cloudflare in this tab, then wait...`);
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
        season_ids: seasonIds,
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
    season_ids: seasonIds,
    sport_id: 1,
    states: STATES,
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
