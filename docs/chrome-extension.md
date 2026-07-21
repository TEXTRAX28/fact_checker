# Chrome Extension Strategy - Fast, Reliable Fact Checker

Status: strategy only; implementation has not started.

This document defines the Chrome extension architecture for the existing Python
fact-checker. The extension must feel responsive even when evidence collection
and LLM verification take tens of seconds. It must also preserve the existing
source and verdict safeguards instead of trading correctness for apparent
speed.

---

## Product Goal

Build a Manifest V3 Chrome extension with three modes:

1. **Current Page**
   - User clicks the extension action while viewing an article.
   - The extension extracts readable article text from that active tab.
   - The Python backend fact-checks the extracted text.

2. **URL**
   - User pastes a URL.
   - The Python backend retrieves and fact-checks the article.

3. **Text**
   - User pastes a paragraph, transcript, or article excerpt.
   - The Python backend fact-checks the submitted text.

This is not a chatbot and it is not a second fact-checking implementation. The
extension is a browser interface for the existing Python pipeline.

### Explicit scope boundary

The Chrome extension has exactly the three modes above. It will not include:

- microphone capture
- browser-tab audio capture
- speech-to-text
- live conversation checking
- live video or stream URL checking

Any use of the word **streaming** elsewhere in this document means streaming
backend progress events or partial verdicts to the side panel. It never means
audio, microphone, video, or live-stream processing.

---

## Decisions That Replace the Earlier Plan

The earlier plan was directionally correct but not good enough for a process
that can take 40-90 seconds.

| Earlier decision | Revised decision | Reason |
| --- | --- | --- |
| Popup as the main UI | Persistent Chrome side panel | Popups close when focus moves away and are unsuitable for long jobs. |
| One long API request | Immediate streamed response over one held-open connection | Chrome service workers and network requests are not reliable job owners; a single streamed call needs no job-id, snapshot, or reconnect machinery for a single local user. |
| Partial results deferred | Progressive results are required in V1 | Perceived speed is a core product requirement. |
| Current Page sends only the URL | Extract readable text in the active tab first | Avoids a duplicate page fetch and handles client-rendered or logged-in pages the user can already see. |
| Default executor sizes | Explicit, measured concurrency limits | Prevents bursts of 15 Tavily calls followed by 15 DeepInfra calls. |
| Wait for every search before verification | Pipeline search completion into verification | Removes the all-searches barrier and improves time to first verdict. |
| Display results in claim order only | Reveal completed results immediately, place them in stable claim slots | Removes head-of-line blocking without making the UI jump. |
| Tavily domain snippet may become article input | Never treat a domain search snippet as the requested article | A snippet from another page can produce a fact-check of the wrong content. |

---

## Honest Performance Baseline

The Chrome-to-Python hop is not the current bottleneck. The existing pipeline
has several larger delays:

- Article retrieval is serial: Jina, then Wayback availability, then Wayback
  content, then Tavily fallback.
- Claim extraction is one DeepInfra request and has previously taken roughly
  18-47 seconds in manual runs.
- Every evidence search finishes before any verification starts.
- Verification futures are consumed in claim order, so a slow first claim can
  delay already-completed later claims.
- Search and verification use `ThreadPoolExecutor()` without explicit worker
  counts. In the current Python environment that means up to 20 workers per
  pool.
- Installed SDK defaults permit requests and retries to continue far longer
  than a user-facing browser operation should.

The extension cannot honestly promise instant full results. It can promise an
instant response to the click, visible progress, early partial verdicts,
bounded completion time, and fast cache hits.

### Initial Performance Targets

These are engineering targets, not claims about current performance. Record
p50 and p95 measurements before changing them.

| User-visible event | Local target | Public warm target |
| --- | ---: | ---: |
| Action click to side panel visible | < 300 ms | < 300 ms |
| Current-page text extracted | p50 < 500 ms, p95 < 2 s | Same; extraction is local |
| Job accepted and ID returned | < 250 ms | < 1 s |
| First progress event | < 1 s | < 2 s |
| Claims extracted | p50 < 12 s, p95 < 30 s | p50 < 15 s, p95 < 35 s |
| First verdict | p50 < 20 s, p95 < 45 s | p50 < 25 s, p95 < 50 s |
| Ten-claim completion | p50 < 45 s, p95 < 90 s | p50 < 55 s, p95 < 100 s |
| Valid cache hit | < 1 s | < 2 s |

