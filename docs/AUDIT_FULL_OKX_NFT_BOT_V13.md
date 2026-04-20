# Full audit — okx_nft_bot_v13

Date: 2026-04-05
Archive: `/mnt/data/okx_nft_bot_v13.zip`
Extracted to: `/mnt/data/okx_nft_bot_v13`

## Executive verdict

Functionally, the codebase is stronger than a typical ad-hoc trading bot: the repository is modular, the main execution runtime has explicit guardrails, and the test suite is substantial and currently green after dependencies are installed.

Operationally and from a security standpoint, the repository is **not safe to store, share, or deploy as-is**.

The two dominant risks are:

1. **Credential / session / signing-material exposure** in the repository itself.
2. **A live execution bypass** in `ParasiteHunter` that sidesteps the main execution governor and live-arm controls.

## Scope and method

Reviewed:

- source tree under `src/okx_nft_bot/`
- `config/`, `deploy/`, `compose.yaml`, Dockerfiles
- runtime artifacts under `data/`
- test suite under `tests/`
- environment files and cookie artifacts

Validation performed:

- archive extracted successfully
- `python -m compileall -q src tests` → **OK**
- `pip install -e .[dev]` → **OK**
- `pytest -q` → **205 passed in 22.91s**

Codebase metrics:

- source Python files: **79**
- test files: **43**
- source LOC: **23,621**
- repository size on disk: **~3.3 GB**
- largest source files:
  - `src/okx_nft_bot/sniper/parasite_hunter.py` — 3447 lines
  - `src/okx_nft_bot/counterbid/okx_api.py` — 1595 lines
  - `src/okx_nft_bot/sales_stream.py` — 1473 lines
  - `src/okx_nft_bot/cli.py` — 1234 lines
  - `src/okx_nft_bot/telegram_bot.py` — 1054 lines

## Findings

### CRITICAL-1 — live secrets, wallet key, cookies, and live flags are committed

Evidence:

- `.env` contains real OKX credentials, OpenSea key, Telegram bot token, wallet address, and buyer private key.
  - `.env:13-15`
  - `.env:35`
  - `.env:62-63`
  - `.env:70-71`
- `.env` also enables live execution paths:
  - `DRY_RUN=0` at `.env:96`
  - `MASS_OFFER_DRY_RUN=0` at `.env:97`
  - `PARASITE_HUNTER_DRY_RUN=0` at `.env:101`
- `config/okx_cookies.json` contains real browser/session cookies, including session-bearing names such as `tmx_session_id` and `ok-ses-id`.
- The repository has **no `.gitignore`** and ships runtime artifacts directly in the archive.

Impact:

- account takeover / API abuse
- wallet compromise risk
- Telegram bot hijack risk
- session replay risk
- accidental live trading from a copied checkout

Required action:

- rotate all exposed API keys and tokens immediately
- treat the buyer private key as compromised
- invalidate OKX cookies / sessions
- stop all live bots until new secrets are issued and the repo is sanitized

---

### CRITICAL-2 — signed order payloads and signatures are written into logs

Evidence in code:

- `src/okx_nft_bot/counterbid/okx_api.py:797-804` injects the signature into nested `items[].protocolData`
- `src/okx_nft_bot/counterbid/okx_api.py:838-842` only redacts the **top-level** `signature` field before logging the body
- `src/okx_nft_bot/counterbid/okx_api.py:337-338` logs large API responses verbatim

Evidence in shipped data:

- `data/debug.log:4`
- `data/debug.log:53`
- `data/debug.log.1:374`

Those log entries contain full `protocolData`, `r`, `s`, wallet address, order parameters, and nested signatures.

Impact:

- signed execution payload leakage
- replay / misuse analysis surface for anyone with log access
- sensitive market/wallet behavior permanently stored in artifacts

Required action:

- stop logging nested `protocolData`, `r`, `s`, signatures, and wallet identifiers
- purge existing logs from the repository and storage
- rotate credentials/session material because logs were shipped together with secrets

---

### HIGH-1 — `ParasiteHunter` bypasses the main execution governor and live-arm protections

Evidence:

