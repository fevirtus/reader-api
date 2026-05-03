# Features - Reader API

Tinh nang backend cho web + mobile.

## Public/User-Facing API

| Domain | Endpoint Group | Status | Notes |
|---|---|---|---|
| Health | `/api/health` | done | Healthcheck |
| Auth | `/api/auth/mobile-login` | done | Mobile JWT login |
| User | `/api/user/*` | done | profile, bookmarks, progress, settings, recommendations |
| Catalog | `/api/genres`, `/api/novels/*` | done | browse/detail |
| Reading | `/api/truyen/*`, `/api/chapters/*` | done | toc, comments, rate, chapter detail |
| Search | `/api/truyen/suggest` | done | web dang dung, mobile con gap |

## MOD/ADMIN API

| Domain | Endpoint Group | Status | Notes |
|---|---|---|---|
| Content management | `/api/mod/*` | partial | da co nhieu route, tiep tuc bo sung nhu cau |
| EPUB import | `/api/import/*` | done | review-first wizard APIs + progress session |

## Contract + Parity Responsibility

- Canonical contract owner cho ca 2 clients.
- Moi endpoint moi phai update:
  - `README.md`
  - `CONTRACT.md`
  - `CROSS_REPO_ENDPOINT_MATRIX.md`
