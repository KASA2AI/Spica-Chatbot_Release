from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui" / "config_studio"


def test_config_studio_shell_is_local_accessible_and_responsive():
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "studio.css").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert all(tag in html for tag in ("<header", "<nav", "<main", "<aside"))
    assert 'aria-live="polite"' in html
    assert "https://" not in html and "http://" not in html
    assert 'href="/assets/studio.css"' in html
    assert 'src="/assets/studio.js"' in html

    assert 'url("/assets/background.png")' in css
    assert "background-size: cover" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert "@media" in css
    assert (
        ".writer-dialog__card, .sensitive-preview-summary, "
        ".rollback-preview-summary { scrollbar-color:"
    ) in css

    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "/api/v1/session/bootstrap" in javascript
    assert "history.replaceState" in javascript
    assert ".style." not in javascript
    assert 'document.createElement("button")' in javascript
    assert "field.description" in javascript
    assert "field.literal_choices" in javascript
    assert "field.dependencies" in javascript
    assert "prefers-reduced-motion: reduce" in javascript


def test_reload_refreshes_the_session_csrf_before_loading_protected_catalog():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'getJson("/api/v1/session/csrf")' in javascript
    assert javascript.index("await establishSession()") < javascript.index(
        'getJson("/api/v1/meta")'
    )
    assert "meta.csrf_token" not in javascript


def test_manual_bootstrap_uses_a_nonpersistent_high_entropy_paste_field():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="manual-bootstrap"' in html
    assert 'id="manual-bootstrap-token"' in html
    assert 'type="password"' in html
    assert 'autocomplete="off"' in html
    assert 'maxlength="256"' in html
    assert 'autocapitalize="none"' in html
    assert 'spellcheck="false"' in html
    bootstrap_input = html.split('id="manual-bootstrap-token"', 1)[1].split(
        ">", 1
    )[0]
    assert " name=" not in bootstrap_input
    assert "waitForManualBootstrap" in javascript
    assert 'input.value = ""' in javascript
    assert "if (submit.disabled) return" in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "console." not in javascript


def test_self_check_surface_is_accessible_allowlisted_and_fail_closed():
    html = (UI / "index.html").read_text(encoding="utf-8")

    for element_id in (
        "self-check-light-start",
        "self-check-heavy-start",
        "self-check-cancel",
        "self-check-confirm-heavy",
        "self-check-enable-llm",
        "self-check-confirm-llm",
        "self-check-confirm-include-disabled",
        "self-check-allow-downloads",
        "self-check-confirm-downloads",
        "self-check-monitor",
        "self-check-progress",
        "self-check-results",
    ):
        assert f'id="{element_id}"' in html

    for subsystem in (
        "tts",
        "stt",
        "moondream",
        "ocr",
        "song_uvr",
        "song_rvc",
        "llm",
    ):
        assert f'value="{subsystem}"' in html

    assert 'id="self-check-monitor"' in html and 'aria-live="polite"' in html
    assert '<button id="self-check-light-start"' in html
    assert '<button id="self-check-heavy-start"' in html
    assert html.count('class="primary-button" type="button" disabled') >= 3


def test_self_check_client_uses_server_receipts_polling_and_bounded_dtos():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'postJson("/api/v1/self-check/jobs", { mode: "light" })' in javascript
    assert 'postJson("/api/v1/self-check/confirm", {' in javascript
    assert "acknowledgements: heavyAcknowledgements()" in javascript
    assert "confirmation_receipt: confirmation.confirmation_receipt" in javascript
    assert "/api/v1/self-check/jobs/${encodeURIComponent(jobId)}" in javascript
    assert "/cancel`" in javascript
    assert "meta.capabilities.self_check" in javascript
    assert '"consents"' not in javascript

    assert "const MAX_RENDERED_RESULTS" in javascript
    assert "const MAX_RENDERED_PROGRESS" in javascript
    assert "boundedText" in javascript
    assert "stderr_total_line_count" in javascript
    assert "raw_stderr" not in javascript
    assert "stderr_text" not in javascript


def test_self_check_start_and_job_observation_capabilities_fail_closed_independently():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert "selfCheckEnabled: false" in javascript
    assert "selfCheckJobsEnabled: false" in javascript
    assert "meta.capabilities.self_check === true" in javascript
    assert "meta.capabilities.self_check_jobs === true" in javascript
    assert "if (!state.selfCheckJobsEnabled) return;" in javascript
    assert "if (!state.selfCheckJobsEnabled || !jobId) return;" in javascript
    assert "const canStart = state.selfCheckEnabled" in javascript
    assert "const canCancel = state.selfCheckJobsEnabled" in javascript


