# Domain Docs

How repo-level engineering skills should read Spica project context.

## Reading Order

Read these before engineering work, in order:

1. `AGENTS.md`
2. `CLAUDE.md`, if it exists
3. `docs/DEVELOPMENT_GUARDRAILS.md`
4. `docs/FUTURE_FEATURE_PLAYBOOK.md`
5. `docs/ARCHITECTURE_FOR_ALGORITHM_ENGINEERS.md`
6. `docs/REAL_ARCHITECTURE_MAP.md`

## Domain Layout

This is a single-context repo. If `CONTEXT.md` or `docs/adr/` appear later, skills may use them for domain vocabulary and durable architectural decisions. Their absence is not an error.

## Spica Rules

Repo-level skills do not override Spica guardrails. In particular, do not bypass `run_turn`, config resolution, `CapabilityRegistry`, `RuntimeEvent`, or the ports/adapters seams documented in the project guides.
