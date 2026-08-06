#!/usr/bin/env python3
"""Fail-closed scanner for a public financial-bot repository.

History of this file, kept because each round closed a specific hole.

R1 returned REVISE on an untouched tree, so the gate could never be green.
16 of its 21 findings were false: the secret rule matched *names* rather than
*values*, so ``self.settings.buyer_wallet_private_key`` and the documentation
placeholder ``0xYOUR_PRIVATE_KEY_HERE`` were reported as leaked secrets. The
other 5 were a rule collision -- the path rule forbids ``.bak-*`` while the
work order forbids deleting the four ``.bak-*`` blobs already tracked.

R2 fixed the false positives but introduced a bypass: exemptions were tested
against the whole source line, so a real key could be hidden behind a comment::

    PRIVATE_KEY=<real 64 hex>   # example

The word ``example`` anywhere on the line silenced the rule.

R3 (this file) removes that bypass:

* an exemption is evaluated against the right-hand side of the assignment
  only -- an inline comment is stripped before the check and can no longer
  neutralise anything;
* lookups (``settings.*``, ``config.*``, ``os.getenv``, ``os.environ``, a
  function call) count only when they *are* the RHS expression;
* a placeholder counts only when the RHS *value itself* is a placeholder;
* rules without an assignment (PEM block, vendor tokens) accept no exemption
  at all;
* the ``scanner:selftest-fixture`` pragma is honoured only inside this file
  and only on the exact fixture line.

R4 closes the last shape of the same hole. R3 anchored the key name at the
start of a line and knew only a fixed list of private/secret names, so both of
these slipped through::

    {"private_key": "<real 64 hex>"}
    'api_key': '<long literal secret>'

The key is now recognised quoted or bare, anywhere an assignment can start, and
the generic families (``API_KEY``, ``TOKEN``, ``CLIENT_SECRET``, ...) are
covered. Generic names block only on a long or credential-shaped value, so
``TOKEN = "test"`` stays clean. Values are still parsed apart from comments.

Exit codes: 0 = PASS, 2 = REVISE or BLOCK.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

SCHEMA = "okx.public_repo_scan.receipt/v4"
SCANNER_SELF_PATH = "scripts/ci_public_repo_scan.py"

# -- Legacy blobs kept deliberately -------------------------------------------
# The work order forbids deleting these. They are pinned by content: the exact
# blob SHA is part of the contract, so a silent edit is impossible. A new
# .bak-* path, or a change to one of these, is a BLOCK rather than a warning.
LEGACY_BAK_ALLOWLIST: dict[str, str] = {
    "config/buy_config.json.bak-aog-20260521-051129":
        "d80f8e64b903f3db1b3c307c2433b3b7d0b961de",
    "config/buy_config.json.bak-bulk070-20260521-053650":
        "646811f63edeb95f5171eb1f3de092eb18c4dc23",
    "config/buy_config.json.bak-coll252b-20260521-055425":
        "86990c8dbfc36de1689117e3e53f95c29a12555e",
    "src/okx_nft_bot/sniper/counter_bidder.py.bak-aggressive-20260523-063434":
        "20ba0d248319a7687a7eb8bb52d251fcc4430286",
}

TEMPLATE_ENV_SUFFIXES = (".env.example", ".env.template", ".env.sample")

FORBIDDEN_PATH_PATTERNS = [
    ("claude_local_settings", re.compile(r"(^|/)\.claude/settings\.local\.json$", re.I)),
    ("dotenv_file", re.compile(r"(^|/)\.env($|\.)", re.I)),
    ("key_material_file", re.compile(r"\.(pem|key)$", re.I)),
    ("runtime_dir", re.compile(r"(^|/)(data|logs)/", re.I)),
    ("backup_file", re.compile(r"\.bak(?:$|[-.])", re.I)),
    ("ad_hoc_patch_script", re.compile(r"(^|/)_patch_.*\.py$", re.I)),
    ("database_file", re.compile(r"\.(sqlite3?|db)(?:$|[-.])", re.I)),
]

# -- Literal secret material --------------------------------------------------
_HEX = r"(?:0x)?[0-9a-fA-F]{32,}"
_B64 = r"[A-Za-z0-9+/]{40,}={0,2}"
_UUID = r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"

# Assignment rules.
#
# R4 (2026-08-06): R3 ловил только имя ключа без кавычек в начале строки, поэтому
# секрет проезжал в JSON и в питоновском словаре:
#
#     {"private_key": "<реальный 64-hex>"}
#     'api_key': '<длинный литерал>'
#
# а generic-имена API_KEY / TOKEN / CLIENT_SECRET не проверялись вовсе.
# Теперь имя распознаётся и в кавычках, и без, а значение по-прежнему разбирается
# отдельно, так что комментарий рядом ничего не отменяет.
#
# Имена делятся на две группы. Строгие (private/secret) считаются секретом при
# любом опаковом значении. Generic (api_key, token, client_secret) — только при
# достаточно длинном или структурно похожем на секрет значении, иначе
# TOKEN="test" в тестах и доках поднимал бы ложную тревогу.
_STRICT_KEYS = r"PRIVATE_KEY|BUYER_WALLET_PRIVATE_KEY|SECRET_KEY|API_SECRET|OKX_SECRET|OKX_API_SECRET|WEBHOOK_SECRET|CLIENT_SECRET"
_GENERIC_KEYS = r"API_KEY|ACCESS_TOKEN|AUTH_TOKEN|BEARER_TOKEN|REFRESH_TOKEN|TOKEN"

# Ключ слева от = или :, с необязательными кавычками вокруг имени.
_KEY = r"""(?P<kq>['"]?)(?P<key>%s)(?P=kq)"""

# Опаковое значение: длинный hex, длинный base64, UUID.
_OPAQUE = r"(?:" + _HEX + r"|" + _B64 + r"|" + _UUID + r")"
# Значение generic-ключа: то же самое ЛИБО просто длинная непробельная строка.
_GENERIC_VALUE = r"(?:" + _OPAQUE + r"|[A-Za-z0-9_\-]{24,})"

ASSIGNMENT_SECRETS = [
    ("private_key_literal", re.compile(
        r"""(?im)(?:^|[\s{,])(?:export[ \t]+)?""" + (_KEY % _STRICT_KEYS) +
        r"""[ \t]*[=:][ \t]*(?P<quote>['"]?)(?P<value>""" + _OPAQUE + r""")""")),
    ("generic_secret_literal", re.compile(
        r"""(?im)(?:^|[\s{,])(?:export[ \t]+)?""" + (_KEY % _GENERIC_KEYS) +
        r"""[ \t]*[=:][ \t]*(?P<quote>['"]?)(?P<value>""" + _GENERIC_VALUE + r""")""")),
]

# Standalone rules. There is no right-hand side to reason about, so nothing
# short of the in-file self-test pragma may silence them.
STANDALONE_SECRETS = [
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
]

SECRET_RULE_NAMES = [n for n, _ in ASSIGNMENT_SECRETS] + [n for n, _ in STANDALONE_SECRETS]

# A lookup is an exemption only when it *is* the right-hand side.
RHS_LOOKUP_PATTERNS = [
    ("settings_lookup", re.compile(r"^(?:self\.)?settings\s*[.\[]")),
    ("config_lookup", re.compile(r"^(?:self\.)?config\s*[.\[]")),
    ("env_lookup", re.compile(r"^os\s*\.\s*(?:getenv|environ)\b|^getenv\s*\(|^environ\s*\[")),
    ("dotenv_lookup", re.compile(r"^(?:dotenv_values|load_dotenv)\s*\(")),
    ("function_call_value", re.compile(r"^_?[A-Za-z][A-Za-z0-9_]*\s*\(")),
]

# A placeholder is an exemption only when the VALUE itself is one.
RHS_PLACEHOLDER_PATTERNS = [
    ("placeholder_value", re.compile(
        r"(?i)^(?:0x)?(?:your[_ -]?\w*|<[^>]{0,60}>|x{3,}|change[_ -]?me|placeholder"
        r"|example|dummy|fake|redacted|fill[_ -]?in|todo|none|null|\.{3})[\w-]*$")),
    ("placeholder_marker_in_value", re.compile(
        r"(?i)^[\w-]*(?:your[_ -]|_here$|_here[\w-]|placeholder|change[_ -]?me)[\w-]*$")),
    ("known_public_test_key", re.compile(
        r"(?i)^(?:0x)?ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80$")),
]

SELFTEST_PRAGMA = re.compile(r"#\s*scanner:selftest-fixture\b")

PUBLIC_DOC_FORBIDDEN = [
    ("live_mode_claim", re.compile(r"(?<![A-Z0-9_])DRY_RUN\s*=\s*0(?![0-9])")),
    ("host_root_path", re.compile(r"(?<![\w.-])/root/[A-Za-z0-9_./-]+")),
    ("live_control_command", re.compile(r"/(?:armlive|disarmlive|killswitch)\b")),
    ("weak_password_note", re.compile(r"(?i)weak password")),
    ("exact_daily_cap", re.compile(r"\bMAX_[A-Z0-9_]*PER_DAY\s*=\s*[0-9.]")),
]

PUBLIC_DOCS = {"AGENTS.md", "README.md", "README.txt"}

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".ini",
    ".cfg", ".sh", ".ps1", ".bat", ".env", ".html", ".css", ".js",
    ".example", ".template", ".sample",
}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "surrogateescape")


