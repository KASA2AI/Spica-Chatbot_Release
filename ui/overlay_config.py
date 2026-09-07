from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from spica.adapters.config_studio.platform import current_platform_capabilities
from spica.config.document_transaction import (
    DocumentConflictError,
    DocumentTransactionError,
    ManagedDocumentTransaction,
)
from spica.config.overlay_owner import (
    OverlayConfig,
    overlay_field_bounds,
    resolve_overlay_config,
)
from ui.widgets.common import (
    DEFAULT_DIALOGUE_OPACITY,
    MAX_DIALOGUE_OPACITY,
    MIN_DIALOGUE_OPACITY,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("overlay_config.json")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BACKUP_ROOT = _REPO_ROOT / "spica_data" / "config_studio" / "backups"
_DIALOGUE_BOX_VISIBLE_KEY = "dialogue_box_visible"
_DIALOGUE_OPACITY_KEY = "dialogue_opacity"


def _load_overlay_document(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    config_path = path or CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config_path, {}
    except Exception as exc:
        logger.warning("event=overlay_config_fallback path=%s reason=%s", config_path, exc)
        return config_path, {}

    if not isinstance(raw, dict):
        logger.warning("event=overlay_config_fallback path=%s reason=not_object", config_path)
        raw = {}
    return config_path, raw


def load_overlay_preferences(
    path: Path | None = None,
) -> tuple[OverlayConfig, bool]:
    """Resolve managed values and the UI-only key from one file snapshot."""

    config_path, raw = _load_overlay_document(path)
    value = raw.get(_DIALOGUE_BOX_VISIBLE_KEY, True)
    if type(value) is bool:
        visible = value
    else:
        logger.warning(
            "event=dialogue_visibility_fallback path=%s reason=type_mismatch",
            config_path,
        )
        visible = True
    return resolve_overlay_config(raw), visible


def load_overlay_config(path: Path | None = None) -> OverlayConfig:
    return load_overlay_preferences(path)[0]


def load_dialogue_box_visible(path: Path | None = None) -> bool:
    """Read the UI-only unmanaged visibility key with strict bool semantics."""

    return load_overlay_preferences(path)[1]


def load_dialogue_opacity(path: Path | None = None) -> float:
    """UI-only appearance preference, like dialogue visibility; preserve Studio ownership."""
    _, raw = _load_overlay_document(path)
    value = raw.get(_DIALOGUE_OPACITY_KEY, DEFAULT_DIALOGUE_OPACITY)
    if type(value) not in (int, float) or not math.isfinite(value):
        return DEFAULT_DIALOGUE_OPACITY
    return max(MIN_DIALOGUE_OPACITY, min(MAX_DIALOGUE_OPACITY, float(value)))


def save_dialogue_opacity(
    opacity: float,
    path: Path | None = None,
    *,
    backup_root: Path | None = None,
) -> bool:
    if type(opacity) not in (int, float) or not math.isfinite(opacity):
        return False
    value = max(MIN_DIALOGUE_OPACITY, min(MAX_DIALOGUE_OPACITY, float(opacity)))
    return _save_overlay_document_value(
        _DIALOGUE_OPACITY_KEY, value, path, backup_root=backup_root,
    )


def save_overlay_config_value(
    key: str,
    value: Any,
    path: Path | None = None,
    *,
    backup_root: Path | None = None,
) -> bool:
    """Persist one overlay-config key through the shared transaction owner.

    Existing desktop callers use this narrow merge-safe seam; Config Studio has
    its own typed preview/commit seam over the same document transaction. Every
    other hand-edited key is preserved. This function never raises: a missing
    file becomes a fresh object, an unreadable/corrupt file is left intact, and
    a failed write degrades to session-only. It returns True only when the value
    was actually persisted.
    """
    if key not in OverlayConfig.__dataclass_fields__:
        logger.warning("event=overlay_config_save_skip reason=unsupported_key")
        return False
    return _save_overlay_document_value(
        key,
        value,
        path,
        backup_root=backup_root,
    )


def save_dialogue_box_visible(
    visible: bool,
    path: Path | None = None,
    *,
    backup_root: Path | None = None,
) -> bool:
    """Persist the UI-only key without adding it to Config Studio ownership."""

    if type(visible) is not bool:
        logger.warning("event=dialogue_visibility_save_skip reason=type_mismatch")
        return False
    return _save_overlay_document_value(
        _DIALOGUE_BOX_VISIBLE_KEY,
        visible,
        path,
        backup_root=backup_root,
    )


def _save_overlay_document_value(
    key: str,
    value: Any,
    path: Path | None,
    *,
    backup_root: Path | None,
) -> bool:
    config_path = path or CONFIG_PATH
    if backup_root is not None:
        state_root = backup_root
    elif path is not None:
        # Injected/sandbox documents must never create production RestorePoints.
        state_root = config_path.parent / ".config_studio_backups"
    else:
        state_root = _DEFAULT_BACKUP_ROOT
    transaction = ManagedDocumentTransaction(
        config_path,
        backup_root=state_root,
        lock_root=state_root.parent / "locks",
        retention=5,
        platform_capabilities=current_platform_capabilities(),
    )
    for attempt in range(2):
        try:
            captured = transaction.preview(b"").current
            if captured.revision.exists:
                raw = json.loads(captured.content.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not_object")
            else:
                raw = {}
            raw[key] = value
            candidate = (
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            committed = transaction.commit(
                candidate,
                expected_revision=captured.revision,
            )
            if committed.maintenance_code is not None:
                logger.warning(
                    "event=overlay_config_save_degraded reason=%s",
                    committed.maintenance_code,
                )
            return True
        except DocumentConflictError:
            if attempt == 0:
                continue
            logger.warning("event=overlay_config_save_failed reason=DOCUMENT_CONFLICT")
            return False
        except (DocumentTransactionError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", "DOCUMENT_INVALID")
            logger.warning("event=overlay_config_save_skip reason=%s", code)
            return False
        except OSError:
            logger.warning("event=overlay_config_save_failed reason=DOCUMENT_IO_ERROR")
            return False
    return False
