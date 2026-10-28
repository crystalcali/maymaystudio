# Architecture and operational boundaries

The first deploy intentionally uses one same-origin web service: React/Vite build served by FastAPI. PostgreSQL is durable control state; private S3 stores datasets, snapshots, exported weights, trainer state and media. No model weights or GPU execution reside in the web container.

The API supports one authenticated owner. Session cookies are HttpOnly, SameSite=Strict and Secure in production. Mutating browser requests check Origin. Worker authentication is separate hashed bearer tokens with one-use pairing. User tokens entered through Settings are encrypted using a persistent SECRET_KEY; workers never receive YouGPU credentials.

Jobs have durable states and attempt IDs. Claim uses a guarded status update and PostgreSQL row locking; worker callbacks must match job owner/attempt. The worker renews a 120-second lease on a separate thread and stops the process group after 95 seconds without successful renewal. A child-side watchdog also terminates orphaned runner process groups after 110 seconds without a renewed lease file. Lost tasks are reconciled, not automatically duplicated. The worker retains local files on errors. Stop/delete of provider instances is never inferred from deleting a UI card.

A process per task isolates CUDA state. Each subprocess launches fixed argument arrays, not shell commands. GPU code and model revisions are recorded. Warm model serving, DDP and tensor parallel are deferred. Independent workers allow task-level concurrency.

Artifacts are invisible until bytes are uploaded and finalization succeeds. Keys contain job/attempt/hash, and repeated completion is idempotent. Local uploads verify SHA256; S3 verifies length after transfer, with content hash computed by the trusted worker. S3 multipart supports large state archives. Metadata describes provenance; image/video/preview are the only gallery kinds. Dataset derivatives remain outside the gallery.

Dataset imports reject traversal/symlinks/oversized ZIPs and verify image decoding. Exact duplicates are skipped. Perceptual hash candidates are excluded pending review and inherit a source group. Train/validation split is by source group. Frozen ZIPs have deterministic member timestamps and a content hash, so resume can require the same snapshot.

Price estimates distinguish the hourly quote, useful computation, preparation and idle rental. Benchmarks match recipe, hardware and relevant settings; estimates are unknown until suitable measurements exist. Provider billing remains authoritative. Budget cancellation is best-effort, not a guaranteed spending cap. Auto-termination requires policy opt-in, a Studio-owned instance, a live agent and no unfinished tasks. Loss of the web service/agent requires external billing supervision.

External contracts:
- YouGPU adapter: /catalog/gpus, /catalog/offers/{id}, /instances, /instances/{id}, /instances/{id}/terminate. Never automatically retry a paid create after timeout.
- Hugging Face: metadata access and file presence preflight, revision pinning; downloads occur on worker.
- S3-compatible object API: private bucket, signed GET/PUT, multipart; lifecycle should abort abandoned uploads.

Initial deployment creates schema v3. Upgrades that change schema need an explicit migration/backup; create_all is not a schema migration tool. One web process/replica is the supported initial configuration. PostgreSQL/S3 provider and Docker/GPU integration are not validated by SQLite API tests.