def tracked_blobs(root: Path) -> dict[str, str]:
    """Map tracked path -> blob SHA, read from the index so staging counts."""
    out = _git(root, "ls-files", "-s", "-z")
    result: dict[str, str] = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 2 and path:
            result[path.replace("\\", "/")] = parts[1]
    return result


def is_text_candidate(path: Path) -> bool:
    return path.name in {".gitignore", "Dockerfile"} or path.suffix.lower() in TEXT_SUFFIXES


def line_of(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start:end if end != -1 else len(text)]


def strip_inline_comment(expr: str) -> str:
    """Drop a trailing comment. Key material never contains # or //."""
    for marker in ("#", "//", ";"):
        pos = expr.find(marker)
        if pos != -1:
            expr = expr[:pos]
    return expr.strip()


def rhs_of(line: str, key_end: int | None = None) -> str:
    """Right-hand side of the assignment, comment removed.

    R4: with a quoted key (``"api_key": "..."``) the first ``:`` on the line is
    the separator, but the first ``"`` belongs to the key. ``key_end`` lets the
    caller say where the key finished so the split cannot land inside it.
    """
    tail = line if key_end is None else line[key_end:]
    pos = min((p for p in (tail.find("="), tail.find(":")) if p != -1), default=-1)
    if pos == -1:
        return ""
    value = strip_inline_comment(tail[pos + 1:]).strip()
    return value.strip("'\"").strip(",").strip().strip("'\"").strip("}").strip()


