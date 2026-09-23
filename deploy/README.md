# Audio book deployment

V1 device TTS remains independent. V2 renders immutable chapter AAC/M4A files using
VieNeu 3.7.1 with model revision 61b85e3d937fbbacb387714180e8182823512523.
The model uses CPU ONNX; no inference code is loaded into the HTTP API or mobile app.

1. Run `tests/test_audiobooks.py` and the existing web/offline compatibility tests
   against the disposable localhost `reader_sync_test` database.
2. Build API and worker images via `.github/workflows/docker-publish.yml`.
3. Roll out the API using its immutable `sha-<commit>` image. Additive tables are
   initialized during API startup. Do not remove these tables on rollback.
4. Substitute `AUDIOBOOK_IMAGE` in `audiobook-worker.yaml` with the built worker
   image (`ghcr.io/fevirtus/reader-api-audiobook:sha-<commit>`) and apply.
5. Verify worker memory and source-to-audio playback before enabling user requests
   through the new web/app UI. One worker is enforced by a PostgreSQL session lock.
6. Roll out web, then distribute the Android app. Retain previous image digests
   for rollback. Scale only the worker to zero to pause rendering; ready audio remains usable.

Storage: chapter audio is under the existing text chapter's parent directory:
`audio/<chapter hash>/<edition id>/<asset id>/chapter.m4a`.
Whole-book exports are under `novel-<id>/audiobook/<edition>/exports/<revision>/complete.m4b`.
Exports include chapter markers and represent the complete available chapter list
at that revision, including unfinished novels. They are rebuilt after the render
queue drains. Original chapter text paths do not change.

A render subprocess is bounded to one hour and two ONNX threads. It is isolated
per chapter so a hung native model can be terminated. Model weights are cached on
PVC and copied out of Hugging Face blob symlinks for ONNX external-data validation.
Inference is retried up to three times with five-minute backoff. Failed jobs stay
visible; operators can reset attempts after fixing the underlying problem.

The worker reconciles requested editions with chapter hashes every minute. Old
ready versions stay downloadable. Chapter/novel deletion cascades job metadata;
unreferenced worker-owned files are removed after seven days. No automated GC
removes referenced historical snapshots. Monitor disk capacity and retention.

`GET /api/audiobooks/assets/<id>` supports HEAD and HTTP Range. Public audio follows
the same access policy as public chapter text. Request creation and listening
progress require the existing user authentication. Model endpoints are not exposed.

Initial storage is served by the API's FileResponse without retaining a DB
connection during playback. If egress/HTTP load grows, put a file-serving proxy or
object storage in front using the same immutable asset IDs; no model scaling is
required for cached listeners.