If the claim-extraction stage cannot meet these targets with the current model,
evaluate a faster extraction model against a fixed accuracy corpus. Keep the
stronger model for verification unless evidence shows that a smaller model is
equally reliable. Do not switch models based on latency alone.

---

## Target Architecture

```text
Chrome action click
  -> service worker captures active tab context
  -> opens persistent side panel
  -> Current Page: activeTab + scripting + bundled Readability extraction
  -> URL/Text: input from side panel
  -> POST /v1/check opens one streamed connection
  -> bounded Python pipeline runs inline
       -> normalize and validate input
       -> extract claims
       -> search and verify through bounded pipeline
       -> write one NDJSON event per stage/verdict to the open response
  -> side panel reads the stream and fills claim slots as verdicts arrive
  -> closing the panel aborts the fetch, which cancels the check
  -> chrome.storage.session remembers the last mode/input, not an active job
```

The service worker coordinates the action click, active tab, and side-panel
opening. It must not own the long-running API request or the job state.

The side panel owns the live user interface. The backend owns durable job
execution and authoritative results.

---

## Current Page Extraction

Current Page should not make the backend retrieve a page that Chrome has
already rendered.

### Primary path

1. User clicks the extension action, granting temporary `activeTab` access.
2. The service worker receives the active `tabId`.
3. Use `chrome.scripting.executeScript()` in the main frame.
4. Run a bundled copy of `@mozilla/readability`; do not load remote code.
5. Clone the document before parsing because Readability mutates its input.
6. Return only structured metadata and `textContent`, not unsanitized HTML:
   - URL
   - title
   - site name
   - byline when available
   - published time when available
   - article text
   - character count
7. Enforce an input-size limit before sending text to the backend.

Readability pattern to follow from its official documentation:

```javascript
const documentClone = document.cloneNode(true);
const article = new Readability(documentClone).parse();
```

Use `article.textContent`. The extension does not need to render
`article.content`, which avoids an unnecessary HTML-sanitization surface.

### Fallback path

Current-page extraction can fail on browser-internal pages, the Chrome Web
Store, protected PDFs, frames without permission, or pages that are not
article-like.

When it fails:

1. If a normal `http` or `https` URL is available, offer **Try URL fetch**.
2. If URL fetch cannot retrieve usable article content, switch directly to the
   Text tab and show a clear message.
3. Never silently replace the requested article with a Tavily domain snippet.

Recommended message:

```text
We could not read this page automatically. It may be protected, paywalled,
or not structured like an article. Paste the relevant text to check it.
```

### Privacy requirement

Current Page mode sends readable page text to the selected backend. Local mode
keeps that text on the machine except for the existing evidence/model provider
calls. Public mode must disclose that page text is sent to the hosted service
before submission and must define a retention policy.

---

## URL Retrieval Strategy

URL mode remains useful when the target page is not open in Chrome.

### Required behavior

1. Accept only `http` and `https` URLs.
2. Normalize the URL and remove fragments.
3. Validate the destination before any request.
4. Try Jina Reader with a bounded deadline.
5. Accept content only when it passes explicit usability checks.
6. If Jina fails, use Wayback only after the response is parsed as HTML with a
   real parser. The current Markdown regex cleaner is not sufficient for raw
   Wayback HTML.
7. If no full article can be recovered, return an unreadable-page result and
   direct the user to Text mode.

### Forbidden behavior

- Do not use `site:{domain}` search results as the article body.
- Do not claim that a page was checked when only a search snippet was checked.
- Do not follow redirects to private or local network addresses.
- Do not allow `file`, `ftp`, `data`, `javascript`, or browser-internal URLs.
- Do not download unbounded response bodies.

---

## Backend Service Boundary

The CLI currently mixes user interaction, progress printing, article fetching,
normalization, and core results. Add a shared service layer before adding the
API.

