from __future__ import annotations

from pathlib import Path
import re

from pydantic import BaseModel

from spica.config.overlay_owner import OVERLAY_FIELD_SPECS
from spica.config.schema import AppConfig


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui" / "config_studio"
LOCALES = ("zh-CN", "en", "ja")
LOCALIZED_PROTOCOL_REGISTRIES = (
    "CATEGORY_LABELS",
    "CONTROL_LABELS",
    "SOURCE_LABELS",
    "EFFECT_LABELS",
    "OWNER_LABELS",
    "STATUS_LABELS",
    "LEVEL_LABELS",
    "PATH_KIND_LABELS",
    "DOCUMENT_PRESENTATIONS",
    "READONLY_REASON_LABELS",
    "ENVIRONMENT_SETTING_LABELS",
    "SECRET_SLOT_LABELS",
    "SENSITIVE_COMMAND_LABELS",
    "SECRET_CHANGE_LABELS",
    "LANE_LABELS",
    "CHECK_LABELS",
    "HEALTH_ISSUE_LABELS",
)


def _typed_app_config_leaf_paths() -> set[str]:
    paths: set[str] = set()

    def visit(model_type: type[BaseModel], prefix: tuple[str, ...]) -> None:
        for name, field in model_type.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                visit(annotation, prefix + (name,))
                continue
            paths.add(".".join(prefix + (name,)))

    visit(AppConfig, ())
    return paths


def _chinese_field_presentation_paths(javascript: str) -> set[str]:
    start = javascript.index("const FIELD_PRESENTATIONS_ZH = Object.freeze({")
    end = javascript.index("\n  });", start)
    registry = javascript[start:end]
    return set(re.findall(r'^    "([a-z0-9_.]+)": Object\.freeze\(', registry, re.M))


def _chinese_overlay_presentation_keys(javascript: str) -> set[str]:
    start = javascript.index("const OVERLAY_PRESENTATIONS_ZH = Object.freeze({")
    end = javascript.index("\n  });", start)
    registry = javascript[start:end]
    return set(re.findall(r'^    "([a-z0-9_]+)": Object\.freeze\(', registry, re.M))


def _localized_presentation_entries(
    javascript: str,
    start_marker: str,
    end_marker: str,
) -> dict[str, str]:
    registry = javascript[
        javascript.index(start_marker):javascript.index(end_marker)
    ]
    matches = list(
        re.finditer(r'^    "([a-z0-9_.]+)": Object\.freeze\(\{', registry, re.M)
    )
    return {
        match.group(1): registry[
            match.start():(matches[index + 1].start() if index + 1 < len(matches) else len(registry))
        ]
        for index, match in enumerate(matches)
    }


def _localized_label_entries(javascript: str, registry_name: str) -> dict[str, str]:
    start = javascript.index(f"const {registry_name} = Object.freeze({{")
    end = javascript.index("\n  });", start)
    registry = javascript[start:end]
    matches = list(
        re.finditer(r'^    "([^"]+)": Object\.freeze\(\{', registry, re.M)
    )
    return {
        match.group(1): registry[
            match.start():(matches[index + 1].start() if index + 1 < len(matches) else len(registry))
        ]
        for index, match in enumerate(matches)
    }


def _presentation_copy(
    javascript: str,
    start_marker: str,
    end_marker: str,
) -> dict[str, tuple[str, str, str]]:
    registry = javascript[
        javascript.index(start_marker):javascript.index(end_marker)
    ]
    entries = re.findall(
        r'^    "([^"]+)": Object\.freeze\(\{\n'
        r'      title: "([^"]+)",\n'
        r'      description: "([^"]+)",\n'
        r'      advice: "([^"]+)",\n'
        r'    \}\),',
        registry,
        re.MULTILINE,
    )
    return {path: (title, description, advice) for path, title, description, advice in entries}


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


