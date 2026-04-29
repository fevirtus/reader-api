# Shared Development Skills (Reader Suite)

Tai lieu mo ta bo skill de phat trien thong nhat giua web/app/api.

## skill: feature-parity-check
- Muc dich: so sanh 1 feature giua web va mobile theo cung API contract.
- Input: ten feature, endpoint list, expected UX behavior.
- Output: parity report (done/missing/risk) cho 3 repo.

## skill: api-contract-guard
- Muc dich: review thay doi API de tranh breaking web/mobile.
- Input: diff endpoint, request/response schema, auth requirement.
- Output: compatibility checklist + migration huong dan.

## skill: auth-flow-verifier
- Muc dich: verify luong dang nhap web cookie va mobile JWT map cung identity.
- Input: login flow, token/session behavior, protected endpoints.
- Output: matrix pass/fail + fix recommendation.

## skill: release-readiness
- Muc dich: chuan bi release dong bo 3 repo.
- Input: danh sach thay doi theo repo.
- Output: rollout order, smoke-test list, rollback note.