def test_self_check_layout_has_responsive_and_keyboard_visible_styles():
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "studio.css").read_text(encoding="utf-8")

    assert "<fieldset" in html and "<legend>" in html
    assert 'aria-busy="false"' in html
    for selector in (
        ".self-check-grid",
        ".check-choice-grid",
        ".confirmation-stack",
        ".check-report-grid",
        ".secondary-button",
    ):
        assert selector in css
    assert "accent-color: var(--rose)" in css
    assert "@media (max-width: 1120px)" in css
    assert "@media (max-width: 560px)" in css


def test_dynamic_studio_navigation_and_status_remain_accessible_at_all_widths():
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "studio.css").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="session-state" role="status" aria-live="polite"' in html
    assert 'root.setAttribute("aria-label", label)' in javascript
    assert ".session-state span:last-child {" in css
    assert ".session-state span:last-child { display: none; }" not in css

    assert 'button.setAttribute("aria-pressed", String(active));' in javascript
    assert 'heading.focus({ preventScroll: true });' in javascript
    assert 'id="inspector-title" tabindex="-1" aria-live="polite"' in html
    assert 'byId("inspector-title").focus({ preventScroll: true });' in javascript


def test_small_metadata_keeps_readable_contrast_and_4k_line_length():
    css = (UI / "studio.css").read_text(encoding="utf-8")

    assert "--faint: #aaa4b8;" in css
    assert "font-size: 8px" not in css
    assert "font-size: 9px" not in css
    assert ".hero-row h1 { max-width: 1100px; }" in css


def test_schema_path_health_is_visible_without_exposing_or_browsing_paths():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert "field.path_health" in javascript
    assert '"Path health"' in javascript
    assert "expected_kind" in javascript
    assert "PATH_HEALTHY" not in javascript
    assert "showDirectoryPicker" not in javascript
    assert "webkitdirectory" not in javascript


def test_app_scalar_writer_is_capability_gated_and_uses_typed_preview_commit():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for element_id in (
        "field-editor",
        "field-editor-control",
        "field-editor-set",
        "field-editor-unset",
        "app-preview-button",
        "app-preview-dialog",
        "app-preview-changes",
        "app-preview-cancel",
        "app-preview-commit",
        "operation-result",
    ):
        assert f'id="{element_id}"' in html
    assert '<dialog id="app-preview-dialog"' in html
    assert '<button id="app-preview-button"' in html
    assert '<button id="app-preview-commit"' in html
    assert "disabled" in html.split('id="app-preview-button"', 1)[1].split(">", 1)[0]
    assert "disabled" in html.split('id="app-preview-commit"', 1)[1].split(">", 1)[0]

    assert "appWriteEnabled: false" in javascript
    assert "meta.capabilities.app_config_write === true" in javascript
    assert "field.editable && appAuthoringEnabled()" in javascript
    assert "path: field.path" in javascript
    assert 'kind: "set"' in javascript
    assert 'kind: "unset"' in javascript
    assert 'postJson("/api/v1/app/previews"' in javascript
    assert 'postJson("/api/v1/app/commits"' in javascript
    assert "if (!appAuthoringEnabled()" in javascript
    assert "state.draftOperations.clear()" in javascript


def test_app_recovery_only_closes_authoring_but_keeps_capable_rollback_visible():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="app-recovery-notice"' in html
    assert 'role="status"' in html.split('id="app-recovery-notice"', 1)[1].split(
        ">", 1
    )[0]
    assert "appRecoveryOnly: true" in javascript
    assert "state.appRecoveryOnly = catalog.recovery_only !== false" in javascript
    assert "metaHealth.recovery_only !== false" in javascript
    assert "function appAuthoringEnabled()" in javascript
    assert "state.appWriteEnabled && state.appRecoveryOnly === false" in javascript
    assert "if (!appAuthoringEnabled()" in javascript

    rollback_guard = javascript[
        javascript.index("function restoreLaneEnabled"):
        javascript.index("function renderRestorePoints")
    ]
    assert "state.appWriteEnabled && state.rollbackEnabled" in rollback_guard
    assert "appAuthoringEnabled" not in rollback_guard


def test_recovery_without_a_restore_point_shows_stable_manual_repair_guidance():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="app-manual-repair-guidance"' in html
    assert "APP_YAML_MANUAL_REPAIR_REQUIRED" in html
    assert "data/config/app.yaml" in html
    assert "不提供默认重置" in html
    assert "function syncAppManualRepairGuidance" in javascript
    assert "hasValidRestorePoint" in javascript
    assert "syncAppManualRepairGuidance" in javascript[
        javascript.index("function renderRestorePoints"):
        javascript.index("async function loadRestorePoints")
    ]


