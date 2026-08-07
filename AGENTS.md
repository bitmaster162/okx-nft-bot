# AGENTS.md — Public Repository Safety Contract

## Project

OKX NFT market automation and analytics. This repository contains source code,
tests, and non-secret configuration templates.

## Risk classification

A3: the broader system may interact with financial and on-chain infrastructure.
Repository changes are not authority to perform live actions.

## Development rules

1. Begin from a clean Git baseline and record branch, HEAD, and tree.
2. Work only on a dedicated candidate branch.
3. Keep credentials, runtime databases, logs, cookies, local agent settings,
   backups, exports, and host-specific scripts out of Git.
4. Run compile, tests, packaging, and the public-repository scanner before
   proposing review.
5. Treat tests as evidence of code behavior, not proof of production safety.
6. Do not claim repository-to-runtime equivalence without an independent,
   read-only digest map.
7. Do not push to the default branch, merge, deploy, restart services, or change
   live controls without separate operator authorization.

## Live-effect boundary

Live offers, buys, sales, cancellations, wallet actions, signing, arming,
kill-switch changes, limit changes, and capital effects are outside ordinary
repository work. The default posture for development and CI is no live effect.

## Secret handling

Never commit or print secrets. Use local environment files or an approved secret
store. Examples, tests, and documentation must use obvious synthetic values.

## Completion evidence

A change is ready for review only when the exact candidate HEAD/tree, diff,
test output, package result, scanner receipt, and no-effect receipt are
available. Review readiness is not merge or deployment authorization.
