## What and why

## Checklist
- [ ] `make lint` and `make test` pass (and `make test-pg` if state/SQL changed)
- [ ] Money-moving or multi-step changes have a fault-injection test and an idempotent, deterministic Ledger reference
- [ ] No secrets; new dependencies justified and added to `THIRD_PARTY.md`
- [ ] Docs updated (`docs/`, `CHANGELOG.md`)
- [ ] Commits are signed off (`git commit -s`)
