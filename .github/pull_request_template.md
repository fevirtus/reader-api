## Summary
- What problem does this PR solve?
- Why now?

## Scope
- [ ] Web (`reader`)
- [ ] API (`reader-api`)
- [ ] Mobile (`reader-app`)

## API Contract Impact
- [ ] No API changes
- [ ] Backward-compatible API changes
- [ ] Breaking API changes (must include migration plan)

Changed endpoints:
- `...`

## Auth Impact
- [ ] None
- [ ] Web session (NextAuth cookie)
- [ ] Mobile JWT

## Cross-Platform Parity Checklist
- [ ] Web behavior aligned with API response
- [ ] Mobile behavior aligned with API response
- [ ] Error states consistent (401/403/4xx/5xx)
- [ ] Pagination behavior consistent (`page`, `limit`)

## Test Evidence
- [ ] Unit tests
- [ ] Integration/API tests
- [ ] Manual smoke test Web
- [ ] Manual smoke test Mobile

Commands / notes:
```bash
# paste test/build commands executed
```

## Rollout & Rollback
- Rollout order: API -> Web/Mobile -> QA
- Rollback strategy:
  - API:
  - Web:
  - Mobile:
