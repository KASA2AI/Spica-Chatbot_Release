# Spica Local Config Studio — accepted v1 design

Status: Accepted for implementation on 2026-07-11
Implementation fixed point: `22512ee9be026de133bcaaa4afc852e88d25aa6c`
Architecture decision: [ADR 0001](adr/0001-config-studio-loopback-sidecar.md)

Current implementation checkpoint: the read-only catalogue, self-check, and
interactive Linux writer lanes are implemented. The normal CLI delegates to a
fixed-path production composition root, which independently enables app,
overlay, sensitive, and rollback capabilities only when their concrete owners,
verified Linux platform adapter, and private transaction-state paths pass their
checks. Missing, unsafe, unsupported, or partially available owners remain
server-side capability-closed; the browser cannot unlock them. Automatic launch
uses the fragment handoff; disabled or failed browser launch uses the same high-
entropy, short-lived, one-shot grant through an explicit nonpersistent paste
dialog. It never degrades to a short human-memorable code.

This is an uncommitted implementation checkpoint, not a claim that Windows or
real-machine secret/env smoke has passed, not a final integration-test report,
and not a formal `$code-review` result.

This document is the implementation contract for Config Studio v1. It records the
accepted design decisions, security boundaries, non-goals, public seams, error
codes, and verification gates. Changing a security boundary or broadening the
write surface requires an explicit design review; implementation convenience is
not sufficient justification.

## 1. Product outcome

Config Studio is a separately launched browser configuration application for
ordinary Spica users. It binds only to a loopback address, serves all assets
locally, and presents configuration through production owners instead of a
generic YAML/JSON editor.

The Studio provides:

- a schema-driven catalogue of every `AppConfig` leaf;
- current document value, schema default, `next_launch_value`, winning source,
  validation state, owner, and concrete effect policy;
- field search plus Basic and Advanced views;
- a focused write surface for already-typed owners;
- validation and semantic diff before commit;
- byte-preserving, conflict-detecting, atomic publication with RestorePoints;
- write-only secret management and clear-only mapped repo override management;
- lightweight and explicitly confirmed heavy self-check jobs;
- a local Spica-specific UI using the accepted repository image, with a safe CSS
  fallback when the decorative asset is invalid.

`next_launch_value` has a precise meaning: the value that the production owner
would resolve on the next Spica launch from a specified, explicit environment
snapshot and the candidate documents. It is not a claim about the value inside
an already-running Spica process.

## 2. Non-goals

V1 deliberately does not:

- embed in, construct, or import `AppHost`;
- start with Spica automatically;
- claim to know whether Spica is running;
- promise runtime hot reload or a generic “pending restart” state;
- construct a second LLM, TTS, STT, OCR, VLM, song, or plugin runtime in the
  sidecar itself; an independently confirmed self-check may launch the existing
  diagnostic CLI under its fixed protocol;
- expose a generic YAML, JSON, dotenv, file-browser, shell, or arbitrary-command
  interface;
- make role TTS, role visual, or advanced song dictionaries writable before
  their production loaders own canonical typed schemas;
- turn `tts.yaml` or `visual.yaml` into `app.yaml` fields;
- put secrets in `app.yaml`, API reads, browser storage, logs, exceptions,
  screenshots, reports, or worker payloads;
- reset `app.yaml` to `AppConfig()` defaults (there is no canonical production
  reset document and those defaults differ from the shipped document);
- make a cross-document atomic transaction out of an `app.yaml` save followed by
  a repo dotenv override clear;
- migrate or silently fix the legacy `ManagementSurface` writers;
- modify `scripts/self_check.py` or `scripts/dump_resolved_config.py`;
- support production writes on Windows until the platform-specific smoke gates
  in section 15 pass.

## 3. Real documents and owners

“ConfigurationCarrier” keeps its existing repository meaning. Config Studio
uses “ManagedDocument” for a fixed, owner-approved file that it may snapshot and,
where permitted, write.

| Document or source | Production owner | V1 treatment | Effect policy |
| --- | --- | --- | --- |
| `data/config/app.yaml` | `spica.config.ConfigManager` + `AppConfig` | Typed fields editable, with exceptions below | Next Spica launch |
| `app.yaml:plugins` | `spica.plugins.manifest` | Strict typed list editable only when retired `plugins.yaml` is absent | Next Spica launch |
| `app.yaml:tts.enabled` | typed app config / host assembly | Editable | Next Spica launch |
| `app.yaml:song.enabled` | existing song owner’s strict boolean contract | Editable as a native JSON boolean only | Next Spica launch |
| Remaining `app.yaml:song` keys | `agent_tools.function_tools.song.config` raw dictionary owner | Complete read-only catalogue | Next Spica launch |
| Active character `tts.yaml` (normally `data/config/tts.yaml` or package override) | character package + TTS loader | Complete read-only catalogue only when inside an allowed local root; external is metadata-only | Next TTS construction; no Studio runtime promise |
| Active character `visual.yaml` (normally `data/config/visual.yaml` or package override) | character package + visual loader | Complete read-only catalogue only when inside an allowed local root; external is metadata-only | Owner supports mtime reread at its existing boundary |
| Character package metadata (`meta.json` and referenced role data) | character package loader | Read-only owner/path/health catalogue inside an allowed local root; external is metadata-only | Owner-specific |
| `ui/overlay_config.json` | `spica.config.overlay_owner` owns Qt-free defaults/ranges/resolution; `ui.overlay_config` owns IO and delegates persistence | Owner-supported fields editable | Per owner; live UI application is separate from persistence |
| Repo `xiaosan.env` | `SecretStore` / config env owners | Write-only secret set/clear; mapped non-secret overrides clear-only | Next Spica launch and subsequent explicitly spawned self-check |
| Parent `xiaosan.env` | production dotenv loading order | Read-only source/health information | Next Spica launch |
| Inherited process environment | process launcher + production owners | Read-only source information | Snapshot-specific |
| `screen_vision_config.json`, `song_config.json`, `plugins.yaml` | retired legacy fallback surfaces | Health warning only; never created or updated | Legacy hazard |
| External absolute character documents | external package owner | Metadata-only: `external_read_only`, owner, known basename (otherwise unavailable), and bounded health; never opened or returned | Owner-specific |

`ManagementSurface.write_config()` remains a legacy unsafe full-dump writer. Its
plugin mutator may recreate retired `plugins.yaml`. Config Studio never calls
either path and does not claim to be the repository’s only writer.

## 4. V1 write scope

Writable:

1. Existing typed authoring fields of `AppConfig`, except runtime-derived fields.
2. Strict plugin entries: unique validated names and native booleans, resolved
   against a construction-free built-in catalogue.
3. Owner-supported overlay preferences.
4. The four secret slots through typed write-only commands.
5. Clear-only repo dotenv operations for variables dynamically sourced from
   `APP_ENV_MAP` and `SCREEN_ENV_MAP`.

Special cases:

- `tts.enabled` is already typed and writable.
- `song.enabled` is writable only through the current owner’s strict boolean
  contract. The rest of the song dictionary is read-only.
- Runtime-derived `character_id`, built persona/profile, and display name are
  read-only. Operations on an ancestor object that could replace or remove one
  of those leaves are rejected at the API owner boundary as well.
- A reappearing retired `plugins.yaml`, `screen_vision_config.json`, or
  `song_config.json` makes its corresponding app section read-only and emits a
  health error. Preview, commit, rollback preparation, and rollback publication
  recheck this condition; Studio never creates or edits a retired owner.
