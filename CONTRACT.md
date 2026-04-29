# API Contract Standard (Reader Suite)

Tai lieu contract chung cho `reader`, `reader-app`, `reader-api`.

## Base and Versioning

- Base path: `/api/*`
- Current mode: unversioned with backward-compatible evolution.
- Breaking changes bat buoc qua ke hoach migration va release note.

## Response Convention

- Success:
  - `200/201`: tra JSON payload domain data.
  - `204`: khong co body.
- Error (standardized):

```json
{
  "code": "string",
  "message": "human readable",
  "details": {}
}
```

## HTTP Status Usage

- `400`: input/validation khong hop le.
- `401`: chua dang nhap hoặc token/session invalid.
- `403`: da dang nhap nhung khong du quyen.
- `404`: resource khong ton tai.
- `409`: xung dot du lieu.
- `422`: payload format dung JSON nhung khong dat rule nghiep vu.
- `500`: loi he thong.

## Pagination Convention

- Query params: `page`, `limit`.
- Response envelope:

```json
{
  "items": [],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total": 0,
    "totalPages": 0
  }
}
```

## Auth Matrix

- Web: NextAuth session cookie.
- Mobile: Bearer JWT.
- Backend phai map ca 2 vao cung user identity + role.

## Compatibility Rules

- Khong xoa hoac doi y nghia field dang duoc client su dung.
- Field moi phai optional theo default trong giai doan rollout.
- Doi ten field => tao migration layer hoac add field moi, deprecate sau.
- Endpoint moi can update README + mapping matrix.
