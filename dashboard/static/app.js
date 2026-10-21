(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const fmt = new Intl.NumberFormat("en-US");
  const n = (x) => (x == null ? "–" : fmt.format(x));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bytes = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : b >= 1e3 ? (b / 1e3).toFixed(0) + " KB" : (b || 0) + " B");
  const dur = (sec) => {
    if (sec == null) return "–";
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(s2).padStart(2, "0")}s` : `${s2}s`;
  };
  const runPill = (label = "Running") => `<span class="pill info run"><span class="spin" aria-hidden="true"></span>${esc(label)}</span>`;
  let errSeq = 0;
  const errPill = (count, samples, inline = true) => {
    if (!count) return "";
    const id = "err" + (++errSeq);
    const msg = (samples && samples.length ? samples : ["No message recorded."]).join("\n");
    return `<button type="button" class="pill bad errbtn" aria-expanded="false" aria-controls="${id}" data-msg="${esc(msg)}" data-inline="${inline}">${n(count)} error${count === 1 ? "" : "s"}</button>`;
  };
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

  let data = null, mode = "any", selected = null, world = null;
  const NAME_ALIAS = { Somaliland: "706" };  // 지도에 ISO 코드가 없는 지역: 문서는 소말리아로 분류된다
  const NO_ISO = new Set(["Kosovo", "N. Cyprus"]);
  const LISTABLE = ["route_deep", "route_mid", "route_alt", "wayback_only", "route_recent"];
  const key = (d) => d.id || NAME_ALIAS[d.properties.name] || null;
  function anyState(num) {
    const nc = data._byNumeric.news[num], hc = data._byNumeric.hum[num];
    const news = !!(nc && nc.outlets.some((o) => LISTABLE.includes(o.class)));
    const docs = !!(hc && hc.total);
    return news && docs ? "both" : docs ? "docs" : news ? "news" : "none";
  }

  // ---------- top strip and sources ----------
  function renderStrip(sys) {
    const free = sys.disk_free_gb, warn = free < sys.min_free_gb;
    $("#disk").innerHTML = `${n(Math.round(free))} GB ${warn ? '<span class="pill bad">Below minimum</span>' : ""}`;
    $("#raw").textContent = sys.raw_gb >= 1 ? sys.raw_gb.toFixed(2) + " GB" : Math.round(sys.raw_gb * 1000) + " MB";
    $("#jobs").innerHTML = sys.jobs.length
      ? sys.jobs.map((j) => `<span class="pill info run" title="started ${esc(new Date(j.since).toLocaleString())}"><span class="spin" aria-hidden="true"></span>${esc(j.job)} · ${esc(dur(j.runtime_sec))}</span>`).join("")
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
      const job = jobs.find((j) => (s.id === "reliefweb" ? j.job.startsWith("reliefweb") : j.job.includes(s.id)));
      const statePill = cls === "info" ? runPill(job ? `Running · ${dur(job.runtime_sec)}` : label) : `<span class="pill ${cls}">${esc(label)}</span>`;
      let body = "";
      if (s.selected != null) {
        const pct = s.selected ? Math.min(100, (100 * s.ok) / s.selected) : 0;
        body = `
          <div><div class="prog"><span>Documents fetched</span><span>${n(s.ok)} / ${n(s.selected)}</span></div>
          <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="${s.selected}" aria-valuenow="${s.ok}"><span style="width:${pct.toFixed(1)}%"></span></div></div>
          <ul class="facts">
            <li>Includes audit sample of excluded: <b>${n(s.audit_sample)}</b></li>
            ${s.attached_files ? `<li>Attached files: <b>${n(s.attached_files)}</b></li>` : ""}
            <li>Failed: <b>${n(s.failed)}</b> ${errPill(s.failed, s.error_samples)}</li>
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
            ${s.error_samples && s.error_samples.length ? `<li>${s.months_done >= s.months_expected ? "Earlier stops (resumed and finished)" : "Stops logged"}: ${errPill(s.error_samples.length, s.error_samples)}</li>` : ""}
            <li>Top formats: ${esc((s.formats || []).slice(0, 3).map((f) => f[0] + " " + fmt.format(f[1])).join(", "))}</li>
          </ul>`;
      } else {
        body = `<ul class="facts"><li>Server cannot reach the site. Download the books into the manual folder.</li></ul>`;
      }
      return `<article class="src">
        <div class="src-head"><span class="src-name">${esc(s.name)}</span>${statePill}</div>
        <div><div class="big">${n(s.metadata)}</div><div class="unit">${esc(s.metadata_unit)}</div></div>
        ${body}
        <div class="src-access">${esc(s.access)}</div>
      </article>`;
    }).join("") + globalCard(h);
  }

  function globalCard(h) {
    const rw = h.sources.find((s) => s.id === "reliefweb");
    if (!rw || !rw.global_reports) return "";
    const label = (c) => (c === "wld" ? "World" : c.toUpperCase());
    return `<article class="src">
      <div class="src-head"><span class="src-name">Global and regional reports</span><span class="pill idle">Not on the map</span></div>
      <div><div class="big">${n(rw.global_reports)}</div><div class="unit">ReliefWeb reports whose main subject is not a single country</div></div>
      <ul class="facts">${(rw.global_breakdown || []).map((g) => `<li>${esc(label(g[0]))}: <b>${n(g[1])}</b></li>`).join("")}</ul>
      <div class="src-access">Counted in ReliefWeb totals; excluded from country figures</div>
    </article>`;
  }

  // ---------- map ----------
  function colorFor(num, name) {
    if (!num) return css("--land-none");
    const nc = data._byNumeric.news[num], hc = data._byNumeric.hum[num];
    if (mode === "any") {
      return css({ both: "--ramp-4", docs: "--ramp-2", news: "--ramp-3", none: "--land-none" }[anyState(num)]);
    }
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
    if (mode === "any") {
      items = [["--ramp-4", "News and documents"], ["--ramp-3", "News only"], ["--ramp-2", "Documents only"], ["--land-none", "Nothing yet, or no ISO code in the map (Kosovo, N. Cyprus)"]];
    } else if (mode === "hum") {
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
    if (!num) return h + "<br>No ISO code in the map data; documents cannot be joined";
    if (name === "Somaliland") h += " <span style=\"color:var(--muted)\">(documents filed under Somalia)</span>";
    if (mode === "any") {
      const st = anyState(num);
      h += "<br>" + { both: "Listable news outlets and humanitarian documents", docs: "Humanitarian documents only", news: "Listable news outlets only", none: "Nothing collected yet" }[st];
      h += `<br>Documents ${n(hc?.total || 0)} · outlets tried ${nc ? nc.outlets.length : 0}`;
    } else if (mode === "hum") {
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
      .attr("class", (d) => "c" + (selected && key(d) === selected.numeric ? " sel" : ""))
      .attr("d", path)
      .attr("fill", (d) => colorFor(key(d), d.properties.name))
      .attr("tabindex", (d) => (key(d) && (data._byNumeric.news[key(d)] || data._byNumeric.hum[key(d)]) ? 0 : null))
      .on("mousemove", (ev, d) => {
        tip.innerHTML = tipHtml(key(d), d.properties.name);
        tip.hidden = false;
        const r = box.getBoundingClientRect();
        let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
        if (x + 260 > r.width) x = ev.clientX - r.left - 270;
        tip.style.left = Math.max(0, x) + "px";
        tip.style.top = y + "px";
      })
      .on("mouseleave", () => { tip.hidden = true; })
      .on("click", (ev, d) => key(d) && select(key(d), d.properties.name))
      .on("keydown", (ev, d) => { if ((ev.key === "Enter" || ev.key === " ") && key(d)) { ev.preventDefault(); select(key(d), d.properties.name); } });
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
    d3.selectAll("#map path.c").classed("sel", (d) => key(d) === num);
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
    const stage = (c) => c.state === "listing" ? runPill("Listing") : `<span class="pill ${(st[c.state] || ["idle"])[0]}">${esc((st[c.state] || [0, c.state])[1])}</span>`;
    const rows = (news.collection || []).slice().sort((a, b) => a.country.localeCompare(b.country) || a.role.localeCompare(b.role));
    $("#coltable tbody").innerHTML = rows.map((c) => {
      const [cls, label] = st[c.state] || ["idle", c.state];
      return `<tr><td>${esc(c.name)}</td><td>${esc(c.country)}</td><td><span class="role">${esc(ROLE[c.role] || c.role)}</span></td>
        <td>${stage(c)} ${errPill(c.errors, c.error_samples, false)}</td>
        <td class="dur" title="${c.started_at ? "started " + esc(new Date(c.started_at).toLocaleString()) : ""}">${c.state === "not started" ? "–" : dur(c.runtime_sec)}${c.state === "listed" ? " total" : ""}</td>
        <td class="num">${n(c.listed)}</td><td class="num">${n(c.sitemap_requests)}</td><td class="num">${n(c.fetched)}</td></tr>`;
    }).join("") || '<tr><td colspan="8">No pilot outlets in the registry.</td></tr>';
  }

  const openErrors = new Set();
  function showErrors(b, id) {
    const box = document.createElement("div");
    box.className = "errbox"; box.id = id; box.textContent = b.dataset.msg;
    const tr = b.closest("tr");
    if (b.dataset.inline === "false" && tr) {
      const row = document.createElement("tr"); row.className = "errrow"; row.id = id;
      const td = document.createElement("td"); td.colSpan = tr.cells.length; box.removeAttribute("id"); td.appendChild(box);
      row.appendChild(td); tr.after(row);
    } else {
      b.parentElement.appendChild(box);
    }
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
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("button.errbtn");
    if (!b) return;
    const open = b.getAttribute("aria-expanded") === "true";
    const id = b.getAttribute("aria-controls");
    document.getElementById(id)?.remove();
    if (open) { b.setAttribute("aria-expanded", "false"); openErrors.delete(b.dataset.key); return; }
    b.setAttribute("aria-expanded", "true");
    showErrors(b, id);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawMap);

  fetch("/static/vendor/countries-110m.json").then((r) => r.json()).then((w) => { world = w; drawMap(); });
  load();
  setInterval(load, 60000);
})();