Recommended Python interfaces:

```python
check_url(url, *, on_progress=None, on_result=None, cancel_event=None)
check_text(text, *, metadata=None, on_progress=None, on_result=None,
           cancel_event=None)
```

Return a structured result that distinguishes:

- completed with verdicts
- completed with no checkable claims
- completed with claims but insufficient evidence
- partially completed
- invalid input
- article unreadable
- provider timeout
- provider rate limit
- internal error
- cancelled

Do not use `[]` to represent all of these states.

Callbacks are observers. Exceptions raised by a progress or result callback
must be caught and logged without changing the authoritative fact-check result.

The existing URL/text CLI and the API should call the same service functions.
They may format output differently, but they must not duplicate claim
extraction, search, verification, or verdict logic.

---

## Job API

Use FastAPI with typed Pydantic request and response models. Pin a tested set of
FastAPI, Starlette, Uvicorn, and Pydantic versions before release.

Use Starlette `StreamingResponse` to emit NDJSON — not FastAPI's SSE helper,
which solves a different problem (a client-initiated `EventSource` reconnect
model this design deliberately doesn't need) and not a hand-written,
undocumented streaming abstraction.

### `GET /health`

Returns backend readiness and a non-secret build version.

```json
{
  "ok": true,
  "version": "local-dev"
}
```

### `POST /v1/check`

Opens one streamed connection and keeps it open for the life of the check.
There is no separate job-creation step, no `check_id`, and nothing to poll or
reconnect to: the response body *is* the job. This is a deliberate
simplification for the local, single-user milestone — see
[Deferred Until Public](#deferred-until-public) for what changes if this needs
to survive a closed panel or serve multiple concurrent users later.

Current Page request:

```json
{
  "mode": "page",
  "url": "https://example.com/article",
  "title": "Article title",
  "text": "Readable article text",
  "force_refresh": false
}
```

URL request:

```json
{
  "mode": "url",
  "url": "https://example.com/article",
  "force_refresh": false
}
```

Text request:

```json
{
  "mode": "text",
  "text": "Paragraph or article text",
  "force_refresh": false
}
```

Response is `Content-Type: application/x-ndjson`, one JSON object per line, in
stage order. The final line is always a terminal event (`complete`, `failed`,
or `cancelled`) that includes the full result.

Event types:

```text
reading_page
extracting_claims
claims_extracted
searching
verifying
verdict
warning
complete
failed
cancelled
```

Each event includes an event sequence number, timestamp, and only the fields
needed for that event. Use `Starlette.StreamingResponse`; this does not need
FastAPI's SSE helper since there is no separate client-initiated `EventSource`
connection to manage.

### Cancellation

There is no `DELETE` endpoint. The side panel cancels by calling
`AbortController.abort()` on the in-flight `fetch`, which closes the
connection. The backend must check `await request.is_disconnected()` between
pipeline stages and stop unscheduled work when the client is gone. This is
best-effort: a synchronous provider request already in flight may continue
until its own timeout.

If the panel closes mid-check, the check simply ends — there is no job to
recover. Re-clicking Check re-runs the pipeline; if the input is unchanged and
within the cache window, the cached result returns immediately instead of
rerunning search and verification.

---

## Pipeline Performance Strategy

### 1. Add structured progress

Extend the core pipeline with progress events for:

- input accepted
- page retrieval started/completed
- claim extraction started/completed
- each search started/completed
- each verification started/completed
- dropped claim with reason
- job completed/failed/cancelled

Do not parse CLI output to obtain progress.

### 2. Remove the all-searches barrier

The current pipeline waits for every Tavily search before starting any
verification. Replace that flow with a bounded pipeline:

```text
extract all claims
  -> submit at most SEARCH_WORKERS searches
  -> as each search completes, submit its verification
  -> as each verification completes, emit its verdict
```

Use `concurrent.futures.as_completed()` or an equivalent documented pattern.
Attach a stable claim index to every future so a completed result cannot drift
to another claim.

### 3. Use explicit concurrency limits

Initial local defaults for benchmarking:

```text
ACTIVE_CHECK_JOBS=1
SEARCH_WORKERS=4
VERIFY_WORKERS=3
```

These are starting values, not permanent truths. Change them only after
measuring latency, rate-limit responses, memory, and cost. The public service
also needs provider-wide concurrency limits because per-job limits alone do not
control several users at once.

Do not combine many Uvicorn workers, FastAPI's thread pool, and large internal
executors without calculating the resulting outbound concurrency.

### 4. Set real deadlines

Configure explicit provider timeouts and retry rules rather than accepting SDK
defaults. Initial safety budget:

```text
Jina article request: 12 seconds
claim extraction: 60 seconds
Tavily search per claim: 25 seconds
verification per claim: 60 seconds
whole check: 150 seconds
```

Retries must fit inside the whole-check deadline. Retry only transient transport
errors, provider 429 responses with a usable retry delay, and retryable 5xx
responses. Do not retry invalid input, malformed URLs, or deterministic parsing
failures indefinitely.

### 5. Optimize claim extraction separately

Claim extraction controls time to every later stage. Build a fixed evaluation
set before choosing a faster model or shorter prompt.

Measure:

- checkable-claim recall
- unsupported or invented claim rate
- query presence and quality
- valid structured-output rate
- duplicated/overlapping claim rate
- latency and cost

A faster extractor is accepted only when it meets the agreed quality threshold.
Verification remains evidence-based and independent.

### 6. Make coverage visible

If the pipeline intentionally checks only the most important claims, disclose
that limit. Show, for example:

```text
42 paragraphs analyzed. 10 checkable claims selected and checked.
```

Never imply that every statement on a page was checked when the extractor or a
configured claim limit selected only part of it.

---

## Caching Strategy

Caching should make repeated checks fast without hiding stale evidence.

### Cache key

Use a hash of:

- normalized URL when present
- normalized article/text content
- extraction prompt version
- verification prompt version
- model identifiers
- source-policy version

Changing prompts, models, or source rules must invalidate old results.

### Local mode

Use SQLite from the Python standard library for a small persistent cache. Store
structured results and timing metadata, not raw provider secrets.

Initial result freshness: 30 minutes. Show `checked_at` and provide **Recheck**
to bypass the cache. This value must be configurable and evaluated for breaking
news use cases.

### Public mode

Deferred — see [Deferred Until Public](#deferred-until-public). In-memory
dictionaries are correct for the single-process local milestone.

---

## Side Panel Experience

The side panel is the primary product surface. A small popup is unnecessary if
the toolbar action opens the panel directly.

### Required controls

- Segmented mode control: Current Page, URL, Text
- Current page title/domain or URL/text input
- Check button
- Cancel button while running
- Recheck button for cached results
- Backend connection status
- Stable progress indicator
- Result list

### Required states

```text
idle
collecting_page
queued
extracting_claims
searching
verifying
complete
complete_no_claims
partial
failed
cancelled
backend_offline
```

After claims are extracted, create stable claim placeholders. When a verification
finishes, fill its matching placeholder immediately. This reveals fast claims
without reordering the page or blocking behind claim 1.

Each final result shows:

- TRUE, FALSE, or UNVERIFIABLE
- claim text
- confidence
- explanation
- source links
- warnings about source quality or partial completion

Do not show a fake percentage when total work is unknown. Use stage progress and
counts such as `Verifying 4 of 10 claims`.

Store the last input in `chrome.storage.session` so the panel can restore the
form after a service-worker restart:

```text
mode
page title/domain or URL/text input
```

This is not job recovery. A check that was running when the panel closed does
not resume — the user re-clicks Check, and the cache makes that cheap when the
content hasn't changed.

---

## Chrome Manifest Strategy

Use Manifest V3 and set `minimum_chrome_version` to `116` because programmatic
`chrome.sidePanel.open()` requires Chrome 116 or newer.

Minimum permissions:

```json
{
  "manifest_version": 3,
  "minimum_chrome_version": "116",
  "permissions": [
    "activeTab",
    "scripting",
    "sidePanel",
    "storage"
  ],
  "host_permissions": [
    "http://localhost/*",
    "http://127.0.0.1/*"
  ],
  "side_panel": {
    "default_path": "sidepanel.html"
  }
}
```

Add the production API origin only in the public build:

```json
"https://api.example.com/*"
```

Do not request broad webpage host permissions. `activeTab` plus an explicit user
action is sufficient for current-page extraction and produces a narrower trust
boundary.

The extension service worker may:

- handle `chrome.action.onClicked`
- capture the active `tabId`
- call `chrome.sidePanel.open()` after that user action
- execute the packaged extraction scripts
- write compact state to `chrome.storage.session`

It must not:

- wait on the whole fact-check request
- keep authoritative job state only in memory
- assume a message port keeps it alive
- contain provider API keys

---

## Security and Privacy

### Secrets

Never place these in extension code or storage:

- `DEEPINFRA_API_KEY`
- `TAVILY_API_KEY`
- `GROQ_API_KEY`
- any reusable backend administrator secret

A Chrome extension cannot safely keep a public-service secret. Users can inspect
the package. CORS and extension-origin checks reduce browser exposure but do not
prevent direct API abuse.

SSRF protection for URL mode (rejecting loopback/private/link-local
destinations, revalidating redirects) is deferred — see
[Deferred Until Public](#deferred-until-public). Local mode only ever fetches
whatever URL the local user typed in, which is a much smaller trust boundary
than an internet-facing endpoint.

### Prompt injection

Webpage text is untrusted data. Prompts must clearly delimit it as evidence or
claim input and instruct the model not to follow commands contained inside it.
The backend must never execute code or fetch arbitrary URLs requested by page
text.

### CORS and local access

Use an exact allowlist for known extension origins. Localhost and `127.0.0.1`
are separate origins. Do not use `allow_origins=["*"]` for the public API.

Run the local API on `127.0.0.1`, not `0.0.0.0`, unless access from other devices
is explicitly required.

Rate limits, quotas, and abuse protection are deferred — see
[Deferred Until Public](#deferred-until-public). A single local user cannot
abuse their own backend.

---

## Project Structure

Recommended target shape:

```text
fact-checker/
  api.py
  service.py
  fact_checker.py
  main.py
  extension/
    manifest.json
    service-worker.js
    sidepanel.html
    sidepanel.css
    sidepanel.js
    extract-page.js
    vendor/
      Readability.js
    icons/
      icon16.png
      icon48.png
      icon128.png
  tests/
    test_service.py
    test_api.py
    test_pipeline.py
    test_url_safety.py
```

`service.py` owns reusable application behavior. `api.py` and `main.py` are
adapters. The extension contains no fact-checking prompts or provider
credentials.

---

## Phased Implementation Plan

### Phase 0 - Baseline and documentation lock

**Implement**

- Record timings for at least ten representative URL checks and ten text checks.
- Record extraction, search, verification, first-verdict, and total timings.
- Select and pin compatible FastAPI/Starlette/Uvicorn/Pydantic versions.
- Save the exact Chrome APIs and minimum versions used by the extension.
- Build a small accuracy corpus for later extraction-model evaluation.

**Documentation references**

- Chrome Side Panel API and `minimum_chrome_version` documentation.
- Chrome `activeTab`, `scripting`, storage, and service-worker lifecycle docs.
- FastAPI request body, response model, CORS, and concurrency docs.
- Starlette `StreamingResponse` documentation.
- Mozilla Readability README and API reference.

**Verification**

- Baseline report contains p50/p95 where the sample permits.
- Dependency versions are reproducible.
- Every planned API appears in official documentation.

**Do not**

- Promise target latency before measuring it.
- invent Chrome keepalive behavior.
- select a faster model without an accuracy baseline.

### Phase 1 - Shared service boundary

**Implement**

- Extract URL/text application functions from CLI-only code.
- Return structured outcomes instead of ambiguous empty lists.
- Add safe `on_progress` and `on_result` callbacks.
- Keep the existing URL and text CLI behavior working through the shared
  service.

**Code references**

- Reuse normalization flow from `main.py::_run_fact_check`.
- Reuse the existing per-verdict callback concept from
  `fact_checker.py::fact_check`.
- Preserve the empty-evidence guard in `fact_checker.py::_verify_one`.

**Verification**

- Existing self-check passes.
- Unit tests distinguish no claims, no evidence, failure, partial, and success.
- A callback exception does not discard valid results.

**Do not**

- duplicate prompts or verdict logic in `api.py`.
- parse terminal output to build API responses.
- change verdict semantics as part of this refactor.

### Phase 2 - Bounded, progressive pipeline

**Implement**

- Add explicit search and verification worker limits.
- Start verification as each search finishes.
- Reveal each completed verdict by stable claim index.
- Add per-call and whole-job deadlines.
- Add best-effort cancellation checks between stages.

**Code references**

- Replace the all-searches barrier around the current search executor.
- Replace claim-order future consumption with completion-order handling plus
  stable indexes.
- Preserve per-future exception isolation.

**Verification**

- Tests prove peak search and verification concurrency stays within limits.
- A deliberately slow claim 1 does not delay a completed claim 2 result.
- Timeouts produce structured partial outcomes.
- Result-to-claim pairing remains correct under out-of-order completion.

**Do not**

- use default executor sizes.
- create one executor per claim.
- retry beyond the whole-job deadline.

### Phase 3 - Local streaming API

**Implement**

- Add typed FastAPI models for the one `POST /v1/check` endpoint.
- Stream NDJSON events over one held-open response per check.
- Enforce `ACTIVE_CHECK_JOBS=1`: a second concurrent check is rejected or
  queued, not run in parallel.
- Bind Uvicorn to `127.0.0.1:8000`.
- Add exact CORS origins, explicit `GET`/`POST` methods, and request-size
  limits.
- Support and test Chrome Private Network Access preflights for the local API.
  If `allow_private_network=True` is used, pin a compatible Starlette version
  (0.51.0 or newer).

**Documentation references**

- Copy typed request/response patterns from FastAPI Request Body and Response
  Model documentation.
- Copy `StreamingResponse` usage from Starlette's official documentation.
- Copy CORS configuration from FastAPI `CORSMiddleware` documentation.

**Verification**

- API schema rejects invalid mode/input combinations.
- The client receives the first event before the pipeline finishes.
- Aborting the client fetch stops the backend from scheduling further stages.
- A real unpacked extension can pass the localhost CORS and Private Network
  Access preflight.
- Local API is unreachable through the machine's LAN address by default.

**Do not**

- call blocking `fact_check()` directly inside `async def`.
- assume disconnect cancels synchronous provider calls already in flight.
- use wildcard CORS.
- build job-id/snapshot/reconnect endpoints for this milestone.

### Phase 4 - Side panel extension

**Implement**

- Create the Manifest V3 extension and persistent side panel.
- Open the panel from a user action.
- Implement Current Page extraction with packaged Readability.
- Implement URL and Text modes.
- Submit checks, consume the NDJSON stream, and store the last input in
  session state.
- Render all progress, partial, empty, error, and complete states.

**Documentation references**

- Copy the action-to-side-panel pattern from Chrome Side Panel examples.
- Copy `activeTab` plus `chrome.scripting.executeScript()` from Chrome's
  official scripting examples.
- Copy `document.cloneNode(true)` plus `Readability(...).parse()` from Mozilla's
  official README.
- Copy session-state operations from Chrome Storage examples.

**Verification**

- Load unpacked in Chrome 116 or newer.
- Current Page extracts the requested article, not navigation/comments.
- URL and Text modes send the expected typed payloads.
- Closing the panel mid-check aborts cleanly; reopening starts a fresh check,
  which hits cache immediately if the content is unchanged.
- Switching tabs never checks a different tab silently.
- Playwright screenshots verify desktop side-panel states without overlap or
  clipped text.

**Do not**

- inject remote JavaScript.
- request all-sites host permission.
- make the service worker own the long request.
- render Readability HTML directly.

### Phase 5 - Cache and performance tuning

**Implement**

- Add versioned SQLite caching and Recheck.
- Add structured stage timings and cache-hit telemetry.
- Evaluate extraction-model alternatives against the fixed corpus.
- Tune worker counts from measured provider behavior.

**Verification**

- Same content and pipeline version produces a cache hit.
- Prompt/model/source-policy changes invalidate cached results.
- Cached results display age and source timestamps.
- Performance report compares baseline and optimized p50/p95.
- Accuracy evaluation shows no unacceptable regression.

**Do not**

- cache forever.
- hide cache age.
- optimize only the fastest successful examples.

Phase 6 (public deployment hardening) is not planned yet — this is a local-only
tool for now. See [Deferred Until Public](#deferred-until-public) for what it
would take to change that, so the list exists when it's actually needed instead
of being designed and built speculatively today.

### Final verification

- Run all unit and integration tests.
- Run the existing `python fact_checker.py` self-check.
- Run live tests for Current Page, URL, Text, unreadable page, no claims,
  provider timeout, cancellation, and cache hit.
- Verify side-panel screenshots at supported Chrome sizes.
- Inspect the packaged extension for secrets and undeclared network origins.
- Compare final p50/p95 latency and accuracy against Phase 0.
- Update `README.md`, `Documentation.md`, and `HANDOFF.md` only after behavior is
  implemented and verified.

---

## V1 Completion Gate

Local V1 is complete only when:

- all three modes work through the side panel
- Current Page extracts readable text under explicit user activation
- the streamed response begins before the check finishes
- progress and partial verdicts appear before full completion
- closing the panel mid-check cancels cleanly; reopening starts a fresh check
- concurrency and timeouts are bounded
- no article is silently replaced by an unrelated search snippet
- no provider key exists in extension code
- automated tests and live Chrome checks pass
- measured latency and known limitations are documented honestly

Public release is a separate milestone, not started and not designed in detail
yet. Local success does not prove the system is safe, affordable, or reliable
for anonymous internet traffic.

---

## Deferred Until Public

Nothing below this line should be built for the local milestone. It exists so
the requirements aren't lost, and so Phase 6 has a starting checklist instead
of a blank page, whenever public deployment actually becomes the next goal.

### SSRF and URL safety

- resolve and reject loopback, link-local, private, multicast, and reserved IPs
- revalidate every redirect destination
- limit redirect count and response bytes
- apply connect, read, and total deadlines
- reject credentials embedded in URLs
- log safe metadata without logging secrets or complete private content

### Abuse and cost protection

- per-user and per-IP rate limits
- per-account quotas or another enforceable usage policy
- maximum active jobs, text size, and claim count
- provider-wide concurrency limits
- cost and error monitoring
- privacy policy and retention controls

An extension ID is not an identity system. If public usage can create
meaningful cost, user accounts or another server-enforced entitlement will
eventually be required.

### Infrastructure changes the single-endpoint design assumes away

- shared job/cache storage once more than one API worker/process runs
- reintroducing a job-id + snapshot/reconnect model if a check needs to
  survive a closed client (multi-device use, mobile companion, etc.)
- production extension package with only the production API origin

---

## Official Documentation Used

- Chrome Side Panel API:
  https://developer.chrome.com/docs/extensions/reference/api/sidePanel
- Chrome activeTab permission:
  https://developer.chrome.com/docs/extensions/develop/concepts/activeTab
- Chrome Scripting API:
  https://developer.chrome.com/docs/extensions/reference/api/scripting
- Chrome Storage API:
  https://developer.chrome.com/docs/extensions/reference/api/storage
- Chrome service-worker lifecycle:
  https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle
- Chrome cross-origin network requests:
  https://developer.chrome.com/docs/extensions/develop/concepts/network-requests
- FastAPI request body:
  https://fastapi.tiangolo.com/tutorial/body/
- FastAPI response models:
  https://fastapi.tiangolo.com/tutorial/response-model/
- FastAPI CORS:
  https://fastapi.tiangolo.com/tutorial/cors/
- Starlette streaming responses:
  https://www.starlette.io/responses/#streamingresponse
- Starlette thread pool:
  https://www.starlette.io/threadpool/
- Uvicorn settings:
  https://www.uvicorn.org/settings/
- Mozilla Readability:
  https://github.com/mozilla/readability
