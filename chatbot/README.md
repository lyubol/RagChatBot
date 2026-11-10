# chatbot — PDF RAG ingestion pipeline

Databricks-native solution that turns a volume of PDFs into a searchable Vector
Search index for RAG, using built-in AI functions wherever possible
(`ai_parse_document`, `ai_prep_search`, managed embeddings).

## Solution layout

```
src/
├─ SetupNotebook                            # creates catalog rag_chatbot_dev, bronze/silver schemas, docs volume
├─ builtin/                                 # built-in AI functions pipeline (production path)
│  ├─ PDF Parsing (built-in AI functions)   # ai_parse_document -> bronze.doc_parsed_builtin (hash-cached)
│  ├─ PDF Chunking (built-in AI functions)  # ai_prep_search -> silver.doc_chunks_builtin
│  └─ Vector Search Setup (built-in AI functions)  # endpoint + Delta Sync index (TRIGGERED sync)
├─ pypdf/                                   # free pypdf variant (kept for comparison, not scheduled)
│  ├─ PDF Parsing (pypdf)                   # pypdf text extraction -> bronze.doc_parsed_pypdf
│  └─ PDF Chunking (pypdf)                  # fixed-window chunking -> silver.doc_chunks_pypdf
└─ app/                                     # RAG chatbot (Databricks App, Streamlit)
   ├─ app.py                                # retrieval + grounded answers + citations
   ├─ app.yaml                              # app command + environment configuration
   └─ requirements.txt
resources/
├─ pdf_rag_ingestion_job.yml                # job: setup -> parse -> chunk -> vector search sync
└─ rag_chatbot.app.yml                       # the chatbot app, deployed by the same bundle
```

All steps are idempotent and cache-aware: PDFs are parsed once (re-parsed only
when content changes, detected via SHA-256 hash), chunking refreshes cleanly,
and the Vector Search index sync (`TRIGGERED`) refreshes on demand. The index
embeds the context-enriched `content_to_embed` column with
`databricks-gte-large-en` on a standard endpoint (`rag-chatbot-dev`).

## Deployment

### 1. Deploy the bundle

- Click the **deployment rocket** 🚀 in the left sidebar to open the **Deployments** panel, then click **Deploy**.
- Or from the CLI: `databricks bundle deploy -t <dev|prod>` (see the target
  `workspace.host` values in `databricks.yml` — update them when deploying to a
  different workspace).

The deploy creates the `pdf_rag_ingestion` job (setup -> parse -> chunk ->
vector search sync) AND the `pdf-rag-chatbot-dev` app, and syncs all `src/`
notebooks. Deploying to a fresh workspace requires: permissions to create
catalogs, the `docs` volume populated with PDFs, and access to built-in AI
functions / model endpoints.

**Deploy the app's source code.** `bundle deploy` creates the app object but
does NOT put its code on compute — the app stays `UNAVAILABLE` until you run
this once (and after any change to `src/app/`):

```bash
databricks apps deploy pdf-rag-chatbot-dev \n  --source-code-path /Workspace/<path-to>/chatbot/src/app \n  --mode SNAPSHOT
```

If the app compute is stopped, start it first:
`databricks apps start pdf-rag-chatbot-dev`.

### One-time app permissions

After the FIRST deploy, the chatbot app's service principal (created by the
deploy) needs grants to reach the data and models. Do these once per workspace.

**1. Find the app's service principal UUID:**

```bash
databricks apps get pdf-rag-chatbot-dev --output JSON
```

Use the `service_principal_client_id` value as `<APP_SP_UUID>` below.

**2. Grant Vector Search endpoint access.** The CLI takes the endpoint's UUID,
not its name (get it with `databricks vector-search-endpoints get-endpoint rag-chatbot-dev`):

```bash
databricks vector-search-endpoints update-permissions <ENDPOINT_UUID> \n  --json '{"access_control_list": [{"service_principal_name": "<APP_SP_UUID>", "permission_level": "CAN_USE"}]}'
```

**3. Grant Unity Catalog access** (SQL editor, run as the catalog owner):

```sql
GRANT USE CATALOG ON CATALOG rag_chatbot_dev TO `<APP_SP_UUID>`;
GRANT USE SCHEMA ON SCHEMA rag_chatbot_dev.silver TO `<APP_SP_UUID>`;
GRANT SELECT ON TABLE rag_chatbot_dev.silver.doc_chunks_builtin TO `<APP_SP_UUID>`;
-- the Delta Sync index is its own securable — easy to miss:
GRANT SELECT ON TABLE rag_chatbot_dev.silver.doc_chunks_builtin_vs TO `<APP_SP_UUID>`;
```

**4. Grant LLM access.** The app generates answers with
`system.ai.meta-llama-3-3-70b-instruct` via Unity Gateway. App service
principals inherit no default access (they belong to no groups), and grants on
`system` / `system.ai` require a **metastore admin**:

```sql
GRANT USE CATALOG ON CATALOG system TO `<APP_SP_UUID>`;
GRANT USE SCHEMA ON SCHEMA system.ai TO `<APP_SP_UUID>`;
GRANT EXECUTE ON MODEL SERVICE system.ai.meta-llama-3-3-70b-instruct TO `<APP_SP_UUID>`;
```

Self-service alternative (no admin needed): create a model service in your own
catalog (e.g. `rag_chatbot_dev.silver.chatbot_llama`) routing to the same
Databricks-hosted llama, grant the app SP `EXECUTE` on it, and point the app at
it via the `LLM_MODEL` env var in `src/app/app.yaml`.

### 2. Run the job

- Hover the `pdf_rag_ingestion` job in the **Deployments** panel and click **Run**.
- First run in a new workspace takes ~10-15 minutes extra: it provisions the
  Vector Search endpoint. Subsequent runs are cache-hits and fast.

### 3. Use the chatbot

Open the `pdf-rag-chatbot-dev` app (Apps in the workspace sidebar, or the URL
from `databricks apps get pdf-rag-chatbot-dev`) and ask questions — answers are
grounded in the retrieved chunks and cite their source PDFs.

### 4. Query the index directly

Query `rag_chatbot_dev.silver.doc_chunks_builtin_vs` on endpoint
`rag-chatbot-dev` (SDK `query_index`, or the `ai_vector_search()` SQL
function).

## Documentation

- [Declarative Automation Bundles in the workspace](https://docs.databricks.com/aws/en/dev-tools/bundles/workspace-bundles)
- [Declarative Automation Bundles configuration reference](https://docs.databricks.com/aws/en/dev-tools/bundles/reference)
