#!/bin/bash
sqlite3 /app/data/execution.sqlite3 "INSERT OR REPLACE INTO state_kv (key, value, updated_at) VALUES ('live_arm', '{\"armed\": true, \"expires_at\": \"2026-04-19T18:00:00+00:00\", \"armed_by\": \"cli\", \"reason\": \"debug\"}', datetime('now'));"
