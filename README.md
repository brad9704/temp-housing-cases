# temp-housing-cases

A research pipeline that builds a worldwide record of **problems in disaster sheltering and temporary housing** — what went wrong, why, and how it unfolded — from news archives and humanitarian documents.

The goal is coverage of problem *types*: every kind of problem that has occurred in evacuation shelters, prolonged shelter living, and temporary housing (prefab units, containers, tents, rental subsidies, hotels, host families, and so on).

## Scope

- **Stages** follow Quarantelli (1995): `emergency_sheltering`, `temporary_sheltering`, `temporary_housing`, `permanent_housing`. The first three are the analysis target; `permanent_housing` is kept as context.
- **Facility type and stage are separate axes.** A school gym used as a shelter is an existing building in `temporary_sheltering`; a rented flat under a rental-subsidy scheme (e.g. Japan's *minashi kasetsu*) is an existing building in `temporary_housing`.
- **Sources**
  1. News outlets: per country, one national daily and one broadcaster (core), plus one regional paper from the affected area (pilot probe).
  2. Humanitarian documents: IFRC GO appeal documents, Shelter Cluster, Shelter Projects case studies; ReliefWeb once API access is approved.
- **Unit of analysis**: a claim in one article or document. Episode (site × problem type × period), site, and disaster tables are always derived and never overwrite source records.

Disasters are defined from the collected texts. External disaster databases (EM-DAT etc.) are used only to normalise names and link records, not to discover events.

## Collection policy

This project collects only what sites allow, under an honest identity.

- One fixed User-Agent, `Mozilla/5.0 (compatible; UNISTDisasterHousingBot/<version>[; +<info URL>])`. No browser or crawler impersonation, no UA or proxy rotation, no TLS fingerprint spoofing.
- robots.txt is honoured, including `Crawl-delay`. An unreachable robots.txt is treated as "disallow all".
- Requests are paced per domain: a random delay between configured bounds, held across processes with a file lock. There is never more than one concurrent request per domain. `Retry-After` is honoured, followed by exponential backoff.
- Consecutive 403/429 responses or bot challenges (e.g. Cloudflare) stop collection from that domain. Challenges are never solved or bypassed. The legitimate routes are permission from the outlet, verified-bot identity (e.g. Web Bot Auth), or licensed databases.
- Outlets with an explicit text-and-data-mining or AI-use reservation are excluded. robots rules that only name specific AI crawlers, and Cloudflare Content-Signal values, are recorded but are not exclusion reasons, since this is not AI training.
- The Wayback Machine is used only to list articles of sites that allow us, or to read deleted articles of such sites. It is never used to get around a site that blocks us.
- No paywall or login bypass.
- Raw text is kept for internal analysis only and is never redistributed. Public outputs are limited to metadata, extracted fields, and short evidence quotes.

## Pipeline

| Stage | What it does |
|---|---|
| recon | Profile candidate outlets (access path, archive depth, extraction quality) |
| collect | Sitemap / archive-page / Wayback / API collectors; generic extraction first, custom parsers only where needed |
| dedup | Remove wire re-publications and near-duplicates (MinHash LSH), keeping the original records |
| filter | Two-tier regex filter in the original language (`codebook/regex_v0.yaml`) |
| audit | Recall audit on a random sample of filtered-out articles → next dictionary version |
| translate | Paragraph-level machine translation to English, for filtered articles only |
| classify | LLM relevance and stage classification |
| extract | Problem-episode fields, each with an evidence span |
| taxonomy | Open extraction → embedding clusters → human-defined categories → closed recoding |
| link | Site-name normalisation, episode clustering, disaster linking |
| validate | Inter-coder agreement, filter recall, linking precision/recall, coverage by country and language |

## Repository layout

```
collectors/   http.py (PoliteClient), recon_probe.py, pool.py, ifrc_go.py
codebook/     regex dictionaries, schema
registry/     outlets.csv, outlet_profiles/, screening.csv, frames/
reports/      stage summaries (recon, regional probe, outlet pool)
infra/llm/    llama-server launch script
tests/        unit tests for the HTTP client and screening helpers
data/         raw HTML (zstd), DuckDB, LLM caches — local only, not committed
```

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env        # then edit: crawler UA, delays, paths, LLM endpoints
uv run pytest -q
make diskcheck              # checks free space on RAW_DIR before collecting
```

LLM classification, translation, and extraction use local llama-server instances (OpenAI-compatible API) started via `infra/llm/start_llm.sh`. Endpoints and model names are read from `.env`.

## Common commands

```bash
# Inspect one outlet
uv run python -m collectors.recon_probe robots https://example.com
uv run python -m collectors.recon_probe fetch URL
uv run python -m collectors.recon_probe sitemap SITEMAP_URL --grep 2017-11

# Lightweight screening (≤10 requests per outlet), single or batch
uv run python -m collectors.recon_probe screen https://example.com --country KR --name "..." --outlet-type national_daily --lang ko --out screen.jsonl
uv run python -m collectors.recon_probe screen-batch candidates.csv --out screen.jsonl --workers 12

# Merge profiles and screening results into registry/screening.csv
uv run python -m collectors.pool screen.jsonl ... --out registry/screening.csv

# Validate outlet profiles
uv run python -m collectors.recon_probe validate registry/outlet_profiles/*.json

# Humanitarian track: IFRC GO appeal-document metadata
uv run python -m collectors.ifrc_go list-documents
uv run python -m collectors.ifrc_go summary

# Back up raw data after each collection batch (needs BACKUP_DIR in .env)
make backup
```

## Reproducibility

Every collection, filter, translation, and extraction record stores `code_commit` and `code_dirty`. Production runs are done only from a clean tree. Changes to regex dictionaries, prompts, and schemas each go in their own commit, and the commit message cites the evidence for the change (e.g. a recall-audit file).

## Status

- Recon done for the pilot countries (KR, JP, TR, IT, US, CL, MA, AU): 63 outlet profiles.
- Lightweight screening done for 379 outlets across 64 countries; the distribution is in `reports/outlet_pool.md`.
- IFRC GO metadata collected: 7,663 documents, 1,824 appeals, 157 countries.
- Next: Shelter Cluster document listing, sitemap and Wayback collectors, recall audit of the v0 dictionary.

## Limitations

- Non-English text is machine-translated, which is a limitation of the analysis.
- Coverage depends on what outlets keep online and allow us to crawl. Wayback-dependent outlets are incomplete, and capture rates are reported.
- Humanitarian documents are largely self-reported by implementing organisations. They are stored with a separate `source_type` and analysed separately from news.