- Dynamic maps without a canonical authoring schema are rendered read-only with
  an explicit reason; they are not silently omitted.
- App authoring exposes typed `set` and `unset` operations. `unset` removes the
  file-level override so the production resolver may fall back to an env
  override or schema default; it is not a generic mapping editor. Whole nested
  Pydantic model operations are rejected so a caller cannot bypass leaf-level
  constraints through an ancestor value. Canonical non-model containers such as
  the strict plugin list retain their explicit owner contract.

Future write unlocks for song, role TTS, and role visual are separate vertical
TDD migrations. Each production loader must delegate to its canonical typed
schema with a zero-diff production gate before its form is enabled.

### 4.1 Fixed-path Linux owner composition

The normal sidecar entry delegates writer selection to
`ui.config_studio.composition.create_production_config_studio_services()`. The
composition accepts no browser-selected path. It may construct owners only for:

- `<repo>/data/config/app.yaml`;
- `<repo>/ui/overlay_config.json`;
- `<repo>/xiaosan.env`;
- `<repo>/spica_data/config_studio/{backups,locks}` as private transaction
  state.

On the verified Linux lane, app and overlay owners require the concrete managed-
document write capability plus a safe transaction-state tree. Sensitive writes
additionally require the sensitive-document capability and a live repo dotenv
whose health is `MISSING`, `PRIVATE`, or `TOO_PERMISSIVE`; the last state remains
writable only because the preview explicitly declares permission hardening and
publication must prove `0600`.

Capability derivation is lane-specific and fail-closed:

- an unsafe state root, backup root, or lock root closes every writer and is not
  repaired during composition;
- an unavailable or unsafe app owner closes `app_config_write` only;
- an unavailable or unsafe overlay owner closes `overlay_write` only;
- a hardlinked, wrong-owner, symlinked, or otherwise unsafe repo dotenv closes
  `sensitive_write` without unnecessarily closing safe app/overlay owners;
- unverified platforms produce the read-only service and do not create the
  transaction state tree merely by starting the Studio.

When at least one writer owner is valid, the service exposes only that exact
capability set and the separately gated rollback surface. Every HTTP route
rechecks its capability before parsing or dispatching a write. Rollback routes
also require their lane's own write capability, and the client applies the same
AND gate for app, overlay, and sensitive controls. CSS visibility is never the
security boundary.

## 5. Resolution model

### 5.1 Explicit environment snapshot

`EnvironmentSnapshot` contains only non-sensitive configuration overrides and
their provenance. It is built from explicit mappings. The pure builder does not
read `os.environ`; existing whitelist modules such as `manager.py` and
`secrets.py` may read the environment and pass values into the builder.

Secrets remain in a separate immutable `Secrets` value. They are combined with
the latest non-sensitive snapshot only in memory when an approved self-check
subprocess is started.

The snapshot models the production precedence rather than mutating process
globals. In particular it prevents repeated previews from observing values that
`load_dotenv(override=False)` froze into the Studio process before a repo dotenv
edit.

The Studio entry requests `prime_process=False`. A safe `LoadedSecrets.refresh()`
re-reads the fixed repo and parent dotenv documents for each catalogue/meta or
self-check environment request. It uses no-follow reads plus regular-file,
owner, and link-count checks; an unsafe refresh fails closed without falling
back to stale process globals.

Dotenv interpolation may reference inherited names outside the public roster.
`LoadedSecrets` therefore retains the complete explicit inherited interpolation
base supplied at construction and reuses it on refresh; it never reconstructs
that base from roster names or rereads current `os.environ`. The private base is
immutable, has a redacted `repr`, and refuses JSON serialization just like the
public secret-bearing values.

### 5.2 Production owner seam

The public seam is:

```python
ConfigManager.resolve_snapshot(raw_document, environment_snapshot)
```

It returns an immutable resolution DTO containing validated `AppConfig` data,
per-leaf next-launch values, and provenance. It does not mutate `os.environ` and
does not read dotenv files. `ConfigManager.load()` reads through the existing
whitelist boundary, delegates to this seam, and translates resolution errors to
the existing startup error behaviour.

`ConfigCatalog` consumes this DTO. It must not reproduce file/env/default merge
or coercion logic.

Fields folded only after `ConfigManager` (active character identity/profile,
`platform.os=auto`, and `stt.mic_backend=auto`) are labelled
`owner_derived`; their `next_launch_value` is unavailable rather than falsely
showing the pre-fold token. The two user-authored `auto` Literals remain
editable so a user can choose an explicit value.

`EnvironmentSnapshot`, the resolution DTO, and `Secrets` use safe `repr` output
and are intentionally not JSON serializable. API DTOs are constructed by an
explicit redacting projection. Canary tests pin these properties.

`load_secrets()` with no arguments remains source-compatible and returns
`Secrets`. A named opt-in return object may expose the associated explicit
environment snapshot without changing existing callers.

### 5.3 Preview freshness across documents

Preview validity includes all owner inputs that determine its semantic summary,
not only the target file revision. App preview captures the latest explicit
environment snapshot. Immediately before commit, the app owner obtains the
snapshot again; if a repo dotenv, parent dotenv, or inherited owner snapshot has
changed, the preview is consumed and commit returns `CONFIRMATION_REQUIRED`
without publishing. The user must obtain a new semantic preview.

Conversely, every mapped-override or sensitive rollback preview obtains the
latest fixed `app.yaml` document through its injected base-document owner.
Therefore the accepted two-step workflow—commit `app.yaml`, then request a new
dotenv preview—shows the newly saved app fallback rather than a construction-
time copy. Catalogue and meta reads likewise use the latest safe
`LoadedSecrets.refresh()` result. This provides cross-document semantic
freshness without pretending that app and dotenv writes form one atomic
transaction.

Sensitive previews and rollback receipts also bind the parent dotenv owner
inputs that determine source precedence and interpolation. Commit recomputes the
complete semantic preview from the current repo bytes, parent owner, inherited
mapping, and current app document. A parent-owner change returns
`CONFIRMATION_REQUIRED` (or the rollback-specific invalid-confirmation code),
even if the repo document revision and public redacted summary are unchanged.
The private comparison uses opaque all-layer owner material and never a public
hash or secret value.

## 6. Schema-driven catalogue and authoring

### 6.1 Catalogue generation

The catalogue walks `AppConfig`/Pydantic metadata recursively. It derives field
paths, types, nullability, literals, numeric constraints, defaults, nested
models, lists, and dynamic maps from the production schema. Presentation
metadata may add section, Basic/Advanced level, owner, effect policy, and a
read-only reason, but it may not redefine types or defaults.

Every production schema leaf must yield exactly one of:

- an editable control backed by an accepted owner operation; or
- a visible read-only entry with a stable unsupported reason.

A coverage test compares schema leaves to catalogue entries. Adding a schema
field therefore fails tests rather than silently omitting the field from the UI.

Controls are type-driven:

- `Literal` becomes a select;
- `bool` becomes a switch and accepts only native JSON booleans;
- bounded numbers carry schema range metadata and are validated server-side;
- schema-declared path fields get a non-following existence check only;
- lists and typed maps use their item schema;
- untyped/dynamic maps remain read-only unless a production owner provides a
  strict operation.