- `src/okx_nft_bot/sniper/parasite_hunter.py:219-220` uses its own `PARASITE_HUNTER_ENABLED` and `PARASITE_HUNTER_DRY_RUN`
- `src/okx_nft_bot/sniper/parasite_hunter.py:2524` explicitly states: `bypasses governor/live-arm`
- `src/okx_nft_bot/sniper/parasite_hunter.py:2562-2572` submits BSC offers directly via `client.create_offer(...)`
- repository search showed no use of `ExecutionGovernor`, `check_live_submit_allowed`, or `force_dry_run` inside `parasite_hunter.py`
- `.env:101` sets `PARASITE_HUNTER_DRY_RUN=0`

Impact:

- a live execution path exists outside the main guardrail model
- hourly / daily caps, live-arm window, cooldown, and killswitch expectations can be bypassed from this path

Assessment:

This is the most important architectural defect in the current runtime.

Required action:

- route all live submissions through `ExecutionGovernor`
- make `ParasiteHunter` consume the same arm-live / force-dry-run state
- add tests proving that parasite submissions are blocked when live arm is absent or killswitch is active

---

### HIGH-2 — status lifecycle bug: `retired` is written but treated as invalid state

Evidence:

- allowed active-offer states are defined in `src/okx_nft_bot/undercutter/state.py:53-60`
- `retired` is **not** included there
- `src/okx_nft_bot/undercutter/engine.py:435` writes `status="retired"`

Reproduction result:

A minimal local reproduction shows that writing `retired` causes `audit_integrity()` to quarantine the row with:

- `issue_count = 1`
- `quarantine_count = 1`
- note: `Invalid status for active_offers[...].status: 'retired'`

Additional note:

Repository search found no test coverage for `retired`; it appears only in the engine path.

Impact:

- normal runtime behavior can self-corrupt execution state
- integrity audit will mark a legitimate lifecycle transition as invalid
- alerting / operator diagnostics become noisy or misleading

Required action:

Choose one of:

1. add `retired` to `_ACTIVE_OFFER_ALLOWED_STATUSES`, or
2. stop using `retired` and reuse an existing canonical status.

Also add a regression test for the defense re-bid path.

---

### HIGH-3 — fail-open behavior in balance safety checks

Evidence:

- `src/okx_nft_bot/sniper/parasite_hunter.py:2517-2519`

When balance lookup fails, `_check_balance_for_offer()` logs the problem and returns `True` (“proceeding anyway”).

Impact:

- network/RPC failure disables the balance guard exactly when conditions are uncertain
- bot may continue submitting offers without confirmed capacity to cover acceptance

Required action:

- change this path to fail closed for live mode
- optionally allow fail-open only in explicit dry-run/test mode

---

### MEDIUM-1 — configuration model is fragmented; many modules bypass central settings

Evidence:

- central settings exist in `src/okx_nft_bot/config.py`
- but there are **84 direct `os.getenv(...)` calls outside `config.py`**
- heaviest examples:
  - `src/okx_nft_bot/sales_stream.py`
  - `src/okx_nft_bot/sniper/offer_blaster.py`
  - `src/okx_nft_bot/sniper/buyer.py`
  - `src/okx_nft_bot/sniper/fat_finger.py`
  - `src/okx_nft_bot/sniper/parasite_hunter.py`

Impact:

- profile overrides become inconsistent
- redaction guarantees from `Settings.__repr__` do not apply everywhere
- testing and deployment behavior diverge more easily

Required action:

- move execution/runtime flags behind one typed settings object
- treat direct `os.getenv` in execution code as deprecated

---

### MEDIUM-2 — deployment wrappers intentionally swallow Telegram poller failures

Evidence:

- `compose.yaml:49-55`
- `deploy/systemd/okx-nft-bot-telegram.service:12`

Both wrappers run an infinite shell loop and use `poll-telegram-once || true`.

Impact:

- repeated command failures are masked
- operator may see a healthy service that is silently doing no useful work
- troubleshooting becomes harder because failure semantics move from process supervisor into shell glue

Required action:

- let the process fail normally and rely on supervisor restart policy
- keep retry/backoff in Python if needed, with structured health reporting

---

