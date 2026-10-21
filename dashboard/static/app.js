(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const fmt = new Intl.NumberFormat("en-US");
  const n = (x) => (x == null ? "–" : fmt.format(x));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bytes = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : b >= 1e3 ? (b / 1e3).toFixed(0) + " KB" : (b || 0) + " B");
  const ago = (iso) => {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  };

  const CLASS = {
    route_deep: ["good", "Listable"], route_mid: ["good", "Listable"], route_alt: ["good", "Alt. route"],
    wayback_only: ["info", "Wayback only"], route_recent: ["warn", "Recent only"], route_unknown: ["warn", "Route unknown"],
    no_route: ["bad", "No archive route"], blocked: ["bad", "Blocked"],
  };
  const CLASS_ORDER = ["route_deep", "route_mid", "route_alt", "wayback_only", "route_recent", "route_unknown", "no_route", "blocked"];
  const CLASS_LABEL = {
    route_deep: "Listable to 2015 or earlier", route_mid: "Listable from 2016–2020", route_alt: "Listable via IDs or date archive",
    wayback_only: "Wayback only", route_recent: "Sitemap from 2021 only", route_unknown: "Accessible, route unknown",
    no_route: "No route for the event window", blocked: "Blocked",
  };
  const VERDICT = {
    blocked_http: "HTTP block or bot challenge", blocked_robots: "Disallowed by robots.txt",
    blocked_robots_unreachable: "robots.txt unreachable", tdm_reserved: "Text and data mining reserved",
    crawl_delay_excessive: "Crawl-delay too long", sitemap_deep: "Sitemap", sitemap_mid: "Sitemap",
    sitemap_recent_only: "Sitemap", sitemap_undated: "Undated sitemap", no_sitemap: "No sitemap", screen_error: "Screen error",
  };
  const ROLE = { core_daily: "Core daily", core_broadcaster: "Core broadcaster", regional_probe: "Regional probe", candidate_rejected: "Rejected" };
  const TYPE = { national_daily: "National daily", regional_daily: "Regional daily", broadcaster: "Broadcaster", wire: "Wire agency", online: "Online" };
  const TIER = {
    A: "Press and broadcast both listable", B: "Only one side listable", C: "Recent sitemaps only", D: "Tried, nothing listable",
  };

  let data = null, mode = "past", selected = null, world = null;

  // ---------- top strip and sources ----------
  function renderStrip(sys) {
    const free = sys.disk_free_gb, warn = free < sys.min_free_gb;
    $("#disk").innerHTML = `${n(Math.round(free))} GB ${warn ? '<span class="pill bad">Below minimum</span>' : ""}`;
    $("#raw").textContent = sys.raw_gb >= 1 ? sys.raw_gb.toFixed(2) + " GB" : Math.round(sys.raw_gb * 1000) + " MB";
    $("#jobs").innerHTML = sys.jobs.length
      ? sys.jobs.map((j) => `<span class="pill info" title="started ${esc(ago(j.since))}">${esc(j.job)}</span>`).join("")
      : '<span class="pill idle">None</span>';
  }

  function sourceState(s, jobs) {
    const running = jobs.some((j) => j.job.includes(s.id.replace("_go", "")) || (s.id === "reliefweb" && j.job.startsWith("reliefweb")));
    if (running) return ["info", "Running"];
    if (s.id === "shelter_projects") return s.metadata ? ["good", "Downloaded"] : ["warn", "Waiting for manual download"];
    if (s.id === "reliefweb") return s.months_done >= s.months_expected ? ["good", "Listing done"] : ["warn", "Paused"];
    if (s.selected && s.ok >= s.selected) return ["good", "Done"];
    if (s.attempted) return ["warn", "Paused"];
    return ["idle", "Not started"];
  }

  function renderSources(h, jobs) {
    $("#sources").innerHTML = h.sources.map((s) => {
      const [cls, label] = sourceState(s, jobs);
      let body = "";
      if (s.selected != null) {
        const pct = s.selected ? Math.min(100, (100 * s.ok) / s.selected) : 0;
        body = `
          <div><div class="prog"><span>Documents fetched</span><span>${n(s.ok)} / ${n(s.selected)}</span></div>
          <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="${s.selected}" aria-valuenow="${s.ok}"><span style="width:${pct.toFixed(1)}%"></span></div></div>
          <ul class="facts">
            <li>Includes audit sample of excluded: <b>${n(s.audit_sample)}</b></li>
            ${s.attached_files ? `<li>Attached files: <b>${n(s.attached_files)}</b></li>` : ""}
            <li>Failed: <b>${n(s.failed)}</b>${s.errors && s.errors.length ? " (" + esc(s.errors.map((e) => e[0] + " " + e[1]).join(", ")) + ")" : ""}</li>
            <li>Stored: <b>${bytes(s.bytes)}</b> · last fetch ${esc(ago(s.last_fetch))}</li>
          </ul>`;
      } else if (s.id === "reliefweb") {
        const pct = (100 * s.months_done) / s.months_expected;
        body = `
          <div><div class="prog"><span>Months listed (1990–now)</span><span>${n(s.months_done)} / ${n(s.months_expected)}</span></div>
          <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="${s.months_expected}" aria-valuenow="${s.months_done}"><span style="width:${pct.toFixed(1)}%"></span></div></div>
          <ul class="facts">
            <li>API calls today: <b>${n(s.calls_today)}</b> of ${n(s.daily_cap)}</li>
            <li>Next step: <b>${esc(s.stage)}</b></li>
            <li>Top formats: ${esc((s.formats || []).slice(0, 3).map((f) => f[0] + " " + fmt.format(f[1])).join(", "))}</li>
          </ul>`;
      } else {
        body = `<ul class="facts"><li>Server cannot reach the site. Download the books into the manual folder.</li></ul>`;
      }
      return `<article class="src">
        <div class="src-head"><span class="src-name">${esc(s.name)}</span><span class="pill ${cls}">${esc(label)}</span></div>
        <div><div class="big">${n(s.metadata)}</div><div class="unit">${esc(s.metadata_unit)}</div></div>
        ${body}
        <div class="src-access">${esc(s.access)}</div>
      </article>`;
    }).join("");
  }

  // ---------- map ----------
  function colorFor(num) {
    const nc = data._byNumeric.news[num], hc = data._byNumeric.hum[num];
    if (mode === "hum") {
      const t = hc ? hc.total : 0;
      if (!t) return css("--land-none");
      return css(t >= 5000 ? "--ramp-4" : t >= 500 ? "--ramp-3" : t >= 50 ? "--ramp-2" : "--ramp-1");
    }
    if (!nc) return css("--land-none");
    const tier = mode === "past" ? nc.tier_past : nc.tier_recent;
    return css({ A: "--ramp-4", B: "--ramp-3", C: "--ramp-2", D: "--ramp-1" }[tier]);
  }

  function renderLegend() {
    let items;
    if (mode === "hum") {
      items = [["--ramp-4", "5,000+ documents"], ["--ramp-3", "500–4,999"], ["--ramp-2", "50–499"], ["--ramp-1", "1–49"], ["--land-none", "None"]];
    } else {
      const tiers = mode === "past" ? ["A", "B", "C", "D"] : ["A", "B", "D"];
      items = tiers.map((t) => [{ A: "--ramp-4", B: "--ramp-3", C: "--ramp-2", D: "--ramp-1" }[t], TIER[t]]).concat([["--land-none", "Not screened"]]);
    }
    $("#legend").innerHTML = items.map(([v, l]) => `<span><i style="background:${css(v)}"></i>${esc(l)}</span>`).join("");
  }

  function tipHtml(num, name) {
    const nc = data._byNumeric.news[num], hc = data._byNumeric.hum[num];
    let h = `<b>${esc(nc?.name || hc?.name || name)}</b>`;
    if (mode === "hum") {
      h += hc ? `<br>IFRC documents ${n(hc.ifrc_docs || 0)} · fetched ${n(hc.ifrc_fetched || 0)}<br>ReliefWeb reports ${n(hc.reliefweb_reports || 0)}` : "<br>No humanitarian documents";
    } else if (nc) {
      const ok = nc.outlets.filter((o) => ["route_deep", "route_mid", "route_alt", "wayback_only"].includes(o.class)).length;
      const bl = nc.outlets.filter((o) => o.class === "blocked").length;
      h += `<br>${TIER[mode === "past" ? nc.tier_past : nc.tier_recent]}<br>${nc.outlets.length} tried · ${ok} listable · ${bl} blocked`;
    } else h += "<br>No outlets screened";
    return h;
  }

  function drawMap() {
    if (!world) return;
    const svg = d3.select("#map");
    const W = 960, H = 470;
    svg.attr("viewBox", `0 0 ${W} ${H}`);
    const land = topojson.feature(world, world.objects.countries).features.filter((f) => f.id !== "010");
    const proj = d3.geoNaturalEarth1().fitExtent([[8, 8], [W - 8, H - 8]], { type: "FeatureCollection", features: land });
    const path = d3.geoPath(proj);
    const tip = $("#tip"), box = $(".mapbox");
    svg.selectAll("path.c").data(land, (d) => d.id).join("path")
      .attr("class", (d) => "c" + (selected && d.id === selected.numeric ? " sel" : ""))
      .attr("d", path)
      .attr("fill", (d) => colorFor(d.id))
      .attr("tabindex", (d) => (data._byNumeric.news[d.id] || data._byNumeric.hum[d.id] ? 0 : null))
      .on("mousemove", (ev, d) => {
        tip.innerHTML = tipHtml(d.id, d.properties.name);
        tip.hidden = false;
        const r = box.getBoundingClientRect();
        let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
        if (x + 260 > r.width) x = ev.clientX - r.left - 270;
        tip.style.left = Math.max(0, x) + "px";
        tip.style.top = y + "px";
      })
      .on("mouseleave", () => { tip.hidden = true; })
      .on("click", (ev, d) => select(d.id, d.properties.name))
      .on("keydown", (ev, d) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(d.id, d.properties.name); } });
    renderLegend();
  }

  // ---------- detail ----------
  function select(num, fallbackName) {
    const nc = data._byNumeric.news[num], hc = data._byNumeric.hum[num];
    selected = { numeric: num, cc: nc?.cc || hc?.cc };
    let h = `<h3>${esc(nc?.name || hc?.name || fallbackName)}</h3>`;
    h += `<p class="meta">${nc ? (nc.pilot ? "Pilot country (detailed recon)" : "Screened country") : "No news outlets screened"}</p>`;
    h += `<div class="hstats">
      <div><b>${n(hc?.ifrc_docs || 0)}</b><span>IFRC documents</span></div>
      <div><b>${n(hc?.ifrc_fetched || 0)}</b><span>IFRC fetched</span></div>
      <div><b>${n(hc?.reliefweb_reports || 0)}</b><span>ReliefWeb reports</span></div></div>`;
    if (nc) {
      h += `<p class="meta">Before 2021: ${esc(TIER[nc.tier_past])} · From 2021: ${esc(TIER[nc.tier_recent])}</p><ul class="olist">`;
      h += nc.outlets.map((o) => {
        const [cls, label] = CLASS[o.class] || ["idle", o.class];
        const col = o.listed ? ` · <b>${n(o.listed)}</b> articles listed` : o.collect_state === "listing" ? " · listing…" : "";
        const why = o.class === "blocked" ? (VERDICT[o.verdict] || o.verdict) : o.year ? (o.class === "route_recent" ? "from " : "to ") + o.year : "";
        return `<li><span class="on">${esc(o.name)}</span><span class="pill ${cls}">${esc(label)}</span>
          <span class="od">${o.role ? `<span class="role">${esc(ROLE[o.role] || o.role)}</span> · ` : ""}${esc(TYPE[o.type] || o.type)} · ${esc(o.domain)}${why ? " · " + esc(why) : ""}${col}</span></li>`;
      }).join("");
      h += "</ul>";
    }
    $("#detail").innerHTML = h;
    d3.selectAll("#map path.c").classed("sel", (d) => d.id === num);
    document.querySelectorAll("#ctable tbody tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.num === num));
  }

  // ---------- news summary + table ----------
  function renderNews(news, hum) {
    const cc = news.class_counts;
    $("#classbar").innerHTML = CLASS_ORDER.filter((k) => cc[k]).map((k) => `<span class="pill ${CLASS[k][0]}">${esc(CLASS_LABEL[k])} · ${n(cc[k])}</span>`).join("");
    const rows = Object.entries(news.countries).sort((a, b) => a[1].name.localeCompare(b[1].name));
    $("#ctable tbody").innerHTML = rows.map(([code, c]) => {
      const ok = c.outlets.filter((o) => ["route_deep", "route_mid", "route_alt", "wayback_only"].includes(o.class)).length;
      const bl = c.outlets.filter((o) => o.class === "blocked").length;
      const hc = hum.countries[code] || {};
      const pill = (t) => `<span class="pill ${{ A: "good", B: "warn", C: "warn", D: "bad" }[t]}">${t} · ${esc(TIER[t])}</span>`;
      return `<tr data-num="${esc(c.numeric || "")}" tabindex="0"><td>${esc(c.name)}${c.pilot ? ' <span class="role">Pilot</span>' : ""}</td>
        <td class="num">${c.outlets.length}</td><td class="num">${ok}</td><td class="num">${bl}</td>
        <td>${pill(c.tier_past)}</td><td>${pill(c.tier_recent)}</td>
        <td class="num">${n(hc.ifrc_docs || 0)}</td><td class="num">${n(hc.reliefweb_reports || 0)}</td></tr>`;
    }).join("");
    document.querySelectorAll("#ctable tbody tr").forEach((tr) => {
      const go = () => tr.dataset.num && select(tr.dataset.num, tr.cells[0].textContent);
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
  }

  // ---------- news collection ----------
  function renderCollection(news) {
    const st = { listing: ["info", "Listing"], listed: ["good", "Listed"], "not started": ["idle", "Not started"] };
    const rows = (news.collection || []).slice().sort((a, b) => a.country.localeCompare(b.country) || a.role.localeCompare(b.role));
    $("#coltable tbody").innerHTML = rows.map((c) => {
      const [cls, label] = st[c.state] || ["idle", c.state];
      return `<tr><td>${esc(c.name)}</td><td>${esc(c.country)}</td><td><span class="role">${esc(ROLE[c.role] || c.role)}</span></td>
        <td><span class="pill ${cls}">${esc(label)}</span>${c.errors ? ` <span class="pill warn">${n(c.errors)} errors</span>` : ""}</td>
        <td class="num">${n(c.listed)}</td><td class="num">${n(c.sitemap_requests)}</td><td class="num">${n(c.fetched)}</td></tr>`;
    }).join("") || '<tr><td colspan="7">No pilot outlets in the registry.</td></tr>';
  }

  // ---------- load ----------
  function index(d) {
    const news = {}, hum = {};
    for (const [cc, c] of Object.entries(d.news.countries)) if (c.numeric) news[c.numeric] = { ...c, cc };
    for (const [cc, c] of Object.entries(d.humanitarian.countries)) hum[c.numeric] = { ...c, cc };
    d._byNumeric = { news, hum };
    return d;
  }

  async function load() {
    $("#refresh").disabled = true;
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      data = index(await r.json());
      $("#updated").textContent = "Updated " + new Date(data.generated_at).toLocaleString();
      renderStrip(data.system);
      renderSources(data.humanitarian, data.system.jobs);
      renderNews(data.news, data.humanitarian);
      renderCollection(data.news);
      drawMap();
      if (selected) select(selected.numeric, "");
    } catch (e) {
      $("#updated").textContent = "Could not load status (" + e.message + "). Is the dashboard server running?";
    } finally {
      $("#refresh").disabled = false;
    }
  }

  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    drawMap();
  }));
  $("#refresh").addEventListener("click", load);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawMap);

  fetch("/static/vendor/countries-110m.json").then((r) => r.json()).then((w) => { world = w; drawMap(); });
  load();
  setInterval(load, 60000);
})();