The catalogue exposes current document value, schema default,
`next_launch_value`, winning source, owner, health, effect policy, and override
warnings. It never describes the latter as a running value.

An external absolute character document is never parsed or returned. Its
catalogue projection is limited to `external_read_only`, owner, a sanitized
known basename (or unavailable), and bounded semantic health. The Studio does
not open the external package manifest merely to discover referenced TTS/visual
names. Hiding the absolute path is not a license
to disclose external TTS, visual, metadata, or role-data content.

The initial schema path metadata covers only true path fields:
`character.skill_dir`, `character.package_dir`, `stt.download_root`,
`ocr.trt.engine_cache_dir`, `anime.download_dir`, `anime.cookies_file`, and
`anime.library_file`. Ambiguous `stt.model` and command-like
`anime.player_command` are intentionally not treated as paths. Checks return
bounded semantic health only and never expose an external absolute path.
Overlay controls consume `spica.config.overlay_owner`; they do not copy its
defaults or ranges. `screen.max_side` range metadata likewise comes only from
the production `AppConfig` field.

### 6.2 Authoring validator

The public seam is:

```python
ConfigAuthoringValidator.validate(
    base_document,
    candidate_document,
    operations,
)
```

Operations are the typed commands `SetValue(path, value)` and
`UnsetValue(path)`. Paths use typed segments, not arbitrary dotted strings. This
keeps dynamic map keys unambiguous and lets the validator distinguish existing
unknown keys from newly introduced unknown keys. `UnsetValue` deletes only the
selected file override and never synthesizes a default into the document.

Validation rules include:

- final candidate validation through the production PyYAML parser and production
  owner resolver, even though ruamel.yaml preserves round-trip bytes/comments;
- native boolean semantics (`"false"` is never accepted as `False`);
- literal, numeric, list, and owner-specific constraints;
- no silently dropped unknown fields;
- duplicate YAML mapping keys are rejected at every depth;
- app authoring rejects YAML alias/shared-mutable graphs, while the read-only
  catalogue may represent a unique-key alias through bounded alias metadata;
- app preview, commit, rollback preparation, and rollback publication each use
  one immutable `LoadedSecrets` owner result for both its non-sensitive
  `EnvironmentSnapshot` and secret-material guard. The guard retains every
  rostered/legacy definition observed across inherited, repo, and parent
  layers, including shadowed and repeated dotenv assignments and their resolved
  interpolation values; it never exposes that collection. Candidate validation
  rejects any observed material with `DOCUMENT_INVALID`, including a secret in
  a submitted value, dynamic-map key, or PyYAML `!!binary` scalar. The recursive
  scan is Pydantic-schema-aware so fixed field names are not mistaken for user
  data. The scan runs both on the raw document and the production owner's typed
  semantic result, closing scalar coercions such as an integer becoming a
  secret-matching string; typed `unset` remains available to remove an
  already-leaked value;
- all externally visible data slots compare strings and canonical JSON
  `bool`/`int`/`float` scalars against the opaque all-layer sanitizer. A match is
  replaced before DTO construction, while fixed schema/protocol metadata is not
  rewritten merely because a secret is a common word such as `false`;
- authoring re-reads the full owner result after semantic validation and again
  immediately before publication. Any snapshot or opaque secret-material change
  invalidates the operation; if the candidate has meanwhile become secret it is
  rejected rather than published. Preview/receipt records retain only safe-repr,
  non-wire owner objects and are session/TTL bounded;
- existing unknown keys are preserved and surfaced, while operations cannot
  introduce new unknown keys;
- a human-readable semantic `ChangePreview` is required before commit.

If syntax damage prevents resolution, Studio enters recovery-only mode. It may
rollback to a valid RestorePoint. Without one, it remains read-only and shows a
stable manual repair guide. V1 has no generic app-default reset.

## 7. ManagedDocument transaction protocol

The transaction kernel is byte-oriented and stdlib-only. It has no dependency
on FastAPI or ruamel.yaml so existing UI persistence owners can reuse it.
Platform details are injected through the pure port described in section 15;
the transaction owner never detects the OS or imports `fcntl` itself.

The fixed-document reader and transaction kernel both consume the explicit
capabilities supplied by the platform adapter. They use no-follow identity
checks and reject symlinks/reparse points and non-regular files. Ordinary app
and overlay targets, not only the sensitive dotenv, also reject a link count
other than one and, on the verified POSIX lane, an owner UID other than the
injected current UID. An unsafe read returns bounded health; an unsafe write or
rollback fails with `DOCUMENT_UNSAFE` without publishing.

Public seam:

```python
ManagedDocumentTransaction.preview(...)
ManagedDocumentTransaction.commit(...)
ManagedDocumentTransaction.rollback(...)
```

Each document uses both an in-process mutex and a stable cross-process file
lock. A commit performs, while holding both locks:

1. `lstat` and safe-path checks;
2. revision reread and exact `DocumentRevision` comparison;
3. candidate validation;
4. RestorePoint creation for the current bytes/existence state;
5. temporary-file creation in the destination directory;
6. complete write, flush, `fsync`, required permission setup and verification;
7. a second validation where required;
8. for removal, the final safe-file-type `lstat` check;
9. a second target revision reread after temporary-file fsync and immediately
   before `os.replace`, or after the final removal type check and immediately
   before unlink;
10. `os.replace` publication or unlink;
11. parent-directory `fsync` where supported;
12. post-publication revision capture;
13. retention pruning only after successful publication.

`os.replace` is atomic publication, not compare-and-swap. Both commit and
rollback recheck the target after RestorePoint creation and immediately before
publication while still holding the shared lock. This catches a non-cooperating
writer that edits or deletes between the initial check and publication. A
missing target discovered by the final removal `lstat` still performs this
revision recheck and is a conflict, not a successful Studio removal. A bounded lock
timeout returns `DOCUMENT_BUSY`; either mismatch returns `DOCUMENT_CONFLICT`
without overwriting the external bytes.

RestorePoints use opaque, exclusively created identifiers that cannot be
interpreted as paths. They store exact bytes and whether the original existed.
The API exposes no bytes, download, raw diff, hash, length, or file size.
Ordinary ManagedDocuments retain the newest five restore points. Listing is
also a validation operation: an entry is returned only after its private
metadata, existence marker, optional content, and recorded content SHA-256 have
loaded and matched. Missing, unsafe, malformed, or tampered entries remain
unlisted and cannot be selected merely because their directory name looks like
an opaque RestorePoint ID.

Rollback itself first creates a RestorePoint of the current state and prunes
only after a successful publication, so a rollback can be undone once.

App and overlay owners expose RestorePoint listing, semantic prepare, and
receipt-bound rollback through their owner-backed service and HTTP routes. Each
route requires both the document's own write capability and the separate
`rollback` capability; enabling rollback for one ordinary document does not
require or unlock the sensitive owner. Responses contain only opaque IDs and
bounded semantic summaries, never bytes, raw diff, path, hash, or size. A
rollback preview returns at most 128 changed field names. `truncated` and
stable omitted-field counters report any remainder instead of failing the
preview or silently dropping it; app previews count file and next-launch field
sets independently.

`save_overlay_config_value()` delegates persistence to this transaction kernel.
Volume drag remains live at every tick, but disk persistence happens only on
slider release, editing-finished, or a bounded debounce—never one backup/fsync
transaction per drag tick.

## 8. Sensitive repo dotenv document

Public seam:

