"""Secret configuration -- the only permanent reader of secret env vars.

INVARIANT (CLAUDE.md #4): API keys and other secrets live in the environment /
``xiaosan.env``, never in plain config files. Business code obtains them via
``load_secrets()``; only this module (and ``manager.py``) may read ``os.getenv``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv
from dotenv.parser import parse_stream
from dotenv.variables import parse_variables

from spica.config.env_roster import (
    APP_ENV_MAP,
    LEGACY_SECRET_ENV_VARS,
    LEGACY_ENV_VARS,
    RESPEAKER_ENV_MAP,
    RUNTIME_CACHE_ENV_MAP,
    SCREEN_ENV_MAP,
    SECRETS_ENV_MAP,
)
from spica.config.environment_snapshot import EnvironmentSnapshot

_REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
_MAX_DOTENV_BYTES = 2 * 1024 * 1024
_REPARSE_POINT = 0x0400
_SECRET_MATERIAL_ORDER = tuple(SECRETS_ENV_MAP.values()) + tuple(
    LEGACY_SECRET_ENV_VARS
)
_SECRET_MATERIAL_NAMES = frozenset(_SECRET_MATERIAL_ORDER)
_HEURISTIC_SECRET_LABEL = "heuristic_secret".upper()
_SECRET_MATERIAL_LABELS = _SECRET_MATERIAL_NAMES | {_HEURISTIC_SECRET_LABEL}
_HEURISTIC_SECRET_TOKENS = frozenset(
    {
        "accesskey",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "secret",
        "token",
    }
)


def _looks_like_secret_name(name: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", normalized) if part)
    if _HEURISTIC_SECRET_TOKENS.intersection(parts):
        return True
    return any(
        pair in {("api", "key"), ("access", "key"), ("private", "key")}
        for pair in zip(parts, parts[1:])
    )


class EnvironmentRefreshError(RuntimeError):
    """Safe owner failure without paths, values, or environment-name-like codes."""


class _InheritedInterpolationBase:
    """Opaque immutable copy of explicitly supplied dotenv interpolation input."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        if any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise TypeError(
                "inherited interpolation base must contain string names and values"
            )
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("inherited interpolation base is immutable")

    def copy_mapping(self) -> dict[str, str]:
        return dict(self._items)

    def __repr__(self) -> str:
        return "InheritedInterpolationBase(<redacted>)"