def test_language_switch_uses_an_allowlisted_url_locale_without_browser_storage():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert 'id="language-switch"' in html
    for locale, label in (
        ("zh-CN", "中文"),
        ("en", "English"),
        ("ja", "日本語"),
    ):
        assert f'data-locale="{locale}"' in html
        assert f'>{label}</button>' in html
    assert 'aria-label="界面语言"' in html
    assert 'const SUPPORTED_LOCALES = Object.freeze(["zh-CN", "en", "ja"])' in javascript
    assert "function localeFromLocation()" in javascript
    assert "function requestLocaleChange(locale)" in javascript
    assert 'searchParams.get("lang")' in javascript
    assert 'searchParams.set("lang", locale)' in javascript
    assert "window.location.assign" in javascript
    assert "document.documentElement.lang = state.locale" in javascript
    assert 'button.setAttribute("aria-pressed", String(active))' in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "navigator.language" not in javascript
    assert "document.cookie" not in javascript
    assert "indexedDB" not in javascript


def test_language_switch_warns_before_discarding_browser_only_work():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    change = javascript[
        javascript.index("function hasUnsavedLocaleState()"):
        javascript.index("function text(value)")
    ]

    for state_check in (
        "state.draftOperations.size",
        "state.appEditorDirty",
        "state.appPreview",
        "state.overlayEditorDirty",
        "state.overlayPreview",
        "state.sensitivePreview",
        "state.rollbackConfirmation",
        'byId("secret-value-input")',
    ):
        assert state_check in change
    assert 'window.confirm(localizedCopy("locale.unsaved_confirmation"))' in change
    assert "destination.hash =" not in change
    assert "postJson(" not in change


def test_every_fixed_field_presentation_has_exactly_three_complete_locales():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    app_entries = _localized_presentation_entries(
        javascript,
        "const FIELD_PRESENTATIONS = Object.freeze({",
        "const KNOWN_DYNAMIC_FIELD_PRESENTATIONS",
    )
    overlay_entries = _localized_presentation_entries(
        javascript,
        "const OVERLAY_PRESENTATIONS = Object.freeze({",
        "const DOCUMENT_PRESENTATIONS",
    )

    assert set(app_entries) == _typed_app_config_leaf_paths()
    assert set(overlay_entries) == set(OVERLAY_FIELD_SPECS)
    for entry in (*app_entries.values(), *overlay_entries.values()):
        assert entry.count('"zh-CN": Object.freeze({') == 1
        assert entry.count('en: Object.freeze({') == 1
        assert entry.count('ja: Object.freeze({') == 1
        assert entry.count('title: "') == 3
        assert entry.count('description: "') == 3
        assert entry.count('advice: "') == 3


def test_every_protocol_label_has_exactly_the_same_three_locales():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for registry_name in LOCALIZED_PROTOCOL_REGISTRIES:
        entries = _localized_label_entries(javascript, registry_name)
        assert entries, registry_name
        for entry in entries.values():
            for locale in LOCALES:
                assert entry.count(f'"{locale}": "') == 1

    assert "_LABELS_ZH" not in javascript


def test_static_html_translation_keys_match_the_three_locale_catalog():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    html_keys = set(
        re.findall(r'data-i18n(?:-placeholder|-aria-label)?="([^"]+)"', html)
    )
    static_copy = _localized_label_entries(javascript, "STATIC_COPY")

    assert html_keys
    assert html_keys == set(static_copy)
    for entry in static_copy.values():
        for locale in LOCALES:
            assert entry.count(f'"{locale}": "') == 1

    assert 'data-i18n="document.title"' in html
    assert 'data-i18n-placeholder="search.placeholder"' in html
    assert 'data-i18n-aria-label="language_switch.label"' in html


