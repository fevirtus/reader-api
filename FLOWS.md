# Flows - Reader API

Backend flow theo domain, de web/mobile follow giong nhau.

## Flow A: Auth Identity Unification

- Input:
  - Web session cookie (NextAuth)
  - Mobile bearer JWT
- Behavior:
  1. Resolve current user identity.
  2. Validate role/permission.
  3. Return consistent auth errors (`401/403`).

## Flow B: Discovery and Reading

- Discovery:
  - `/api/genres`
  - `/api/novels/browse`
  - `/api/novels/{idOrSlug}`
- Reading:
  - `/api/truyen/{id}/chapters`
  - `/api/chapters/{chapterId}` or chapter-by-number variant
- Rule: response shape on dinh de client render.

## Flow C: User Personalization

- Bookmark: `/api/user/bookmarks`.
- Progress: `/api/user/reading-progress`.
- Settings: `/api/user/settings`.
- Recommendations: `/api/user/recommendations`.
- Rule: idempotent where possible, clear conflict semantics.

## Flow D: Social Interaction

- Comments: `/api/truyen/{id}/comments`.
- Rating: `/api/truyen/{id}/rate`.
- Rule: enforce auth + anti-invalid payload + stable error format.