```python
SensitiveEnvDocument.status(...)
SensitiveEnvDocument.preview(...)
SensitiveEnvDocument.commit(...)
SensitiveEnvDocument.rollback(...)
```

Secret set/clear and mapped non-secret override clear are distinct typed
commands. GET responses reveal only whether each secret slot is configured.
They never reveal old or new secret values.

Secret set and mapped-override clear commit only a server-stored opaque preview
ID. Secret clear is deliberately more destructive: the client first obtains the
semantic preview, exchanges that exact preview for a session/revision-bound
short-lived confirmation receipt, and commits with both values. Preview and
receipt references are one-shot; reuse or expiry returns
`CONFIRMATION_REQUIRED` and never replays a write.

The read-only browser projection also shows each slot's winning layer,
repo/parent document permission and parse health, stable legacy-entry names,
and aggregate mapped-override source counts. Parent dotenv and inherited
process sources remain permanently read-only. No value, raw diff, filesystem
path, hash, or size crosses the API.

Service-side opaque preview references share the owner's short TTL and are
pruned before lookup and capacity checks. Abandoned previews therefore cannot
permanently exhaust the bounded cache, and no candidate/secret bytes are copied
into that reference cache.

### 8.1 Clear-only non-secret overrides

The allowlist is derived at runtime from `APP_ENV_MAP` and `SCREEN_ENV_MAP`
(19 and 15 names respectively at the implementation fixed point; counts are not
hard-coded into Studio behaviour).
It intentionally excludes:

- `RESPEAKER_*`;
- `SPICA_RUNTIME_CACHE_DIR`;
- the four secret variables;
- legacy `DEEPSEEK_*` entries;
- inherited process environment;
- parent `xiaosan.env`.

`RESPEAKER_*` and `SPICA_RUNTIME_CACHE_DIR` still appear as bounded read-only
owner/source/health/effect DTOs with no clear action. `DEEPSEEK_*` appears only
as a retired legacy health warning and is never edited.

A clear removes every repo-file definition of the selected variable, including
duplicates and `export NAME=...`, while preserving all unrelated original bytes,
line endings, and multiline secret content. `python-dotenv` parsing may be used;
its writer is not used because it does not participate in Studio locking,
revision, backup, permission, and fsync protocols.

The preview returns only the variable name, affected app fields, before/after
next-launch values, winning source, `still_shadowed`, and semantic warnings. A
clear is allowed as a repair path when the current override makes resolution
fail. A clean-only commit is allowed when a higher inherited or parent source
still wins, but the UI must not claim behaviour changed.

Before a mapped-override preview crosses the service/API boundary, both
`before_next_launch` and `after_next_launch` are redacted against the latest
secret roster and legacy canaries. The stable semantic field names remain, but
a fallback or interpolation result can never turn a known secret into response
data.

Saving `app.yaml` and then clearing an override are two independent CAS commits.
After the app save, the client must obtain a fresh dotenv revision and preview.

### 8.2 Permissions and RestorePoints

The sensitive document shares one RestorePoint sequence across secret and
mapped override operations and retains only the newest one.

On POSIX:

- backup directory: `0700`;
- RestorePoint: `0600`;
- newly published repo `xiaosan.env`: `0600`;
- inputs must be ordinary files owned by the current user, with no symlink or
  unsafe path component;
- the existing `0664` file produces a health warning only at startup;
- the first approved write previews `permission_hardening=true` and publishes
  the replacement as `0600`;
- inability to set or verify permissions aborts before replacement;
- post-publication verification must still find the published candidate at
  `0600`; if that verification fails while the target remains this candidate,
  the transaction restores the prior state and returns
  `PERMISSION_HARDENING_FAILED` rather than a successful maintenance warning.
  This internal recovery publication does not create a user-visible
  RestorePoint and cannot retain the failed secret candidate in the rollback
  sequence. Content identity, owner, link count, and the final permission mode
  come from one `O_NOFOLLOW` descriptor and its final `fstat`; recovery mode
  hardening uses `fchmod` on that verified descriptor while the transaction lock
  is held, never a path-following `chmod` after unlock.

If post-publication verification instead finds that a third party has already
replaced the candidate, recovery must not overwrite those external bytes; the
operation returns `DOCUMENT_CONFLICT`.

On Windows, chmod is not treated as a security guarantee. Sensitive backup,
write, and rollback remain disabled until an owner-only DACL adapter has passed a
real-machine smoke test. The stable error is
`SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS`.

No deletion is described as secure erasure; SSD and copy-on-write media prevent
that guarantee.

### 8.3 Sensitive rollback

Rollback applies to the whole repo dotenv ManagedDocument, not one secret.
Preview may expose only:

- each secret slot’s `unchanged`, `will_set`, `will_clear`, or `will_replace`;
- mapped non-secret next-launch changes;
- `unmanaged_content_changed` and a count, never source text;
- permission hardening, parse errors, and winning-source warnings.

Every mapped override entry in a sensitive preview or rollback is sanitized by
the union of the transition's before/after secret material before the semantic
DTO is created; the response-time latest owner is an additional defense. This
covers historical, shadowed, repeated, and newly winning values independently
for both before/after next-launch fields. A RestorePoint containing an otherwise
valid dotenv document is never permission to disclose a secret-derived semantic
value. The related DTOs and fixed-file read results also use safe representations
that omit candidate bytes, semantic values, paths, receipts, and hashes.

An unparseable candidate is rejected for user-initiated rollback. Internal crash
recovery is a separate transaction recovery path.

A second-confirmation receipt binds the session, current revision, RestorePoint
ID, semantic summary, short expiry, and a one-time nonce. It is consumed once.

## 9. Local HTTP security model

Public seams:

```python
create_config_studio_app(services, security_context)
LoopbackServer.bind()
```

The server defaults to `127.0.0.1:8765`. CLI `--port` accepts 1024–65535;
internal tests may use port 0. Binding any non-loopback address is rejected.
Automatic default-browser opening is supported. The product requirement also
requires a usable way to disable it.

The automatic path passes the one-time token only in a fragment. Browser-launch
success is not proof that a user received it: if the grant remains unredeemed
after a short bounded delay, the launcher prints the same grant as a terminal
fallback. When browser opening is disabled or fails, it prints the fallback
immediately together with the exact local origin. The user opens that origin and
pastes the grant into a password-style field that has no name, autocomplete,
storage, logging, screenshot, or persistence path. The field is cleared before
the exchange. The grant has at least 128 bits of entropy, a short TTL, one-shot
consumption, bounded failed attempts, and is suitable for copy/paste; it is not a
six-digit code.