class _SecretMaterial:
    """Opaque immutable secret values observed while resolving owner layers."""

    __slots__ = ("_complete", "_entries")

    def __init__(
        self,
        entries: tuple[tuple[str, str], ...] = (),
        *,
        complete: bool = True,
    ) -> None:
        if any(
            name not in _SECRET_MATERIAL_LABELS or not isinstance(value, str)
            for name, value in entries
        ):
            raise TypeError("secret material must use approved text labels")
        if type(complete) is not bool:
            raise TypeError("secret material completeness must be boolean")
        object.__setattr__(self, "_entries", tuple(entries))
        object.__setattr__(self, "_complete", complete)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "_SecretMaterial":
        return cls(
            tuple(
                (name, values[name])
                for name in _SECRET_MATERIAL_ORDER
                if name in values
            )
            + tuple(
                (_HEURISTIC_SECRET_LABEL, values[name])
                for name in sorted(values)
                if name not in _SECRET_MATERIAL_NAMES
                and _looks_like_secret_name(name)
            )
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("secret material is immutable")

    def combined(self, *others: "_SecretMaterial") -> "_SecretMaterial":
        return _SecretMaterial(
            self._entries
            + tuple(entry for other in others for entry in other._entries),
            complete=self._complete and all(other._complete for other in others),
        )

    def contains(self, text: str) -> bool:
        if not isinstance(text, str):
            raise TypeError("secret material query must be text")
        if not self._complete:
            return bool(text)
        return any(value in text for value in self._replacement_map())

    def sanitize(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("secret material value must be text")
        if not self._complete:
            return text if not text else "«REDACTED:UNVERIFIED_SECRET_MATERIAL»"
        replacements = self._replacement_map()
        if not replacements:
            return text
        pattern = re.compile(
            "|".join(
                re.escape(value)
                for value in sorted(replacements, key=len, reverse=True)
            )
        )
        return pattern.sub(
            lambda match: f"«REDACTED:{replacements[match.group(0)]}»",
            text,
        )

    def same_as(self, other: "_SecretMaterial") -> bool:
        if not isinstance(other, _SecretMaterial):
            raise TypeError("secret material comparison requires secret material")
        return (
            self._complete == other._complete
            and self._entries == other._entries
        )

    def _replacement_map(self) -> dict[str, str]:
        replacements: dict[str, str] = {}
        for name, value in self._entries:
            variants = {value}
            try:
                variants.add(value.encode("unicode_escape").decode("ascii"))
                variants.add(json.dumps(value, ensure_ascii=True)[1:-1])
                variants.add(json.dumps(value, ensure_ascii=False)[1:-1])
                variants.add(repr(value)[1:-1])
            except (UnicodeError, ValueError):
                pass
            for variant in variants:
                if variant:
                    replacements.setdefault(variant, name)
        return replacements

    def __repr__(self) -> str:
        return "SecretMaterial(<redacted>)"


class _ParsedDotenv:
    """Private parser result whose representation never exposes assignments."""

    __slots__ = ("_secret_material", "_values")

    def __init__(
        self,
        values: Mapping[str, str],
        secret_material: _SecretMaterial,
    ) -> None:
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "_secret_material", secret_material)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("parsed dotenv is immutable")

    def copy_values(self) -> dict[str, str]:
        return dict(self._values)

    def secret_material(self) -> _SecretMaterial:
        return self._secret_material

    def __repr__(self) -> str:
        return "ParsedDotenv(<redacted>)"


def _ensure_env_loaded(
    repo_env_path: Path | None = None,
    parent_env_path: Path | None = None,
) -> None:
    load_dotenv(repo_env_path or (_REPO_ROOT / "xiaosan.env"))
    load_dotenv(
        parent_env_path or (_REPO_ROOT.parent / "xiaosan.env"),
        override=False,
    )


class Secrets:
    """Immutable secret slots with no generic record/JSON representation."""

    __slots__ = (
        "_openai_api_key",
        "_judge_api_key",
        "_bilibili_cookie",
        "_qbittorrent_password",
    )

    def __init__(
        self,
        openai_api_key: str | None = None,
        judge_api_key: str | None = None,
        bilibili_cookie: str | None = None,
        qbittorrent_password: str | None = None,
    ) -> None:
        object.__setattr__(self, "_openai_api_key", openai_api_key)
        object.__setattr__(self, "_judge_api_key", judge_api_key)
        object.__setattr__(self, "_bilibili_cookie", bilibili_cookie)
        object.__setattr__(self, "_qbittorrent_password", qbittorrent_password)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Secrets is immutable")

    @property
    def openai_api_key(self) -> str | None:
        return self._openai_api_key

    @property
    def judge_api_key(self) -> str | None:
        return self._judge_api_key

    @property
    def bilibili_cookie(self) -> str | None:
        return self._bilibili_cookie

    @property
    def qbittorrent_password(self) -> str | None:
        return self._qbittorrent_password

    def __repr__(self) -> str:
        return "Secrets(<redacted>)"


class ResolvedRepoEnvironment:
    """Candidate resolution retaining only safe repository-name metadata."""

    __slots__ = ("_loaded", "_repo_names")

    def __init__(
        self,
        loaded: "LoadedSecrets",
        repo_values: Mapping[str, str],
    ) -> None:
        object.__setattr__(self, "_loaded", loaded)
        object.__setattr__(self, "_repo_names", frozenset(repo_values))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ResolvedRepoEnvironment is immutable")

    @property
    def environment_snapshot(self) -> EnvironmentSnapshot:
        return self._loaded.environment_snapshot

    @property
    def secrets(self) -> Secrets:
        return self._loaded.secrets

    @property
    def tainted_environment_names(self) -> tuple[str, ...]:
        return self._loaded.tainted_environment_names

    def secret_source(self, slot: str) -> str | None:
        environment_name = SECRETS_ENV_MAP.get(slot)
        if environment_name is None:
            raise ValueError("unknown secret slot")
        return dict(self._loaded._secret_source_layers).get(environment_name)

    def secret_configured(self, slot: str) -> bool:
        if slot not in SECRETS_ENV_MAP:
            raise ValueError("unknown secret slot")
        return bool(getattr(self._loaded.secrets, slot))

    def repo_contains(self, environment_name: str) -> bool:
        return environment_name in self._repo_names

    def contains_secret_material(self, text: str) -> bool:
        """Return whether text contains any secret observed by this resolution."""

        return self._loaded.contains_secret_material(text)

    def sanitize_secret_material(self, text: str) -> str:
        """Redact every observed secret without exposing the retained values."""

        return self._loaded.sanitize_secret_material(text)

    def same_secret_material(self, other: "ResolvedRepoEnvironment") -> bool:
        """Compare candidate secret material without exposing either value set."""

        if not isinstance(other, ResolvedRepoEnvironment):
            raise TypeError("secret material comparison requires a resolved owner")
        return self._loaded.same_secret_material(other._loaded)

    def __repr__(self) -> str:
        return "ResolvedRepoEnvironment(<redacted>)"


class RepoEnvironmentTransition:
    """Safe semantic comparison produced while raw candidate maps are transient."""

    __slots__ = ("_after", "_before", "_changed_names")

    def __init__(
        self,
        *,
        before: ResolvedRepoEnvironment,
        after: ResolvedRepoEnvironment,
        changed_names: frozenset[str],
    ) -> None:
        object.__setattr__(self, "_before", before)
        object.__setattr__(self, "_after", after)
        object.__setattr__(self, "_changed_names", changed_names)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("RepoEnvironmentTransition is immutable")

    @property
    def before(self) -> ResolvedRepoEnvironment:
        return self._before

    @property
    def after(self) -> ResolvedRepoEnvironment:
        return self._after

    def repo_changed(self, environment_name: str) -> bool:
        return environment_name in self._changed_names

    def repo_change(self, environment_name: str) -> str:
        before_present = self._before.repo_contains(environment_name)
        after_present = self._after.repo_contains(environment_name)
        if not before_present and after_present:
            return "will_set"
        if before_present and not after_present:
            return "will_clear"
        if environment_name in self._changed_names:
            return "will_replace"
        return "unchanged"

    def contains_secret_material(self, text: str) -> bool:
        """Return whether either candidate observed secret material in text."""

        return (
            self._before.contains_secret_material(text)
            or self._after.contains_secret_material(text)
        )

    def sanitize_secret_material(self, text: str) -> str:
        """Redact the union of before/after candidate secret material."""

        material = self._before._loaded._secret_material.combined(
            self._after._loaded._secret_material
        )
        return material.sanitize(text)

    def same_secret_material(self, other: "RepoEnvironmentTransition") -> bool:
        """Compare both transition snapshots without exposing their values."""

        if not isinstance(other, RepoEnvironmentTransition):
            raise TypeError("secret material comparison requires a transition")
        return self._before.same_secret_material(
            other._before
        ) and self._after.same_secret_material(other._after)

    def __repr__(self) -> str:
        return "RepoEnvironmentTransition(<redacted>)"


class LoadedSecrets:
    """Named opt-in result for entry points that also need config provenance."""

    __slots__ = (
        "_secrets",
        "_environment_snapshot",
        "_inherited_interpolation_base",
        "_tainted_environment_names",
        "_legacy_secret_canaries",
        "_legacy_secret_source_layers",
        "_refresh_parent_env_path",
        "_refresh_repo_env_path",
        "_secret_material",
        "_secret_source_layers",
    )

    def __init__(
        self,
        *,
        secrets: Secrets,
        environment_snapshot: EnvironmentSnapshot,
        tainted_environment_names: tuple[str, ...] = (),
        legacy_secret_canaries: tuple[tuple[str, str], ...] = (),
        secret_source_layers: tuple[tuple[str, str], ...] = (),
        legacy_secret_source_layers: tuple[tuple[str, str], ...] = (),
        inherited_interpolation_base: Mapping[str, str] | None = None,
        refresh_repo_env_path: Path | None = None,
        refresh_parent_env_path: Path | None = None,
        _secret_material: _SecretMaterial | None = None,
    ) -> None:
        object.__setattr__(self, "_secrets", secrets)
        object.__setattr__(self, "_environment_snapshot", environment_snapshot)
        object.__setattr__(
            self,
            "_tainted_environment_names",
            tuple(sorted(set(tainted_environment_names))),
        )
        object.__setattr__(
            self,
            "_legacy_secret_canaries",
            tuple(legacy_secret_canaries),
        )
        object.__setattr__(
            self,
            "_secret_source_layers",
            tuple(secret_source_layers),
        )
        object.__setattr__(
            self,
            "_legacy_secret_source_layers",
            tuple(legacy_secret_source_layers),
        )
        winner_material = _SecretMaterial(
            tuple(
                (name, value)
                for name, value in (
                    (
                        SECRETS_ENV_MAP["openai_api_key"],
                        secrets.openai_api_key,
                    ),
                    (
                        SECRETS_ENV_MAP["judge_api_key"],
                        secrets.judge_api_key,
                    ),
                    (
                        SECRETS_ENV_MAP["bilibili_cookie"],
                        secrets.bilibili_cookie,
                    ),
                    (
                        SECRETS_ENV_MAP["qbittorrent_password"],
                        secrets.qbittorrent_password,
                    ),
                    *legacy_secret_canaries,
                )
                if value is not None
            )
        )
        object.__setattr__(
            self,
            "_secret_material",
            winner_material
            if _secret_material is None
            else _secret_material.combined(winner_material),
        )
        if inherited_interpolation_base is None and (
            refresh_repo_env_path is not None
            or refresh_parent_env_path is not None
        ):
            raise TypeError(
                "refresh-capable LoadedSecrets requires an explicit interpolation base"
            )
        interpolation_base = (
            _InheritedInterpolationBase(inherited_interpolation_base)
            if inherited_interpolation_base is not None
            else _InheritedInterpolationBase(
                _reconstruct_inherited_environment(
                    secrets=secrets,
                    environment_snapshot=environment_snapshot,
                    secret_source_layers=secret_source_layers,
                    legacy_secret_canaries=legacy_secret_canaries,
                    legacy_secret_source_layers=legacy_secret_source_layers,
                )
            )
        )
        object.__setattr__(
            self,
            "_inherited_interpolation_base",
            interpolation_base,
        )
        object.__setattr__(
            self,
            "_refresh_repo_env_path",
            Path(refresh_repo_env_path).absolute()
            if refresh_repo_env_path is not None
            else None,
        )
        object.__setattr__(
            self,
            "_refresh_parent_env_path",
            Path(refresh_parent_env_path).absolute()
            if refresh_parent_env_path is not None
            else None,
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("LoadedSecrets is immutable")

    @property
    def secrets(self) -> Secrets:
        return self._secrets

    @property
    def environment_snapshot(self) -> EnvironmentSnapshot:
        return self._environment_snapshot

    @property
    def tainted_environment_names(self) -> tuple[str, ...]:
        return self._tainted_environment_names

    @property
    def legacy_secret_canaries(self) -> tuple[tuple[str, str], ...]:
        return self._legacy_secret_canaries

    def secret_source(self, slot: str) -> str | None:
        environment_name = SECRETS_ENV_MAP.get(slot)
        if environment_name is None:
            raise ValueError("unknown secret slot")
        return dict(self._secret_source_layers).get(environment_name)

    def contains_secret_material(self, text: str) -> bool:
        """Return whether text contains any value seen in a secret owner slot."""

        return self._secret_material.contains(text)

    def sanitize_secret_material(self, text: str) -> str:
        """Redact every seen secret value without returning the private roster."""

        return self._secret_material.sanitize(text)

    def same_secret_material(self, other: "LoadedSecrets") -> bool:
        """Compare all observed material without returning secret plaintext."""

        if not isinstance(other, LoadedSecrets):
            raise TypeError("secret material comparison requires a loaded owner")
        return self._secret_material.same_as(other._secret_material)

    def resolve_repo_dotenv(self, content: bytes) -> ResolvedRepoEnvironment:
        """Resolve candidate repository bytes using fixed owner precedence."""

        resolved, _ = self._resolve_repo_dotenv_transient(content)
        return resolved

    def resolve_repo_transition(
        self,
        before_content: bytes,
        after_content: bytes,
    ) -> RepoEnvironmentTransition:
        """Compare candidates while keeping their plaintext maps on this stack."""

        before, before_values = self._resolve_repo_dotenv_transient(before_content)
        after, after_values = self._resolve_repo_dotenv_transient(after_content)
        missing = object()
        changed_names = frozenset(
            name
            for name in set(before_values) | set(after_values)
            if before_values.get(name, missing) != after_values.get(name, missing)
        )
        return RepoEnvironmentTransition(
            before=before,
            after=after,
            changed_names=changed_names,
        )

    def repo_secret_roundtrips(
        self,
        content: bytes,
        *,
        slot: str,
        expected_value: str,
    ) -> bool:
        """Check candidate dotenv semantics without returning plaintext."""

        environment_name = SECRETS_ENV_MAP.get(slot)
        if environment_name is None:
            raise ValueError("unknown secret slot")
        if not isinstance(expected_value, str):
            raise TypeError("expected secret value must be text")
        _, repo_values = self._resolve_repo_dotenv_transient(content)
        return repo_values.get(environment_name) == expected_value

    def _resolve_repo_dotenv_transient(
        self,
        content: bytes,
    ) -> tuple[ResolvedRepoEnvironment, dict[str, str]]:
        if not isinstance(content, bytes):
            raise TypeError("repository dotenv content must be bytes")
        if self._refresh_parent_env_path is None:
            raise EnvironmentRefreshError("environment owner paths unavailable")
        inherited = self._inherited_environment()
        repo_document = _read_dotenv_bytes_explicit(content, inherited)
        repo_values = repo_document.copy_values()
        effective_after_repo = dict(repo_values)
        effective_after_repo.update(inherited)
        parent_document = _read_dotenv_explicit(
            self._refresh_parent_env_path,
            effective_after_repo,
        )
        parent_values = parent_document.copy_values()
        resolved = _loaded_from_explicit_layers(
            inherited_all=inherited,
            repo_values=repo_values,
            parent_values=parent_values,
            repo_path=self._refresh_repo_env_path,
            parent_path=self._refresh_parent_env_path,
            warn_legacy=False,
            secret_material=_SecretMaterial.from_mapping(inherited).combined(
                repo_document.secret_material(),
                parent_document.secret_material(),
            ),
        )
        resolved = self._preserve_inherited_taint(resolved)
        return ResolvedRepoEnvironment(resolved, repo_values), repo_values

    def refresh(self) -> "LoadedSecrets":
        """Re-read owned dotenv paths without consulting primed process globals."""

        if (
            self._refresh_repo_env_path is None
            or self._refresh_parent_env_path is None
        ):
            return self
        inherited = self._inherited_environment()
        refreshed = load_secrets(
            with_environment_snapshot=True,
            inherited_environment=inherited,
            repo_env_path=self._refresh_repo_env_path,
            parent_env_path=self._refresh_parent_env_path,
            prime_process=False,
        )
        if not isinstance(refreshed, LoadedSecrets):
            raise EnvironmentRefreshError("environment refresh failed")
        return self._preserve_inherited_taint(refreshed)

    def _inherited_environment(self) -> dict[str, str]:
        return self._inherited_interpolation_base.copy_mapping()

    def _preserve_inherited_taint(
        self,
        refreshed: "LoadedSecrets",
    ) -> "LoadedSecrets":
        inherited_taint = {
            name: layer
            for name in self._environment_snapshot.tainted_names
            if (layer := self._environment_snapshot.layer_for(name)) is not None
            and (
                refreshed.environment_snapshot.get(name) is not None
                or refreshed.environment_snapshot.is_tainted(name)
            )
        }
        if inherited_taint:
            refreshed_snapshot = refreshed.environment_snapshot.quarantine(
                inherited_taint
            )
            refreshed = LoadedSecrets(
                secrets=refreshed.secrets,
                environment_snapshot=refreshed_snapshot,
                tainted_environment_names=tuple(
                    sorted(
                        set(refreshed.tainted_environment_names)
                        | set(inherited_taint)
                    )
                ),
                legacy_secret_canaries=refreshed.legacy_secret_canaries,
                secret_source_layers=refreshed._secret_source_layers,
                legacy_secret_source_layers=(
                    refreshed._legacy_secret_source_layers
                ),
                inherited_interpolation_base=(
                    refreshed._inherited_interpolation_base.copy_mapping()
                ),
                refresh_repo_env_path=refreshed._refresh_repo_env_path,
                refresh_parent_env_path=refreshed._refresh_parent_env_path,
                _secret_material=refreshed._secret_material,
            )
        return refreshed

    def __repr__(self) -> str:
        return "LoadedSecrets(<redacted>)"


def load_secrets(
    *,
    with_environment_snapshot: bool = False,
    inherited_environment: Mapping[str, str] | None = None,
    repo_env_path: Path | None = None,
    parent_env_path: Path | None = None,
    prime_process: bool = True,
) -> Secrets | LoadedSecrets:
    if not with_environment_snapshot:
        # Keep the long-standing zero-argument owner call intact.  Existing
        # entry points and tests replace this boundary with a no-argument
        # function; explicit paths are a Config Studio-only opt-in.
        if repo_env_path is None and parent_env_path is None:
            _ensure_env_loaded()
        else:
            _ensure_env_loaded(repo_env_path, parent_env_path)
        for legacy_name in LEGACY_ENV_VARS:
            if os.getenv(legacy_name):
                _warn_legacy(legacy_name)
        return Secrets(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            judge_api_key=os.getenv("JUDGE_API_KEY"),
            bilibili_cookie=os.getenv("BILIBILI_COOKIE"),
            qbittorrent_password=os.getenv("QBITTORRENT_PASSWORD"),
        )

    inherited_all = (
        dict(inherited_environment)
        if inherited_environment is not None
        else dict(os.environ)
    )
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in inherited_all.items()
    ):
        raise TypeError("inherited environment must contain string names and values")
    repo_path = repo_env_path or (_REPO_ROOT / "xiaosan.env")
    parent_path = parent_env_path or (_REPO_ROOT.parent / "xiaosan.env")
    repo_document = _read_dotenv_explicit(repo_path, inherited_all)
    repo_values = repo_document.copy_values()
    effective_after_repo = dict(repo_values)
    effective_after_repo.update(inherited_all)
    parent_document = _read_dotenv_explicit(parent_path, effective_after_repo)
    parent_values = parent_document.copy_values()
    if prime_process:
        _ensure_env_loaded(repo_path, parent_path)

    return _loaded_from_explicit_layers(
        inherited_all=inherited_all,
        repo_values=repo_values,
        parent_values=parent_values,
        repo_path=repo_path,
        parent_path=parent_path,
        warn_legacy=True,
        secret_material=_SecretMaterial.from_mapping(inherited_all).combined(
            repo_document.secret_material(),
            parent_document.secret_material(),
        ),
    )


def _configuration_environment_names() -> set[str]:
    return {
        value
        for mapping in (
            APP_ENV_MAP,
            SCREEN_ENV_MAP,
            RUNTIME_CACHE_ENV_MAP,
            RESPEAKER_ENV_MAP,
        )
        for value in mapping.values()
    }


def _reconstruct_inherited_environment(
    *,
    secrets: Secrets,
    environment_snapshot: EnvironmentSnapshot,
    secret_source_layers: tuple[tuple[str, str], ...],
    legacy_secret_canaries: tuple[tuple[str, str], ...],
    legacy_secret_source_layers: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """Compatibility fallback for manually constructed ``LoadedSecrets`` DTOs."""

    inherited: dict[str, str] = {}
    for name in _configuration_environment_names():
        if environment_snapshot.layer_for(name) != "inherited":
            continue
        value = environment_snapshot.get(name)
        if value is not None:
            inherited[name] = value
    secret_values = {
        SECRETS_ENV_MAP["openai_api_key"]: secrets.openai_api_key,
        SECRETS_ENV_MAP["judge_api_key"]: secrets.judge_api_key,
        SECRETS_ENV_MAP["bilibili_cookie"]: secrets.bilibili_cookie,
        SECRETS_ENV_MAP["qbittorrent_password"]: secrets.qbittorrent_password,
    }
    for name, layer in secret_source_layers:
        value = secret_values.get(name)
        if layer == "inherited" and value is not None:
            inherited[name] = value
    legacy_values = dict(legacy_secret_canaries)
    for name, layer in legacy_secret_source_layers:
        value = legacy_values.get(name)
        if layer == "inherited" and value is not None:
            inherited[name] = value
    return inherited


def _loaded_from_explicit_layers(
    *,
    inherited_all: Mapping[str, str],
    repo_values: Mapping[str, str],
    parent_values: Mapping[str, str],
    repo_path: Path | None,
    parent_path: Path,
    warn_legacy: bool,
    secret_material: _SecretMaterial | None = None,
) -> LoadedSecrets:
    environment_names = _configuration_environment_names()

    def winning_value(name: str) -> str | None:
        for layer in (inherited_all, repo_values, parent_values):
            if name in layer:
                return layer[name]
        return None

    def winning_layer(name: str) -> str | None:
        for layer, values in (
            ("inherited", inherited_all),
            ("repo_dotenv", repo_values),
            ("parent_dotenv", parent_values),
        ):
            if name in values:
                return layer
        return None

    if warn_legacy:
        for legacy_name in LEGACY_ENV_VARS:
            if winning_value(legacy_name):
                _warn_legacy(legacy_name)
    resolved_secrets = Secrets(
        openai_api_key=winning_value(SECRETS_ENV_MAP["openai_api_key"]),
        judge_api_key=winning_value(SECRETS_ENV_MAP["judge_api_key"]),
        bilibili_cookie=winning_value(SECRETS_ENV_MAP["bilibili_cookie"]),
        qbittorrent_password=winning_value(
            SECRETS_ENV_MAP["qbittorrent_password"]
        ),
    )
    legacy_secret_canaries = tuple(
        (name, value)
        for name in LEGACY_SECRET_ENV_VARS
        if (value := winning_value(name))
    )
    observed_secret_material = secret_material or _SecretMaterial.from_mapping(
        inherited_all
    ).combined(
        _SecretMaterial.from_mapping(repo_values),
        _SecretMaterial.from_mapping(parent_values),
    )
    tainted_environment_names = tuple(
        sorted(
            name
            for name in environment_names
            if (value := winning_value(name)) is not None
            and observed_secret_material.contains(value)
        )
    )
    tainted_layers = {
        name: winning_layer(name)
        for name in tainted_environment_names
        if winning_layer(name) is not None
    }
    safe_environment_names = environment_names - set(tainted_environment_names)
    return LoadedSecrets(
        secrets=resolved_secrets,
        environment_snapshot=EnvironmentSnapshot.from_layers(
            inherited={
                name: inherited_all[name]
                for name in safe_environment_names
                if name in inherited_all
            },
            repo_dotenv={
                name: repo_values[name]
                for name in safe_environment_names
                if name in repo_values
            },
            parent_dotenv={
                name: parent_values[name]
                for name in safe_environment_names
                if name in parent_values
            },
            tainted=tainted_layers,
        ),
        tainted_environment_names=tainted_environment_names,
        legacy_secret_canaries=legacy_secret_canaries,
        secret_source_layers=tuple(
            (name, layer)
            for name in SECRETS_ENV_MAP.values()
            if (layer := winning_layer(name)) is not None
        ),
        legacy_secret_source_layers=tuple(
            (name, layer)
            for name in LEGACY_SECRET_ENV_VARS
            if (layer := winning_layer(name)) is not None
        ),
        inherited_interpolation_base=inherited_all,
        refresh_repo_env_path=repo_path,
        refresh_parent_env_path=parent_path,
        _secret_material=observed_secret_material,
    )


def _warn_legacy(name: str) -> None:
    logger.warning(
        "legacy env var %s is set but no longer read by any code "
        "(密钥已统一为 OPENAI_API_KEY，请从 xiaosan.env 删除该行)",
        name,
    )


def _read_dotenv_explicit(
    path: Path,
    interpolation_base: Mapping[str, str],
) -> _ParsedDotenv:
    content = _read_owned_dotenv_bytes(path)
    if content is None:
        return _ParsedDotenv({}, _SecretMaterial())
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvironmentRefreshError("environment owner document invalid") from exc
    return _parse_dotenv_text_explicit(text, interpolation_base)


def _parse_dotenv_text_explicit(
    text: str,
    interpolation_base: Mapping[str, str],
) -> _ParsedDotenv:
    resolved: dict[str, str] = {}
    secret_entries: list[tuple[str, str]] = []
    complete = True
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            complete = False
            continue
        if binding.key is None or binding.value is None:
            continue
        environment: dict[str, str | None] = dict(resolved)
        environment.update(interpolation_base)
        value = "".join(
            atom.resolve(environment) for atom in parse_variables(binding.value)
        )
        resolved[binding.key] = value
        if binding.key in _SECRET_MATERIAL_NAMES:
            secret_entries.append((binding.key, value))
        elif _looks_like_secret_name(binding.key):
            secret_entries.append((_HEURISTIC_SECRET_LABEL, value))
    return _ParsedDotenv(
        resolved,
        _SecretMaterial(tuple(secret_entries), complete=complete),
    )


def _read_owned_dotenv_bytes(path: Path) -> bytes | None:
    """Read one fixed owner document without following links or retaining paths."""

    target = Path(os.path.abspath(os.fspath(path)))
    chain = list(reversed(target.parents)) + [target]

    def inspect_chain() -> os.stat_result | None:
        target_stat: os.stat_result | None = None
        for index, component in enumerate(chain):
            if str(component) == component.anchor:
                continue
            try:
                info = component.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise EnvironmentRefreshError(
                    "environment owner document unavailable"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            ):
                raise EnvironmentRefreshError("environment owner document unsafe")
            is_target = index == len(chain) - 1
            if is_target:
                if not stat.S_ISREG(info.st_mode):
                    raise EnvironmentRefreshError(
                        "environment owner document unsafe"
                    )
                if info.st_nlink != 1:
                    raise EnvironmentRefreshError(
                        "environment owner document unsafe"
                    )
                if os.name == "posix" and info.st_uid != os.getuid():
                    raise EnvironmentRefreshError(
                        "environment owner document unsafe"
                    )
                target_stat = info
            elif not stat.S_ISDIR(info.st_mode):
                raise EnvironmentRefreshError("environment owner document unsafe")
        return target_stat

    before = inspect_chain()
    if before is None:
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise EnvironmentRefreshError("environment owner document unsafe")
            if opened.st_nlink != 1:
                raise EnvironmentRefreshError("environment owner document unsafe")
            if os.name == "posix" and opened.st_uid != os.getuid():
                raise EnvironmentRefreshError("environment owner document unsafe")
            if opened.st_size > _MAX_DOTENV_BYTES:
                raise EnvironmentRefreshError("environment owner document invalid")
            chunks: list[bytes] = []
            remaining = _MAX_DOTENV_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            final = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except EnvironmentRefreshError:
        raise
    except OSError as exc:
        raise EnvironmentRefreshError("environment owner document unsafe") from exc
    content = b"".join(chunks)
    if len(content) > _MAX_DOTENV_BYTES:
        raise EnvironmentRefreshError("environment owner document invalid")
    after = inspect_chain()
    def stable_generation(file_stat: os.stat_result) -> tuple[int, ...]:
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
            file_stat.st_mode,
            file_stat.st_nlink,
            file_stat.st_uid,
        )

    if after is None or not (
        stable_generation(before)
        == stable_generation(opened)
        == stable_generation(final)
        == stable_generation(after)
    ):
        raise EnvironmentRefreshError("environment owner document unsafe")
    return content


def _read_dotenv_bytes_explicit(
    content: bytes,
    interpolation_base: Mapping[str, str],
) -> _ParsedDotenv:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _ParsedDotenv({}, _SecretMaterial(complete=False))
    return _parse_dotenv_text_explicit(text, interpolation_base)