def test_incomplete_catalog_closes_set_but_preserves_unset_only_repair():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="catalog-fields-incomplete-notice"' in html
    assert "CATALOG_FIELDS_INCOMPLETE" in html
    assert "set fail-closed" in html
    assert "unset 修复路径仍可用" in html
    assert "不恢复默认" in html
    assert "catalogFieldsComplete: false" in javascript
    assert "state.catalogFieldsComplete = catalog.fields_complete === true" in javascript
    assert "function renderCatalogCompletenessState" in javascript

    app_gate = javascript[
        javascript.index("function appAuthoringEnabled"):
        javascript.index("function fieldAuthoringComplete")
    ]
    set_handler = javascript[
        javascript.index("function queueAppSet"):
        javascript.index("function queueAppUnset")
    ]
    unset_handler = javascript[
        javascript.index("function queueAppUnset"):
        javascript.index("function syncAppControls")
    ]
    draft_gate = javascript[
        javascript.index("function appDraftOperationsAllowed"):
        javascript.index("function invalidateAppPreview")
    ]
    preview_handler = javascript[
        javascript.index("async function previewAppChanges"):
        javascript.index("async function commitAppPreview")
    ]
    commit_handler = javascript[
        javascript.index("async function commitAppPreview"):
        javascript.index("function visibleFields")
    ]

    assert "catalogFieldsComplete" not in app_gate
    assert "state.catalogFieldsComplete !== true" in set_handler
    assert "catalogFieldsComplete" not in unset_handler
    assert 'operation.kind === "unset"' in draft_gate
    assert "!appDraftOperationsAllowed()" in preview_handler
    assert "!appDraftOperationsAllowed()" in commit_handler


def test_structured_app_writer_uses_schema_rows_without_raw_document_editor():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert "field.structured_schema" in javascript
    assert "function renderStructuredEditor" in javascript
    assert "function defaultForStructuredSchema" in javascript
    assert "const structuredReaders = new WeakMap()" in javascript
    assert 'schema.type === "array"' in javascript
    assert 'schema.type === "object"' in javascript
    assert 'schema.additionalProperties' in javascript
    assert 'className = "structured-add"' in javascript
    assert 'className = "structured-remove"' in javascript
    assert "structuredReaders.get(control)" in javascript
    assert "<textarea" not in html
    assert 'document.createElement("textarea")' not in javascript
    assert "showDirectoryPicker" not in javascript


def test_structured_writer_is_complete_or_fail_closed_at_the_256_item_limit():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    renderer = javascript[
        javascript.index("function renderStructuredEditor"):
        javascript.index("function scalarEditor")
    ]
    set_handler = javascript[
        javascript.index("function queueAppSet"):
        javascript.index("function queueAppUnset")
    ]
    unset_handler = javascript[
        javascript.index("function queueAppUnset"):
        javascript.index("function syncAppControls")
    ]

    assert "const MAX_STRUCTURED_ITEMS = 256" in javascript
    assert ".slice(0, 64)" not in renderer
    assert "rows.contains(entry.row)" in renderer
    assert "authoring_complete === true" in javascript
    assert "FIELD_AUTHORING_INCOMPLETE" in javascript
    assert "field.authoring_complete !== true" in set_handler
    assert "authoring_complete" not in unset_handler
    assert "add.disabled" in renderer
    assert "最多 256 项" in renderer


def test_nullable_scalar_has_an_explicit_null_toggle_distinct_from_empty_text():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    scalar_editor = javascript[
        javascript.index("function scalarEditor"):
        javascript.index("function renderAppFieldEditor")
    ]

    assert "field.nullable !== true" in scalar_editor
    assert 'className = "nullable-scalar"' in scalar_editor
    assert "显式写入 null" in scalar_editor
    assert "空字符串不是 null" in scalar_editor
    assert "return null" in scalar_editor
    assert "structuredReaders.set" in scalar_editor


