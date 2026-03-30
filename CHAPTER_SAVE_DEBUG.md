# 🐛 Chapter Save Debugging Guide

## Vấn Đề Đã Fix

### ✅ Fix 1: Ownership Check (Line 932)
- **Vấn đề:** MOD không thể tạo chapter cho truyện mặc định (uploaderId = NULL)
- **Fix:** Thêm `OR uploaderId IS NULL` vào WHERE clause
- **Dòng:** 932, 1002

### ✅ Fix 2: Input Validation
- **Vấn đề:** Content có thể trống, number có thể âm  
- **Fix:** Thêm validation trước insert
- **Dòng:** 920-927, 997-1004

### ✅ Fix 3: Data Consistency Logging
- **Vấn đề:** Nếu MongoDB insert succeed nhưng PostgreSQL fail → dữ liệu inconsistent
- **Fix:** Thêm separate error handling và logging [CRITICAL]
- **Dòng:** 956-974

---

## 🔍 Testing Checklist

### A. Network Debugging (Browser DevTools)

1. **Mở DevTools:** F12 → Network tab
2. **Ấn "Lưu chương"**
3. **Kiểm tra request `POST /api/mod/chuong`:**
   - ✅ Status code: `201` = success, `4xx`/`5xx` = error
   - ✅ Response body: Phần lấy `id`, `number`, `title`
   - ❌ Status 403: Ownership issue
   - ❌ Status 400: Duplicate or validation error
   - ❌ Status 500: Server error (check [CRITICAL] logs)

### B. MongoDB Verification

```bash
# Access MongoDB
mongosh  # hoặc mongo

# Switch to database
use reader_db  # (thay bằng tên DB thực tế)

# List recent chapters
db.chapters.find({}, {novelId: 1, number: 1, title: 1, createdAt: 1})
  .sort({_id: -1})
  .limit(5)

# Check specific novel
db.chapters.countDocuments({novelId: "YOUR_NOVEL_ID"})

# Check for duplicates (race condition)
db.chapters.find({novelId: "YOUR_NOVEL_ID", number: 1})
```

### C. PostgreSQL Verification

```bash
# Access PostgreSQL
psql  # hoặc your database client

# Check novel total chapters count
SELECT id, title, "totalChapters" FROM "Novel" WHERE id = 'YOUR_NOVEL_ID';

# Verify it matches MongoDB count
-- MongoDB should have same count as "totalChapters"
```

### D. Server Log Analysis

Look for these patterns in backend logs:

```
✅ Success:
[timestamp] POST /mod/chuong - Status 201
[timestamp] Inserted chapter id: xxx

❌ Issues:
[CRITICAL] ⚠️ INCONSISTENT STATE: Chapter inserted in MongoDB...
[timestamp] Lỗi MongoDB: [error message]
[timestamp] Ownership check failed: 403
```

---

## 🚀 Common Scenarios & Solutions

### Scenario 1: Network Shows 201 But Chapter Not Visible

**Cause:** Chapter saved but not refreshed in UI
**Solution:** 
- Press F5 to refresh page
- Check MongoDB to confirm chapter exists
- Check if `fetchChapters()` was called after save

### Scenario 2: Network Shows 403 Forbidden

**Cause:** Novel ownership check failed
**Solution:**
- Verify you are MOD or ADMIN user
- Verify novel exists in PostgreSQL: 
  ```sql
  SELECT id, title, "uploaderId" FROM "Novel" WHERE id = 'YOUR_ID';
  ```
- If uploaderId is NULL (default), ensure you're MOD user

### Scenario 3: Network Shows 400 Bad Request

**Causes:**
- Chapter number already exists
- Title or content empty
- Chapter number ≤ 0

**Solution:** Check response detail message and fix input

### Scenario 4: Network Shows 500 Server Error

**Cause:** MongoDB or PostgreSQL failure
**Solution:**
- Check server logs for [CRITICAL] message
- If MongoDB failed: Check MongoDB connection
- If PostgreSQL failed: Check PostgreSQL connection
- Contact admin with error message

---

## 🔧 Advanced Debug Commands

### Check MongoDB Connection Status

```bash
# From backend terminal
python3 -c "
import asyncio
from app.database import mongo_db

async def check():
    await mongo_db.command('ping')
    print('✓ MongoDB Connected!')

asyncio.run(check())
"
```

### Check PostgreSQL Connection Status

```bash
# From backend terminal
python3 -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from app.database import SessionLocal

async def check():
    async with SessionLocal() as session:
        result = await session.execute('SELECT 1')
        print('✓ PostgreSQL Connected!')

asyncio.run(check())
"
```

### Manually Sync Total Chapters

```bash
# If totalChapters is out of sync
mongosh
use reader_db

# Get count
db.chapters.countDocuments({novelId: "YOUR_NOVEL_ID"})

# Then update PostgreSQL manually:
# psql
UPDATE "Novel" SET "totalChapters" = 123 WHERE id = 'YOUR_NOVEL_ID';
```

---

## 📋 Test Cases

### Test 1: Basic Chapter Save
```
1. Create novel
2. Save chapter #1
3. ✅ Should appear in chapter list
4. ✅ totalChapters should be 1
```

### Test 2: Sequential Chapters
```
1. Save chapters 1, 2, 3
2. ✅ All should appear with correct numbers
3. ✅ Next chapter field should suggest 4
```

### Test 3: Duplicate Prevention
```
1. Save chapter #5
2. Try to save chapter #5 again
3. ✅ Should show "Chương 5 đã tồn tại"
```

### Test 4: Default Novel (MOD Permission)
```
1. Verify a novel with uploaderId = NULL exists
2. As MOD user, save chapter to that novel
3. ✅ Should succeed (not 403 Forbidden)
```

### Test 5: No Empty Content
```
1. Try to save chapter with empty title
2. ✅ Should show "Tiêu đề chương không được trống"
3. Try to save chapter with empty content
4. ✅ Should show "Nội dung chương không được trống"
```

---

## 🆘 Still Having Issues?

1. **Run checklist A, B, C above** and collect outputs
2. **Screenshot of Network tab response**
3. **MongoDB output from `find()`**
4. **Server log output (especially [CRITICAL] lines)**
5. Share these with: [your-dev-contact]

---

## 📊 Monitoring

### Health Check Script

```bash
#!/bin/bash
# save as monitor-save.sh

echo "=== Chapter Save System Health Check ==="
echo ""
echo "1. MongoDB Connection:"
# mongosh check here

echo ""
echo "2. PostgreSQL Connection:"
# psql check here

echo ""
echo "3. Backend API:"
curl -s http://localhost:8000/docs | head -20

echo ""
echo "=== Done ==="
```

Run: `bash monitor-save.sh`