Uvicorn is configured with proxy headers and access logging disabled. OpenAPI,
Swagger/ReDoc, external telemetry, CDN resources, and external fonts are absent.
Server/date headers are disabled where the server permits. HTML, bootstrap, and
API responses carry `Cache-Control: no-store`, `X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, a deny-by-default `Permissions-Policy`,
`Cross-Origin-Opener-Policy: same-origin`,
`Cross-Origin-Resource-Policy: same-origin`, and a local-only CSP including
`form-action 'none'`. JSON request bodies accept only `application/json` with an
optional UTF-8 charset; another or absent media type is rejected before parsing.

Host validation uses the exact bound loopback host/port set, preventing DNS
rebinding. Every state-changing endpoint requires an exact same-origin `Origin`.
Bootstrap alone is exempt from an existing session and CSRF; every other API
requires a valid session, and every other mutating API also requires the
session-bound CSRF token. There are no arbitrary command or file-browsing inputs.
An owner refresh failure maps to stable `ENVIRONMENT_REFRESH_UNAVAILABLE` with
HTTP 503. Sensitive status first performs the fixed no-follow safety inspection,
so a hardlink, wrong-owner file, or symlink can still report its bounded health
without attempting to refresh or parse unsafe bytes.

`/api/v1/session/bootstrap` is the only API endpoint that does not require an
existing session. Fixed HTML, JS, CSS, and background routes may load without a
session.

The launcher puts a random, one-time, short-lived bootstrap token in the URL
fragment. This keeps it out of HTTP queries, Referer, and access logs, but an
automatically launched browser may expose it in that browser process’s argv.
Same-UID malicious processes are outside the v1 threat model. Hiding the token
from launcher argv would require an interactive bootstrap code and is
incompatible with automatic fragment bootstrap.

Bootstrap comparison is constant-time and failed exchanges consume a bounded
attempt budget. A successful exchange creates an
HttpOnly, `SameSite=Strict` session cookie and immediately clears the fragment.
Plain loopback HTTP cannot truthfully set `Secure` and does not pretend to.

Write capabilities are enforced in service/API policy, not only by hidden UI
controls.

`self_check` controls creation/confirmation of new jobs. The separate
`self_check_jobs` capability keeps retained jobs queryable and cancellable after
the new-start safety latch trips.

## 10. Catalogue response and redaction budgets

Redaction first uses the explicit secret roster and owner metadata. A recursive
key-name heuristic is a conservative fallback, not the primary classification.
Fixed owner metadata such as JSON Schema keywords, path segment kinds, and
Literal choices is not rewritten merely because a short secret canary happens
to equal a common schema word. Actual file/next-launch/current data slots,
dynamic map keys, and display paths remain canary-redacted.

All externally visible DTOs enforce maximum depth, collection item counts,
string length, and total encoded response size. Stable truncation metadata tells
the UI what was omitted. Owner exception text, arbitrary object `repr`, secret
text, and absolute external paths never cross the API.

Each field carries `authoring_complete`. Any value replacement, graph marker,
secret redaction, external-path marker, structured-schema projection failure,
or per-field item limit makes it false and closes `set`; an existing file value
may still use the typed `unset` repair path. The top-level `fields_complete`
flag is false when graph projection, row limits, or either pre- or post-
redaction byte budgets omit AppConfig rows. In that state the browser rejects
all set drafts, while an unset-only repair preview remains available. Missing
flags fail closed. This is distinct from ManagedDocument truncation.

Change and rollback previews apply the same rule. Values are classified as paths
only from production `AppConfig` path metadata; absolute POSIX or Windows values
become `<external-path>`, while arbitrary non-path strings are not heuristically
rewritten. A schema-owned safe relative path (including a field whose name
contains `cookie`) remains visible; an absolute path projection is explicitly
not authoring-complete, so the marker can never be written back.

An existence check is available only for schema fields explicitly marked as
paths. It uses `lstat`, never follows symlinks, and returns a bounded semantic
status rather than browsing a directory.

## 11. Plugin safety

The Studio may inspect the construction-free built-in plugin catalogue. It never
calls `PluginHost.load()` and never imports a plugin.

Plugin names, duplicates, and native booleans are strictly validated. Fixed-root
paths are checked one component at a time with `lstat`; symlink, reparse point,
or root escape is unsafe/missing. If retired `plugins.yaml` exists, plugin
authoring becomes read-only with a health error.

Each plugin catalogue row reports configured state, next-launch enabled state,
and package `present`/`missing`/`unsafe` health. It never imports or reads plugin
code to determine that status.

## 12. Self-check jobs

Public seams:

```python
SelfCheckPlanBuilder.build(...)
SelfCheckJobManager.start(...)
SelfCheckJobManager.get(...)
SelfCheckJobManager.cancel(...)
```

`scripts/self_check.py` remains unchanged and is the only check implementation.
Studio uses a fixed seven-item heavy allowlist:

`tts`, `stt`, `moondream`, `ocr`, `song_uvr`, `song_rvc`, `llm`.

New script checks do not become remotely selectable automatically; a coverage
test prompts explicit security review.

Generated argv contains only:

1. `sys.executable`;
2. the absolute fixed `scripts/self_check.py` path;
3. always `--json`;
4. confirmed combinations of `--full`, fixed `--only`, `--llm`, `--all`, and
   `--allow-model-downloads`.

It uses `shell=False` and never emits `--force`, `--timeout-scale`, or
`--worker`. Real heavy checks,
LLM checks, inclusion of disabled checks through `--all`, and model downloads
each require an independent explicit acknowledgement bound into the confirmation
receipt. Selecting one never grants another. Model downloads are off by default.
Browser clients cannot choose a timeout; the server applies fixed hard limits.

### 12.1 Output protocol

Stdout must fit the byte budget and contain one JSON document with no non-empty
prefix or suffix. The mode must match the plan; result names must be unique and
planned; status/reason/detail values must pass type, depth, string, and total-size
budgets.

Studio recomputes the exit code from statuses. The JSON `exit_code`, recomputed
code, and real process return code must all agree. Any mismatch is
`INTERNAL_ERROR`. Before storage or API exposure, the validated DTO is passed
through the same opaque all-layer owner sanitizer used by Catalog and writer
previews, covering shadowed and repeated repo/parent/inherited values as well as
the child environment's winning secrets. String values, JSON keys, and canonical
numeric/boolean data scalars are covered; fixed status/protocol metadata remains
stable. Raw process stdout/stderr are excluded from object representations.

Stderr is drained continuously. Only strictly anchored
`running <allowlisted-name> ...` lines become Studio-owned progress DTOs. Other
stderr contributes only a line count and truncation flag; raw stderr is never
stored, returned, or logged.

For a full job only, rc 3 plus no valid final JSON plus the exact current
script’s Spica-running precondition sentence maps to
`PRECONDITION_SPICA_RUNNING`. Wording drift safely maps to `INTERNAL_ERROR`. It
is a per-job compatibility classifier, not a running-state API.

### 12.2 Lifecycle and cancellation

There is at most one active job and at most 20 bounded terminal DTOs. Job IDs are
opaque. Durations use a monotonic clock. State remains queryable after a page
closes, but is not persisted and has no crash-recovery promise.

The complete externally visible job state set is `QUEUED`, `RUNNING`, `PASS`,
`UNVERIFIED`, `DEGRADED`, `FAIL`, `CANCELLING`, `CANCELLED`, and
`INTERNAL_ERROR`.

Cancellation during `QUEUED`, before a process handle exists, immediately
reports `CANCELLED` to the client while retaining the single active slot until
the in-flight launch attempt resolves. If that attempt nevertheless produces a
process, the manager contains and cancels its tree; inability to prove cleanup
changes the terminal result to `INTERNAL_ERROR`. A queued cancellation never
starts a second job concurrently.

Top-level CLI process containment must be established before a job starts. The
script’s existing Windows Job Object protects workers; the Studio’s outer
containment exists to cancel the top-level CLI tree. If containment cannot be
established, start is rejected.

After `CANCELLING`, confirmed tree cleanup wins over any process rc and yields
`CANCELLED`. If cleanup cannot be proven, the result is `INTERNAL_ERROR`.
Normal service shutdown follows the same cancellation path. A hard timeout also
uses containment cleanup before terminal classification.

If shutdown begins while `runner.start()` is still resolving a queued job, it
waits for that launch for a fixed bounded interval. A late process is contained
and cancelled before shutdown returns. If launch does not resolve within that
interval, the job becomes `INTERNAL_ERROR` with
`PROCESS_START_SHUTDOWN_TIMEOUT`; shutdown never claims cleanup was proven.

The child receives a newly composed environment made from the latest explicit
non-sensitive snapshot plus in-memory `Secrets`. It never inherits a dotenv-
primed Studio `os.environ` implicitly.

## 13. UI information architecture

The fixed client is local HTML/CSS/JavaScript under `ui/config_studio/`.
No build-time dependency, CDN, external font, telemetry, browser storage of
secrets, or directory server is required.

Primary navigation:

1. **Overview** — configuration health, next-launch effect summaries,
   self-check summary, background/legacy/permission warnings. No runtime-status
   claim.
2. **Feature switches** — all typed enable/backend switches plus dependency and
   effect-policy explanations.
3. **Configuration** — LLM, TTS, STT, Screen/OCR, Song, Anime, Galgame, Memory,
   Plugins, and Stream/UI groups; search and Basic/Advanced toggle.
4. **Character data** — allowed-root local role TTS/visual/package dictionaries,
   owner/path health, and read-only reasons; external documents are metadata-only.
5. **Secrets & overrides** — configured state, write-only commands, mapped
   clear-only overrides, semantic warnings.
6. **Self-check** — light plan, selected heavy plans, confirmation, progress,
   cancellation, terminal results.
7. **Restore** — safe RestorePoint metadata and semantic rollback previews.

Desktop wireframe:

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ Spica Config Studio              Search…        Basic ◉ Advanced ○      │
├───────────────┬──────────────────────────────────────────────────────────┤
│ Overview      │  Configuration health       Next launch effects         │
│ Switches      │  ┌────────────────────┐      ┌───────────────────────┐   │
│ Configuration│  │ source / warnings  │      │ owner effect policies │   │
│ Character     │  └────────────────────┘      └───────────────────────┘   │
│ Secrets       │                                                        │
│ Self-check    │  Selected section / schema-driven field cards          │
│ Restore       │  value · default · next launch · source · owner        │
│               │                                                        │
│               │                  Preview changes                        │
└───────────────┴──────────────────────────────────────────────────────────┘
```

