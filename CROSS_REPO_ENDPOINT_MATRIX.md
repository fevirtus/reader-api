# Cross-Repo Endpoint Mapping Matrix

Muc tieu: map 1-1 giua API backend, Web va Mobile cho user-facing flows.

Legend:
- `Y`: da tich hop
- `P`: partial / can verify them
- `N`: chua tich hop

| Domain | Endpoint | API | Web | Mobile | Notes |
|---|---|---|---|---|---|
| Health | `GET /api/health` | Y | P | P | Dung cho monitor, khong phai main UI flow |
| Auth | `POST /api/auth/mobile-login` | Y | Y | Y | Web dung route adapter login, mobile dung JWT |
| User | `GET /api/user/profile` | Y | P | Y | Web dang goi qua user route proxy |
| User | `GET/POST /api/user/bookmarks` | Y | Y | Y | Parity can test theo tabs bookshelf |
| User | `DELETE /api/user/bookmarks/{novelId}` | Y | P | Y | Web co remove flow can verify UX parity |
| User | `POST /api/user/reading-progress` | Y | P | Y | Can doi chieu dong bo chapter progress |
| User | `GET/POST /api/user/settings` | Y | Y | N | Mobile chua thay call settings ro rang |
| User | `GET/POST/DELETE /api/user/recommendations` | Y | Y | N | Mobile chua thay provider recommendation |
| Catalog | `GET /api/genres` | Y | Y | Y | |
| Catalog | `GET /api/novels/browse` | Y | Y | Y | |
| Catalog | `GET /api/novels/{idOrSlug}` | Y | Y | Y | |
| Novel | `GET /api/truyen/{id}/chapters` | Y | Y | Y | |
| Novel | `GET /api/truyen/{id}/chapters/by-number/{n}` | Y | Y | N | Mobile doc chapter theo chapterId endpoint |
| Chapter | `GET /api/chapters/{chapterId}` | Y | N | Y | Web doc chapter qua truyen/by-number |
| Comment | `GET/POST /api/truyen/{id}/comments` | Y | Y | Y | |
| Rating | `POST /api/truyen/{id}/rate` | Y | Y | N | Mobile chua thay rating flow |
| Search | `GET /api/truyen/suggest` | Y | Y | N | Mobile search suggest can bo sung |
| Import | `POST /api/import/uploads/preview` | Y | Y | N | Upload EPUB multipart (preview) |
| Import | `POST /api/mod/epub`, `POST /api/mod/epub/ai-suggest` | Y | Y | N | Luong `/mod/import` |
| Import | `GET/POST/PUT/DELETE /api/mod/the-loai` | Y | Y | N | MOD quan ly the loai trong wizard |

## Priority gaps de dong bo tiep

1. Mobile: `user/settings`, `recommendations`, `rate`, `suggest`.
2. Web/Mobile chapter-read strategy can unify (`chapters/{id}` vs `by-number`).
3. Chuan hoa error contract implementation theo `CONTRACT.md`.