def test_overlay_writer_uses_owner_metadata_and_its_independent_capability():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for element_id in (
        "overlay-settings",
        "overlay-fields",
        "overlay-operation-result",
        "overlay-preview-dialog",
        "overlay-preview-summary",
        "overlay-preview-cancel",
        "overlay-preview-commit",
    ):
        assert f'id="{element_id}"' in html
    assert "overlayWriteEnabled: false" in javascript
    assert "meta.capabilities.overlay_write === true" in javascript
    assert 'documentInfo.id === "overlay_preferences"' in javascript
    assert "field.minimum" in javascript and "field.maximum" in javascript
    assert 'postJson("/api/v1/overlay/previews"' in javascript
    assert 'postJson("/api/v1/overlay/commits"' in javascript
    assert "if (!state.overlayWriteEnabled" in javascript
    overlay_renderer = javascript[
        javascript.index("function renderOverlayDocument"):
        javascript.index("function renderSensitiveStatus")
    ]
    assert "appWriteEnabled" not in overlay_renderer


def test_plugin_status_catalog_is_rendered_as_bounded_read_only_owner_health():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="plugin-status-catalog"' in html
    assert 'id="plugin-statuses"' in html
    assert "pluginStatuses: []" in javascript
    assert "catalog.plugin_statuses" in javascript
    assert "function renderPluginStatuses()" in javascript
    renderer = javascript[
        javascript.index("function renderPluginStatuses"):
        javascript.index("function renderSensitiveStatus")
    ]
    for wire_field in (
        "configured",
        "next_launch_enabled",
        "package_status",
        "package_health_code",
        "owner",
        "effect_policy",
    ):
        assert f"plugin.{wire_field}" in renderer
    assert 'document.createElement("button")' not in renderer
    assert "addEventListener" not in renderer


def test_environment_only_catalog_is_rendered_from_safe_read_only_dtos():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="environment-only-catalog"' in html
    assert 'id="environment-only-settings"' in html
    assert "environmentOnlySettings: []" in javascript
    assert "catalog.environment_only_settings" in javascript
    assert "function renderEnvironmentOnlySettings()" in javascript
    renderer = javascript[
        javascript.index("function renderEnvironmentOnlySettings"):
        javascript.index("function renderSensitiveStatus")
    ]
    for wire_field in (
        "id",
        "environment_variable",
        "configured",
        "configured_value",
        "source_kind",
        "environment_layer",
        "owner",
        "effect_policy",
        "editable",
        "unsupported_reason",
    ):
        assert f"setting.{wire_field}" in renderer
    assert 'document.createElement("button")' not in renderer
    assert "addEventListener" not in renderer
    assert '"RESPEAKER_REQUIRE_HARDWARE_VAD"' not in javascript
    assert '"SPICA_RUNTIME_CACHE_DIR"' not in javascript


def test_secret_writer_is_write_only_capability_gated_and_receipt_bound():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for element_id in (
        "secret-actions",
        "secret-slot-select",
        "secret-value-input",
        "secret-set-preview",
        "secret-clear-preview",
        "sensitive-preview-dialog",
        "sensitive-preview-summary",
        "sensitive-clear-confirm",
        "sensitive-preview-cancel",
        "sensitive-preview-commit",
    ):
        assert f'id="{element_id}"' in html
    secret_input = html.split('id="secret-value-input"', 1)[1].split(">", 1)[0]
    assert 'type="password"' in secret_input
    assert 'autocomplete="off"' in secret_input
    assert 'spellcheck="false"' in secret_input
    assert " name=" not in secret_input

    assert "sensitiveWriteEnabled: false" in javascript
    assert "meta.capabilities.sensitive_write === true" in javascript
    assert 'kind: "set_secret"' in javascript
    assert 'kind: "clear_secret"' in javascript
    assert 'postJson("/api/v1/sensitive/previews"' in javascript
    assert "/confirm-clear`" in javascript
    assert 'postJson("/api/v1/sensitive/commits"' in javascript
    assert "if (!state.sensitiveWriteEnabled" in javascript
    assert 'secretInput.value = ""' in javascript
    assert "confirmation_receipt" in javascript
    assert 'id="sensitive-confirmation-receipt"' not in html
    assert "localStorage" not in javascript and "sessionStorage" not in javascript


def test_mapped_override_clear_is_owner_discovered_and_never_hardcoded():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert "function renderMappedOverrides" in javascript
    assert "sensitive.managed_overrides" in javascript
    assert "item.repo_defined === true" in javascript
    assert 'kind: "clear_mapped_override"' in javascript
    assert "environment_variable: item.environment_variable" in javascript
    assert "preview.still_shadowed" in javascript
    assert "preview.resolution_error_before" in javascript
    assert "preview.permission_hardening" in javascript
    assert '"SPICA_SCREEN_ENABLED"' not in javascript
    assert '"OPENAI_BASE_URL"' not in javascript
    assert "RESPEAKER_" not in javascript
    assert "SPICA_RUNTIME_CACHE_DIR" not in javascript