The visual style uses the accepted 4K original, a dark gradient scrim,
restrained glass surfaces, and strong status hierarchy. It must feel specific to
Spica rather than a stock admin template. It supports 1280×720, 1920×1080, and
4K; keyboard-visible focus; sufficient contrast; and `prefers-reduced-motion`.

The implemented client consumes the server's exact capabilities and remains
disabled by default. On a capable Linux composition it provides schema-driven
typed app `set`/`unset`, structured list/map controls without a raw-document
textarea, independent overlay-owner controls, write-only secret set/clear,
owner-discovered mapped-override clear, and lane-specific semantic rollback.
Every operation displays its server-owned preview before commit. Secret clear
and every rollback additionally require their bound one-shot receipt; receipts
are kept only in memory and never rendered into a hidden DOM field or browser
storage. Recovery-only app state closes authoring while leaving a separately
capable rollback lane available. An independently incomplete Catalog closes
set and displays `CATALOG_FIELDS_INCOMPLETE`, while preserving unset-only
repair; it never fabricates a default document.

The real-browser visual checkpoint uses only a sandbox service and synthetic
catalogue. It covers all three resolutions, keyboard/focus, reduced motion, and
background fallback. Automated tests enforce structural, security, and
responsive contracts but do not claim to judge visual quality.

The earlier self-check screenshot with a black obstruction remains rejected and
is not counted as evidence. Its unobstructed replacement evidence was accepted.
After browser chrome, the recorded content viewport heights were 634 px for the
1280×720 capture, 994 px for 1920×1080, and 2074 px for 3840×2160. This accepts
the visual direction and removes the visual checkpoint as a Linux writer
blocker; it does not weaken capability gates or substitute for Windows,
transaction, secret, or integration verification.

## 14. Background asset

After implementation authorization, the main-worktree source is revalidated as
an ordinary non-symlink file:

- source: `0125_4_CG_GE01_0202_waifu2x_2x_3n_png.png`;
- SHA-256: `0a1116f8fb71f48156b0fd29d6beba9478bee6890b847c58b37c0d36c186eb68`;
- size: 10,281,151 bytes;
- PNG, 3840×2160, RGBA.

Exact bytes are exclusively copied to
`ui/config_studio/assets/0125_4_CG_GE01_0202_waifu2x_2x_3n_png.png`, published as
Git mode `100644`, and served only at `/assets/background.png`. There is no path
parameter, directory listing, symlink, or filesystem path disclosure.

Ingestion exclusively creates the target and never overwrites an inconsistent
existing file. After copying it rechecks target size/hash plus PNG dimensions and
RGBA, then rechecks the source hash to detect a concurrent source change. A
failure leaves the source untouched and stops ingestion.

Runtime validates size and SHA-256 with the stdlib. A committed test validates
format, dimensions, and RGBA, avoiding a Pillow runtime dependency. Missing or
invalid bytes are not served; `/meta` reports `BACKGROUND_ASSET_INVALID` and the
UI falls back to an internal CSS gradient while all configuration/security tools
remain available.

The original main-worktree file remains unchanged, unmoved, and executable-mode
`0755` as found.

## 15. Windows capability gates

Loopback service and read-only catalogue may be supported first.

Platform detection, cross-process file locking, and containment implementations
live in `spica/adapters/config_studio/platform.py` and
`spica/adapters/config_studio/self_check_process.py` (and future sibling adapters).
Core owners depend only on the immutable
`PlatformCapabilities` and `CrossProcessFileLockPort` contracts in
`spica/ports/config_studio_platform.py`; they receive those capabilities explicitly
from the sidecar or overlay composition root. Config Studio's general platform
selection, cross-process lock, and containment implementation do not live in
`spica/config`, and there is no reverse compatibility facade. Existing
secret-file owner/link safety checks in `spica/config/secrets.py` remain an
intentional whitelist boundary and are not migrated by this decision.

The POSIX process-group signals, `start_new_session` launch, and Linux `pwd`/
base-child-environment construction are concrete adapter responsibilities. The
self-check plan/job/service layer receives a runner and full base environment;
it contains no POSIX fallback implementation.

The currently verified production write lane is Linux POSIX with a valid current
UID and the concrete `flock` adapter. Other POSIX runtimes, Windows, and unknown
platforms fail closed rather than inheriting Linux capability by analogy.

Non-sensitive app/overlay writes are not production-supported on Windows until
real-machine smoke tests prove `LockFileEx`, replacement, stable locking, and
path/file safety. Injection tests alone are insufficient. Until then the API
returns `WRITES_UNVERIFIED_ON_WINDOWS`.

Sensitive writes additionally require a verified owner-only DACL smoke gate;
until then they return `SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS`.

These write gates never disable `GET /api/v1/sensitive/status` or the redacted
secret/source/permission health catalogue. Safe read-only status remains
available on Windows independently of `sensitive_write`.

These gates do not block the Linux sidecar implementation.

## 16. Stable API error codes

