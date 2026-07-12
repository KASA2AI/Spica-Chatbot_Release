(() => {
  "use strict";

  const HEAVY_CHECKS = Object.freeze([
    "tts",
    "stt",
    "moondream",
    "ocr",
    "song_uvr",
    "song_rvc",
    "llm",
  ]);
  const ACTIVE_JOB_STATUSES = new Set(["QUEUED", "RUNNING", "CANCELLING"]);
  const JOB_STATUSES = new Set([
    ...ACTIVE_JOB_STATUSES,
    "PASS",
    "UNVERIFIED",
    "DEGRADED",
    "FAIL",
    "CANCELLED",
    "INTERNAL_ERROR",
  ]);
  const RESULT_STATUSES = new Set([
    "PASS",
    "UNVERIFIED",
    "DEGRADED",
    "FAIL",
    "SKIPPED_DISABLED",
  ]);
  const MAX_RENDERED_RESULTS = 12;
  const MAX_RENDERED_PROGRESS = 12;
  const MAX_STRUCTURED_ITEMS = 256;
  const SELF_CHECK_POLL_MS = 1200;
  const RESTORE_ROUTES = Object.freeze({
    app: Object.freeze({
      list: `/api/v1/app/restore-points`,
      rollback: `/api/v1/app/rollbacks`,
    }),
    overlay: Object.freeze({
      list: `/api/v1/overlay/restore-points`,
      rollback: `/api/v1/overlay/rollbacks`,
    }),
    sensitive: Object.freeze({
      list: `/api/v1/sensitive/restore-points`,
      rollback: `/api/v1/sensitive/rollbacks`,
    }),
  });

  const state = {
    csrf: null,
    meta: null,
    fields: [],
    managedDocuments: [],
    managedDocumentsOmitted: null,
    pluginStatuses: [],
    environmentOnlySettings: [],
    level: "basic",
    category: "all",
    query: "",
    appWriteEnabled: false,
    appRecoveryOnly: true,
    catalogFieldsComplete: false,
    appWriteBusy: false,
    selectedField: null,
    draftOperations: new Map(),
    appPreview: null,
    overlayWriteEnabled: false,
    overlayWriteBusy: false,
    overlayPreview: null,
    sensitiveWriteEnabled: false,
    sensitiveWriteBusy: false,
    sensitivePreview: null,
    secretSlots: new Map(),
    rollbackEnabled: false,
    rollbackBusy: false,
    rollbackConfirmation: null,
    selfCheckEnabled: false,
    selfCheckJobsEnabled: false,
    selfCheckBusy: false,
    selfCheckJob: null,
    selfCheckPollTimer: null,
  };

  const byId = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));
  const structuredReaders = new WeakMap();

  function text(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function boundedText(value, maximum = 180) {
    const rendered = typeof value === "string" ? value : String(value ?? "");
    const safe = rendered.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
    return safe.length > maximum ? `${safe.slice(0, maximum - 1)}…` : safe;
  }

  function setSession(kind, label) {
    const root = byId("session-state");
    if (!root) return;
    root.setAttribute("aria-label", label);
    const dot = root.querySelector(".status-dot");
    dot.className = `status-dot status-dot--${kind}`;
    root.querySelector("span:last-child").textContent = label;
  }

  function toast(message) {
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = message;
    byId("toast-region").append(item);
    window.setTimeout(() => item.remove(), 4600);
  }

  function bootstrapToken() {
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    return fragment.get("bootstrap");
  }

  async function exchangeBootstrapToken(token, { clearFragment = false } = {}) {
    if (typeof token !== "string" || token.length < 22 || token.length > 256) {
      if (clearFragment) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
      return false;
    }
    try {
      const response = await fetch("/api/v1/session/bootstrap", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Spica-Bootstrap": token },
      });
      if (clearFragment) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
      if (!response.ok) throw new Error("bootstrap rejected");
      const payload = await response.json();
      state.csrf = payload.csrf_token || null;
      return true;
    } catch (_error) {
      if (clearFragment) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
      return false;
    }
  }

  async function refreshCsrf() {
    const payload = await getJson("/api/v1/session/csrf");
    if (!payload || typeof payload.csrf_token !== "string" || !payload.csrf_token) {
      throw new Error("session refresh rejected");
    }
    state.csrf = payload.csrf_token;
  }

  async function establishSession() {
    const fragmentToken = bootstrapToken();
    if (
      fragmentToken
      && await exchangeBootstrapToken(fragmentToken, { clearFragment: true })
    ) return;
    try {
      await refreshCsrf();
    } catch (_error) {
      await waitForManualBootstrap();
    }
  }

  function waitForManualBootstrap() {
    const gate = byId("manual-bootstrap");
    const input = byId("manual-bootstrap-token");
    const submit = byId("manual-bootstrap-submit");
    const status = byId("manual-bootstrap-status");
    gate.hidden = false;
    input.value = "";
    input.focus();
    return new Promise((resolve) => {
      const attempt = async () => {
        if (submit.disabled) return;
        const token = input.value.trim();
        input.value = "";
        submit.disabled = true;
        status.textContent = "正在验证一次性授权…";
        const accepted = await exchangeBootstrapToken(token);
        submit.disabled = false;
        if (!accepted) {
          status.textContent = "授权无效、已过期或尝试次数已用尽。";
          input.focus();
          return;
        }
        gate.hidden = true;
        status.textContent = "";
        resolve();
      };
      submit.addEventListener("click", attempt);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          attempt();
        }
      });
    });
  }

  async function getJson(path) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: state.csrf ? { "X-Spica-CSRF": state.csrf } : {},
    });
    if (!response.ok) throw new Error(`request failed: ${response.status}`);
    return response.json();
  }

  async function postJson(path, body) {
    if (!state.csrf) throw new Error("CSRF_UNAVAILABLE");
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Spica-CSRF": state.csrf,
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let code = `HTTP_${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.error && typeof payload.error.code === "string") {
          code = boundedText(payload.error.code, 64);
        }
      } catch (_error) {
        // Stable HTTP status fallback; response bodies are never surfaced.
      }
      throw new Error(code);
    }
    return response.json();
  }

  function fieldPath(field) {
    if (typeof field.path === "string") return field.path;
    if (typeof field.display_path === "string") return field.display_path;
    if (Array.isArray(field.path)) {
      return field.path.map((part) => {
        if (Object.hasOwn(part, "name")) return part.name;
        if (Object.hasOwn(part, "key")) return `[${JSON.stringify(part.key)}]`;
        if (Object.hasOwn(part, "index")) return `[${part.index}]`;
        return "?";
      }).join(".");
    }
    return "unknown";
  }

  function categoryOf(field) {
    const path = fieldPath(field);
    return path.split(".")[0] || "other";
  }

  function sourceClass(kind) {
    if (kind === "env_override" || kind === "secret_tainted_env_override") {
      return "source-pill source-pill--env";
    }
    if (kind === "default") return "source-pill source-pill--default";
    return "source-pill";
  }

  function pathHealthText(field) {
    const health = field.path_health;
    if (!health || typeof health !== "object") return "—";
    return [health.status, health.code, health.expected_kind]
      .map((item) => boundedText(item || "unknown", 64))
      .join(" · ");
  }

  function fieldRow(field) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "field-row";
    button.dataset.path = fieldPath(field);
    button.innerHTML = `
      <span class="field-row__name"><strong></strong><small></small></span>
      <span class="field-value"></span>
      <span class="${sourceClass(field.source_kind)}"></span>`;
    button.querySelector("strong").textContent = fieldPath(field);
    const pathSummary = field.path_health
      ? ` · path ${boundedText(field.path_health.status || "unknown", 32)}`
      : "";
    button.querySelector("small").textContent = `${field.control || field.value_type || "field"} · ${field.owner || "production owner"}${pathSummary}`;
    button.querySelector(".field-value").textContent = text(field.next_launch_value);
    button.querySelector(".source-pill").textContent = field.source_kind || "unknown";
    button.addEventListener("click", () => inspectField(field, button));
    return button;
  }

  function inspectField(field, row) {
    all(".field-row.is-selected").forEach((item) => item.classList.remove("is-selected"));
    if (row) row.classList.add("is-selected");
    byId("inspector-title").textContent = fieldPath(field);
    const details = [
      ["Type", field.control || field.value_type],
      ["Description", field.description || "Owner has not supplied a description."],
      ["View level", field.level || "advanced"],
      ["File value", field.file_present ? text(field.file_value) : "未写入"],
      ["Schema default", text(field.default_value)],
      ["Next launch", text(field.next_launch_value)],
      ["Source", field.source_kind],
      ["Environment", field.environment_variable || "—"],
      ["Owner", field.owner || "—"],
      ["Effect", field.effect_policy || "—"],
      ["Path health", pathHealthText(field)],
      [
        "Choices",
        Array.isArray(field.literal_choices) && field.literal_choices.length
          ? field.literal_choices.map(text).join(" · ")
          : "—",
      ],
      [
        "Range",
        field.minimum !== null || field.maximum !== null
          ? `${field.minimum ?? "−∞"} … ${field.maximum ?? "+∞"}`
          : "—",
      ],
      [
        "Dependencies",
        Array.isArray(field.dependencies) && field.dependencies.length
          ? field.dependencies.map((item) => `${item.display_path} = ${text(item.expected_value)}`).join(" · ")
          : "—",
      ],
      [
        "Authoring",
        field.editable && appAuthoringEnabled()
          ? "可编辑 · 保存后按 owner 生效策略处理"
          : field.editable
            ? state.appRecoveryOnly
              ? "Recovery-only · 仅允许符合 capability 的语义回滚"
              : "服务端 capability 未开放"
            : field.unsupported_reason || "只读",
      ],
    ];
    const list = document.createElement("dl");
    list.className = "detail-list";
    details.forEach(([name, value]) => {
      const item = document.createElement("div");
      item.className = "detail-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = text(value);
      item.append(term, description);
      list.append(item);
    });
    byId("inspector-content").replaceChildren(list);
    renderAppFieldEditor(field);
    if (byId("inspector-title").offsetParent !== null) {
      byId("inspector-title").focus({ preventScroll: true });
    }
  }

  function defaultForStructuredSchema(schema) {
    if (!schema || typeof schema !== "object") return null;
    if (Object.hasOwn(schema, "default")) return schema.default;
    if (Array.isArray(schema.anyOf)) {
      if (schema.anyOf.some((branch) => branch && branch.type === "null")) {
        return null;
      }
      return defaultForStructuredSchema(schema.anyOf[0]);
    }
    if (schema.type === "array") return [];
    if (schema.type === "object") return {};
    if (schema.type === "boolean") return false;
    if (schema.type === "integer" || schema.type === "number") return 0;
    if (schema.type === "string") return "";
    return null;
  }

  function structuredPrimitive(schema, value) {
    let control;
    if (Array.isArray(schema.enum) && schema.enum.length) {
      control = document.createElement("select");
      schema.enum.forEach((choice, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = text(choice);
        option.selected = Object.is(choice, value);
        control.append(option);
      });
      structuredReaders.set(control, () => schema.enum[Number(control.value)]);
      return control;
    }
    control = document.createElement("input");
    control.autocomplete = "off";
    if (schema.type === "boolean") {
      control.type = "checkbox";
      control.checked = value === true;
      structuredReaders.set(control, () => control.checked === true);
      return control;
    }
    if (schema.type === "integer" || schema.type === "number") {
      control.type = "number";
      control.step = schema.type === "integer" ? "1" : "any";
      if (typeof schema.minimum === "number") control.min = String(schema.minimum);
      if (typeof schema.maximum === "number") control.max = String(schema.maximum);
      if (typeof value === "number" && Number.isFinite(value)) {
        control.value = String(value);
      }
      structuredReaders.set(control, () => {
        const number = Number(control.value);
        if (!Number.isFinite(number)) throw new Error("FIELD_VALUE_INVALID");
        if (schema.type === "integer" && !Number.isSafeInteger(number)) {
          throw new Error("FIELD_VALUE_INVALID");
        }
        return number;
      });
      return control;
    }
    if (schema.type === "string") {
      control.type = "text";
      control.spellcheck = false;
      control.value = typeof value === "string" ? value : "";
      structuredReaders.set(control, () => control.value);
      return control;
    }
    return null;
  }

  function renderStructuredEditor(schema, value, depth = 0) {
    if (!schema || typeof schema !== "object" || depth > 8) return null;
    if (Array.isArray(schema.anyOf)) {
      const nonNull = schema.anyOf.find((branch) => branch && branch.type !== "null");
      const nullable = schema.anyOf.some((branch) => branch && branch.type === "null");
      if (!nonNull || !nullable) return null;
      const root = document.createElement("div");
      root.className = "structured-optional";
      const toggleLabel = document.createElement("label");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = value !== null && value !== undefined;
      const toggleCopy = document.createElement("span");
      toggleCopy.textContent = "写入结构值（关闭则写入 null）";
      toggleLabel.append(toggle, toggleCopy);
      const childRoot = document.createElement("div");
      let child = null;
      const renderChild = () => {
        childRoot.replaceChildren();
        child = toggle.checked
          ? renderStructuredEditor(
            nonNull,
            value ?? defaultForStructuredSchema(nonNull),
            depth + 1,
          )
          : null;
        if (child) childRoot.append(child);
      };
      toggle.addEventListener("change", renderChild);
      renderChild();
      root.append(toggleLabel, childRoot);
      structuredReaders.set(root, () => {
        if (!toggle.checked) return null;
        const reader = child && structuredReaders.get(child);
        if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
        return reader();
      });
      return root;
    }

    const primitive = structuredPrimitive(schema, value);
    if (primitive) return primitive;

    if (schema.type === "array" && schema.items) {
      const root = document.createElement("div");
      root.className = "structured-array";
      const rows = document.createElement("div");
      rows.className = "structured-rows";
      const entries = [];
      let add = null;
      const limitNote = document.createElement("p");
      limitNote.className = "operation-result";
      limitNote.textContent = "最多 256 项；已达到客户端安全上限。";
      limitNote.hidden = true;
      const activeEntries = () => entries.filter((entry) => rows.contains(entry.row));
      const syncLimit = () => {
        const atLimit = activeEntries().length >= MAX_STRUCTURED_ITEMS;
        if (add) add.disabled = atLimit;
        limitNote.hidden = !atLimit;
      };
      const addEntry = (itemValue) => {
        if (activeEntries().length >= MAX_STRUCTURED_ITEMS) {
          syncLimit();
          return false;
        }
        const row = document.createElement("div");
        row.className = "structured-item";
        const child = renderStructuredEditor(schema.items, itemValue, depth + 1);
        if (!child) return false;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "structured-remove";
        remove.textContent = "移除";
        remove.addEventListener("click", () => {
          row.remove();
          syncLimit();
        });
        row.append(child, remove);
        rows.append(row);
        entries.push({ row, child });
        syncLimit();
        return true;
      };
      const initialItems = Array.isArray(value) ? value : [];
      if (initialItems.length > MAX_STRUCTURED_ITEMS || !initialItems.every(addEntry)) {
        return null;
      }
      add = document.createElement("button");
      add.type = "button";
      add.className = "structured-add";
      add.textContent = "新增一项";
      add.addEventListener("click", () => {
        addEntry(defaultForStructuredSchema(schema.items));
        syncLimit();
      });
      root.append(rows, add, limitNote);
      syncLimit();
      structuredReaders.set(root, () => entries
        .filter((entry) => rows.contains(entry.row))
        .map((entry) => {
          const reader = structuredReaders.get(entry.child);
          if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
          return reader();
        }));
      return root;
    }

    if (schema.type === "object") {
      const root = document.createElement("div");
      root.className = "structured-object";
      const current = value && typeof value === "object" && !Array.isArray(value)
        ? value
        : {};
      const fixedEntries = [];
      const properties = schema.properties && typeof schema.properties === "object"
        ? schema.properties
        : {};
      const propertyEntries = Object.entries(properties);
      if (propertyEntries.length > MAX_STRUCTURED_ITEMS) return null;
      let fixedPropertiesComplete = true;
      propertyEntries.forEach(([name, childSchema]) => {
        const row = document.createElement("label");
        row.className = "structured-property";
        const caption = document.createElement("span");
        caption.textContent = boundedText(name, 128);
        const child = renderStructuredEditor(
          childSchema,
          Object.hasOwn(current, name)
            ? current[name]
            : defaultForStructuredSchema(childSchema),
          depth + 1,
        );
        if (!child) {
          fixedPropertiesComplete = false;
          return;
        }
        row.append(caption, child);
        root.append(row);
        fixedEntries.push({ name, child });
      });
      if (!fixedPropertiesComplete) return null;
      const dynamicEntries = [];
      const additional = schema.additionalProperties;
      let dynamicRows = null;
      if (additional && typeof additional === "object") {
        dynamicRows = document.createElement("div");
        dynamicRows.className = "structured-rows";
        let add = null;
        const limitNote = document.createElement("p");
        limitNote.className = "operation-result";
        limitNote.textContent = "最多 256 项；已达到客户端安全上限。";
        limitNote.hidden = true;
        const activeEntries = () => dynamicEntries.filter(
          (entry) => dynamicRows.contains(entry.row),
        );
        const syncLimit = () => {
          const atLimit = activeEntries().length >= MAX_STRUCTURED_ITEMS;
          if (add) add.disabled = atLimit;
          limitNote.hidden = !atLimit;
        };
        const addDynamic = (key, itemValue) => {
          if (activeEntries().length >= MAX_STRUCTURED_ITEMS) {
            syncLimit();
            return false;
          }
          const row = document.createElement("div");
          row.className = "structured-map-row";
          const keyInput = document.createElement("input");
          keyInput.type = "text";
          keyInput.autocomplete = "off";
          keyInput.spellcheck = false;
          keyInput.maxLength = 128;
          keyInput.placeholder = "key";
          keyInput.value = typeof key === "string" ? key : "";
          const child = renderStructuredEditor(additional, itemValue, depth + 1);
          if (!child) return false;
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "structured-remove";
          remove.textContent = "移除";
          remove.addEventListener("click", () => {
            row.remove();
            syncLimit();
          });
          row.append(keyInput, child, remove);
          dynamicRows.append(row);
          dynamicEntries.push({ row, keyInput, child });
          syncLimit();
          return true;
        };
        const initialEntries = Object.entries(current)
          .filter(([name]) => !Object.hasOwn(properties, name));
        if (
          initialEntries.length > MAX_STRUCTURED_ITEMS
          || !initialEntries.every(([name, itemValue]) => addDynamic(name, itemValue))
        ) return null;
        add = document.createElement("button");
        add.type = "button";
        add.className = "structured-add";
        add.textContent = "新增键";
        add.addEventListener("click", () => {
          addDynamic("", defaultForStructuredSchema(additional));
          syncLimit();
        });
        root.append(dynamicRows, add, limitNote);
        syncLimit();
      }
      structuredReaders.set(root, () => {
        const result = {};
        fixedEntries.forEach(({ name, child }) => {
          const reader = structuredReaders.get(child);
          if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
          result[name] = reader();
        });
        dynamicEntries.filter(
          (entry) => dynamicRows && dynamicRows.contains(entry.row),
        ).forEach((entry) => {
          const key = entry.keyInput.value.trim();
          if (!key || Object.hasOwn(result, key)) throw new Error("FIELD_VALUE_INVALID");
          const reader = structuredReaders.get(entry.child);
          if (!reader) throw new Error("FIELD_EDITOR_UNAVAILABLE");
          result[key] = reader();
        });
        return result;
      });
      return root;
    }
    return null;
  }

  function readScalarControl(field, control) {
    const kind = control.dataset.valueKind;
    if (kind === "boolean") return control.checked === true;
    if (kind === "literal") {
      const index = Number(control.value);
      if (!Number.isSafeInteger(index) || !Array.isArray(field.literal_choices)) {
        throw new Error("FIELD_VALUE_INVALID");
      }
      return field.literal_choices[index];
    }
    if (kind === "number") {
      const value = Number(control.value);
      if (!Number.isFinite(value)) throw new Error("FIELD_VALUE_INVALID");
      return value;
    }
    if (kind === "text") return control.value;
    throw new Error("FIELD_VALUE_INVALID");
  }

  function scalarEditor(field) {
    const current = field.file_present ? field.file_value : field.next_launch_value;
    const withNullableToggle = (control) => {
      if (field.nullable !== true) return control;
      const root = document.createElement("div");
      root.className = "nullable-scalar";
      const label = document.createElement("label");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = current === null;
      const copy = document.createElement("span");
      copy.textContent = "显式写入 null";
      const hint = document.createElement("p");
      hint.className = "operation-result";
      hint.textContent = "关闭 null 后按字段类型写值；空字符串不是 null。";
      const syncNullable = () => {
        control.disabled = toggle.checked;
      };
      toggle.addEventListener("change", syncNullable);
      label.append(toggle, copy);
      root.append(label, control, hint);
      syncNullable();
      structuredReaders.set(root, () => {
        if (toggle.checked) return null;
        return readScalarControl(field, control);
      });
      return root;
    };
    if (field.control === "switch") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = current === true;
      input.dataset.valueKind = "boolean";
      return withNullableToggle(input);
    }
    if (field.control === "select" && Array.isArray(field.literal_choices)) {
      const select = document.createElement("select");
      select.dataset.valueKind = "literal";
      field.literal_choices.forEach((choice, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = text(choice);
        option.selected = Object.is(choice, current);
        select.append(option);
      });
      return withNullableToggle(select);
    }
    if (field.control === "number") {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.dataset.valueKind = "number";
      if (typeof field.minimum === "number") input.min = String(field.minimum);
      if (typeof field.maximum === "number") input.max = String(field.maximum);
      if (typeof current === "number" && Number.isFinite(current)) {
        input.value = String(current);
      }
      return withNullableToggle(input);
    }
    if (field.control === "text") {
      const input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.dataset.valueKind = "text";
      input.value = typeof current === "string" ? current : "";
      return withNullableToggle(input);
    }
    if (field.control === "structured" && field.structured_schema) {
      return renderStructuredEditor(field.structured_schema, current);
    }
    return null;
  }

  function renderAppFieldEditor(field) {
    const editor = byId("field-editor");
    const controlRoot = byId("field-editor-control");
    state.selectedField = field;
    controlRoot.replaceChildren();
    editor.hidden = !field.editable;
    if (!field.editable) {
      syncAppControls();
      return;
    }
    const control = scalarEditor(field);
    if (control) {
      const label = document.createElement(
        field.control === "structured" || control.classList.contains("nullable-scalar")
          ? "div"
          : "label",
      );
      label.className = "field-editor__label";
      const caption = document.createElement("span");
      caption.textContent = `${fieldPath(field)} 的候选 file value`;
      control.classList.add("field-editor-value");
      label.append(caption, control);
      controlRoot.append(label);
    } else {
      const unsupported = document.createElement("p");
      unsupported.className = "operation-result";
      unsupported.textContent = "该结构化字段将在专用 typed editor 中修改。";
      controlRoot.append(unsupported);
    }
    byId("field-editor-status").textContent = state.appRecoveryOnly
      ? "app.yaml 处于 recovery-only；set、unset 与预览保持关闭。"
      : !state.appWriteEnabled
        ? "服务端未开放 app_config_write；此处保持只读。"
        : !state.catalogFieldsComplete
          ? "CATALOG_FIELDS_INCOMPLETE：set fail-closed；现有 file override 的 unset 修复路径仍可用。"
          : !fieldAuthoringComplete(field)
            ? "FIELD_AUTHORING_INCOMPLETE：服务端未确认完整 authoring projection；set 已关闭，现有 file override 仍可 unset。"
            : "变更只保存在本页内存中，预览后才可保存。";
    syncAppControls();
  }

  function appAuthoringEnabled() {
    return state.appWriteEnabled && state.appRecoveryOnly === false;
  }

  function fieldAuthoringComplete(field) {
    return Boolean(field && field.authoring_complete === true);
  }

  function renderAppRecoveryState() {
    const notice = byId("app-recovery-notice");
    notice.hidden = !state.appRecoveryOnly;
    syncAppManualRepairGuidance(false);
    if (!state.appRecoveryOnly) return;
    state.appPreview = null;
    state.draftOperations.clear();
    const dialog = byId("app-preview-dialog");
    if (dialog.open) dialog.close();
    syncAppControls();
  }

  function renderCatalogCompletenessState() {
    byId("catalog-fields-incomplete-notice").hidden = state.catalogFieldsComplete;
  }

  function selectedScalarValue() {
    const field = state.selectedField;
    const control = byId("field-editor-control").querySelector(".field-editor-value");
    if (!field || !control) throw new Error("FIELD_EDITOR_UNAVAILABLE");
    const structuredReader = structuredReaders.get(control);
    if (structuredReader) return structuredReader();
    return readScalarControl(field, control);
  }

  function appDraftOperationsAllowed() {
    return state.catalogFieldsComplete || Array.from(
      state.draftOperations.values(),
    ).every((operation) => operation && operation.kind === "unset");
  }

  function invalidateAppPreview() {
    state.appPreview = null;
    const dialog = byId("app-preview-dialog");
    if (dialog.open) dialog.close();
    byId("app-preview-changes").replaceChildren();
    byId("operation-result").textContent = "";
  }

  function queueAppSet() {
    const field = state.selectedField;
    if (
      !appAuthoringEnabled()
      || state.appWriteBusy
      || !field
      || !field.editable
      || state.catalogFieldsComplete !== true
      || field.authoring_complete !== true
      || !Array.isArray(field.path)
    ) return;
    try {
      state.draftOperations.set(fieldPath(field), {
        kind: "set",
        path: field.path,
        value: selectedScalarValue(),
      });
      invalidateAppPreview();
      byId("field-editor-status").textContent = "已加入本页草稿；尚未写入文件。";
    } catch (error) {
      byId("field-editor-status").textContent = boundedText(error.message, 64);
    }
    syncAppControls();
  }

  function queueAppUnset() {
    const field = state.selectedField;
    if (
      !appAuthoringEnabled()
      || state.appWriteBusy
      || !field
      || !field.editable
      || !field.file_present
      || !Array.isArray(field.path)
    ) return;
    state.draftOperations.set(fieldPath(field), {
      kind: "unset",
      path: field.path,
    });
    invalidateAppPreview();
    byId("field-editor-status").textContent = "将移除文件 override，并回落到 env/default owner 语义。";
    syncAppControls();
  }

  function syncAppControls() {
    const field = state.selectedField;
    const scalarAvailable = Boolean(
      field
      && (
        ["switch", "select", "number", "text"].includes(field.control)
        || (
          field.control === "structured"
          && field.structured_schema
          && byId("field-editor-control").querySelector(".field-editor-value")
        )
      ),
    );
    const canEdit = Boolean(
      field && field.editable && appAuthoringEnabled() && !state.appWriteBusy,
    );
    const canSet = canEdit
      && state.catalogFieldsComplete
      && fieldAuthoringComplete(field);
    byId("field-editor-set").disabled = !canSet || !scalarAvailable;
    byId("field-editor-unset").disabled = !canEdit || !field.file_present;
    byId("change-count").textContent = `${state.draftOperations.size} 项`;
    byId("app-preview-button").disabled = !appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || state.draftOperations.size === 0;
    byId("app-preview-commit").disabled = !appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || !state.appPreview;
  }

  function renderAppPreview(preview) {
    const changes = Array.isArray(preview.changes) ? preview.changes : [];
    const rows = changes.slice(0, 64).map((change) => {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      const values = document.createElement("span");
      title.textContent = boundedText(change.display_path || "typed field", 128);
      values.textContent = `${text(change.file_value_before)} → ${text(change.file_value_after)} · next launch ${text(change.next_launch_value_before)} → ${text(change.next_launch_value_after)}`;
      item.append(title, values);
      if (change.file_value_shadowed || change.semantic_warning) {
        const warning = document.createElement("small");
        warning.textContent = boundedText(
          change.semantic_warning || "env override 仍控制下一次启动值",
          180,
        );
        item.append(warning);
      }
      return item;
    });
    byId("app-preview-changes").replaceChildren(...rows);
    if (!rows.length) byId("app-preview-changes").textContent = "候选文档无语义变化。";
  }

  async function previewAppChanges() {
    if (
      !appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || state.draftOperations.size === 0
    ) return;
    state.appWriteBusy = true;
    syncAppControls();
    try {
      const preview = await postJson("/api/v1/app/previews", {
        operations: Array.from(state.draftOperations.values()),
      });
      if (
        !preview
        || typeof preview.preview_id !== "string"
        || !Array.isArray(preview.changes)
      ) throw new Error("PREVIEW_INVALID");
      state.appPreview = preview;
      renderAppPreview(preview);
      byId("operation-result").textContent = preview.changed
        ? "候选已通过生产 owner 校验；确认后才会原子发布。"
        : "候选与当前文档相同；保存不会创建 RestorePoint。";
      byId("app-preview-dialog").showModal();
    } catch (error) {
      state.appPreview = null;
      toast(`预览失败：${boundedText(error.message, 64)}`);
    } finally {
      state.appWriteBusy = false;
      syncAppControls();
    }
  }

  async function commitAppPreview() {
    const preview = state.appPreview;
    if (!appAuthoringEnabled()
      || state.appWriteBusy
      || !appDraftOperationsAllowed()
      || !preview
    ) return;
    state.appWriteBusy = true;
    syncAppControls();
    try {
      await postJson("/api/v1/app/commits", { preview_id: preview.preview_id });
      state.draftOperations.clear();
      state.appPreview = null;
      byId("app-preview-dialog").close();
      await reloadStudioData();
      toast("app.yaml 已原子保存；按 owner 提示在下次 Spica 启动生效。");
    } catch (error) {
      state.appPreview = null;
      byId("operation-result").textContent = `保存失败：${boundedText(error.message, 64)}。本页草稿仍保留，请重新预览。`;
    } finally {
      state.appWriteBusy = false;
      syncAppControls();
    }
  }

  function visibleFields() {
    const query = state.query.trim().toLocaleLowerCase();
    return state.fields.filter((field) => {
      const category = categoryOf(field);
      const levelOk = state.level === "advanced" || (field.level || "basic") === "basic";
      const categoryOk = state.category === "all" || category === state.category;
      const haystack = `${fieldPath(field)} ${field.owner || ""} ${field.source_kind || ""} ${field.description || ""} ${(field.literal_choices || []).join(" ")}`.toLocaleLowerCase();
      return levelOk && categoryOk && (!query || haystack.includes(query));
    });
  }

  function renderFeatured() {
    const root = byId("featured-fields");
    root.setAttribute("aria-busy", "false");
    root.replaceChildren(...state.fields.slice(0, 4).map(fieldRow));
    if (!state.fields.length) root.textContent = "Catalog 暂无可展示字段。";
  }

  function renderCatalog() {
    const root = byId("catalog-fields");
    root.replaceChildren(...visibleFields().map(fieldRow));
    if (!root.children.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "没有匹配当前筛选的字段。";
      root.append(empty);
    }
  }

  function renderCategories() {
    const categories = ["all", ...new Set(state.fields.map(categoryOf))];
    const root = byId("category-filter");
    root.replaceChildren(...categories.map((category) => {
      const button = document.createElement("button");
      button.type = "button";
      const active = category === state.category;
      button.className = `filter-chip${active ? " is-active" : ""}`;
      button.setAttribute("aria-pressed", String(active));
      button.textContent = category === "all" ? "全部" : category;
      button.addEventListener("click", () => {
        state.category = category;
        renderCategories();
        renderCatalog();
      });
      return button;
    }));
  }

  function renderSwitches() {
    const fields = state.fields.filter((field) => field.control === "switch" || fieldPath(field).endsWith(".backend"));
    byId("switch-grid").replaceChildren(...fields.map((field) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "setting-card";
      card.setAttribute("aria-label", `查看 ${fieldPath(field)} 配置详情`);
      const copy = document.createElement("div");
      const title = document.createElement("h3");
      const detail = document.createElement("p");
      title.textContent = fieldPath(field);
      detail.textContent = `${field.source_kind || "unknown"} · ${field.effect_policy || "owner-specific"}`;
      copy.append(title, detail);
      const toggle = document.createElement("span");
      toggle.className = `toggle${field.next_launch_value === true ? " is-on" : ""}`;
      toggle.setAttribute("aria-hidden", "true");
      card.append(copy, toggle);
      card.addEventListener("click", () => inspectField(field));
      return card;
    }));
  }

  function renderManagedDocuments() {
    const root = byId("character-documents");
    const completenessNotice = byId("managed-documents-incomplete-notice");
    const completenessCopy = byId("managed-documents-incomplete-copy");
    completenessNotice.hidden = state.managedDocumentsOmitted === 0;
    completenessCopy.textContent = Number.isSafeInteger(
      state.managedDocumentsOmitted,
    )
      ? `ManagedDocument Catalog 已省略 ${state.managedDocumentsOmitted} 份文档；下方仅显示其余安全投影。`
      : "ManagedDocument Catalog 完整性元数据不可用；下方卡片可能不完整。";
    const cards = state.managedDocuments
      .filter((documentInfo) => documentInfo.id !== "overlay_preferences")
      .map((documentInfo) => {
      const card = document.createElement("article");
      card.className = "document-card";
      const title = document.createElement("h3");
      title.textContent = documentInfo.title || documentInfo.id || "Managed document";
      const owner = document.createElement("p");
      const health = documentInfo.health || {};
      const fieldCount = Array.isArray(documentInfo.fields) ? documentInfo.fields.length : 0;
      owner.textContent = `${documentInfo.owner || "production owner"} · ${fieldCount} fields`;
      const status = document.createElement("p");
      status.textContent = `${health.status || "unknown"} · ${documentInfo.effect_policy || "owner-specific"}`;
      const documentTruncation = documentInfo.truncation
        && typeof documentInfo.truncation === "object"
        ? documentInfo.truncation
        : {};
      const truncationDetails = [
        ["strings", "字符串"],
        ["collections", "集合"],
        ["depth", "深度"],
        ["unsupported", "不支持值"],
        ["total_bytes", "字节预算"],
      ].flatMap(([key, label]) => {
        const count = documentTruncation[key];
        return Number.isSafeInteger(count) && count > 0
          ? [`${label} ${count}`]
          : [];
      });
      const action = document.createElement("button");
      action.type = "button";
      action.className = "text-button";
      action.textContent = "只读 Catalog";
      action.disabled = true;
      card.append(title, owner, status);
      if (truncationDetails.length) {
        const warning = document.createElement("p");
        warning.className = "document-card__warning";
        warning.textContent = `安全投影截断：${truncationDetails.join(" · ")}；当前卡片仅展示截断后的安全投影。`;
        card.append(warning);
      }
      card.append(action);
      return card;
      });
    root.replaceChildren(...cards);
    if (!cards.length) root.textContent = "当前没有可安全展示的角色文档。";
  }

  function renderOverlayDocument() {
    const documentInfo = state.managedDocuments.find(
      (documentInfo) => documentInfo.id === "overlay_preferences",
    );
    const root = byId("overlay-fields");
    if (!documentInfo || !Array.isArray(documentInfo.fields)) {
      root.textContent = "Overlay owner metadata 不可用；写入保持关闭。";
      byId("overlay-operation-result").textContent = "";
      return;
    }
    const rows = documentInfo.fields.slice(0, 32).map((field) => {
      const row = document.createElement("div");
      row.className = "overlay-field";
      const label = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = boundedText(field.display_path || "overlay field", 128);
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      if (typeof field.minimum === "number") input.min = String(field.minimum);
      if (typeof field.maximum === "number") input.max = String(field.maximum);
      if (typeof field.current_value === "number" && Number.isFinite(field.current_value)) {
        input.value = String(field.current_value);
      }
      input.disabled = !state.overlayWriteEnabled || field.editable !== true;
      label.append(caption, input);
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "secondary-button";
      preview.textContent = "预览";
      preview.disabled = input.disabled || state.overlayWriteBusy;
      preview.addEventListener("click", () => previewOverlayField(field, input));
      row.append(label, preview);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = "Overlay owner 没有可编辑字段。";
    byId("overlay-operation-result").textContent = state.overlayWriteEnabled
      ? "每次只保存一个 fixed owner 字段；不会实时修改运行中的 Overlay。"
      : "服务端未开放 overlay_write；UI 保持只读。";
  }

  async function previewOverlayField(field, input) {
    if (!state.overlayWriteEnabled || state.overlayWriteBusy) return;
    const value = Number(input.value);
    if (!Number.isFinite(value)) {
      byId("overlay-operation-result").textContent = "请输入有限数字。";
      return;
    }
    state.overlayWriteBusy = true;
    renderOverlayDocument();
    try {
      const preview = await postJson("/api/v1/overlay/previews", {
        key: field.display_path,
        value,
      });
      if (!preview || typeof preview.preview_id !== "string") {
        throw new Error("PREVIEW_INVALID");
      }
      state.overlayPreview = preview;
      byId("overlay-preview-summary").textContent = `${boundedText(preview.key, 128)}：${text(preview.file_value_before)} → ${text(preview.file_value_after)}。${preview.effect_policy === "next_spica_launch" ? "下次 Spica 启动生效。" : "按 owner 策略生效。"}`;
      byId("overlay-preview-dialog").showModal();
    } catch (error) {
      state.overlayPreview = null;
      byId("overlay-operation-result").textContent = `预览失败：${boundedText(error.message, 64)}`;
    } finally {
      state.overlayWriteBusy = false;
      renderOverlayDocument();
      syncOverlayControls();
    }
  }

  function syncOverlayControls() {
    byId("overlay-preview-commit").disabled = !state.overlayWriteEnabled
      || state.overlayWriteBusy
      || !state.overlayPreview;
  }

  async function commitOverlayPreview() {
    const preview = state.overlayPreview;
    if (!state.overlayWriteEnabled || state.overlayWriteBusy || !preview) return;
    state.overlayWriteBusy = true;
    syncOverlayControls();
    try {
      await postJson("/api/v1/overlay/commits", {
        preview_id: preview.preview_id,
      });
      state.overlayPreview = null;
      byId("overlay-preview-dialog").close();
      await reloadStudioData();
      toast("Overlay 偏好已原子保存；下次 Spica 启动生效。");
    } catch (error) {
      state.overlayPreview = null;
      byId("overlay-preview-summary").textContent = `保存失败：${boundedText(error.message, 64)}。请重新预览。`;
    } finally {
      state.overlayWriteBusy = false;
      renderOverlayDocument();
      syncOverlayControls();
    }
  }

  function renderPluginStatuses() {
    const root = byId("plugin-statuses");
    const rows = state.pluginStatuses.slice(0, 256).map((plugin) => {
      const row = document.createElement("article");
      row.className = "readonly-status-row";
      row.setAttribute("role", "listitem");
      const head = document.createElement("div");
      head.className = "readonly-status-row__head";
      const name = document.createElement("strong");
      name.textContent = boundedText(plugin.name || "unknown plugin", 80);
      const packageStatus = ["present", "missing", "unsafe"].includes(
        plugin.package_status,
      ) ? plugin.package_status : "unknown";
      const status = document.createElement("span");
      status.className = `source-pill${packageStatus === "present" ? "" : " source-pill--env"}`;
      status.textContent = packageStatus;
      head.append(name, status);
      const semantics = document.createElement("p");
      const nextLaunch = plugin.next_launch_enabled === true
        ? "enabled"
        : plugin.next_launch_enabled === false
          ? "disabled"
          : "unavailable";
      semantics.textContent = `configured ${plugin.configured === true ? "yes" : "no"} · next launch ${nextLaunch} · ${boundedText(plugin.package_health_code || "PLUGIN_HEALTH_UNKNOWN", 96)}`;
      const owner = document.createElement("p");
      owner.textContent = `${boundedText(plugin.owner || "production owner", 96)} · ${boundedText(plugin.effect_policy || "owner-specific", 64)} · read only`;
      row.append(head, semantics, owner);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = "当前没有已配置的插件状态。";
  }

  function renderEnvironmentOnlySettings() {
    const root = byId("environment-only-settings");
    const rows = state.environmentOnlySettings.slice(0, 256).map((setting) => {
      const row = document.createElement("article");
      row.className = "readonly-status-row";
      row.setAttribute("role", "listitem");
      const head = document.createElement("div");
      head.className = "readonly-status-row__head";
      const name = document.createElement("strong");
      name.textContent = boundedText(
        setting.id || setting.environment_variable || "environment setting",
        96,
      );
      const status = document.createElement("span");
      status.className = setting.configured === true
        ? "source-pill source-pill--env"
        : "source-pill source-pill--default";
      status.textContent = setting.configured === true ? "configured" : "default";
      head.append(name, status);
      const source = document.createElement("p");
      source.textContent = `${boundedText(setting.environment_variable || "owner variable unavailable", 128)} · ${boundedText(setting.source_kind || "unknown", 64)} · ${boundedText(setting.environment_layer || "no override", 64)}`;
      const value = document.createElement("p");
      value.textContent = setting.configured === true
        ? `safe configured value: ${boundedText(text(setting.configured_value), 180)}`
        : "未配置；生产 owner 将使用自身默认语义。";
      const owner = document.createElement("p");
      const readOnlyContract = setting.editable === false
        ? boundedText(setting.unsupported_reason || "read_only", 96)
        : "unexpected owner contract · forced read only";
      owner.textContent = `${boundedText(setting.owner || "production owner", 96)} · ${boundedText(setting.effect_policy || "owner-specific", 64)} · ${readOnlyContract}`;
      row.append(head, source, value, owner);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = "服务端没有返回环境专属设置。";
  }

  function renderSensitiveStatus(meta) {
    const sensitive = meta.sensitive_document || {};
    const slots = sensitive.secret_slots || {};
    const sources = sensitive.secret_sources || {};
    const allowedSources = new Set(["inherited", "repo_dotenv", "parent_dotenv"]);
    const slotRows = Object.keys(slots).slice(0, 8).map((slot) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      const status = document.createElement("strong");
      const source = allowedSources.has(sources[slot]) ? sources[slot] : "source unknown";
      name.textContent = boundedText(slot, 64);
      status.textContent = `${slots[slot] === true ? "configured" : "not configured"} · ${source}`;
      item.append(name, status);
      return item;
    });
    byId("secret-slot-status").replaceChildren(...slotRows);
    if (!slotRows.length) byId("secret-slot-status").textContent = "配置状态不可用。";
    state.secretSlots = new Map(
      Object.keys(slots).slice(0, 32).map((slot) => [slot, slots[slot] === true]),
    );
    const slotSelect = byId("secret-slot-select");
    const selectedSlot = slotSelect.value;
    const options = Array.from(state.secretSlots).map(([slot, configured]) => {
      const option = document.createElement("option");
      option.value = slot;
      option.textContent = `${boundedText(slot, 64)} · ${configured ? "configured" : "not configured"}`;
      option.selected = slot === selectedSlot;
      return option;
    });
    slotSelect.replaceChildren(...options);

    const healthText = (documentStatus) => {
      const status = documentStatus || {};
      const legacyCount = Array.isArray(status.legacy_entries)
        ? status.legacy_entries.length
        : 0;
      return `${boundedText(status.permission_health || "UNKNOWN", 48)} · ${boundedText(status.parse_health || "UNKNOWN", 48)} · legacy ${legacyCount}`;
    };
    byId("repo-env-health").textContent = healthText(sensitive);
    byId("parent-env-health").textContent = healthText(
      meta.parent_environment_document,
    );

    const sourceCounts = new Map();
    state.fields.forEach((field) => {
      if (!field.environment_variable) return;
      const source = field.environment_layer || "no override";
      sourceCounts.set(source, (sourceCounts.get(source) || 0) + 1);
    });
    const sourceSummary = Array.from(sourceCounts.entries())
      .slice(0, 8)
      .map(([source, count]) => `${boundedText(source, 48)} ${count}`)
      .join(" · ");
    byId("override-source-summary").textContent = sourceSummary || "当前没有映射 override。";
    renderMappedOverrides(sensitive);
    syncSensitiveControls();
  }

  function renderMappedOverrides(sensitive) {
    const root = byId("mapped-overrides");
    const rows = Array.isArray(sensitive.managed_overrides)
      ? sensitive.managed_overrides
        .filter((item) => item && item.repo_defined === true)
        .slice(0, 64)
      : [];
    const cards = rows.map((item) => {
      const row = document.createElement("div");
      row.className = "mapped-override-row";
      const copy = document.createElement("div");
      const name = document.createElement("strong");
      const fields = document.createElement("small");
      name.textContent = boundedText(item.environment_variable, 128);
      fields.textContent = Array.isArray(item.affected_fields)
        ? item.affected_fields.slice(0, 8).map((field) => boundedText(field, 128)).join(" · ")
        : "owner field unavailable";
      copy.append(name, fields);
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "secondary-button";
      clear.textContent = "预览清理";
      clear.disabled = !state.sensitiveWriteEnabled || state.sensitiveWriteBusy;
      clear.addEventListener("click", () => previewMappedOverrideClear(item));
      row.append(copy, clear);
      return row;
    });
    root.replaceChildren(...cards);
    if (!cards.length) {
      root.textContent = state.sensitiveWriteEnabled
        ? "repo xiaosan.env 中没有可清理的 owner-mapped override。"
        : "写 capability 未开放；仅显示安全来源摘要。";
    }
  }

  async function previewMappedOverrideClear(item) {
    if (
      !state.sensitiveWriteEnabled
      || state.sensitiveWriteBusy
      || !item
      || item.repo_defined !== true
      || typeof item.environment_variable !== "string"
    ) return;
    await previewSensitiveCommand({
      kind: "clear_mapped_override",
      environment_variable: item.environment_variable,
    });
  }

  function syncSensitiveControls() {
    const slot = byId("secret-slot-select").value;
    const canWrite = state.sensitiveWriteEnabled
      && !state.sensitiveWriteBusy
      && state.secretSlots.has(slot);
    byId("secret-slot-select").disabled = !state.sensitiveWriteEnabled
      || state.sensitiveWriteBusy
      || state.secretSlots.size === 0;
    byId("secret-value-input").disabled = !canWrite;
    byId("secret-set-preview").disabled = !canWrite
      || byId("secret-value-input").value.length === 0;
    byId("secret-clear-preview").disabled = !canWrite
      || state.secretSlots.get(slot) !== true;
    const clearRequired = Boolean(
      state.sensitivePreview
      && state.sensitivePreview.command_kind === "clear_secret",
    );
    byId("sensitive-clear-confirm-row").hidden = !clearRequired;
    byId("sensitive-preview-commit").disabled = !state.sensitiveWriteEnabled
      || state.sensitiveWriteBusy
      || !state.sensitivePreview
      || (clearRequired && !byId("sensitive-clear-confirm").checked);
  }

  function renderSensitivePreview(preview) {
    const details = [
      ["Command", preview.command_kind],
      ["Target", preview.target],
      ["Secret change", preview.secret_change || "—"],
      ["Affected fields", Array.isArray(preview.affected_fields) ? preview.affected_fields.join(" · ") : "—"],
      ["Before next launch", text(preview.before_next_launch)],
      ["After next launch", text(preview.after_next_launch)],
      ["Winning source", `${text(preview.winning_source_before)} → ${text(preview.winning_source_after)}`],
      ["Still shadowed", text(preview.still_shadowed)],
      ["Permission hardening", text(preview.permission_hardening)],
      ["Resolution error", `${text(preview.resolution_error_before)} → ${text(preview.resolution_error_after)}`],
    ];
    const list = document.createElement("dl");
    list.className = "detail-list";
    details.forEach(([name, value]) => {
      const item = document.createElement("div");
      item.className = "detail-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = boundedText(value, 256);
      item.append(term, description);
      list.append(item);
    });
    byId("sensitive-preview-summary").replaceChildren(list);
  }

  async function previewSensitiveCommand(command) {
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy) return;
    state.sensitiveWriteBusy = true;
    syncSensitiveControls();
    try {
      const preview = await postJson("/api/v1/sensitive/previews", { command });
      if (
        !preview
        || typeof preview.preview_id !== "string"
        || typeof preview.command_kind !== "string"
      ) throw new Error("PREVIEW_INVALID");
      state.sensitivePreview = preview;
      byId("sensitive-clear-confirm").checked = false;
      renderSensitivePreview(preview);
      byId("sensitive-preview-dialog").showModal();
    } catch (error) {
      state.sensitivePreview = null;
      byId("sensitive-operation-result").textContent = `预览失败：${boundedText(error.message, 64)}`;
    } finally {
      state.sensitiveWriteBusy = false;
      syncSensitiveControls();
    }
  }

  async function previewSecretSet() {
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy) return;
    const slot = byId("secret-slot-select").value;
    const secretInput = byId("secret-value-input");
    const value = secretInput.value;
    secretInput.value = "";
    syncSensitiveControls();
    if (!state.secretSlots.has(slot) || !value) return;
    await previewSensitiveCommand({ kind: "set_secret", slot, value });
  }

  async function previewSecretClear() {
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy) return;
    const slot = byId("secret-slot-select").value;
    if (!state.secretSlots.has(slot) || state.secretSlots.get(slot) !== true) return;
    await previewSensitiveCommand({ kind: "clear_secret", slot });
  }

  async function commitSensitivePreview() {
    const preview = state.sensitivePreview;
    if (!state.sensitiveWriteEnabled || state.sensitiveWriteBusy || !preview) return;
    state.sensitiveWriteBusy = true;
    syncSensitiveControls();
    try {
      let confirmationReceipt = null;
      if (preview.command_kind === "clear_secret") {
        if (!byId("sensitive-clear-confirm").checked) return;
        const confirmation = await postJson(
          `/api/v1/sensitive/previews/${encodeURIComponent(preview.preview_id)}/confirm-clear`,
          {},
        );
        if (!confirmation || typeof confirmation.confirmation_receipt !== "string") {
          throw new Error("CONFIRMATION_INVALID");
        }
        confirmationReceipt = confirmation.confirmation_receipt;
      }
      const body = { preview_id: preview.preview_id };
      if (confirmationReceipt) body.confirmation_receipt = confirmationReceipt;
      await postJson("/api/v1/sensitive/commits", body);
      state.sensitivePreview = null;
      byId("sensitive-clear-confirm").checked = false;
      byId("sensitive-preview-dialog").close();
      await reloadStudioData();
      toast("敏感文档已按 owner 协议保存；现有 secret 明文从未返回页面。");
    } catch (error) {
      state.sensitivePreview = null;
      byId("sensitive-operation-result").textContent = `提交失败：${boundedText(error.message, 64)}。请重新预览。`;
    } finally {
      state.sensitiveWriteBusy = false;
      renderSensitiveStatus(state.meta || {});
      syncSensitiveControls();
    }
  }

  function restoreLaneEnabled(lane) {
    if (!state.rollbackEnabled) return false;
    if (lane === "app") return state.appWriteEnabled && state.rollbackEnabled;
    if (lane === "overlay") return state.overlayWriteEnabled && state.rollbackEnabled;
    if (lane === "sensitive") {
      return state.sensitiveWriteEnabled && state.rollbackEnabled;
    }
    return false;
  }

  function syncAppManualRepairGuidance(hasValidRestorePoint) {
    const guidance = byId("app-manual-repair-guidance");
    guidance.hidden = !state.appRecoveryOnly || hasValidRestorePoint === true;
  }

  function renderRestorePoints(lane, points) {
    const root = byId(`restore-${lane}`);
    const availablePoints = Array.isArray(points) ? points : [];
    if (lane === "app") {
      syncAppManualRepairGuidance(availablePoints.length > 0);
    }
    if (!restoreLaneEnabled(lane)) {
      root.textContent = "该 lane 的写入或 rollback capability 未开放。";
      return;
    }
    const rows = availablePoints.slice(0, 5).map((point) => {
      const row = document.createElement("div");
      row.className = "restore-point-row";
      const copy = document.createElement("div");
      const id = document.createElement("strong");
      const created = document.createElement("small");
      id.textContent = `opaque · ${boundedText(point.restore_point_id, 64)}`;
      const milliseconds = Number(point.created_at_ns) / 1000000;
      created.textContent = Number.isFinite(milliseconds)
        ? new Date(milliseconds).toLocaleString()
        : "时间不可用";
      copy.append(id, created);
      const prepare = document.createElement("button");
      prepare.type = "button";
      prepare.className = "secondary-button";
      prepare.textContent = "准备回滚";
      prepare.disabled = state.rollbackBusy;
      prepare.addEventListener("click", () => prepareRollback(lane, point));
      row.append(copy, prepare);
      return row;
    });
    root.replaceChildren(...rows);
    if (!rows.length) root.textContent = "暂无 RestorePoint。";
  }

  async function loadRestorePoints() {
    for (const lane of Object.keys(RESTORE_ROUTES)) {
      if (!restoreLaneEnabled(lane)) {
        renderRestorePoints(lane, []);
        continue;
      }
      try {
        const payload = await getJson(RESTORE_ROUTES[lane].list);
        renderRestorePoints(lane, payload.restore_points);
      } catch (_error) {
        byId(`restore-${lane}`).textContent = "RestorePoint 暂时不可查询。";
      }
    }
  }

  function renderRollbackPreview(lane, confirmation) {
    const lines = [];
    lines.push(["Lane", lane]);
    lines.push(["RestorePoint", confirmation.restore_point_id]);
    if (lane === "sensitive") {
      lines.push(["Scope", "整个敏感 ManagedDocument"]);
      const secretChanges = Array.isArray(confirmation.secret_changes)
        ? confirmation.secret_changes.map((item) => `${item.slot}: ${item.change}`)
        : [];
      lines.push(["Secret slots", secretChanges.join(" · ") || "unchanged"]);
      const overrides = Array.isArray(confirmation.override_changes)
        ? confirmation.override_changes.map((item) => (
          `${item.environment_variable}: ${text(item.before_next_launch)} → ${text(item.after_next_launch)}`
          + ` · source ${text(item.winning_source_before)} → ${text(item.winning_source_after)}`
          + `${item.still_shadowed ? " · still shadowed" : ""}`
        ))
        : [];
      lines.push(["Mapped overrides", overrides.join(" · ") || "unchanged"]);
      lines.push(["Unmanaged content", `${text(confirmation.unmanaged_content_changed)} · ${text(confirmation.unmanaged_change_count)}`]);
      lines.push(["Permission hardening", text(confirmation.permission_hardening)]);
    } else {
      lines.push([
        "Changed fields",
        Array.isArray(confirmation.changed_fields)
          ? confirmation.changed_fields.join(" · ") || "none"
          : "unavailable",
      ]);
      if (lane === "app") {
        lines.push([
          "Next-launch changes",
          Array.isArray(confirmation.next_launch_changed_fields)
            ? confirmation.next_launch_changed_fields.join(" · ") || "none"
            : "unavailable",
        ]);
      }
      lines.push(["Unmanaged content", `${text(confirmation.unmanaged_content_changed)} · ${text(confirmation.unmanaged_change_count)}`]);
      if (confirmation.truncation && confirmation.truncation.truncated === true) {
        const changedOmitted = Number.isSafeInteger(
          confirmation.truncation.changed_fields_omitted,
        ) ? confirmation.truncation.changed_fields_omitted : "unavailable";
        const nextLaunchOmitted = Number.isSafeInteger(
          confirmation.truncation.next_launch_changed_fields_omitted,
        ) ? confirmation.truncation.next_launch_changed_fields_omitted : "unavailable";
        lines.push([
          "Truncation",
          lane === "app"
            ? `changed fields omitted ${changedOmitted} · next-launch fields omitted ${nextLaunchOmitted}`
            : `changed fields omitted ${changedOmitted}`,
        ]);
      }
    }
    lines.push([
      "Resolution error",
      `${text(confirmation.resolution_error_before)} → ${text(confirmation.resolution_error_after)}`,
    ]);
    const list = document.createElement("dl");
    list.className = "detail-list";
    lines.forEach(([name, value]) => {
      const item = document.createElement("div");
      item.className = "detail-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = name;
      description.textContent = boundedText(value, 512);
      item.append(term, description);
      list.append(item);
    });
    byId("rollback-preview-summary").replaceChildren(list);
  }

  async function prepareRollback(lane, point) {
    if (
      !restoreLaneEnabled(lane)
      || state.rollbackBusy
      || !point
      || typeof point.restore_point_id !== "string"
    ) return;
    state.rollbackBusy = true;
    syncRollbackControls();
    try {
      const routes = RESTORE_ROUTES[lane];
      const confirmation = await postJson(
        `${routes.list}/${encodeURIComponent(point.restore_point_id)}/prepare-rollback`,
        {},
      );
      if (
        !confirmation
        || typeof confirmation.confirmation_receipt !== "string"
        || confirmation.restore_point_id !== point.restore_point_id
      ) throw new Error("CONFIRMATION_INVALID");
      state.rollbackConfirmation = { lane, confirmation };
      byId("rollback-confirm").checked = false;
      renderRollbackPreview(lane, confirmation);
      byId("rollback-preview-dialog").showModal();
    } catch (error) {
      state.rollbackConfirmation = null;
      byId("restore-operation-result").textContent = `回滚预览失败：${boundedText(error.message, 64)}`;
    } finally {
      state.rollbackBusy = false;
      syncRollbackControls();
    }
  }

  function syncRollbackControls() {
    byId("rollback-preview-commit").disabled = state.rollbackBusy
      || !state.rollbackConfirmation
      || !byId("rollback-confirm").checked;
  }

  async function commitRollback() {
    const record = state.rollbackConfirmation;
    if (
      !record
      || !restoreLaneEnabled(record.lane)
      || state.rollbackBusy
      || !byId("rollback-confirm").checked
    ) return;
    state.rollbackBusy = true;
    syncRollbackControls();
    try {
      const confirmation = record.confirmation;
      await postJson(RESTORE_ROUTES[record.lane].rollback, {
        confirmation_receipt: confirmation.confirmation_receipt,
      });
      state.rollbackConfirmation = null;
      byId("rollback-confirm").checked = false;
      byId("rollback-preview-dialog").close();
      await reloadStudioData();
      toast(`${record.lane} 文档已按语义确认回滚。`);
    } catch (error) {
      state.rollbackConfirmation = null;
      byId("restore-operation-result").textContent = `回滚失败：${boundedText(error.message, 64)}。请重新准备。`;
    } finally {
      state.rollbackBusy = false;
      syncRollbackControls();
      await loadRestorePoints();
    }
  }

  function applyMeta(meta) {
    state.meta = meta;
    const health = meta.health || {};
    const issues = Array.isArray(health.issues) ? health.issues : [];
    byId("config-health").textContent = issues.length ? `${issues.length} 项需要注意` : "Owner resolution healthy";
    byId("health-detail").textContent = issues.length ? String(issues[0].message || issues[0].code) : "生产 owner 已返回一致的下一次启动快照。";
    byId("health-meter").className = issues.length ? "is-warning" : "is-healthy";
    const hasOverride = state.fields.some((field) => field.file_value_shadowed);
    byId("override-warning").hidden = !hasOverride;
    state.appWriteEnabled = Boolean(
      meta.capabilities && meta.capabilities.app_config_write === true,
    );
    state.overlayWriteEnabled = Boolean(
      meta.capabilities && meta.capabilities.overlay_write === true,
    );
    state.sensitiveWriteEnabled = Boolean(
      meta.capabilities && meta.capabilities.sensitive_write === true,
    );
    state.rollbackEnabled = Boolean(
      meta.capabilities && meta.capabilities.rollback === true,
    );
    state.selfCheckEnabled = Boolean(
      meta.capabilities && meta.capabilities.self_check === true,
    );
    state.selfCheckJobsEnabled = Boolean(
      meta.capabilities && meta.capabilities.self_check_jobs === true,
    );
    renderSensitiveStatus(meta);
    if (!state.selfCheckJobsEnabled) {
      byId("self-check-job-summary").textContent = "服务端 capability 尚未开放；没有启动任何自检任务。";
    } else if (!state.selfCheckEnabled) {
      byId("self-check-job-summary").textContent = "新的自检已安全停用；仍可查询或取消已有任务。";
    }
    syncAppControls();
    syncOverlayControls();
    syncSensitiveControls();
    syncRollbackControls();
    syncSelfCheckControls();
  }

  function validSelfCheckJob(job) {
    return Boolean(
      job
      && typeof job === "object"
      && typeof job.job_id === "string"
      && /^[A-Za-z0-9_-]{8,128}$/.test(job.job_id)
      && typeof job.status === "string"
      && JOB_STATUSES.has(job.status)
      && (job.mode === "light" || job.mode === "full")
      && Array.isArray(job.progress)
      && Array.isArray(job.results),
    );
  }

  function replaceSummaryList(root, items, emptyMessage) {
    const children = items.map((label) => {
      const item = document.createElement("li");
      item.textContent = label;
      return item;
    });
    if (!children.length) {
      const empty = document.createElement("li");
      empty.textContent = emptyMessage;
      children.push(empty);
    }
    root.replaceChildren(...children);
  }

  function stopSelfCheckPolling() {
    if (state.selfCheckPollTimer !== null) {
      window.clearTimeout(state.selfCheckPollTimer);
      state.selfCheckPollTimer = null;
    }
  }

  function scheduleSelfCheckPoll() {
    stopSelfCheckPolling();
    if (
      !state.selfCheckJobsEnabled
      || !state.selfCheckJob
      || !ACTIVE_JOB_STATUSES.has(state.selfCheckJob.status)
    ) return;
    state.selfCheckPollTimer = window.setTimeout(pollSelfCheck, SELF_CHECK_POLL_MS);
  }

  function renderSelfCheckJob(job) {
    if (!validSelfCheckJob(job)) throw new Error("SELF_CHECK_JOB_INVALID");
    state.selfCheckJob = job;
    const active = ACTIVE_JOB_STATUSES.has(job.status);
    const duration = Number.isFinite(job.duration_s) && job.duration_s >= 0
      ? `${Math.min(job.duration_s, 86400).toFixed(1)}s`
      : "—";
    const status = boundedText(job.status, 32);
    const badge = byId("self-check-status-badge");
    badge.textContent = status;
    badge.dataset.status = status;
    byId("self-check-job-status").textContent = status;
    byId("self-check-job-summary").textContent = `${job.mode === "full" ? "重检查" : "轻量检查"} · ${duration}${job.error_code ? ` · ${boundedText(job.error_code, 64)}` : ""}`;
    byId("self-check-summary").textContent = status;
    byId("self-check-monitor").setAttribute("aria-busy", String(active));

    const progress = job.progress.slice(0, MAX_RENDERED_PROGRESS).map((item) => {
      const name = item && typeof item.name === "string" ? item.name : "unknown";
      return `${boundedText(name, 48)} · RUNNING`;
    });
    replaceSummaryList(byId("self-check-progress"), progress, active ? "等待进度" : "没有运行中的检查");

    const results = job.results.slice(0, MAX_RENDERED_RESULTS).map((item) => {
      const name = item && typeof item.name === "string" ? boundedText(item.name, 48) : "unknown";
      const resultStatus = item && RESULT_STATUSES.has(item.status) ? item.status : "UNVERIFIED";
      const reason = item && typeof item.reason === "string" ? boundedText(item.reason, 160) : "";
      return `${name} · ${resultStatus}${reason ? ` — ${reason}` : ""}`;
    });
    replaceSummaryList(byId("self-check-results"), results, "暂无结果");

    const totalLines = Number.isSafeInteger(job.stderr_total_line_count)
      ? Math.min(Math.max(job.stderr_total_line_count, 0), 1000000)
      : 0;
    byId("self-check-output-note").textContent = totalLines
      ? `已安全丢弃 ${totalLines} 行 stderr${job.stderr_truncated ? "（计数已截断）" : ""}；原文不会进入页面。`
      : "没有向页面返回原始 stderr。";
    syncSelfCheckControls();
    scheduleSelfCheckPoll();
  }

  async function pollSelfCheck() {
    const jobId = state.selfCheckJob && state.selfCheckJob.job_id;
    if (!state.selfCheckJobsEnabled || !jobId) return;
    try {
      const job = await getJson(`/api/v1/self-check/jobs/${encodeURIComponent(jobId)}`);
      renderSelfCheckJob(job);
    } catch (_error) {
      toast("自检状态暂时无法刷新；将继续重试。");
      if (state.selfCheckJob && ACTIVE_JOB_STATUSES.has(state.selfCheckJob.status)) {
        state.selfCheckPollTimer = window.setTimeout(pollSelfCheck, SELF_CHECK_POLL_MS);
      }
    }
  }

  async function loadSelfChecks() {
    if (!state.selfCheckJobsEnabled) return;
    const payload = await getJson("/api/v1/self-check/jobs");
    const jobs = payload && Array.isArray(payload.jobs) ? payload.jobs.slice(0, 20) : [];
    const job = jobs.find((item) => validSelfCheckJob(item) && ACTIVE_JOB_STATUSES.has(item.status))
      || jobs.find(validSelfCheckJob);
    if (job) renderSelfCheckJob(job);
  }

  function selectedHeavyChecks() {
    const selected = new Set(
      all('input[name="self-check-heavy"]:checked').map((input) => input.value),
    );
    return HEAVY_CHECKS.filter((name) => selected.has(name));
  }

  function syncSelfCheckControls() {
    const jobActive = Boolean(
      state.selfCheckJob && ACTIVE_JOB_STATUSES.has(state.selfCheckJob.status),
    );
    const canStart = state.selfCheckEnabled && !state.selfCheckBusy && !jobActive;
    byId("self-check-light-start").disabled = !canStart;
    byId("self-check-heavy-controls").disabled = !canStart;

    const selected = selectedHeavyChecks();
    const enableLlm = byId("self-check-enable-llm");
    const llmSelected = selected.includes("llm");
    enableLlm.disabled = !canStart || !llmSelected;
    if (!llmSelected) enableLlm.checked = false;
    const confirmLlm = byId("self-check-confirm-llm");
    confirmLlm.disabled = !canStart || !enableLlm.checked;
    if (!enableLlm.checked) confirmLlm.checked = false;

    const includeDisabled = byId("self-check-include-disabled");
    const confirmIncludeDisabled = byId("self-check-confirm-include-disabled");
    confirmIncludeDisabled.disabled = !canStart || !includeDisabled.checked;
    if (!includeDisabled.checked) confirmIncludeDisabled.checked = false;

    const allowDownloads = byId("self-check-allow-downloads");
    const confirmDownloads = byId("self-check-confirm-downloads");
    confirmDownloads.disabled = !canStart || !allowDownloads.checked;
    if (!allowDownloads.checked) confirmDownloads.checked = false;

    const confirmed = byId("self-check-confirm-heavy").checked
      && (!enableLlm.checked || confirmLlm.checked)
      && (!includeDisabled.checked || confirmIncludeDisabled.checked)
      && (!allowDownloads.checked || confirmDownloads.checked);
    byId("self-check-heavy-start").disabled = !canStart || !selected.length || !confirmed;

    const cancel = byId("self-check-cancel");
    const canCancel = state.selfCheckJobsEnabled
      && jobActive
      && !state.selfCheckBusy
      && state.selfCheckJob.status !== "CANCELLING";
    cancel.hidden = !state.selfCheckJobsEnabled || !jobActive;
    cancel.disabled = !canCancel;
  }

  function heavyCommand() {
    return {
      mode: "full",
      only: selectedHeavyChecks(),
      llm: byId("self-check-enable-llm").checked,
      include_disabled: byId("self-check-include-disabled").checked,
      allow_model_downloads: byId("self-check-allow-downloads").checked,
    };
  }

  function heavyAcknowledgements() {
    return {
      full: byId("self-check-confirm-heavy").checked,
      llm: byId("self-check-confirm-llm").checked,
      include_disabled: byId("self-check-confirm-include-disabled").checked,
      model_downloads: byId("self-check-confirm-downloads").checked,
    };
  }

  function confirmationMatches(command, confirmation) {
    const semantic = confirmation && confirmation.semantic;
    return Boolean(
      semantic
      && semantic.mode === "full"
      && Array.isArray(semantic.checks)
      && semantic.checks.length === command.only.length
      && semantic.checks.every((name, index) => name === command.only[index])
      && semantic.llm === command.llm
      && semantic.include_disabled === command.include_disabled
      && semantic.allow_model_downloads === command.allow_model_downloads,
    );
  }

  async function runLightSelfCheck() {
    if (!state.selfCheckEnabled || state.selfCheckBusy) return;
    state.selfCheckBusy = true;
    syncSelfCheckControls();
    try {
      const job = await postJson("/api/v1/self-check/jobs", { mode: "light" });
      renderSelfCheckJob(job);
      toast("轻量自检已排队。");
    } catch (error) {
      toast(`轻量自检未启动：${boundedText(error.message, 64)}`);
    } finally {
      state.selfCheckBusy = false;
      syncSelfCheckControls();
    }
  }

  async function runHeavySelfCheck() {
    if (!state.selfCheckEnabled || state.selfCheckBusy) return;
    const command = heavyCommand();
    if (!command.only.length || byId("self-check-heavy-start").disabled) return;
    state.selfCheckBusy = true;
    syncSelfCheckControls();
    try {
      const confirmation = await postJson("/api/v1/self-check/confirm", {
        ...command,
        acknowledgements: heavyAcknowledgements(),
      });
      if (
        typeof confirmation.confirmation_receipt !== "string"
        || !confirmation.confirmation_receipt
        || !confirmationMatches(command, confirmation)
      ) {
        throw new Error("CONFIRMATION_INVALID");
      }
      const job = await postJson("/api/v1/self-check/jobs", {
        ...command,
        confirmation_receipt: confirmation.confirmation_receipt,
      });
      byId("self-check-confirm-heavy").checked = false;
      byId("self-check-confirm-llm").checked = false;
      byId("self-check-confirm-include-disabled").checked = false;
      byId("self-check-confirm-downloads").checked = false;
      renderSelfCheckJob(job);
      toast("重检查已由服务端确认并排队。");
    } catch (error) {
      toast(`重检查未启动：${boundedText(error.message, 64)}`);
    } finally {
      state.selfCheckBusy = false;
      syncSelfCheckControls();
    }
  }

  async function cancelSelfCheck() {
    const jobId = state.selfCheckJob && state.selfCheckJob.job_id;
    if (!state.selfCheckJobsEnabled || !jobId || state.selfCheckBusy) return;
    state.selfCheckBusy = true;
    syncSelfCheckControls();
    try {
      const job = await postJson(
        `/api/v1/self-check/jobs/${encodeURIComponent(jobId)}/cancel`,
        {},
      );
      renderSelfCheckJob(job);
      toast("已请求取消自检任务。");
    } catch (error) {
      toast(`取消失败：${boundedText(error.message, 64)}`);
    } finally {
      state.selfCheckBusy = false;
      syncSelfCheckControls();
    }
  }

  function showView(name) {
    all(".nav-item").forEach((button) => {
      const active = button.dataset.view === name;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    let activePanel = null;
    all(".view-panel").forEach((panel) => {
      const active = panel.dataset.panel === name;
      panel.classList.toggle("is-active", active);
      if (active) activePanel = panel;
    });
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    byId("workspace").scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
    const heading = activePanel && activePanel.querySelector("h1");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  }

  function bindUi() {
    all(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
    all("[data-view-target]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
    all(".view-button").forEach((button) => button.addEventListener("click", () => {
      state.level = button.dataset.level;
      all(".view-button").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      renderCatalog();
    }));
    byId("field-search").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderCatalog();
    });
    byId("field-editor-set").addEventListener("click", queueAppSet);
    byId("field-editor-unset").addEventListener("click", queueAppUnset);
    byId("app-preview-button").addEventListener("click", previewAppChanges);
    byId("app-preview-commit").addEventListener("click", commitAppPreview);
    byId("app-preview-dialog").addEventListener("close", () => {
      state.appPreview = null;
      syncAppControls();
    });
    byId("overlay-preview-commit").addEventListener("click", commitOverlayPreview);
    byId("overlay-preview-dialog").addEventListener("close", () => {
      state.overlayPreview = null;
      syncOverlayControls();
    });
    byId("secret-slot-select").addEventListener("change", syncSensitiveControls);
    byId("secret-value-input").addEventListener("input", syncSensitiveControls);
    byId("secret-set-preview").addEventListener("click", previewSecretSet);
    byId("secret-clear-preview").addEventListener("click", previewSecretClear);
    byId("sensitive-clear-confirm").addEventListener("change", syncSensitiveControls);
    byId("sensitive-preview-commit").addEventListener("click", commitSensitivePreview);
    byId("sensitive-preview-dialog").addEventListener("close", () => {
      state.sensitivePreview = null;
      byId("sensitive-clear-confirm").checked = false;
      syncSensitiveControls();
    });
    byId("rollback-confirm").addEventListener("change", syncRollbackControls);
    byId("rollback-preview-commit").addEventListener("click", commitRollback);
    byId("rollback-preview-dialog").addEventListener("close", () => {
      state.rollbackConfirmation = null;
      byId("rollback-confirm").checked = false;
      syncRollbackControls();
    });
    byId("self-check-light-start").addEventListener("click", runLightSelfCheck);
    byId("self-check-heavy-start").addEventListener("click", runHeavySelfCheck);
    byId("self-check-cancel").addEventListener("click", cancelSelfCheck);
    [
      ...all('input[name="self-check-heavy"]'),
      byId("self-check-enable-llm"),
      byId("self-check-include-disabled"),
      byId("self-check-allow-downloads"),
      byId("self-check-confirm-heavy"),
      byId("self-check-confirm-llm"),
      byId("self-check-confirm-include-disabled"),
      byId("self-check-confirm-downloads"),
    ].forEach((input) => input.addEventListener("change", syncSelfCheckControls));
    syncAppControls();
    syncOverlayControls();
    syncSensitiveControls();
    syncRollbackControls();
    syncSelfCheckControls();
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        byId("field-search").focus();
      }
    });
  }

  async function start() {
    bindUi();
    try {
      await establishSession();
      await reloadStudioData();
      setSession("ok", "本机安全会话");
      if (state.selfCheckJobsEnabled) {
        try {
          await loadSelfChecks();
        } catch (_error) {
          toast("已有自检任务暂时无法查询。");
        }
      }
    } catch (_error) {
      setSession("error", "会话不可用");
      byId("config-health").textContent = "无法读取 Catalog";
      byId("health-detail").textContent = "请从 Config Studio 启动器重新打开此页面。";
      byId("featured-fields").setAttribute("aria-busy", "false");
      byId("featured-fields").textContent = "安全会话未建立；未读取任何配置。";
      toast("Config Studio 无法建立安全的本机会话。");
    }
  }

  async function reloadStudioData() {
    const [meta, catalog] = await Promise.all([
      getJson("/api/v1/meta"),
      getJson("/api/v1/catalog"),
    ]);
    state.fields = Array.isArray(catalog.fields) ? catalog.fields : [];
    state.catalogFieldsComplete = catalog.fields_complete === true;
    state.managedDocuments = Array.isArray(catalog.managed_documents)
      ? catalog.managed_documents
      : [];
    const managedDocumentsOmitted = catalog.truncation
      && typeof catalog.truncation === "object"
      ? catalog.truncation.managed_documents_omitted
      : null;
    state.managedDocumentsOmitted = Number.isSafeInteger(managedDocumentsOmitted)
      && managedDocumentsOmitted >= 0
      && managedDocumentsOmitted <= MAX_STRUCTURED_ITEMS
      ? managedDocumentsOmitted
      : null;
    state.pluginStatuses = Array.isArray(catalog.plugin_statuses)
      ? catalog.plugin_statuses
      : [];
    state.environmentOnlySettings = Array.isArray(
      catalog.environment_only_settings,
    ) ? catalog.environment_only_settings : [];
    const metaHealth = meta && typeof meta.health === "object" ? meta.health : null;
    state.appRecoveryOnly = catalog.recovery_only !== false;
    if (!metaHealth || metaHealth.recovery_only !== false) {
      state.appRecoveryOnly = true;
    }
    state.selectedField = null;
    byId("field-editor").hidden = true;
    applyMeta(meta);
    renderAppRecoveryState();
    renderCatalogCompletenessState();
    renderFeatured();
    renderCategories();
    renderCatalog();
    renderSwitches();
    renderManagedDocuments();
    renderOverlayDocument();
    renderPluginStatuses();
    renderEnvironmentOnlySettings();
    await loadRestorePoints();
  }

  window.addEventListener("DOMContentLoaded", start, { once: true });
})();
