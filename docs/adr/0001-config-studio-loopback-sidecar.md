# ADR 0001: Config Studio is an independent loopback sidecar

- Status: Accepted
- Date: 2026-07-11
- Fixed point: `22512ee9be026de133bcaaa4afc852e88d25aa6c`

## Context

Spica needs a browser configuration centre, but `AppHost` is not a passive
configuration container. Its construction resolves screen/song configuration,
registers tools, and creates the existing management surface. Starting a web UI
through it would couple configuration authoring back to the desktop runtime and
risk constructing heavy production chains.

Configuration resolution also currently primes dotenv into the process with
`override=False`. A long-running Studio cannot correctly preview a changed repo
dotenv file by repeatedly consulting the already-primed process environment.

The self-check CLI is the existing diagnostic owner and must be reused without
copying its checks. A browser-facing wrapper nevertheless needs a narrow argv,
strict structured-output validation, explicit environment composition, and
top-level cancellation containment.

## Decision

Config Studio is a separately launched local management process that:

- binds only to `127.0.0.1` or `::1`;
- is not embedded in, imported by, or auto-started with `AppHost`;
- does not construct any Spica model or plugin runtime in the sidecar (an
  independently confirmed self-check may launch the existing diagnostic CLI);
- depends on public pure seams supplied by production configuration owners;
- uses an explicit non-sensitive `EnvironmentSnapshot` for all previews and
  combines it with separately held `Secrets` only when launching an approved
  self-check;
- does not prime dotenv values into the Studio process; fixed repo/parent
  documents are safely re-read into explicit owner snapshots;
- labels resolved presentation values `next_launch_value` and makes no claim
  about an already-running Spica process;
- reports concrete owner effect policies instead of a generic hot-reload or
  restart promise;
- targets ManagedDocument publication only through shared-lock atomic
  transactions that the overlay persistence owner can also reuse; commit and
  rollback both check the initial revision under lock and recheck after creating
  the RestorePoint, before publication;
- composes Linux writers only from the fixed repository app, overlay, repo
  dotenv, backup, and lock paths; browser input can select an owner command but
  never a document path;
- derives app, overlay, sensitive, and rollback capabilities independently at
  the composition root. Unsafe state storage closes every writer without repair;
  an unsafe optional owner closes only its lane where isolation can be proved;
  every service/API route and browser control applies the same fail-closed gate;
- captures one full owner result per app operation, using its non-sensitive
  snapshot and opaque all-layer secret-material guard together, then rechecks
  both after semantic validation and immediately before publication.
  Cross-document changes therefore require a fresh semantic preview rather than
  being disguised as one atomic transaction;
- treats all rostered/legacy secret definitions observed in inherited, repo, and
  parent layers—including shadowed, repeated, interpolated, historical, and
  encoded YAML-binary forms—as forbidden app candidate data. Preview, commit,
  rollback preparation, and rollback publication reject matching raw or
  owner-coerced semantic values and dynamic-map keys; typed `unset` remains
  available as the repair operation;
- sanitizes canonical JSON scalar data as well as strings/keys across Catalog,
  writer previews, rollback previews, and self-check results, without rewriting
  fixed schema or protocol metadata for common-word secrets;
- treats heuristic secret/token/password/cookie/credential names from explicit
  inherited, repo, and parent inputs as opaque secret material under one generic
  label. Interpolated typed overrides are quarantined and unmanaged source names
  never enter redaction markers;
- rejects operations on runtime-derived leaves or their ancestors and rejects
  whole nested-model operations that could bypass leaf owner constraints;
- rechecks retired plugin/screen/song owner files across preview, commit, and
  rollback so a reappearing legacy file closes only its corresponding app lane;
- keeps two explicitly different platform owners: `agent_assembly.fold_platform`
  owns the configurable desktop effective platform, while the Config Studio
  adapter alone owns real kernel/UID security facts and never consumes
  `platform.os`; an exact AST allowlist pins both owners and the separate
  `secrets.py` file-ownership checks;