The API maps internal exceptions to bounded messages and the following stable
codes. It never returns raw exception text. HTTP channels are pinned as follows:
authentication is 401, policy/Origin/CSRF is 403, JSON media type is 415,
validation is 400, conflict/receipt/recovery is 409, lock timeout is 423,
missing job is 404, allocation/permission/platform gates are 503, and an
unclassified internal failure is 500. In particular, `DOCUMENT_CONFLICT` is
409, `DOCUMENT_BUSY` is 423, `DOCUMENT_UNSAFE` is 409, and
`PERMISSION_HARDENING_FAILED` is 503.

| Code | Meaning |
| --- | --- |
| `SESSION_REQUIRED` | A protected endpoint lacks a valid session |
| `BOOTSTRAP_INVALID` | Bootstrap token is absent, expired, used, or wrong |
| `JSON_CONTENT_TYPE_REQUIRED` | A JSON endpoint received an absent or unsupported media type |
| `ORIGIN_REJECTED` | Host or Origin is outside the exact local origin |
| `CSRF_INVALID` | Write request lacks the session-bound CSRF proof |
| `CAPABILITY_UNAVAILABLE` | Requested operation is not enabled by server policy |
| `DOCUMENT_BUSY` | Stable document lock could not be acquired in time |
| `DOCUMENT_CONFLICT` | Revision changed before publication |
| `DOCUMENT_UNSAFE` | File/path type, owner, symlink, reparse, or root check failed |
| `DOCUMENT_INVALID` | Candidate fails syntax, typed schema, or owner validation |
| `DOTENV_INVALID` | Candidate dotenv syntax/semantics cannot be safely parsed |
| `UNKNOWN_FIELD` | Operation introduces an unsupported field |
| `RECOVERY_ONLY` | Damaged source permits only safe recovery operations |
| `NO_VALID_RESTORE_POINT` | No validated rollback target exists |
| `CONFIRMATION_REQUIRED` | A destructive or heavy action lacks a bound receipt |
| `CONFIRMATION_INVALID` | A supplied confirmation identifier has an invalid shape |
| `CONFIRMATION_UNAVAILABLE` | A bounded confirmation/receipt slot cannot be allocated |
| `PREVIEW_UNAVAILABLE` | A bounded preview slot cannot be allocated |
| `RESTORE_POINT_INVALID` | A supplied RestorePoint identifier has an invalid shape |
| `OVERLAY_COMMAND_INVALID` | An overlay authoring command is malformed or unsupported |
| `SENSITIVE_COMMAND_INVALID` | A sensitive-document command is malformed or unsupported |
| `PERMISSION_HARDENING_FAILED` | Sensitive publication could not prove or restore private permissions |
| `SELF_CHECK_BUSY` | Another self-check job is active |
| `SELF_CHECK_PLAN_INVALID` | Requested self-check options are outside the allowlist |
| `SELF_CHECK_JOB_INVALID` | A supplied self-check job identifier has an invalid shape |
| `SELF_CHECK_JOB_NOT_FOUND` | Opaque job ID is absent from the bounded retained set |
| `SELF_CHECK_UNAVAILABLE` | Safe child environment or process containment is unavailable |
| `PROCESS_CONTAINMENT_UNAVAILABLE` | Safe top-level process-tree control is unavailable |
| `SELF_CHECK_TIMEOUT` | Server hard timeout fired and cleanup was confirmed |
| `PRECONDITION_SPICA_RUNNING` | Exact full-job compatibility precondition matched |
| `INTERNAL_ERROR` | Protocol mismatch or safely bounded internal failure |
| `WRITES_UNVERIFIED_ON_WINDOWS` | Non-sensitive Windows transaction gate has not passed |
| `SENSITIVE_WRITES_UNVERIFIED_ON_WINDOWS` | Owner-only DACL gate has not passed |

`BACKGROUND_ASSET_INVALID` is a health code rather than an API failure.
`SELF_CHECK_TIMEOUT`, `PROCESS_CONTAINMENT_UNAVAILABLE`,
`PRECONDITION_SPICA_RUNNING`, and protocol `INTERNAL_ERROR` may instead be
terminal job DTO `error_code` values after a job was accepted; they are not
misrepresented as a second HTTP response.
`PROCESS_START_SHUTDOWN_TIMEOUT` is likewise a terminal internal-error code when
a queued launch cannot resolve within the fixed shutdown wait.

## 17. Direct dependencies and installation

Direct runtime dependencies are:

- FastAPI;
- Uvicorn;
- ruamel.yaml.

They live in `requirements-config-studio.txt` and are connected to the canonical
Windows installation entry. Code should use FastAPI exports or pure ASGI
middleware; a direct `starlette.*` import requires a separately declared direct
dependency and is not currently authorized.

ruamel.yaml exists only for round-trip preservation. Production PyYAML and the
production owner resolver validate the final candidate semantics.

Implementation uses a temporary `--system-site-packages` venv. That environment
is convenient installation isolation, not a reproducibility guarantee. The
specific task-temp install was separately authorized and used `python -m pip`
without a shared-environment upgrade or fourth direct dependency. It resolved
FastAPI 0.112.2, Uvicorn 0.47.0, and ruamel.yaml 0.18.17. `pip check` was recorded
before and after: the inherited audio/ONNX/numpy/rotary conflicts were already
present and unchanged, so success means no new Config Studio conflict rather
than a false claim that the shared environment became clean. The venv remains
through test and review evidence and is then removed; this narrow authorization
does not authorize a future install.

## 18. Public implementation seams

Tests exercise these accepted public boundaries:

1. `ConfigManager.resolve_snapshot(raw_document, environment_snapshot)`
2. `ConfigAuthoringValidator.validate(base_document, candidate_document, operations)`
3. `ConfigCatalog.snapshot()`
4. `ManagedDocumentTransaction.preview/commit/rollback`
5. `SensitiveEnvDocument.status/preview/commit/rollback`
6. `SelfCheckPlanBuilder.build()`
7. `SelfCheckJobManager.start/get/cancel`
8. `create_config_studio_app(services, security_context)`
9. `LoopbackServer.bind()`
10. `create_production_config_studio_services(...)`

Tests mock only true system boundaries such as time, randomness, external
processes, and injected filesystem faults. Implementation proceeds as vertical
TDD slices: one behaviour test, then the smallest implementation that passes.

## 19. Test isolation and required coverage

Config Studio tests never receive real repo/parent `xiaosan.env`, inherited real
secret/override variables, links or mounts that point to host/repo files, real
RestorePoints, secret hashes, real baselines, or real values in browser storage,
screenshots, fixtures, or logs. Safety tests may create symlinks or hardlinks
whose source and target both remain inside the complete synthetic `tmp_path`.

Every test builds a complete synthetic repo root, parent dotenv, ManagedDocuments,
and backup root under `tmp_path`. Explicit synthetic mappings build
`EnvironmentSnapshot`; tests actively clear/mask roster and legacy names while
running. Existing global test cleanup is not treated as an isolation boundary.

Dotenv canaries cover multiline values, quotes, backslashes, Unicode, CRLF,
duplicates, `export`, short common words, and values appearing as JSON keys.
True-socket HTTP/security, CAS, rollback, permission-failure, and self-check job
tests target only sandbox services. Self-check always uses a fake runner and
never starts the real script. Teardown asserts temporary processes, files, and
RestorePoints are gone and production `spica_data/config_studio/` was not
created.

