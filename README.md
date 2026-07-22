# KSP · Crime Intelligence & Analytical Platform (Pilot)

State Crime Records Bureau — Karnataka. Pilot implementation of the platform
covering geospatial hotspots + predicted density, deep criminological network
analysis, cross-border crimes, law & order status, anomaly detection, and a
schema-grounded LLM assistant.

The runtime uses **synthetic data whose schema exactly matches the KSP FIR
System** (CaseMaster, ComplainantDetails, Victim, Accused, ArrestSurrender,
Act/Section, CrimeHead/SubHead, all masters). Swapping in real data means
loading the same tables from the CCTNS export — no changes to the API or UI.

---

## Feature map (RFP ↔ implementation)

| Capability                                              | Where                                                     |
| ------------------------------------------------------- | --------------------------------------------------------- |
| Interactive geospatial maps, district drill-down        | Geospatial tab · `/geo/heatmap`, `/geo/points`            |
| Spatiotemporal hotspot clusters                         | Geospatial · 2 km grid heatmap, filterable                |
| **Predicted future crime density**                      | **Predict tab · Holt-Winters forecast per station × head** |
| Emerging trend alerts                                   | Anomalies tab · Poisson z-score + 90-day growth           |
| Relationship mapping (suspect/victim/location)          | Network tab · vis-network graph                           |
| Repeat-offender tracking                                | Network → Repeat offenders                                |
| **Deep criminological network analysis**                | **Network → Communities (Louvain), Central figures, MO similarity** |
| **Cross-border crimes**                                 | **Cross-border tab · arrest map, home→arrest lines, offenders** |
| **Law & order system information**                      | **Law & Order tab · court pendency, IO workload, chargesheet rate, gravity mix** |
| Socio-economic overlays                                 | `/geo/district-summary` — per-lakh rate uses census        |
| Anomaly detection                                       | Anomalies tab                                             |
| **AI/ML-driven natural-language querying**              | **Assistant tab · Text-to-SQL over the KSP schema**       |
| **Open-source LLM training on the schema**              | **`scripts/train_llm.py` — LoRA fine-tune (Phi-3, Gemma, Llama-3)** |

## Architecture

```
┌────────────────────┐     HTTP JSON     ┌─────────────────────────────┐
│  Single-page UI    │ ────────────────▶ │  FastAPI backend            │
│  Leaflet + heat    │                   │                             │
│  Chart.js          │                   │  backend/main.py            │
│  vis-network       │ ◀──────────────── │  backend/prediction.py      │  Holt-Winters
│  vanilla JS        │                   │  backend/network_analysis.py│  NetworkX / Louvain
└────────────────────┘                   │  backend/llm.py             │  Text-to-SQL + safety
                                         └─────────────┬───────────────┘
                                                       │
                                         ┌─────────────▼───────────────┐
                                         │  SQLite (KSP FIR schema)    │
                                         │  ports to PostGIS unchanged │
                                         └─────────────────────────────┘
                                                       ▲
                                         ┌─────────────┴───────────────┐
                                         │  Optional local LLM         │
                                         │  Ollama · vLLM · LM Studio  │
                                         │  or LoRA-fine-tuned model   │
                                         └─────────────────────────────┘
```

## Running

### Native