def test_sensitive_managed_document_titles_are_localized_without_translating_filenames():
    html = (UI / "index.html").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    static_copy = _localized_label_entries(javascript, "STATIC_COPY")

    assert html.count('data-i18n="managed_document.repo_dotenv"') == 2
    assert html.count('data-i18n="managed_document.parent_dotenv"') == 1
    for key in ("managed_document.repo_dotenv", "managed_document.parent_dotenv"):
        assert key in static_copy
        entry = static_copy[key]
        assert all(entry.count(f'"{locale}":') == 1 for locale in LOCALES)
        assert entry.count("xiaosan.env") == len(LOCALES)
    assert "function applyStaticTranslations()" in javascript
    assert "document.title = localizedCopy(\"document.title\")" in javascript


def test_runtime_messages_use_the_same_three_locale_projection():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    messages = _localized_label_entries(javascript, "DYNAMIC_COPY")

    assert len(messages) == 150
    for entry in messages.values():
        for locale in LOCALES:
            assert entry.count(f'"{locale}": "') == 1
    assert "function localizedMessage(key, replacements = [])" in javascript
    formatter = javascript[
        javascript.index("function localizedMessage(key, replacements = [])"):
        javascript.index("function applyStaticTranslations()")
    ]
    assert "rendered.split(/@@(\\d+)@@/)" in formatter
    assert "replacements[Number(part)]" in formatter
    assert "replaceAll" not in formatter


def test_all_readmes_document_config_studio_launch_and_language_switch():
    expectations = {
        "README.md": (
            "本地配置中心",
            "一次性启动授权",
            "语言切换只改变界面说明，不改变配置键和值。",
        ),
        "README.en.md": (
            "Local Config Studio",
            "one-time bootstrap grant",
            "The language switch changes presentation text only; it does not change configuration keys or values.",
        ),
        "README.ja.md": (
            "ローカル設定スタジオ",
            "一度限りの起動認証",
            "言語切り替えは画面の説明だけを変更し、設定キーや値は変更しません。",
        ),
    }

    for filename, localized_copy in expectations.items():
        readme = (ROOT / filename).read_text(encoding="utf-8")
        for exact in (
            "python -m pip install -r requirements-config-studio.txt",
            "python scripts/config_studio.py",
            "--port 8765",
            "--no-open-browser",
            "127.0.0.1:8765",
            "Ctrl+C",
        ):
            assert exact in readme
        assert all(copy in readme for copy in localized_copy)
        assert all(label in readme for label in ("中文", "English", "日本語"))
        assert "--lang" not in readme


def test_every_typed_app_config_leaf_has_explicit_localized_presentation_copy():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    entries = _localized_presentation_entries(
        javascript,
        "const FIELD_PRESENTATIONS = Object.freeze({",
        "const KNOWN_DYNAMIC_FIELD_PRESENTATIONS",
    )

    assert set(entries) == _typed_app_config_leaf_paths()


def test_all_fixed_field_help_copy_is_complete_and_written_for_people():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    app_copy = _localized_presentation_entries(
        javascript,
        "const FIELD_PRESENTATIONS = Object.freeze({",
        "const KNOWN_DYNAMIC_FIELD_PRESENTATIONS",
    )
    overlay_copy = _localized_presentation_entries(
        javascript,
        "const OVERLAY_PRESENTATIONS = Object.freeze({",
        "const DOCUMENT_PRESENTATIONS",
    )

    assert set(app_copy) == _typed_app_config_leaf_paths()
    assert set(overlay_copy) == set(OVERLAY_FIELD_SPECS)
    for path, entry in {**app_copy, **overlay_copy}.items():
        for locale in LOCALES:
            locale_marker = f'"{locale}"' if locale == "zh-CN" else locale
            localized = re.search(
                rf'{re.escape(locale_marker)}: Object\.freeze\(\{{\n'
                r'        title: "((?:\\.|[^"\\])*)",\n'
                r'        description: "((?:\\.|[^"\\])*)",\n'
                r'        advice: "((?:\\.|[^"\\])*)",',
                entry,
            )
            assert localized, (path, locale)
            assert all(section.strip() for section in localized.groups())
        assert re.search(r"[\u4e00-\u9fff]", entry)
        assert re.search(r"[A-Za-z]{2,}", entry)
        assert re.search(r"[\u3040-\u30ff]", entry)