- injects sidecar platform capabilities and file-lock contracts from the
  adapter composition root, keeping its cross-process lock and containment
  implementation out of `spica/config`;
- injects an opaque stable-file-identity contract from that same adapter. The
  verified Linux lane retains the temporary descriptor through replace and
  compares no-follow descriptor/path identity before and after publication;
  same-byte replacement, in-place content mutation, a new hardlink, or an owner
  change is still `DOCUMENT_CONFLICT`. Fixed app/dotenv owner reads compare
  opened, final-descriptor, and final-path mutation facts before their result
  may satisfy a publication guard. Windows supplies no
  production identity implementation and therefore remains write-disabled;
- applies those injected platform facts to ordinary fixed-document reads and
  transactions as well as the sensitive lane: symlink/reparse, non-regular,
  multiply linked, and (on verified POSIX) wrong-UID targets fail closed;
- keeps POSIX process-group launch/signals and Linux child-environment discovery
  in `spica/adapters/config_studio`; self-check plan/job/service code receives
  those platform seams explicitly;
- wraps the unchanged `scripts/self_check.py` with a fixed allowlist, always-on
  JSON mode, independent full/LLM/`--all`/download acknowledgements, three-way
  exit-code agreement, bounded output sanitized by the opaque all-layer secret
  owner, safe process DTO representations, and top-level process-tree
  cancellation. Confirmed cleanup wins over process rc; queued cancellation
  reports cancelled while retaining the active slot until launch resolves;
- binds sensitive previews/receipts to repo, parent, inherited, and app semantic
  inputs. Commit recomputes the preview and requires new confirmation after any
  opaque all-layer owner change even when the public redacted DTO is unchanged.
  Permission-failure recovery uses a private publication path, never records the
  failed secret candidate as a user RestorePoint, and verifies/hardens the same
  no-follow descriptor using final `fstat`/`fchmod` while locked. Recovery is
  permitted only while the live file remains the exact opaque identity Studio
  published; it never overwrites a same-byte replacement from another inode.

The final revision-and-file-identity check happens after candidate temp-file
fsync, directly before replace, and the open temp identity is verified again at
the published path before the descriptor closes. For rollback-to-missing, the
final safe-file-type check happens first, then the final revision check is
immediately adjacent to unlink. An
external deletion discovered by that type check is still revision-checked and
conflicts. Ordinary app and overlay rollback use the same kernel and expose
only capability-gated,
opaque, bounded semantic HTTP operations. Oversize rollback previews return
stable truncation metadata rather than raw content or an internal error;
sensitive ownership is not a prerequisite for an ordinary-document rollback.
RestorePoint listing loads and validates each entry's private metadata,
existence state, optional content, and recorded SHA-256; malformed or tampered
entries are omitted rather than advertised as selectable rollback targets.

The accepted app structured set/unset, overlay authoring, mapped repo-override
clear, write-only secret set/clear, and lane-specific rollback protocols are now
implemented. Only exact `sys.platform == "linux"` with POSIX and a valid UID
selects the verified Linux adapter; prefix variants remain read-only. Production
composition exposes only owners that pass fixed-path and private-state checks;
unsupported or unsafe lanes remain closed. Secret clear and rollback use bound
one-shot receipts, while
app/overlay authoring and non-destructive sensitive commands commit only their
server-stored preview IDs.

Catalog authoring is additionally fail-closed at both field and snapshot scope.
Secret or path markers, graph projection, incomplete structured schema, item
limits, and response-budget row removal cannot be submitted as reconstructed
values. `set` closes for the affected projection; the explicit typed `unset`
repair path remains available. Fixed JSON Schema/Literal metadata is preserved
when a short secret happens to equal a common schema word.

Absolute-path redaction is driven only by production schema path metadata, so
arbitrary non-path model strings are not rewritten heuristically. External
character documents remain metadata-only and are never opened for catalogue
content. Mapped-override and whole-sensitive-document rollback previews redact
every semantic before/after next-launch value against the opaque union of the
transition's before/after secret material before DTO construction, with the
latest owner applied again at API exposure.