def rhs_exemption(line: str, value: str, key_end: int | None = None) -> str | None:
    """Exemption judged on the right-hand side only, never on a comment."""
    rhs = rhs_of(line, key_end)
    if not rhs:
        return None
    for name, pattern in RHS_LOOKUP_PATTERNS:
        if pattern.search(rhs):
            return name
    # The captured literal must itself be the placeholder, and it must be the
    # whole right-hand side -- otherwise a real key sitting next to the word
    # would ride along.
    candidate = rhs.strip("'\"").strip(",").strip("'\"")
    if candidate == value or candidate == value.strip("'\""):
        for name, pattern in RHS_PLACEHOLDER_PATTERNS:
            if pattern.search(candidate):
                return name
    return None


def selftest_pragma_applies(rel: str, line: str) -> bool:
    """The fixture pragma is valid only inside this scanner, on that line."""
    return rel == SCANNER_SELF_PATH and bool(SELFTEST_PRAGMA.search(line))


def secret_findings(rel: str, text: str) -> list[tuple[str, int, str]]:
    """Return (rule, line_number, detail) for every unexempted secret."""
    out: list[tuple[str, int, str]] = []

    for name, pattern in ASSIGNMENT_SECRETS:
        for match in pattern.finditer(text):
            line = line_of(text, match.start())
            if selftest_pragma_applies(rel, line):
                continue
            # позиция конца имени ключа внутри СТРОКИ, а не всего текста
            line_start = text.rfind("\n", 0, match.start()) + 1
            key_end = match.end("key") - line_start
            if rhs_exemption(line, match.group("value"), key_end):
                continue
            out.append((name, text[:match.start()].count("\n") + 1, "literal value"))

    for name, pattern in STANDALONE_SECRETS:
        for match in pattern.finditer(text):
            line = line_of(text, match.start())
            if selftest_pragma_applies(rel, line):
                continue
            out.append((name, text[:match.start()].count("\n") + 1, "standalone token"))

    return out


