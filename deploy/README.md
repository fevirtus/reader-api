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

A warm inference subprocess is bounded to one hour per request and defaults to
one ONNX thread (`AUDIOBOOK_THREADS`). It loads the model once and reuses it for
up to 20 chapters (`AUDIOBOOK_RECYCLE_CHAPTERS`). It is recycled after errors,
timeouts, or RSS above 1800 MiB (`AUDIOBOOK_RECYCLE_RSS_MB`) between jobs. After ten
idle minutes it exits to release RAM. A hung native model can still be killed
without terminating the queue worker. The worker logs model load time, synthesis
time and audio duration/RTF; compare those on representative chapters before
raising CPU or concurrency. One global worker remains deliberate on 2-CPU nodes.
Increasing replica count alone does not increase throughput. Model weights are cached on
PVC and copied out of Hugging Face blob symlinks for ONNX external-data validation.
Inference is retried up to three times with five-minute backoff. Failed jobs stay
visible; operators can reset attempts after fixing the underlying problem.

The worker reconciles requested editions with chapter hashes every minute. Old
ready versions stay downloadable. Chapter/novel deletion cascades job metadata;
unreferenced worker-owned files are removed after seven days. No automated GC
removes referenced historical snapshots. Monitor disk capacity and retention.
Cleanup runs as a single background task: a slow NAS scan cannot stop queue
consumption or heartbeats. A cleanup failure is logged and retried next hour.

`GET /api/audiobooks/assets/<id>` supports HEAD and HTTP Range. Public audio follows
the same access policy as public chapter text. Request creation and listening
progress require the existing user authentication. Model endpoints are not exposed.

Initial storage is served by the API's FileResponse without retaining a DB
connection during playback. If egress/HTTP load grows, put a file-serving proxy or
object storage in front using the same immutable asset IDs; no model scaling is
required for cached listeners.

V2 playback is stream-only. No audio download library is exposed on web/app;
only transient player buffering and durable listening progress remain. Server
chapter assets and whole-book exports are retained. Text offline and V1 TTS are
unchanged. App migration deletes only the retired `audiobooks` support directory.

Initial homelab measurement (2026-09-23, 1.5 CPU quota, same short Vietnamese
passage, three generations per setting): model startup ~6 seconds, two threads
~2.02 seconds compute per second audio; one thread ~1.93. Generation is stochastic
so these are indicative samples, not a whole-chapter SLA. Keep one thread until
representative longer benchmarks justify increasing it. Warm reuse removes model
startup between jobs; it does not imply real-time rendering on this CPU.

Voice previews: the worker generates one short shared sample for each of the 23
pinned presets in `audiobook-previews/<revision>/<voiceId>.m4a` on the content PVC.
A sample and a story chapter get a turn in each loop, avoiding chapter starvation.
Samples publish atomically after encoding/validation; preview errors back off and
do not stop chapter rendering. No public HTTP request performs inference. Wait
for all 23 `previewUrl` values to become non-null before declaring rollout complete.

## Queue and render visibility

The Audio book page on web/app shows completed/total chapters for each voice and
each chapter's waiting, rendering or failed state; it refreshes periodically.
The operator view below is read-only and does not load a model or read chapter files:

```sh
kubectl --kubeconfig ~/.kube/homelab -n reader exec deployment/reader-api -- \
  /app/.venv/bin/python -m app.audiobook_status
kubectl --kubeconfig ~/.kube/homelab -n reader logs -f \
  deployment/reader-audiobook-worker --since=10m --timestamps
```

Status includes counts, jobs awaiting retry (`backoff`), jobs with all three attempts
used (`exhausted`), oldest queued timestamp, and active chapter/voice/elapsed time.
`workerLockHeld` only proves lock ownership, not that the worker is progressing.
The worker emits `Queue heartbeat` every 30 seconds, even during inference or NAS
cleanup, plus `Render claimed`, source/synthesis/encoding/publishing phases and
`Render ready` or `Render failed`. Synthesis reports completed chunks and generated
audio seconds periodically; those are progress indicators, not a percentage or ETA.
The API logs `Audio request accepted` after committing queue entries. No chapter
text or user credentials are logged. Web pod logs do not contain render progress:
inference runs exclusively in the Audio book worker.