The HTTP boundary uses an exact loopback Host/Origin policy, one-time high-
entropy bootstrap grants, HttpOnly `SameSite=Strict` sessions, session-bound
CSRF, local static assets, no proxy trust, no access log, no external resources,
and capability gates at the API layer. JSON routes require
`application/json` (optionally UTF-8) before parsing. No-store responses include
`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, a deny-by-default
`Permissions-Policy`, same-origin COOP/CORP, and a local-only CSP with
`form-action 'none'`. Automatic launch transports the grant in the fragment.
Authenticated CSRF recovery is a read-only GET of the existing session token;
it does not rotate or mutate session state.
Disabled or failed launch uses the same short-lived grant through a
nonpersistent manual paste dialog; it never substitutes a low-entropy code.
A successful launcher return is not proof of redemption, so an unredeemed grant
also gets a delayed terminal fallback.

Synthetic integration evidence includes a real ephemeral loopback socket with
Uvicorn, the production fixed-path composition, bootstrap/session/CSRF exchange,
redacted meta/catalog/sensitive GETs, and an app preview/commit. Separate real
owner-backed HTTP flows cover secret set, receipt-bound clear, mapped override
clear, and whole-sensitive-document rollback without touching a real
`xiaosan.env`.

The unobstructed replacement browser evidence for the synthetic sandbox UI is
accepted. After browser chrome, its recorded content viewport heights are
634 px at 1280×720, 994 px at 1920×1080, and 2074 px at 3840×2160. This closes
the visual checkpoint only; it is not Windows transaction/DACL evidence or a
real secret/env smoke.

## Consequences

Positive:

- AppHost remains thin and its lifecycle is unchanged.
- Studio cannot accidentally load a second model/tool chain.
- Config previews use the same resolution semantics as production without
  mutating global environment state.
- Browser inputs cannot make the backend read an arbitrary client-selected path,
  browse directories, or execute commands; only fixed ManagedDocuments and
  no-follow health checks for schema-marked path values are available.
- Transaction, redaction, and self-check policies are testable without starting
  Spica.

Trade-offs:

- V1 has no authoritative runtime status and no blanket hot-reload promise.
- Changes generally describe their next-launch effect even if a particular
  owner already supports a narrower mtime reread.
- Automatic fragment bootstrap may expose the one-time token in the launched
  browser process argv. Starting with `--no-open-browser` avoids that launcher
  argv; a delayed fallback after automatic launch cannot undo an exposure that
  already occurred. Manual entry requires copy/paste from the terminal; same-UID
  hostile processes remain outside the v1 threat model.
- Windows writes remain capability-gated until real-machine locking/replacement
  and DACL smoke tests pass.
- The Linux implementation and synthetic evidence are preserved in separately
  authorized local checkpoint commits. They are not merged, pushed, or released;
  real `xiaosan.env` and Windows smoke remain unperformed, and the final commit
  range must pass formal `$code-review` before integration.
- The legacy `ManagementSurface` remains an explicitly documented unsafe writer;
  Studio is not yet the repository’s only writer.

## Rejected alternatives

### Embed or auto-start from AppHost

Rejected because it couples Studio availability to desktop lifecycle and risks
constructing production tools/models merely to edit configuration.

### Reimplement merge rules in ConfigCatalog

Rejected because it would drift from `ConfigManager` and repeat the stale
dotenv-process bug. Production owners expose pure resolution seams instead.

### Generic YAML/JSON editor

Rejected because it bypasses typed operations, owner validation, safe paths,
semantic diff, and field-coverage guarantees.

### Treat `os.replace` as compare-and-swap

Rejected because atomic publication does not prevent another writer from
changing the document between preview and publication. Revision recheck must
occur while holding the shared stable lock.

### Copy self-check logic into the server

Rejected because `scripts/self_check.py` is the diagnostic owner. Studio only
builds safe plans and validates its external protocol.