def test_help_includes_effect_and_dependencies_without_translating_dynamic_data():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    helper = javascript[
        javascript.index("function attachFieldHelp(target, field)"):
        javascript.index("function presentationForOverlayField(field)")
    ]
    presentation = javascript[
        javascript.index("function presentationForField(field)"):
        javascript.index("function attachFieldHelp(target, field)")
    ]
    text_helper = javascript[
        javascript.index("function text(value)"):
        javascript.index("function boundedText(value")
    ]

    assert "field.effect_policy" in helper
    assert "field.dependencies" in helper
    assert 'localizedMessage("runtime.005"' in helper
    assert 'localizedMessage("runtime.008"' in helper
    assert "item.display_path" in helper
    assert "text(item.expected_value)" in helper
    assert "Object.hasOwn" in presentation
    assert "registryValue(" in presentation
    assert 'return value ? "true" : "false"' in text_helper
    assert 'KNOWN_DYNAMIC_FIELD_PRESENTATIONS[path]' not in presentation


def test_config_fields_show_chinese_names_raw_keys_and_accessible_help():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    css = (UI / "studio.css").read_text(encoding="utf-8")

    assert 'title: "屏幕图像最大边长"' in javascript
    assert "数值越大保留的细节越多" in javascript
    assert "function presentationForField(field)" in javascript
    assert "function attachFieldHelp(target, field)" in javascript
    assert 'rawKey.textContent = fieldPath(field)' in javascript
    assert 'tooltip.setAttribute("role", "tooltip")' in javascript
    assert 'target.setAttribute("aria-describedby", tooltip.id)' in javascript
    assert '.field-row:hover .field-help-tooltip' in css
    assert '.field-row:focus-visible .field-help-tooltip' in css
    assert '.setting-card:hover .field-help-tooltip' in css
    assert '.setting-card:focus-visible .field-help-tooltip' in css
    assert 'button.querySelector(".field-value").textContent = text(field.next_launch_value)' in javascript


def test_field_search_matches_chinese_help_and_original_configuration_terms():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    search = javascript[
        javascript.index("function searchTextForField(field)"):
        javascript.index("function visibleFields()")
    ]
    visible_fields = javascript[
        javascript.index("function visibleFields()"):
        javascript.index("function renderFeatured()")
    ]

    for searchable in (
        "presentation.title",
        "presentation.description",
        "presentation.advice",
        "fieldPath(field)",
        "field.owner",
        "field.source_kind",
        "field.description",
        "field.literal_choices",
    ):
        assert searchable in search
    assert '.normalize("NFKC")' in search
    assert "searchTextForField(field).includes(query)" in visible_fields
    search_binding = javascript[
        javascript.index('byId("field-search").addEventListener("input"'):
        javascript.index('byId("field-editor-set").addEventListener')
    ]
    assert "state.query = event.target.value" in search_binding
    assert "renderCatalog()" in search_binding


def test_dynamic_keys_and_configuration_values_stay_raw_at_presentation_boundary():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    field_row = javascript[
        javascript.index("function fieldRow(field)"):
        javascript.index("function inspectField(field, row)")
    ]
    scalar_editor = javascript[
        javascript.index("function readScalarControl(field, control)"):
        javascript.index("function renderAppFieldEditor(field)")
    ]

    assert "Object.hasOwn(registry, key)" in javascript
    assert "return null" in javascript[
        javascript.index("function presentationForField(field)"):
        javascript.index("function attachFieldHelp(target, field)")
    ]
    assert "fieldPath(field)" in field_row
    assert 'button.addEventListener("click", () => inspectField(field, button))' in field_row
    assert "option.textContent = text(choice)" in scalar_editor
    assert "return field.literal_choices[index]" in scalar_editor
    assert 'return value ? "true" : "false"' in javascript
    assert "text(field.next_launch_value)" in field_row

    css = (UI / "studio.css").read_text(encoding="utf-8")
    assert ".overlay-field:focus-within .field-help-tooltip" in css
    assert "attachOverlayFieldHelp(row, input, field, presentation)" in javascript