def test_sensitive_capability_is_applied_before_mapped_override_controls_render():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    apply_meta = javascript[
        javascript.index("function applyMeta(meta)"):
        javascript.index("function validSelfCheckJob")
    ]

    assert apply_meta.index("state.sensitiveWriteEnabled = Boolean(") < (
        apply_meta.index("renderSensitiveStatus(meta)")
    )


def test_writer_commit_handlers_reenable_dynamic_controls_after_reload():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    overlay_commit = javascript[
        javascript.index("async function commitOverlayPreview"):
        javascript.index("function renderPluginStatuses")
    ]
    sensitive_commit = javascript[
        javascript.index("async function commitSensitivePreview"):
        javascript.index("function restoreLaneEnabled")
    ]
    rollback_commit = javascript[
        javascript.index("async function commitRollback"):
        javascript.index("function applyMeta")
    ]

    assert overlay_commit.index("state.overlayWriteBusy = false") < (
        overlay_commit.index("renderOverlayDocument()")
    )
    assert sensitive_commit.index("state.sensitiveWriteBusy = false") < (
        sensitive_commit.index("renderSensitiveStatus(state.meta || {})")
    )
    assert rollback_commit.index("state.rollbackBusy = false") < (
        rollback_commit.index("await loadRestorePoints()")
    )


def test_restore_ui_is_lane_specific_semantic_and_receipt_bound():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for element_id in (
        "restore-app",
        "restore-overlay",
        "restore-sensitive",
        "restore-operation-result",
        "rollback-preview-dialog",
        "rollback-preview-summary",
        "rollback-confirm",
        "rollback-preview-cancel",
        "rollback-preview-commit",
    ):
        assert f'id="{element_id}"' in html
    assert "rollbackEnabled: false" in javascript
    assert "meta.capabilities.rollback === true" in javascript
    assert "state.appWriteEnabled && state.rollbackEnabled" in javascript
    assert "state.overlayWriteEnabled && state.rollbackEnabled" in javascript
    assert "state.sensitiveWriteEnabled && state.rollbackEnabled" in javascript
    for lane in ("app", "overlay", "sensitive"):
        assert f'`/api/v1/{lane}/restore-points`' in javascript
        assert f'`/api/v1/{lane}/rollbacks`' in javascript
    assert "/prepare-rollback`" in javascript
    assert "confirmation_receipt: confirmation.confirmation_receipt" in javascript
    assert "整个敏感 ManagedDocument" in javascript
    assert "restore_point_id" in javascript
    assert "sha256" not in javascript
    assert "file_size" not in javascript
    assert 'id="rollback-confirmation-receipt"' not in html


def test_rollback_preview_shows_resolution_sources_and_exact_omitted_counts():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    renderer = javascript[
        javascript.index("function renderRollbackPreview"):
        javascript.index("async function prepareRollback")
    ]

    for semantic_field in (
        "confirmation.resolution_error_before",
        "confirmation.resolution_error_after",
        "item.winning_source_before",
        "item.winning_source_after",
        "changed_fields_omitted",
        "next_launch_changed_fields_omitted",
    ):
        assert semantic_field in renderer
    assert "omitted 计数见服务端语义 DTO" not in renderer


def test_confirmation_receipts_never_enter_visible_renderers_or_storage():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    visible_renderers = (
        javascript[
            javascript.index("function renderSensitivePreview"):
            javascript.index("async function previewSensitiveCommand")
        ],
        javascript[
            javascript.index("function renderRollbackPreview"):
            javascript.index("async function prepareRollback")
        ],
        javascript[
            javascript.index("function renderSelfCheckJob"):
            javascript.index("function syncSelfCheckControls")
        ],
    )

    assert all("confirmation_receipt" not in renderer for renderer in visible_renderers)
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert 'byId("sensitive-preview-dialog").addEventListener("close"' in javascript
    assert 'byId("rollback-preview-dialog").addEventListener("close"' in javascript
    assert "state.sensitivePreview = null" in javascript
    assert "state.rollbackConfirmation = null" in javascript


def test_secrets_page_renders_only_safe_read_status_while_writes_are_gated():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for element_id in (
        "secret-slot-status",
        "repo-env-health",
        "parent-env-health",
        "override-source-summary",
    ):
        assert f'id="{element_id}"' in html
    assert "meta.sensitive_document" in javascript
    assert "meta.parent_environment_document" in javascript
    assert "secret_sources" in javascript
    status_renderer = javascript[
        javascript.index("function renderSensitiveStatus"):
        javascript.index("function applyMeta")
    ]
    assert "file_value" not in status_renderer