Manual visual review likewise uses a synthetic catalogue and sandbox service,
does not call production carrier discovery, and does not touch models, external
network services, or real secrets. Any real-machine secret/env smoke requires a
new explicit authorization naming the target repo and run location, whether it
is read-only, and—if writable—the exact slot/override, expected RestorePoint,
and restoration steps.

The Linux writer integration evidence includes a real loopback socket test. It
binds an ephemeral port through `LoopbackServer`, starts Uvicorn with the
production fixed-path composition against a complete synthetic repo, exchanges
the bootstrap grant for the HttpOnly session and CSRF token, reads
`/api/v1/meta`, `/api/v1/catalog`, and `/api/v1/sensitive/status`, then performs
an app preview and commit. The published synthetic app bytes change while a
secret canary remains absent from every captured HTTP body and unchanged in the
synthetic repo dotenv.

Separate real owner-backed sandbox HTTP flows cover secret set followed by a
configured-only GET, receipt-bound one-shot secret clear, mapped override clear
with a refreshed catalogue, and whole-sensitive-document rollback with a one-
shot receipt. These are synthetic integration proofs only; they do not access
the real repository `xiaosan.env` and are not a real-machine secret smoke.

Required behavioural coverage includes:

- every `AppConfig` leaf represented or carrying an explicit read-only reason;
- a new schema leaf cannot silently disappear;
- schema validation, literals, strict booleans, numeric bounds, and unknown keys;
- next-launch values/provenance match `ConfigManager`;
- env-shadow warnings and clear-only repair;
- semantic diff, atomic publication, write failure, RestorePoint rollback, CAS,
  lock timeout, and permissions;
- secret absence from every GET, log, exception, self-check DTO, and canary
  surface;
- loopback-only binding, exact Host/Origin, bootstrap/session/CSRF;
- fixed self-check argv, confirmation, cancellation, timeout, output budgets,
  status mapping, and three-way exit-code agreement;
- fixed background route and main-page smoke tests;
- Qt layering, no new `os.getenv`, and resolved-config equivalence.

All pytest commands use `python -m pytest ...`, never bare `pytest`.

## 20. Resolved-config implementation gate

Before any feature file change, implementation creates a random task directory
(`0700`) containing:

- `environment-manifest.json` (`0600`) with only explicit synthetic values;
- `resolved-before.json` (`0600`).

The manifest explicitly covers every roster/legacy variable or supplies an empty
string, fixes HOME/temp/cache/locale paths inside the task directory, and builds
the complete child `env=` mapping. It is not a dotenv file. Repo and parent
`xiaosan.env`, the old local baseline, real secrets, and implicit backup data
must be absent from the feature worktree.

The before and after runs use identical manifest bytes and first verify its
SHA-256. They invoke the unmodified production
`scripts/dump_resolved_config.py`: before with `--out`, after with `--diff`.
The final gate requires exit 0 and zero differences. The baseline is never
refreshed after implementation. Loss of the before file blocks the gate.

The existing `post_toggles_baseline.json` is neither copied nor updated. Its
known exit-1/four-difference state is historical evidence, not this feature’s
success gate. The task directory remains through tests/review evidence and is
deleted at task completion.

## 21. Delivery phases

1. **Isolation and contract** — fixed-point worktree, synthetic baseline, this
   design and ADR.
2. **Owner resolution seam** — explicit snapshot, safe DTOs, load delegation,
   zero-diff tests.
3. **Read-only catalogue** — schema coverage, role/song dynamic read-only data,
   provenance, budgets, redaction.
4. **Authoring and transaction kernel** — strict operations, previews, locks,
   atomic commit, RestorePoints; overlay owner migration.
5. **Sensitive dotenv** — status, write-only secrets, mapped clear-only
   overrides, permissions, receipts, rollback.
6. **Self-check service** — plans, confirmation, fake-runner protocol,
   containment, cancellation, bounded job history.
7. **Loopback app** — session/bootstrap/CSRF/Host/Origin/security headers and
   capability gates.
8. **Spica client** — local responsive UI, accepted background ingestion,
   background fallback, sandbox visual checkpoint.
9. **Integration gates** — targeted tests, architecture guards, full suite,
   task-local resolved diff, diff/stat review evidence.
10. **Review** — read-only Standards/Spec pre-review while uncommitted. Formal
    `$code-review` only after separately authorized feature-only checkpoint
    commits, using `22512ee9be026de133bcaaa4afc852e88d25aa6c...HEAD` and this spec.

At the current uncommitted checkpoint, the functional implementation through
the Linux owner composition, browser writer controls, and synthetic socket
evidence exists. Remaining integration gates and review evidence are reported
separately when actually run; this document intentionally records no unfinished
final test total and does not label the current pre-review as formal
`$code-review`.

## 22. Expected file-level impact

The implementation is expected to add or modify only the following feature
areas (exact split may deepen without broadening behaviour):

- `docs/LOCAL_CONFIG_STUDIO_DESIGN.md`
- `docs/adr/0001-config-studio-loopback-sidecar.md`
- `spica/config/manager.py`, `spica/config/secrets.py`
- `spica/config/schema.py`, `spica/config/env_roster.py`
- `spica/config/environment_snapshot.py`
- `spica/config/document_transaction.py`
- `spica/config/overlay_owner.py`
- `spica/ports/config_studio_platform.py`
- `spica/adapters/config_studio/platform.py`
- `spica/adapters/config_studio/self_check_process.py`
- `spica/config_studio/` (catalogue, authoring, sensitive env, self-check,
  security, API, server, schema-metadata projection, DTOs)
- `ui/config_studio/` (fixed client and accepted asset)
- `ui/overlay_config.py` and the minimal overlay persistence call sites needed
  to share the transaction primitive without per-tick writes
- `scripts/config_studio.py`
- `requirements-config-studio.txt` and the canonical Windows installation entry
- `.gitignore` for `spica_data/config_studio/`
- focused `tests/test_config_studio_*.py` plus minimal architecture/equivalence
  extensions.

No AppHost, prompt, turn, LLM/TTS model chain, plugin loading, self-check script,
dump script, role data source, main-worktree WIP, or retired legacy file is in
scope unless a later conflict is reported and separately decided.

## 23. Worktree, review, and authorization boundaries

Implementation occurs in sibling worktree
`/home/san/ai_code/Spica-config-studio` on branch `feat/config-studio`, created
without force from the exact fixed point. No existing worktree is pruned,
cleaned, copied, or reused. Subsequent changes in the main worktree do not enter
the feature worktree implicitly.

Branch/worktree creation was authorized with implementation. The one task-temp
dependency install described in section 17 was separately authorized and is now
complete. Staging, committing, pushing, any future install, real secret/env
smoke, and real heavy/LLM self-check remain separately unauthorized.

Uncommitted changes may receive an ordinary read-only Standards/Spec pre-review,
but that is not the formal `$code-review` skill. Before any local checkpoint,
the diff/stat and staged diff must prove that only feature files are included.
Commit authorization, if later granted, covers only explicitly reviewed feature
files and never implies push. Review fixes require a new commit authorization
and another review.

The shared `.git/info/exclude` ignores `/docs/`, so ordinary `git add -A` would
omit this design and ADR. If staging is later authorized, only these two fixed
documentation paths may use `git add -f`, and the staged diff must prove both are
present. The shared exclude file is not modified.