def test_static_config_studio_copy_is_plain_chinese_with_technical_ids_preserved():
    html = (UI / "index.html").read_text(encoding="utf-8")

    for phrase in (
        "Spica 本地配置中心",
        "基础设置",
        "高级设置",
        "安全与维护",
        "密钥与环境覆盖",
        "下次启动值",
        "配置来源顺序",
        "只读",
        "需要明确确认",
        "安全诊断",
        "恢复备份",
        "字段详细说明",
    ):
        assert phrase in html
    for old_primary_copy in (
        ">Basic<",
        ">Advanced<",
        ">Safety<",
        ">Secrets & Overrides<",
        ">Next launch<",
        ">READ ONLY<",
        ">CONFIRM<",
        ">FIELD INSPECTOR<",
    ):
        assert old_primary_copy not in html
    for technical_id in (
        "app.yaml",
        "overlay_config.json",
        "xiaosan.env",
        "--full",
        "--llm",
        "--all",
        "APP_YAML_MANUAL_REPAIR_REQUIRED",
    ):
        assert technical_id in html


def test_wire_metadata_is_localized_only_at_the_browser_presentation_boundary():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for registry in (
        "CATEGORY_LABELS",
        "CONTROL_LABELS",
        "SOURCE_LABELS",
        "EFFECT_LABELS",
        "OWNER_LABELS",
        "STATUS_LABELS",
    ):
        assert f"const {registry} = Object.freeze" in javascript
    assert "function localizedProtocolLabel(registry, value)" in javascript
    assert "function localizedProtocolLabelWithCode(registry, value)" in javascript
    assert "registryValue(CATEGORY_LABELS, category) || category" in javascript
    assert "localizedProtocolLabel(CONTROL_LABELS" in javascript
    assert "localizedProtocolLabel(SOURCE_LABELS" in javascript
    assert "localizedProtocolLabel(EFFECT_LABELS" in javascript
    assert "localizedProtocolLabel(OWNER_LABELS" in javascript
    assert "localizedProtocolLabel(STATUS_LABELS" in javascript

    self_check = javascript[
        javascript.index("function renderSelfCheckJob(job)"):
        javascript.index("async function pollSelfCheck()")
    ]
    assert "badge.dataset.status = status" in self_check
    assert "localizedProtocolLabel(STATUS_LABELS, status)" in self_check
    assert 'item.status' in self_check

    # Display localization must not rewrite configuration values or API payloads.
    assert 'button.querySelector(".field-value").textContent = text(field.next_launch_value)' in javascript
    assert "value: selectedScalarValue()" in javascript
    assert 'path: field.path' in javascript


def test_every_overlay_preference_has_localized_help_without_changing_its_value():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    entries = _localized_presentation_entries(
        javascript,
        "const OVERLAY_PRESENTATIONS = Object.freeze({",
        "const DOCUMENT_PRESENTATIONS",
    )
    assert set(entries) == set(OVERLAY_FIELD_SPECS)
    renderer = javascript[
        javascript.index("function renderOverlayDocument()"):
        javascript.index("function syncOverlayControls()")
    ]
    assert "presentationForOverlayField(field)" in renderer
    assert "attachOverlayFieldHelp(row, input, field, presentation)" in renderer
    assert "presentation.title" in renderer
    assert "rawKey.textContent = field.display_path" in renderer
    assert "input.value = String(field.current_value)" in renderer


