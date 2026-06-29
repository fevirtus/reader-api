# Features - Reader API

Tinh nang backend cho web + mobile.

## Public/User-Facing API

| Domain | Endpoint Group | Status | Notes |
|---|---|---|---|
| Health | `/api/health` | done | Healthcheck |
| Auth | `/api/auth/mobile-login` | done | Mobile JWT login |
| User | `/api/user/*` | done | profile, bookmarks (`markAsRead`, progress), settings |
| Catalog | `/api/genres`, `/api/novels/*` | done | browse/detail co `latestChapter` |
| Reading | `/api/truyen/*`, `/api/chapters/*` | done | toc, rate (1-10), chapter detail |
| Search | `/api/truyen/suggest` | done | web dang dung, mobile con gap |

## MOD/ADMIN API

| Domain | Endpoint Group | Status | Notes |
|---|---|---|---|
| Content management | `/api/mod/*` | partial | da co nhieu route |
| EPUB import | `/api/import/*` | done | review-first wizard APIs + progress session |

## Da loai bo

- Comment, user/editor recommendations, admin truyen thieu du lieu

## Contract + Parity Responsibility

- Canonical contract owner cho ca 2 clients.
- Moi endpoint moi phai update:
  - `README.md`
  - `CONTRACT.md`
  - `CROSS_REPO_ENDPOINT_MATRIX.md`
