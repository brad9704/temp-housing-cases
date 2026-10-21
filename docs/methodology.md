# Collection methodology

This document records how sources are found, screened, and collected, and the decisions behind each rule. It is kept up to date as the project proceeds; dated entries in the decision log record when each rule took effect.

Status as of 2026-10-21: recon and screening are finished, and humanitarian document metadata is being collected. Full-text news collection has not started yet.

## 1. Research object

The unit we look for is a **problem episode** at a sheltering or temporary-housing site: a problem, its cause(s) as attributed in the source, the response, and how it developed. The goal is coverage of problem *types*, so missing a type matters more than duplicating one.

**Contexts.** Sites are included whatever caused the displacement. Each record gets a `context_type`:

| `context_type` | Examples |
|---|---|
| `disaster` | earthquake, flood, cyclone, fire evacuation shelters; prefab or container housing; rental subsidies after disasters |
| `conflict` | IDP camps, collective centres for people displaced by armed conflict |
| `migration` | reception centres for migrants and refugees |
| `homelessness` | emergency and temporary accommodation for people experiencing homelessness |
| `mixed` / `unknown` | sites serving several populations, or unclear from the source |

**Stages.** Every record also gets a stage, following Quarantelli (1995): `emergency_sheltering`, `temporary_sheltering`, `temporary_housing`, `permanent_housing`. The first three are analysed; `permanent_housing` is kept as context.

