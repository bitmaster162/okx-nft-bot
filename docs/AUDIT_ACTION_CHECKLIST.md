# Action checklist — okx_nft_bot_v13

## Incident response

- [ ] Rotate OKX API key / secret / passphrase
- [ ] Rotate OpenSea API key
- [ ] Rotate Telegram bot token
- [ ] Treat buyer private key as compromised; move funds/permissions to a new wallet
- [ ] Invalidate OKX browser sessions / cookies
- [ ] Stop live bots until sanitized

## Code fixes

- [ ] Remove nested signature / protocolData logging from `counterbid/okx_api.py`
- [ ] Route `sniper/parasite_hunter.py` submissions through `ExecutionGovernor`
- [ ] Enforce live-arm / killswitch / cooldown in parasite paths
- [ ] Fix `retired` status mismatch in undercutter state machine
- [ ] Make balance-check failures fail closed in live mode
- [ ] Add tests for the above

## Repository hygiene

- [ ] Add `.gitignore`
- [ ] Remove `.env` from repo and releases
- [ ] Remove `config/okx_cookies.json` from repo and releases
- [ ] Remove logs / DBs / screenshots / backups from release archives
- [ ] Remove `.idea/`, `.claude/`, `desktop.ini`, `__pycache__`, `.pyc`
- [ ] Create a sanitized release artifact

## Hardening

- [ ] Centralize env access in `Settings`
- [ ] Replace `poll-telegram-once || true` wrappers with proper supervised failures
- [ ] Reduce broad `except Exception` usage in live submission paths
- [ ] Split oversized modules
- [ ] Add CI for tests + packaging sanity checks
