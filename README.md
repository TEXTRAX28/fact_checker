# Fact Checker

An evidence-based fact-checking application for articles and pasted text. The
interface is a Chrome extension backed by a FastAPI service.

The checker extracts factual claims, searches the web for evidence, and assigns
one of three verdicts:

- **TRUE**: retrieved evidence directly supports the claim.
- **FALSE**: retrieved evidence directly contradicts the claim.
- **UNVERIFIABLE**: the available evidence is missing, indirect, or conflicting.

The application does not treat opinions, predictions, or vague statements as
checkable facts.

## Current Interfaces

### Chrome extension

The extension has three modes:

1. **Current Page** extracts readable text from the active browser tab with a
   packaged copy of Mozilla Readability.
2. **Paste URL** asks the backend to retrieve an article through Jina Reader.
3. **Paste Text** checks a paragraph, article excerpt, or other pasted text.

The side panel displays progress and completed verdicts as they arrive. It also
supports cancellation, reconnection through snapshot polling, and JSON or
Markdown export.

## Architecture

```text
Chrome extension
      |
      v
api.py -> jobs.py -> service.py -> fact_checker.py
                                      |
                                      +-> DeepInfra (claim extraction and verification)
                                      +-> Tavily (evidence search)
```

Responsibilities are intentionally separated:

| Component | Responsibility |
| --- | --- |
| `fact_checker.py` | Claim extraction, evidence search, verification, verdict derivation, and bounded per-claim concurrency |
| `providers.py` | Immutable per-job credentials, provider clients, global concurrency gates, and usage accounting |
| `service.py` | Text validation, URL safety, Jina article retrieval, error sanitization, and shared application entry points |
| `jobs.py` | Background execution, capability access, capacity limits, cancellation, progress events, snapshots, and event replay |
| `api.py` | FastAPI validation, exact CORS, BYOK headers, job endpoints, and authenticated Server-Sent Events |
| `extension/` | Manifest V3 side panel, active-page extraction, API client, progress UI, results, and exports |

The extension sends requests to the API, while `service.py` keeps application
behavior separate from HTTP and background-job concerns. Prompts and verdict
logic are not duplicated in frontend code.

## Verification Pipeline

1. Validate and normalize the submitted URL or text.
2. For URL mode, retrieve readable article content through Jina Reader.
3. Ask DeepSeek V4 Flash through DeepInfra to extract up to 15 factual claims and a
   targeted search query for each claim.
4. Search Tavily for evidence, excluding configured social and user-generated
   domains and rejecting low-relevance results.
5. Rank accepted sources and retain up to three evidence sources per claim.
6. Verify each claim against its own evidence.
7. Derive `supported` and `contradicted` from per-source analysis, then derive
   the final verdict deterministically in Python.

Search and verification are pipelined with explicit worker limits. A completed
search can enter verification without waiting for every other search, and
results retain their original claim indexes even when they finish out of order.

## Evidence Safeguards

- Empty search evidence never reaches the verification model.
- The model must classify each source as supporting, contradicting, partial,
  irrelevant, or insufficient.
- Final verdict labels are recomputed in Python instead of trusting the model's
  self-reported verdict.
- Evidence and claim counts are bounded.
- Provider calls have explicit timeouts and bounded retries.
- Social and user-generated domains such as Facebook, X, Reddit, Medium, and
  LinkedIn are excluded from Tavily searches.
- Retrieved source content is bounded before it enters an LLM prompt.
- Provider failures are sanitized before being returned through the API.
- Provider keys are isolated per job and never enter snapshots, events, or
  exports.
- Hosted jobs share explicit process-wide DeepInfra and Tavily concurrency
  limits.

## Requirements

- Python 3.13
- Chrome 116 or newer
- Node.js for extension tests and validation
- A DeepInfra API key
- A Tavily API key

Runtime dependencies are pinned in `requirements.txt`. Development and test
dependencies are defined in `requirements-dev.txt`.

## Setup

Create and activate a virtual environment:

```powershell
py -3.13 -m venv .venv
& .\.venv\Scripts\Activate.ps1
```

Install dependencies for development:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

For runtime-only installation, use `requirements.txt` instead.

The extension uses bring-your-own-key (BYOK). Start the API, open the key button
in the side panel, and enter the DeepInfra and Tavily keys that should pay for
the check. Keys are stored in `chrome.storage.session`, so closing Chrome clears
them.

An `.env` file is optional. It is only a fallback for direct local engine
diagnostics outside the extension. To use that fallback:

```powershell
Copy-Item .env.example .env
```

Fill in both values in `.env`:

