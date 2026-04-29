# Rollout Checklist - NAS Chapter Storage

## Pre-Deploy
- [ ] Backup PostgreSQL schema + critical tables
- [ ] Verify NAS mount/access permissions in API runtime
- [ ] Enable feature flags (default: Mongo fallback on)

## Deploy Order
1. Deploy DB migrations
2. Deploy API with dual-read disabled by default
3. Enable discover/approve/convert job APIs
4. Run pilot import set (small curated EPUB batch)
5. Enable NAS-first for pilot users/env
6. Gradually ramp NAS-first traffic

## Runtime Verification
- [ ] `/api/health` stable
- [ ] Chapter read success rate >= target
- [ ] NAS read timeout/error rate below threshold
- [ ] Mongo fallback rate trending down

## Rollback
- [ ] Switch feature flag to Mongo-first immediately
- [ ] Stop import jobs
- [ ] Keep imported refs for investigation (no destructive cleanup)

## Post-Deploy
- [ ] Compare chapter counts and random content samples
- [ ] Review failed/review_required import queue
- [ ] Publish release notes for web/mobile teams