```bash
pip install -r backend/requirements.txt
python scripts/generate_data.py --db data/ksp.db --firs 20000
KSP_DB=$(pwd)/data/ksp.db uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000**.

### Docker

```bash
docker compose up --build
```

The DB is baked into the image. To point at a real (mounted) DB, set the
`KSP_DB` env var and volume-mount your `.db` file.

## Data model

The complete DDL is in [`data/schema.sql`](data/schema.sql). Highlights:

* `CaseMaster` — one row per FIR. **CrimeNo** follows the spec's 18-digit
  format: `<CaseCatCode(1)><District(4)><Unit(4)><Year(4)><Serial(5)>` with a
  per-station-per-year running serial.
* `ArrestSurrender.ArrestSurrenderStateId` → out-of-state arrests drive the
  Cross-border module.
* `Accused.person_link_id` — synthetic identity link used by the network
  analysis. In production this is populated by the CCTNS Aadhaar/PID join.
* `ChargesheetDetails.cstype` — 'A' (chargesheet) / 'B' (false) / 'C' (undetected).
* `weekly_counts` — materialized weekly aggregate used by the forecaster.

## API surface

Full OpenAPI at `/docs`. Grouped:

**Geospatial** — `/geo/heatmap`, `/geo/points`, `/geo/district-summary`
**Prediction** — `/predict/density-map`, `/predict/unit/{id}/head/{id}`, `/predict/district/{id}/head/{id}`
**Network** — `/offenders/top`, `/network/offender/{pid}`, `/network/communities`, `/network/central-figures`, `/network/mo-similarity`
**Cross-border** — `/cross-border/summary`, `/cross-border/movements`, `/cross-border/offenders`
**Law & Order** — `/law-order/court-pendency`, `/law-order/io-workload`, `/law-order/chargesheet-rate`, `/law-order/gravity-mix`, `/law-order/status-funnel`
**Trends / anomalies** — `/anomalies`, `/trends/emerging`
**Assistant** — `POST /assistant/ask {question}`, `GET /assistant/health`

## Prediction module

`backend/prediction.py` implements Holt-Winters exponential smoothing with an
additive weekly seasonality (falls back to non-seasonal when < 2 cycles of
history are available). It runs per (`Unit`, `CrimeHead`), on a materialized
`weekly_counts` table populated during ingest. The `/predict/density-map`
endpoint rolls point forecasts up to a lat/lng heatmap for the next N weeks —
the "predict future crime density" view the RFP asks for.

## Network module

`backend/network_analysis.py`:
1. Builds the co-accused edge set from `Accused` × `CaseMaster`.
2. Runs **Louvain community detection** (via NetworkX) to reveal gangs.
3. Computes **weighted-degree + betweenness centrality** for a "central
   figures" leaderboard (the graph-theoretic version of a kingpin score).
4. Computes **cosine similarity over sub-head vectors** to surface offenders
   with matching MOs — a case-linkage candidate list.

Results are cached in-process; invalidated on DB mtime change.

## Cross-border module

Reads `ArrestSurrender.ArrestSurrenderStateId` to identify arrests executed
outside Karnataka. The UI overlays home-district → arrest-district lines and
lists offenders arrested across multiple states.

## Law & Order module

Endpoints answer the standard SCRB questions:

* Which courts are the most backed up (`/law-order/court-pendency`)?
* Which IOs are overloaded, and how many of their cases are still open
  (`/law-order/io-workload`)?
* Which districts have the highest chargesheet rate, and how many "B"-report
  (false) or "C"-report (undetected) cases (`/law-order/chargesheet-rate`)?
* What's the case-status funnel state-wide (`/law-order/status-funnel`)?

## LLM assistant

The assistant lets a user ask questions in plain English and returns a
grounded SQLite query + result table. Every request travels through a
**safety layer** — read-only, single statement, allowlisted tables, forced
LIMIT — before hitting the database.

### Zero-config (offline) mode

Works out of the box using intent templates keyed to the KSP schema. Enough
coverage to demo (top offenders, cross-border arrests, chargesheet rates,
gravity by district, etc.) with no LLM download required.

### Local Ollama (recommended for pilot deployment)

```bash
# On the host
brew install ollama    # or apt/yum equivalent
ollama pull llama3.2:1b   # 1.3 GB, fast on CPU; or llama3.1:8b if you have a GPU

# Point the backend at it
export KSP_LLM_BACKEND=ollama
export OLLAMA_HOST=http://localhost:11434
export KSP_LLM_MODEL=llama3.2:1b
uvicorn backend.main:app --port 8000
```

The prompt injects the full KSP schema summary; the model returns strict JSON
`{"sql": "...", "explanation": "..."}` which the safety layer executes.

### OpenAI-compatible (vLLM / LM Studio / LocalAI)

```bash
export KSP_LLM_BACKEND=openai
export OPENAI_BASE_URL=http://localhost:8001/v1
export OPENAI_API_KEY=sk-none
export KSP_LLM_MODEL=phi-3-mini
```

### Fine-tuning on the KSP schema

`scripts/train_llm.py` prepares a supervised text-to-SQL dataset (schema
prompt → NL question → JSON SQL/explanation) and runs LoRA fine-tuning on any
Hugging-Face causal-LM. Curated examples ship in the script and can be
extended via `data/qa.jsonl`.

```bash
# Requires GPU (>= 16 GB VRAM recommended)
pip install torch transformers peft datasets accelerate bitsandbytes
python scripts/train_llm.py \
    --base microsoft/phi-3-mini-4k-instruct \
    --out models/ksp-sql-adapter \
    --epochs 3
# Or just prepare the training JSONL:
python scripts/train_llm.py --prepare-only
```

Merge the adapter, quantise to GGUF, and load via Ollama:

```bash
# Rough workflow
python -m peft.merge_adapter --base_model microsoft/phi-3-mini-4k-instruct \
    --peft_model models/ksp-sql-adapter --output_dir models/ksp-sql-merged