def env_template_violations(text: str) -> list[str]:
    """Assignments in a .env template whose value is credential-shaped.

    A template may legitimately carry non-secret defaults -- paths, URLs,
    numbers, booleans. Only opaque, credential-shaped values are a problem,
    and a trailing ``# placeholder`` comment does not make one acceptable.
    """
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        value = rhs_of(line)
        if not value:
            continue
        for name, pattern in ASSIGNMENT_SECRETS:
            match = pattern.search(line)
            if match and not rhs_exemption(line, match.group("value")):
                bad.append(f"{key}:{name}")
                break
        else:
            if (re.fullmatch(_HEX, value) or re.fullmatch(_B64, value)
                    or re.fullmatch(_UUID, value)) and not rhs_exemption(line, value):
                bad.append(f"{key}:opaque_value")
    return bad


def scan(root: Path) -> dict:
    findings: list[dict] = []
    blobs = tracked_blobs(root)
    legacy_seen: dict[str, str] = {}

    for rel, blob in sorted(blobs.items()):
        path = root / rel

        for name, pattern in FORBIDDEN_PATH_PATTERNS:
            if not pattern.search(rel):
                continue

            if name == "backup_file":
                expected = LEGACY_BAK_ALLOWLIST.get(rel)
                if expected is None:
                    findings.append({"class": "blocked_new_backup", "path": rel,
                                     "pattern": name, "severity": "BLOCK",
                                     "detail": "new .bak-* path; allowlist is closed"})
                elif expected != blob:
                    findings.append({"class": "blocked_legacy_mutation", "path": rel,
                                     "pattern": name, "severity": "BLOCK",
                                     "detail": f"expected blob {expected}, found {blob}"})
                else:
                    legacy_seen[rel] = blob
                continue

            if name == "dotenv_file" and rel.endswith(TEMPLATE_ENV_SUFFIXES):
                if path.is_file():
                    bad = env_template_violations(
                        path.read_bytes().decode("utf-8", "replace"))
                    if bad:
                        findings.append({"class": "blocked_env_template_has_values",
                                         "path": rel, "pattern": name,
                                         "severity": "BLOCK",
                                         "detail": ",".join(sorted(bad))})
                continue

            findings.append({"class": "forbidden_tracked_path", "path": rel,
                             "pattern": name, "severity": "REVISE", "detail": ""})

        if not path.is_file() or not is_text_candidate(path):
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", "replace")

        for rule, lineno, detail in secret_findings(rel, text):
            findings.append({"class": "secret_like_content", "path": rel,
                             "pattern": rule, "severity": "BLOCK",
                             "detail": f"line {lineno} ({detail})"})

        if rel in PUBLIC_DOCS:
            for name, pattern in PUBLIC_DOC_FORBIDDEN:
                if pattern.search(text):
                    findings.append({"class": "public_operational_disclosure",
                                     "path": rel, "pattern": name,
                                     "severity": "REVISE", "detail": ""})

    missing_legacy = sorted(set(LEGACY_BAK_ALLOWLIST) - set(legacy_seen))

    findings.sort(key=lambda item: (item["severity"], item["class"],
                                    item["path"], item["pattern"]))
    if any(f["severity"] == "BLOCK" for f in findings):
        status = "BLOCK"
    elif findings:
        status = "REVISE"
    else:
        status = "PASS"

    return {
        "schema": SCHEMA,
        "root": str(root.resolve()),
        "tracked_count": len(blobs),
        "legacy_allowlist": LEGACY_BAK_ALLOWLIST,
        "legacy_allowlist_verified": legacy_seen,
        "legacy_allowlist_missing": missing_legacy,
        "findings_count": len(findings),
        "findings": findings,
        "status": status,
    }


# -- self-tests ---------------------------------------------------------------
# Positive cases MUST be flagged; negative cases MUST NOT be. They run in CI,
# so a future loosening of the rules fails the build instead of passing quietly.
# The R3 additions are the comment-bypass cases that R2 let through.
def _fixture(*parts: str) -> str:
    """Assemble a fixture value at runtime.

    The self-tests need strings shaped like real provider credentials, but a
    complete literal of that shape cannot live in the source: GitHub Push
    Protection scans every commit being pushed and rejects the whole push.
    Our first attempt was blocked exactly here --

        GH013 ... Amazon AWS Secret Access Key -> ci_public_repo_scan.py:401
        GH013 ... Amazon AWS Access Key ID     -> ci_public_repo_scan.py:409

    -- on two of these fixtures. Splitting them keeps the tested value byte for
    byte identical while leaving no continuous secret-shaped literal in the
    file. The scanner rules are untouched: nothing is allowlisted, and the
    assembled value must still be detected.
    """
    return "".join(parts)