### MEDIUM-3 — repository hygiene is poor; shipping artifacts are mixed with source

Evidence:

- no `.gitignore`
- archive contains:
  - `60` `desktop.ini` files
  - `20` `__pycache__` directories
  - `430` `.pyc` files
  - `8` SQLite databases
  - `9` `.log` files
  - IDE metadata (`.idea/`)
  - local agent metadata (`.claude/`)
  - browser cookies and screenshots
- repository size is ~`3.3 GB`
- largest files include database backups and logs under `data/`

Impact:

- oversized deployments / backups
- accidental sensitive-data propagation
- noisy diffs and poor code review signal
- harder reproducibility

Required action:

- separate source release from runtime data
- add `.gitignore`
- move operational data to ignored paths or external volumes
- publish sanitized release archives only

---

### MEDIUM-4 — exception handling is very broad in core runtime modules

Evidence:

- `except Exception` occurrences in `src/okx_nft_bot/`: **168**
- concentration:
  - `sniper/parasite_hunter.py` — 42
  - `sales_stream.py` — 22
  - `playwright_okx_stream.py` — 20
  - `counterbid/okx_api.py` — 13

Impact:

- real defects can be downgraded to logs
- state drift and partial failures are easier to miss
- alerting quality degrades

Required action:

- narrow exception types in execution and submission paths first
- preserve stack traces for unknown failures
- distinguish retryable transport issues from logic/state defects

---

### LOW-1 — oversized “god modules” reduce maintainability and increase blind spots

Evidence:

- `sniper/parasite_hunter.py` — 3447 lines
- `counterbid/okx_api.py` — 1595 lines
- `sales_stream.py` — 1473 lines
- `cli.py` — 1234 lines
- `telegram_bot.py` — 1054 lines

Impact:

- higher regression risk
- harder reasoning about invariants and guardrails
- tests can pass while cross-cutting lifecycle bugs remain

Required action:

- split by responsibility: transport, policy, orchestration, persistence, notifications
- add narrow integration tests around lifecycle transitions and live-submit gates

## What is good

1. **Test suite quality is materially better than average**
   - 205 tests passed after dependency installation.

2. **Main execution runtime has real guardrail concepts**
   - dry-run mode
   - live-arm window
   - cooldowns
   - hourly/daily caps
   - reconcile/audit logic

3. **Settings model attempts secret redaction**
   - `src/okx_nft_bot/config.py` redacts sensitive fields in `__repr__`.

4. **The repository is modular at package level**
   - clients / providers / storage / analytics / mass_offer / undercutter / fraud.

5. **Docker and systemd artifacts exist**
   - deployment shape is defined, even if hygiene is poor.

## Priority remediation order

### Immediate (today)

1. Rotate all exposed secrets and invalidate cookies/sessions.
2. Stop all live runtimes until the repo is sanitized.
3. Remove `.env`, `config/okx_cookies.json`, logs, databases, screenshots, and backups from distributable artifacts.
4. Patch `counterbid/okx_api.py` logging so nested protocol data and signatures never reach logs.
5. Route `ParasiteHunter` submissions through `ExecutionGovernor`.
6. Fix the `retired` status mismatch.

### Short-term (next pass)

1. Add `.gitignore` and a “clean release” packaging step.
2. Centralize env reads behind `Settings`.
3. Replace shell retry loops with supervised process restarts and structured health checks.
4. Add regression tests for:
   - parasite hunter blocked without live arm
   - killswitch enforcement on parasite paths
   - `retired` lifecycle handling
   - log redaction of nested protocol data

### Medium-term

1. Break up `parasite_hunter.py`, `counterbid/okx_api.py`, `sales_stream.py`.
2. Add SQLite connection policies consistently (`busy_timeout`, WAL where appropriate, explicit transaction expectations).
3. Add CI that runs tests and sanitized packaging checks on every change.

## Final assessment

- **Code quality:** medium to good
- **Test posture:** good
- **Operational safety:** poor in current delivered archive
- **Security posture of delivered artifact:** unacceptable until secrets/logs/sessions are rotated and removed
- **Deployability after sanitation + 3-4 targeted patches:** good

