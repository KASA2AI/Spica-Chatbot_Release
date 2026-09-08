"""Validated, character-scoped personal save files (never exposed to endpoints)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from spica.core.character import character_memory_prefix
from spica.galgame.models import CompanionBeat


class MemoryRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=256)
    scope: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=32000)
    importance: FiniteFloat = Field(ge=0, le=1)
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    use_count: int = Field(default=0, ge=0)
    memory_key: str | None = None
    memory_type: str = "fact"
    source: str = "user"
    confidence: FiniteFloat = Field(default=1.0, ge=0, le=1)
    pinned: int = Field(default=0, ge=0, le=1)
    status: str = "active"


class SavedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_text: str = Field(max_length=64000)
    assistant_text: str = Field(max_length=64000)
    interaction_mode: Literal["chat"] = "chat"
    user_local_time: str | None = None
    screen_observation_context: str | None = None


class CharacterSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal[1] = 1
    character_id: str
    memories: list[MemoryRow] = Field(default_factory=list)
    recent: dict[str, list[SavedTurn]] = Field(default_factory=dict)
    selected_costume: str | None = None
    companion_beats: list[CompanionBeat] = Field(default_factory=list)

    @model_validator(mode="after")
    def scopes(self) -> CharacterSave:
        prefix = character_memory_prefix(self.character_id)
        if any(not row.conversation_id.startswith(prefix) for row in self.memories):
            raise ValueError("save contains another character's memories")
        if any(not key.startswith(prefix) for key in self.recent):
            raise ValueError("save contains another character's recent conversation")
        if any(
            beat.scope.get("character_id") != self.character_id
            for beat in self.companion_beats
        ):
            raise ValueError("save contains another character's companion memories")
        return self


def read_character_save(path: Path, character_id: str) -> CharacterSave:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("personal save exceeds 32 MiB")
    result = CharacterSave.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if result.character_id != character_id:
        raise ValueError("personal save character id does not match the package")
    return result


def restore_character_save(package: Any, services: Any) -> None:
    state_dir = getattr(package, "state_dir", None)
    if not state_dir:
        return
    state = Path(state_dir)
    marker = state / "memory-initialized"
    if marker.exists():
        return
    manifest = getattr(package, "manifest", None)
    if manifest is not None and manifest.memory_file:
        from spica.core.character_manifest import package_file

        save = read_character_save(
            package_file(Path(package.package_root), manifest.memory_file),
            package.character_id,
        )
        prefix = character_memory_prefix(package.character_id)
        if not (
            state / "recent.json"
        ).exists() and services.memory_store.import_empty_namespace(
            prefix, [row.model_dump() for row in save.memories]
        ):
            services.recent_memory.restore(
                {
                    key: [turn.model_dump(exclude_none=True) for turn in turns]
                    for key, turns in save.recent.items()
                }
            )
            game_memory = getattr(services, "game_memory_adapter", None)
            if game_memory is not None:
                game_memory.import_character_beats(
                    package.character_id, save.companion_beats
                )
    marker.write_text("1\n", encoding="utf-8")


def export_character_save(
    package: Any, services: Any, *, selected_costume: str | None = None
) -> dict:
    prefix = character_memory_prefix(package.character_id)
    game_memory = getattr(services, "game_memory_adapter", None)
    save = CharacterSave.model_validate(
        {
            "character_id": package.character_id,
            "memories": services.memory_store.export_namespace(prefix),
            "recent": {
                key: [
                    turn
                    for turn in turns
                    if turn.get("interaction_mode", "chat") == "chat"
                ]
                for key, turns in services.recent_memory.dump().items()
                if key.startswith(prefix)
            },
            "selected_costume": selected_costume,
            "companion_beats": game_memory.export_character_beats(package.character_id)
            if game_memory
            else [],
        }
    )
    return save.model_dump(exclude_none=True)