- The stage is decided by function, meaning whether households have resumed daily routines. It is not decided by building type or by length of stay alone.
- Facility type (new build vs. existing building) is a separate field. For example, rental subsidies and deemed temporary housing (Japan's *minashi kasetsu*) use existing housing stock but are `temporary_housing`. A school gym is an existing building used for `temporary_sheltering`.

**Unit of analysis.** A claim in one article or document. Episodes, sites and events are derived tables and never overwrite source records.

**Events.** Disasters and other events are identified from the texts themselves. External disaster databases (e.g. EM-DAT) are not used to discover events, only to normalise names and link records.

## 2. Sources

Two source types are kept apart through `source_type`:

1. **News** (`news`): outlet archives collected directly from outlet websites.
2. **Humanitarian documents** (`humanitarian`): situation reports, operation updates, final reports, evaluations and case studies from humanitarian organisations.

They complement each other. News coverage is strongest in high-income countries. Humanitarian documents concentrate on internationally assisted crises in low- and middle-income countries. Humanitarian documents are largely self-reports by implementing organisations, so problem-type distributions are reported separately by source type.

## 3. Access and ethics rules

All HTTP requests go through one client (`collectors/http.py`, `PoliteClient`). It enforces:

- **Identity.** One fixed User-Agent: `Mozilla/5.0 (compatible; UNISTDisasterHousingBot/0.1; +https://brad9704.github.io/temp-housing-cases/)`. There is a public information page with blocking instructions. No browser or crawler impersonation, no UA or proxy rotation, no TLS fingerprint spoofing; the UA is validated at start-up.
- **robots.txt.** Rules for `*` and for our token are honoured, including `Crawl-delay`.
  - A 4xx response for robots.txt means allow all. A 5xx, a connection failure or a TLS failure means disallow all (RFC 9309).
  - Parsed robots files are cached for 24 h.
- **Pacing.**
  - At most one concurrent request per domain.
  - Requests are spaced by a uniform random delay of 2–5 s, or by `Crawl-delay` if that is longer.
  - Pacing state is shared across processes through a per-domain file lock, so parallel jobs cannot exceed it.
  - web.archive.org uses a slower override of 5–8 s with a 120 s timeout.
- **Errors.** `Retry-After` is honoured on 429/503, followed by jittered exponential backoff.
  - Collection from a domain stops after repeated 403/429 responses or a bot challenge. Blocked domains are not retried.
  - Screening returns `crawl_delay_excessive` for a `Crawl-delay` above 60 s instead of waiting.
- **No circumvention.** No paywall or login bypass. Bot challenges (e.g. Cloudflare) are never solved or evaded. The legitimate routes are permission from the site, a verified-bot identity (e.g. Web Bot Auth), or a licensed database.
- **Wayback Machine.** It is used only to list articles of sites that allow us, or to read deleted articles of such sites. It is never used to reach a site that blocks us.
- **Use of collected text.** Internal, non-commercial analysis only. Raw text is never redistributed. Public outputs contain metadata, coded fields and short quotations. Content is not used to train AI models.

### Decision log

| Date | Decision |
|---|---|
| 2026-10-20 | Exclude outlets whose robots.txt or terms carry an explicit text-and-data-mining or AI-use reservation, e.g. "reserves the right … text and data mining (§44b UrhG)" or "not permitted … for LLMs, machine learning". |
| 2026-10-20 | Blocked outlets are not collected through Wayback replays (e.g. SNRT, Morocco, rejected). |
| 2026-10-21 | robots rules that only name specific AI crawlers (GPTBot, ClaudeBot, …) and do not apply to `*` or to our token are recorded (`ai_bots_named`) but are not exclusion reasons. |
| 2026-10-21 | Cloudflare Content-Signal values (`ai-train`, `ai-input`) are recorded but are not exclusion reasons, because this is not AI training. Example: Corriere della Sera (`ai-input=no`) remains in the pool. |
| 2026-10-21 | Bot challenges count as blocks. Remedies are permission, verified-bot identity, or licensed databases only. |
| 2026-10-21 | ReliefWeb API is used with an approved appname. `api.reliefweb.int` disallows all crawlers in robots.txt, so it is configured as an explicit, documented exception (`CRAWL_ROBOTS_EXCEPTIONS`), limited to approved API hosts. Responses obtained this way are flagged. |
| 2026-10-21 | Scope covers all three sheltering and temporary-housing stages, and all displacement contexts (disaster, conflict, migration, homelessness). |

## 4. News outlets

### 4.1 Selection rule

For each country the core collection takes **one major national daily and one major broadcaster**. They are chosen for national reach, archive depth and accessibility, and the reason is recorded per outlet. The pilot also adds **one regional outlet from the affected area** as a probe. The probe measures how many problem episodes appear only in regional media, and how that share changes with time since the event.

### 4.2 Detailed recon (pilot countries)

Eight pilot countries were profiled in depth, each with a baseline event and a three-year window: KR, JP, TR, IT, US, CL, MA, AU. Each outlet profile records:

- access path: sitemap, archive page, Wayback, API or none;
- robots status, paywall type and JS-rendering need;
- archive start year and articles per day;
- URL and date patterns, wire-copy markers and correction handling;
- extraction success rate on a sample of baseline-window articles;
- evidence URLs.

Recon was done through a probe CLI (`collectors/recon_probe.py`) that routes every request through `PoliteClient`. It produced 63 profiles: 9 core dailies, 7 core broadcasters, 7 regional probes and 40 rejected candidates, each with its rejection evidence.

**Extraction success** means HTTP 200, a trafilatura body of at least 300 characters, and an extracted date.

### 4.3 Lightweight screening (pool expansion)

The pool was widened beyond the pilot to see how collectable outlets are distributed across countries.

- **Candidates.** Per country, 4–6 candidates were picked from agent knowledge and web search. The aim was 2 national dailies, 1–2 broadcasters, the national wire agency and 1 regional outlet from a recently affected area.
  - This frame is not reproducible. A Wikidata SPARQL frame (`queries/wikidata_news_outlets.rq`) has been prepared and must be run manually, because query.wikidata.org disallows crawlers. It will replace the ad hoc frame.
- **Protocol** (`recon_probe screen`, at most 10 requests and at most 2 sample articles per outlet):
  1. robots.txt: mode, failure reason, `Crawl-delay`, explicit TDM reservation, named AI bots, Content-Signal;
  2. homepage: status, bot-challenge markers, paywall hints;
  3. sitemaps: from robots.txt, or from `/sitemap.xml` and `/sitemap_index.xml`. Year range is parsed from URLs and `lastmod`, keeping only years 1995 to the current year;
  4. one newest and one oldest sample article, with extraction.
- **Verdicts**:
  - `blocked_robots`, `blocked_robots_unreachable`, `blocked_http` (401/402/403/406/429 or challenge), `tdm_reserved`, `crawl_delay_excessive`;
  - `sitemap_deep` (≤2015), `sitemap_mid` (2016–2020), `sitemap_recent_only` (≥2021), `sitemap_undated`, `no_sitemap`.
- **Follow-up.** Up to 3 extra requests per important outlet checked non-sitemap routes such as sequential IDs and date archives. Only routes confirmed by a successful request are recorded (`route_alt`).
- **Runs.**
  - First round: regional agents.
  - Final round: a batch re-screen of all non-blocked outlets with the final tool version (`screen-batch`, parallel across domains only).
  - Domains blocked in the first round (58) were not re-requested.

**Result** (379 outlets, 64 countries plus one international outlet):

| Class | Outlets |
|---|---|
| blocked (robots, HTTP or challenge, TDM, excessive delay) | 115 |
| listing route to ≤2015 (sitemap years; not all verified by article date) | 84 |
| sitemap from 2021 only | 76 |
| accessible, no sitemap or undated (route unknown) | 53 |
| listing route from 2016–2020 | 24 |
| no route for the baseline window (detailed recon) | 12 |
| alternative route confirmed (sequential ID or date archive) | 8 |
| Wayback-only listing | 7 |

For events from 2021 on, 50 of 64 countries have a listable press outlet and a listable broadcaster or wire agency. For events before 2021, 29 countries do.

## 5. Humanitarian documents

| Source | Access | Listing method | Status |
|---|---|---|---|
| IFRC GO | public API (robots allow) | `appeal_document` endpoint, paginated | metadata collected: 7,663 documents, 1,824 appeals, 157 countries |
| ReliefWeb (OCHA) | API with approved appname (explicit robots exception) | `v2/reports`, theme "Shelter and Non-Food Items", monthly slices, ≤900 calls/day | metadata collection in progress (about 171,000 reports) |
| Global Shelter Cluster | website (robots allow; Content-Signal `ai-train=no` only) | sitemap index (13 pages), keeping `/<group>/documents/<slug>` | URL list collected: 21,039 documents in 305 response groups |
| Shelter Projects | case-study books | server connection times out; downloaded manually | pending |

Only metadata and document URLs have been collected so far. Document text (mostly PDF) will be extracted per page, and evidence offsets will be stored as (page, character offset).

## 6. Text processing (planned)

1. **Dedup.** Wire re-publications and near-duplicates are found with MinHash LSH; original records are kept.
2. **Filter.** A two-tier regex filter in the original language:
   - tier 1 terms pass alone;
   - tier 2 terms need a context term nearby.

   The v0 dictionary (`codebook/regex_v0.yaml`) only changes on recall-audit evidence. Candidate terms for the widened scope (`codebook/candidates_v1.yaml`) are recorded but not applied. The v0 context terms are disaster-only, so the widened scope (conflict, migration, homelessness) needs context terms that go through the same audit.
3. **Audit.** A random sample of filtered-out articles is checked for missed relevant ones. The result produces the next dictionary version, recorded in a changelog.
4. **Translate.** Filtered non-English texts are machine-translated per paragraph. Proper nouns keep the original form in parentheses. Paragraph IDs map 1:1 between source and translation.
5. **Classify and extract.** A local LLM does the work (llama-server, Gemma family, quantisation recorded) with schema-constrained JSON output. Every extracted field carries evidence spans in both the original and the translated text.
   - Causes are recorded only as attributed in the source: the quoted person or organisation, the reporter, or unknown. The model's own inferences are not recorded as causes.
6. **Taxonomy.** Open extraction on a pilot sample is clustered with embeddings. Humans then fix the categories, and everything is recoded with the closed set.
7. **Validate.**
   - inter-coder agreement (Krippendorff's α);
   - filter recall;
   - linking precision and recall;
   - coverage by country, language and source type;
   - a second translation path on a random sample, flagging articles whose extracted fields change.

## 7. Reproducibility

- Every collected record stores `code_commit` and `code_dirty`; untracked files are ignored for `code_dirty`. Production runs require a clean tree.
- Dictionary, prompt and schema changes are separate commits, and each message cites its evidence.
- LLM calls are cached as JSONL with model, quantisation and prompt version.
- Raw data, registries and reports stay on the research machine and are backed up incrementally. This repository contains code only.

## 8. Known limitations

- **Machine translation** of non-English text.
- **Selection bias.** Outlets that block automated access, often behind Cloudflare, are excluded. This removes many outlets in Egypt, Jordan, Albania, Zimbabwe and Indonesia.
  - Explicit TDM reservations remove major outlets in Germany (FAZ, SZ, ARD) and the UK (Guardian, BBC).
- **Archive depth and Wayback.** Many outlets keep only recent sitemaps. Wayback captures favour prominent pages, so follow-up stories months after an event are under-captured. Capture rates will be reported per outlet and by time since the event.
- **Screening depth** for many outlets rests on sitemap years rather than verified article dates.
- **Self-reporting.** Humanitarian documents are self-reported and concentrate on internationally assisted crises.
- **Stage ambiguity.** The stage mapping is genuinely ambiguous for:
  - hotel and host-family arrangements;
  - rental subsidies without a confirmed dwelling;
  - long-lived camps;
  - dispersed sheltering (staying at home, in cars or in damaged houses);
  - transitional shelters designed to become permanent.

  Classification rules for these cases are part of the codebook.
