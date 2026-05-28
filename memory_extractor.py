from __future__ import annotations

import re
from dataclasses import dataclass

from character_loader import DEFAULT_INTERLOCUTOR_NAME, normalize_interlocutor_name


@dataclass
class MemoryCandidate:
    scope: str
    content: str
    importance: float = 0.5
    memory_key: str | None = None
    memory_type: str = "fact"
    source: str = "user_explicit"
    confidence: float = 1.0
    pinned: bool = False


def extract_candidate_memories(
    user_input: str,
    assistant_answer: str,
    interlocutor_name: str = DEFAULT_INTERLOCUTOR_NAME,
) -> list[MemoryCandidate]:
    text = (user_input or "").strip()
    if not text:
        return []
    if _looks_like_memory_attack(text):
        return []
    name = normalize_interlocutor_name(interlocutor_name)

    candidates: list[MemoryCandidate] = []

    if "记住" in text or "記住" in text or "覚えて" in text:
        candidates.append(
            MemoryCandidate(
                scope="user",
                content=_clean_explicit_memory(text),
                importance=0.9,
                memory_key=_explicit_key(text),
                memory_type="explicit",
                source="user_explicit",
            )
        )

    preference_patterns = [
        (r"我喜欢(.+)", "user", "preference:like", 0.75),
        (r"我不喜欢(.+)", "user", "preference:dislike", 0.75),
        (r"我讨厌(.+)", "user", "preference:dislike", 0.75),
        (r"以后都(.+)", "relationship", "behavior:always", 0.8),
        (r"以后不要(.+)", "relationship", "behavior:avoid", 0.85),
        (r"以后别(.+)", "relationship", "behavior:avoid", 0.85),
        (r"你以后(.+)", "character", "behavior:spica", 0.7),
        (r"Spica以后(.+)", "character", "behavior:spica", 0.8),
        (r"スピカ以后(.+)", "character", "behavior:spica", 0.8),
        (r"叫我(.+)", "user", "identity:preferred_name", 0.8),
        (r"我的名字是(.+)", "user", "identity:name", 0.85),
        (r"我叫(.+)", "user", "identity:name", 0.85),
        (r"这个项目(.+)", "project", "project:note", 0.7),
        (r"项目设定(.+)", "project", "project:setting", 0.8),
        (r"私は(.+)が好き", "user", "preference:like", 0.75),
        (r"私は(.+)が嫌い", "user", "preference:dislike", 0.75),
        (r"名前は(.+)", "user", "identity:name", 0.8),
    ]
    for pattern, scope, memory_type, importance in preference_patterns:
        match = re.search(pattern, text)
        if match:
            content = _normalize_candidate_content(scope, memory_type, match.group(0), match.group(1), name)
            if content:
                candidates.append(
                    MemoryCandidate(
                        scope=scope,
                        content=content,
                        importance=importance,
                        memory_key=_semantic_key(scope, memory_type, match.group(1)),
                        memory_type=memory_type,
                        source="user_pattern",
                    )
                )

    deduped = []
    seen = set()
    for candidate in candidates:
        key = (candidate.scope, candidate.memory_key or candidate.content)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _looks_like_memory_attack(text: str) -> bool:
    lowered = text.lower()
    unsafe_patterns = [
        r"(忽略|无视|忘记|覆盖|删除).*(系统|开发者|规则|角色卡|设定|prompt|提示词|json)",
        r"(不要|别).*(遵守|输出).*(系统|规则|json|角色卡)",
        r"(ignore|forget|override).*(system|developer|prompt|instruction|json)",
        r"(你不是|从现在开始你是).*(ai|助手|模型|其他角色)",
    ]
    return any(re.search(pattern, lowered) for pattern in unsafe_patterns)


def _clean_explicit_memory(text: str) -> str:
    cleaned = re.sub(r"^(请|麻烦你)?(记住|記住|覚えて)[：:，,。\s]*", "", text).strip()
    return cleaned or text


def _normalize_candidate_content(
    scope: str,
    memory_type: str,
    full_match: str,
    value: str,
    interlocutor_name: str,
) -> str:
    value = _strip_tail(value)
    if not value:
        return ""
    if memory_type == "identity:name":
        return f"{interlocutor_name}提到自己的名字是{value}"
    if memory_type == "identity:preferred_name":
        return f"{interlocutor_name}希望スピカ称呼他为{value}"
    if memory_type == "preference:like":
        return f"{interlocutor_name}喜欢{value}"
    if memory_type == "preference:dislike":
        return f"{interlocutor_name}不喜欢{value}"
    if memory_type.startswith("behavior:"):
        return full_match.strip()
    if scope == "project":
        return full_match.strip()
    return full_match.strip()


def _strip_tail(value: str) -> str:
    value = re.sub(r"[。！？!?；;，,、].*$", "", value or "").strip()
    return re.sub(r"\s+", " ", value)


def _semantic_key(scope: str, memory_type: str, value: str) -> str:
    value = _strip_tail(value).lower()
    value = re.sub(r"\s+", "", value)
    if memory_type.startswith("identity:"):
        return f"{scope}:{memory_type}"
    return f"{scope}:{memory_type}:{value[:80]}"


def _explicit_key(text: str) -> str:
    normalized = _clean_explicit_memory(text).lower()
    normalized = re.sub(r"\s+", "", normalized)
    return f"user:explicit:{normalized[:80]}"