_REAL_PK = _fixture("4c0883a69102937d6231471b5dbb62",
                    "04fe512961708279a1b0e4f4d9b7a1c2e5")
_REAL_SECRET = _fixture("3F7A21B9C4D85E60", "A1B2C3D4E5F60718AABBCCDD")
_REAL_GENERIC = _fixture("sk9Qm2Xv7Lp4Rt8", "Wz1Yc6Nb3Hd5Fg0Jk")
_REAL_B64 = _fixture("bXlzdXBlcnNlY3", "JldHZhbHVlZm9y",
                     "dGVzdGluZ29ubHkxMjM0NTY3OA==")
_REAL_AWS_ID = _fixture("AKIA", "IOSFODNN7ABCDEFG")
_REAL_GH_TOKEN = _fixture("ghp_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
_REAL_SLACK = _fixture("xoxb", "-1234567890-abcdefghij")

POSITIVE_CASES = [
    ("literal hex private key", f'PRIVATE_KEY = "0x{_REAL_PK}"'),
    ("exported literal key", f"export BUYER_WALLET_PRIVATE_KEY={_REAL_PK}"),
    ("okx api secret literal", f"OKX_API_SECRET={_REAL_SECRET}"),
    ("pem block", "-----BEGIN RSA PRIVATE KEY-----"),  # scanner:selftest-fixture
    ("github token", "token = " + _REAL_GH_TOKEN),  # scanner:selftest-fixture
    ("aws key", "aws_key: " + _REAL_AWS_ID),  # scanner:selftest-fixture
    ("slack token", "SLACK = " + _REAL_SLACK),  # scanner:selftest-fixture
    # R3: a comment must never neutralise a literal secret.
    ("real key with # example comment", f"PRIVATE_KEY=0x{_REAL_PK}  # example"),
    ("real secret with # config.example comment",
     f"OKX_API_SECRET={_REAL_SECRET}  # config.example"),
    ("real key with dummy word after value", f"PRIVATE_KEY={_REAL_PK}  # dummy value"),
    ("real key with placeholder comment", f"PRIVATE_KEY=0x{_REAL_PK}  # placeholder"),
    ("real key with redacted comment", f"BUYER_WALLET_PRIVATE_KEY={_REAL_PK}  # redacted"),
    # R4: quoted keys in JSON / dict literals, and the generic secret families.
    ("json quoted private_key", '{"private_key": "0x%s"}' % _REAL_PK),
    ("python dict quoted api_key", "{'api_key': '%s'}" % _REAL_GENERIC),
    ("bare TOKEN assignment", 'TOKEN = "%s"' % _REAL_GENERIC),
    ("quoted client_secret base64", '"client_secret": "%s"' % _REAL_B64),
    ("quoted api_key with example comment",
     '"api_key": "%s"  # example' % _REAL_GENERIC),
    ("ACCESS_TOKEN yaml style", 'ACCESS_TOKEN: "%s"' % _REAL_GENERIC),
    ("AUTH_TOKEN in dict", '{"auth_token": "%s"}' % _REAL_GENERIC),
    ("WEBHOOK_SECRET literal", 'WEBHOOK_SECRET = "%s"' % _REAL_B64),
    ("BEARER_TOKEN literal", 'BEARER_TOKEN="%s"' % _REAL_GENERIC),
    ("nested json api_secret", '{"cfg": {"api_secret": "%s"}}' % _REAL_B64),
]

NEGATIVE_CASES = [
    ("settings lookup", "private_key = self.settings.buyer_wallet_private_key"),
    ("settings kwarg", "private_key=settings.buyer_wallet_private_key,"),
    ("env lookup", 'PRIVATE_KEY = os.getenv("BUYER_WALLET_PRIVATE_KEY")'),
    ("environ lookup", 'API_SECRET = os.environ["OKX_API_SECRET"]'),
    ("doc placeholder", "PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE"),
    ("angle placeholder", "PRIVATE_KEY=<your-key>"),
    ("empty template value", "OKX_API_SECRET="),
    ("function call", "private_key = _load_private_key()"),
    ("secret_key kwarg", "secret_key=self.settings.okx_api_secret"),
    ("hardhat public test key",
     "PRIVATE_KEY=0x" + _fixture("ac0974bec39a17e36ba4a6b4d238ff9",
                                 "44bacb478cbed5efcae784d7bf4f2ff80")),
    ("short hex is not a key", "PRIVATE_KEY=0xdeadbeef"),
    ("plain prose", "The private key is never stored in this repository."),
    ("changeme placeholder", "OKX_API_SECRET=CHANGEME"),
    # R4 negatives: the quoted forms must not fire on placeholders or lookups.
    ("json placeholder value", '{"private_key": "0xYOUR_PRIVATE_KEY_HERE"}'),
    ("json empty value", '{"api_key": ""}'),
    ("short token literal", 'TOKEN = "test"'),
    ("token from env", 'TOKEN = os.getenv("TOKEN")'),
    ("client_secret from settings", '"client_secret": settings.client_secret'),
    ("api_key from config", '{"api_key": config.api_key}'),
    ("api_key from self settings", 'api_key = self.settings.okx_api_key'),
    ("token placeholder", '{"token": "your-token-here"}'),
    ("access token function call", 'ACCESS_TOKEN = _load_token()'),
]

# The .env template rule gets its own cases: a credential-shaped value must be
# blocked even when the line carries a reassuring comment.
ENV_POSITIVE_CASES = [
    ("env secret with placeholder comment",
     f"OKX_API_SECRET={_REAL_SECRET}  # placeholder"),
    ("env private key with example comment",
     f"PRIVATE_KEY=0x{_REAL_PK}  # example"),
]

ENV_NEGATIVE_CASES = [
    ("env empty value", "OKX_API_SECRET="),
    ("env placeholder value", "PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE"),
    ("env plain path", "DB_PATH=./data/okx_nft_bot.sqlite3"),
    ("env url", "OKX_API_BASE=https://web3.okx.com"),
]


def _flags(sample: str, rel: str = "sample.txt") -> str | None:
    hits = secret_findings(rel, sample)
    return hits[0][0] if hits else None


def self_test() -> dict:
    failures: list[dict] = []
    for label, sample in POSITIVE_CASES:
        # Fixtures carrying the pragma are exempt inside this file, so they are
        # checked under a neutral path where the pragma does not apply.
        if _flags(sample) is None:
            failures.append({"kind": "false_negative", "case": label})
    for label, sample in NEGATIVE_CASES:
        hit = _flags(sample)
        if hit is not None:
            failures.append({"kind": "false_positive", "case": label, "rule": hit})
    for label, sample in ENV_POSITIVE_CASES:
        if not env_template_violations(sample):
            failures.append({"kind": "env_false_negative", "case": label})
    for label, sample in ENV_NEGATIVE_CASES:
        bad = env_template_violations(sample)
        if bad:
            failures.append({"kind": "env_false_positive", "case": label, "detail": bad})

    # The pragma must be inert outside this scanner file.
    pragma_leak = _flags(
        "-----BEGIN RSA PRIVATE KEY-----  # scanner:selftest-fixture",
        rel="src/somewhere_else.py")
    if pragma_leak is None:
        failures.append({"kind": "pragma_escaped_its_file",
                         "case": "pragma honoured outside the scanner"})

    return {
        "schema": "okx.public_repo_scan.selftest/v4",
        "positive_cases": len(POSITIVE_CASES),
        "negative_cases": len(NEGATIVE_CASES),
        "env_positive_cases": len(ENV_POSITIVE_CASES),
        "env_negative_cases": len(ENV_NEGATIVE_CASES),
        "pragma_scope_case": 1,
        "failures": failures,
        "status": "PASS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="PUBLIC_REPO_SCAN_RECEIPT.json")
    parser.add_argument("--self-test", action="store_true",
                        help="run built-in rule self-tests and exit")
    args = parser.parse_args()

    if args.self_test:
        receipt = self_test()
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if receipt["status"] == "PASS" else 2

    root = Path(args.root).resolve()
    receipt = scan(root)
    receipt["self_test"] = self_test()
    if receipt["self_test"]["status"] != "PASS":
        receipt["status"] = "BLOCK"

    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