llama.cpp/convert.py models/ksp-sql-merged --outfile models/ksp-sql.gguf --outtype q4_K_M
ollama create ksp-sql -f Modelfile   # Modelfile: FROM ./models/ksp-sql.gguf
export KSP_LLM_MODEL=ksp-sql
```

## Deploying to Zoho Catalyst

The pilot is packaged for **Catalyst AppSail** (Zoho's containerized service
tier). The `app-server/` directory is the self-contained deployable — sync
your latest source into it with `./build-catalyst.sh` before every deploy.

### One-time setup

```bash
# 1. Install the CLI (requires Node.js).
npm install -g zcatalyst-cli

# 2. Log in with your Zoho account (opens a browser).
catalyst login

# 3. Bind this working directory to a Catalyst project.
catalyst init
#   Choose:  Existing project (or Create new)
#   Choose:  AppSail  → Python_3_1x → Catalyst-Managed Runtime
```

`catalyst init` overwrites `catalyst.json` with your real `projectId`. Commit
the updated file so teammates can `catalyst deploy` too.

### Deploy

```bash
./build-catalyst.sh --deploy
# equivalent to: ./build-catalyst.sh && catalyst deploy
```

After the first deploy the CLI prints the public URL (something like
`https://ksp-cip-server-<id>.catalystserverless.com`). Open that URL and the
dashboard loads.

### What happens on Catalyst

1. `catalyst deploy` uploads `app-server/` (source + `app-config.json`).
2. Catalyst runs the `predeploy` hook — `pip install -t .` — which vendors
   FastAPI / uvicorn / networkx / httpx into the build directory.
3. On cold start, the container executes `start.sh`, which:
   - Regenerates the synthetic SQLite DB at `/tmp/ksp.db` (AppSail filesystems
     are ephemeral; `/tmp` is writable per-container).
   - Boots uvicorn on the port Catalyst provides via
     `X_ZOHO_CATALYST_LISTEN_PORT`.

Cold-start DB regeneration takes ~10s for 20k FIRs. To ship a smaller /
larger DB, edit `KSP_FIRS_ON_BOOT` in `app-server/app-config.json`.

### For production data

Do **NOT** rely on the ephemeral `/tmp/ksp.db` for real KSP data — the
filesystem is wiped across redeploys and scaling events. Migrate to one of:

* **Catalyst Data Store** — relational, queried via ZCQL. Requires porting
  the schema/ingest to Catalyst's SQL dialect.
* **Catalyst File Store / Stratus** — bulk upload a real SQLite DB and mount
  it read-only. Simplest path if you just want to promote a curated dataset.
* **External PostgreSQL / PostGIS** — the recommended long-term option; the
  ORM layer in `backend/` is trivially portable (SQL is standard).

### Custom domain & CORS

* Domain mappings: **Cloud Scale → Domain Mappings** in the console; requires
  a free Zoho Group SSL certificate (~48h to issue) and a CNAME record.
* Cross-origin whitelist: **Authentication → Whitelisted Domains** (not code-
  configured).

## Phase 2 roadmap

1. **CCTNS live ingest** — nightly delta + incremental baseline updates.
2. **PostGIS** — replace grid heatmap with H3-indexed KDE.
3. **Spatiotemporal DBSCAN** — cluster events using (lat, lng, time-bucket)
   directly rather than pure lat/lng bins.
4. **Community detection over time** — evolving-graph algorithms to detect
   gang formation/split events.
5. **Case linkage** — extend MO-cosine to (MO + weapon + geo-radius + hour
   band) with a per-district similarity threshold.
6. **RBAC + audit log** — SCRB, District SP, and SHO views; all queries
   (including LLM assistant SQL) are logged.
7. **On-prem LLM cluster** — vLLM behind an internal load balancer; the
   fine-tuned adapter served identically to the pilot's Ollama path.

## Files

```
ksp-cip/
├── backend/
│   ├── main.py                # API + static frontend
│   ├── prediction.py          # Holt-Winters forecast + density map
│   ├── network_analysis.py    # Louvain / centrality / MO similarity
│   ├── llm.py                 # Schema-grounded text-to-SQL + safety
│   └── requirements.txt
├── frontend/
│   ├── index.html             # 8 tabs, no build step
│   ├── app.css
│   └── app.js
├── data/
│   ├── schema.sql             # KSP FIR schema (25 tables)
│   └── ksp.db                 # generated at build time
├── scripts/
│   ├── generate_data.py       # synthetic Karnataka dataset
│   └── train_llm.py           # LoRA fine-tune recipe
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Note on synthetic data

Personal names in the dataset are combinatorial fictions — no relation to any
real person. Only geographic reference data (Karnataka + neighbour district
centroids, KA population/literacy) is real.