def test_plugin_environment_and_secret_views_use_localized_status_copy():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    assert "const ENVIRONMENT_SETTING_LABELS = Object.freeze" in javascript
    assert "const SECRET_SLOT_LABELS = Object.freeze" in javascript
    plugin = javascript[
        javascript.index("function renderPluginStatuses()"):
        javascript.index("function renderEnvironmentOnlySettings()")
    ]
    environment = javascript[
        javascript.index("function renderEnvironmentOnlySettings()"):
        javascript.index("function renderSensitiveStatus(meta)")
    ]
    sensitive = javascript[
        javascript.index("function renderSensitiveStatus(meta)"):
        javascript.index("function restoreLaneEnabled(lane)")
    ]

    for renderer in (plugin, environment, sensitive):
        assert "localizedProtocolLabel(" in renderer
    assert "registryValue(ENVIRONMENT_SETTING_LABELS, setting.id)" in environment
    assert "registryValue(SECRET_SLOT_LABELS, slot)" in sensitive
    assert "localizedMessage(" in plugin + environment + sensitive
    for old_primary_copy in (
        "configured yes",
        "configured no",
        "not configured",
        "safe configured value",
        "unexpected owner contract",
        '["Command",',
        '["Target",',
        '["Secret change",',
    ):
        assert old_primary_copy not in plugin + environment + sensitive

    # Raw user-controlled identifiers and values remain browser data, not copy keys.
    assert "plugin.name" in plugin
    assert "setting.environment_variable" in environment
    assert "option.value = slot" in sensitive
    assert "text(preview.before_next_launch)" in sensitive


def test_restore_health_and_self_check_use_localized_presentation_labels():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    for registry in (
        "LANE_LABELS",
        "CHECK_LABELS",
        "HEALTH_ISSUE_LABELS",
    ):
        assert f"const {registry} = Object.freeze" in javascript

    restore = javascript[
        javascript.index("function renderRestorePoints(lane, points)"):
        javascript.index("function syncRollbackControls()")
    ]
    health = javascript[
        javascript.index("function applyMeta(meta)"):
        javascript.index("function validSelfCheckJob(job)")
    ]
    self_check = javascript[
        javascript.index("function renderSelfCheckJob(job)"):
        javascript.index("async function pollSelfCheck()")
    ]

    assert "localizedProtocolLabel(LANE_LABELS, lane)" in restore
    assert "localizedProtocolLabelWithCode(CHECK_LABELS, name)" in self_check
    assert "localizedHealthIssue(issues[0])" in health
    assert "localizedMessage(" in restore + health + self_check
    assert "localizedCopy(" in restore + health + self_check
    for old_primary_copy in (
        'lines.push(["Lane",',
        '["Scope",',
        '["Secret slots",',
        '["Mapped overrides",',
        '["Changed fields",',
        '["Resolution error",',
        "Owner resolution healthy",
        "服务端 capability",
    ):
        assert old_primary_copy not in restore + health + self_check

    # Opaque IDs, field/env identifiers, and diagnostic text stay unchanged data.
    assert "confirmation.restore_point_id" in restore
    assert "item.environment_variable" in restore
    assert "boundedText(item.reason" in self_check


def test_app_and_overlay_authoring_explanations_are_localized():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    app_editor = javascript[
        javascript.index("function renderAppFieldEditor(field)"):
        javascript.index("function appAuthoringEnabled()")
    ]
    app_preview = javascript[
        javascript.index("function renderAppPreview(preview)"):
        javascript.index("function searchTextForField(field)")
    ]
    overlay = javascript[
        javascript.index("function renderOverlayDocument()"):
        javascript.index("function renderPluginStatuses()")
    ]

    assert "localizedMessage(" in app_editor
    assert "localizedMessage(" in app_preview
    assert "localizedMessage(" in overlay
    for old_primary_copy in (
        "候选 file value",
        "typed editor",
        "recovery-only",
        "set、unset",
        "set fail-closed",
        "authoring projection",
        "typed field",
        "next launch ",
        "production owner",
        "RestorePoint",
        "Overlay owner",
        "fixed owner",
        "按 owner",
        "UI 保持只读",
    ):
        assert old_primary_copy not in app_editor + app_preview + overlay

    # Authoring still submits the exact typed values and paths.
    assert "value: selectedScalarValue()" in javascript
    assert "path: field.path" in javascript
    assert "input.value = String(field.current_value)" in overlay


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
    client = javascript[javascript.index("function validSelfCheckJob(job)"):]

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
    assert "raw_stderr" not in client
    assert "stderr_text" not in client


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

    assert "<fieldset" in html and "<legend" in html
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
    assert '"路径健康"' in javascript
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
    assert "新增或修改操作会安全关闭" in html
    assert "移除文件值" in html
    assert "不会擅自恢复默认值" in html
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