```dotenv
TAVILY_API_KEY=your_tavily_key
DEEPINFRA_API_KEY=your_deepinfra_key
```

The real `.env` file is ignored by Git. Do not commit API keys.

## Run the Extension Locally

Start the API from the project root:

```powershell
python api.py
```

The local server listens on `http://127.0.0.1:8000`. Confirm readiness at:

```text
http://127.0.0.1:8000/health
```

Load the extension:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Select **Load unpacked**.
4. Select this repository's `extension` directory.
5. Open a normal HTTP or HTTPS page and click the Fact Check toolbar action.

The local extension build permits only `localhost` and `127.0.0.1` backend
connections.

## API Overview

| Method | Endpoint | Access | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Backend readiness |
| `GET` | `/privacy` | Public | Privacy policy |
| `GET` | `/support` | Public | Support information |
| `POST` | `/v1/checks` | DeepInfra and Tavily key headers | Create a check |
| `GET` | `/v1/checks/{job_id}` | Job bearer capability | Read the current snapshot |
| `GET` | `/v1/checks/{job_id}/events` | Job bearer capability | Stream events through SSE |
| `DELETE` | `/v1/checks/{job_id}` | Job bearer capability | Request cancellation |
| `POST` | `/v1/checks/{job_id}/claims/{claim_index}/retry` | Job bearer capability and provider key headers | Retry one failed claim |

Example Text request:

```json
{
  "type": "text",
  "text": "The Eiffel Tower is located in Berlin."
}
```

Job execution is asynchronous. `POST /v1/checks` returns HTTP 202 with a
one-time `job_token` and the initial snapshot. The backend stores only its
SHA-256 hash. Every later job operation sends the token as
`Authorization: Bearer <job_token>`.

## Usage Accounting

The side panel and exports show cumulative DeepInfra input, output, and total
tokens plus Tavily search attempts and estimated credits. DeepInfra values come
from provider-reported stream metadata. Tavily advanced searches are labeled as
an estimate of two credits per successful search. A partial marker is shown
when a provider fails or does not report token usage. Replayed SSE events replace
cumulative totals instead of adding them again.

## Railway Beta Configuration

The repository includes a one-service Railpack configuration. Railway should
run one replica because jobs and capability hashes are held in process memory.
Set:

```dotenv
APP_ENV=production
CORS_ORIGINS=chrome-extension://your_32_character_extension_id
JOB_MAX_WORKERS=1
JOB_CAPACITY=8
JOB_CREATION_RATE_LIMIT=5
DEEPINFRA_CONCURRENCY=3
TAVILY_CONCURRENCY=4
```

Do not set developer DeepInfra or Tavily keys on Railway. After Railway assigns
an HTTPS domain, configure the extension and its exact host permission together:

```powershell
Push-Location extension
npm run configure:backend -- https://your-service.up.railway.app
npm run check
Pop-Location
```

For local development again, run the same command with
`http://127.0.0.1:8000`. Production startup fails when `CORS_ORIGINS` is empty
or is not an exact `chrome-extension://` origin. API documentation is disabled
in production.

## Tests

Run the Python suite and self-check:

```powershell
python -m pytest tests -q
python fact_checker.py
```

Run the extension tests and package validator:

```powershell
Push-Location extension
node --test tests/*.test.js
node scripts/validate.mjs
Pop-Location
```

The extension validator checks the Manifest V3 permissions, required local
assets, script syntax, API configuration location, and icon file dimensions.

## Current Scope and Limitations

- The default configuration is local. `railway.json` provides the hosted
  process command, but deployment still requires a Railway project and the
  environment configuration above.
- Jobs and event history are stored in memory and disappear when the API
  process restarts.
- Cancellation is best-effort. A provider call already in progress may run
  until its configured timeout.
- Each check has a 300-second wall-clock deadline. Provider calls use the smaller
  of their own timeout and the remaining job budget; completed verdicts are
  preserved if the deadline produces a partial result.
- Current Page mode sends extracted page text to the configured backend. Relevant
  claim and evidence content is sent to the configured external providers.
- Anonymous capability access protects individual jobs but is not user
  authentication. The first hosted release should remain a small friend beta.

Earlier project experiments and planning discussed microphone input, live-stream
checking, speech-to-text, and a Wayback Machine article fallback. Those features
are not connected to the current extension or runtime pipeline. Historical test
notes remain under `docs/` for local project reference.

## Repository Structure

```text
fact-checker/
  api.py
  jobs.py
  providers.py
  service.py
  fact_checker.py
  public/
  requirements.txt
  requirements-dev.txt
  tests/
  extension/
```

No provider credentials or fact-checking prompts belong in the extension.