def test_managed_document_catalog_discloses_omitted_documents_and_truncation():
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "studio.css").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")

    notice = html.split('id="managed-documents-incomplete-notice"', 1)[1].split(
        "</div>", 1
    )[0]
    assert 'role="status"' in notice
    assert 'aria-live="polite"' in notice
    assert "角色数据文件目录的完整性信息不可用" in notice
    assert ".document-card .document-card__warning" in css

    loader = javascript[
        javascript.index("async function reloadStudioData"):
        javascript.index('window.addEventListener("DOMContentLoaded"')
    ]
    renderer = javascript[
        javascript.index("function renderManagedDocuments"):
        javascript.index("function renderOverlayDocument")
    ]
    assert "catalog.truncation.managed_documents_omitted" in loader
    assert "state.managedDocumentsOmitted" in loader
    assert "managed-documents-incomplete-notice" in renderer
    assert 'localizedMessage("runtime.054"' in renderer
    assert "documentInfo.truncation" in renderer
    assert 'localizedMessage("runtime.061"' in renderer
    for counter in (
        "strings",
        "collections",
        "depth",
        "unsupported",
        "total_bytes",
    ):
        assert counter in renderer


def test_character_data_catalog_exposes_read_only_fields_and_safe_metadata():
    css = (UI / "studio.css").read_text(encoding="utf-8")
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    renderer = javascript[
        javascript.index("function renderManagedDocuments"):
        javascript.index("function renderOverlayDocument")
    ]

    assert 'document.createElement("details")' in renderer
    assert 'document.createElement("summary")' in renderer
    assert 'summary.textContent = localizedMessage("runtime.062")' in renderer
    assert "documentInfo.basename" in renderer
    assert "documentInfo.source_kind" in renderer
    assert "documentInfo.external" in renderer
    assert "documentInfo.unsupported_reason" in renderer
    assert "health.code" in renderer
    assert "documentInfo.fields" in renderer
    assert "field.display_path" in renderer
    assert "field.current_value" in renderer
    assert "field.default_value" in renderer
    assert "field.value_type" in renderer
    assert "heading.textContent = text(field.display_path)" in renderer
    assert "currentValue.textContent = text(field.current_value)" in renderer
    assert "defaultValue.textContent = text(field.default_value)" in renderer
    assert "boundedText(text(field.current_value)" not in renderer
    assert "action.disabled = true" not in renderer
    assert "documentInfo.path" not in renderer
    for selector in (
        ".document-card__details",
        ".document-card__metadata",
        ".document-field-list",
        ".document-field-row",
    ):
        assert selector in css


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
    assert 'localizedMessage("runtime.026")' in renderer


def test_nullable_scalar_has_an_explicit_null_toggle_distinct_from_empty_text():
    javascript = (UI / "studio.js").read_text(encoding="utf-8")
    scalar_editor = javascript[
        javascript.index("function scalarEditor"):
        javascript.index("function renderAppFieldEditor")
    ]

    assert "field.nullable !== true" in scalar_editor
    assert 'className = "nullable-scalar"' in scalar_editor
    assert 'localizedMessage("runtime.030")' in scalar_editor
    assert 'localizedMessage("runtime.031")' in scalar_editor
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
    assert "整个敏感配置文件（ManagedDocument）" in javascript
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
