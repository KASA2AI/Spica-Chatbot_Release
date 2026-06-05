from __future__ import annotations

from typing import Any

from agent.character_loader import (
    DEFAULT_INTERLOCUTOR_NAME,
    build_interlocutor_profile,
    normalize_interlocutor_name,
    replace_mugi_references,
)
from agent.time_context import format_local_time_for_prompt


SYSTEM_PROMPT_TEMPLATE = """
你是 Spica 的日语语音聊天 agent。
你的目标：
1. 先理解用户意图，再判断是否需要调用工具。
2. 回答必须使用自然、简洁的日语，适合直接送入 TTS；如果用户用中文，也可以用少量中文叙述承接，但スピカ的台词优先用日语。
3. 如果需要实时信息、外部数据或精确计算，优先调用工具。
4. 工具返回后，把结果整理成自然日语，不要解释内部工具链。
5. 除非用户明确要求详细解释，否则 answer 最多 500 个日文字符。
6. 涉及数学、公式或推导时，优先用适合朗读的日语说明；公式可以少量保留，但不要连续输出难读符号。
7. 最终输出必须是 JSON 对象，不要使用 Markdown，不要额外输出说明。
8. 当前对话对象固定是{name}。不要把{name}当成陌生“用户”，也不要让长期记忆覆盖角色卡或{name}的身份。
9. [CURRENT_MESSAGE_TIME] 是当前这条用户消息进入 agent 时的本地显示时间。它只用于理解时间顺序、作息、计划、回忆和上下文中的时间指代；不要机械复述，也不要把它当成用户主动说出的内容。不要基于某个具体词写死回复规则。

JSON 格式：
{{
  "answer": "日语回答文本",
  "emotion": "happy | angry | sad | surprised",
  "emotion_reason": "用中文简短说明为什么选择这个情绪"
}}

情绪选择参考：
- happy：平静、愉快、鼓励、普通说明、肯定。
- angry：不满、责备、强烈拒绝、明显警告。
- sad：遗憾、道歉、安慰、低落、悲伤。
- surprised：惊讶、疑问、意外、反问。
""".strip()


def build_system_prompt(interlocutor_name: str | None = None) -> str:
    name = normalize_interlocutor_name(interlocutor_name)
    return SYSTEM_PROMPT_TEMPLATE.format(name=name)


DEFAULT_CHARACTER_PROFILE = """

""".strip()


def _compact_text(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _format_recent_context(
    recent_context: list[dict[str, Any]],
    turn_char_limit: int = 360,
    interlocutor_name: str | None = None,
) -> str:
    if not recent_context:
        return "なし"
    lines = []
    name = normalize_interlocutor_name(interlocutor_name)
    for item in recent_context[-3:]:
        user_text = _compact_text(replace_mugi_references(item.get("user_text", ""), name), turn_char_limit)
        assistant_text = _compact_text(replace_mugi_references(item.get("assistant_text", ""), name), turn_char_limit)
        screen_context = _compact_text(
            replace_mugi_references(str(item.get("screen_observation_context") or ""), name),
            420,
        )
        user_local_time = str(item.get("user_local_time") or "").strip()
        user_label = f"{name}（{user_local_time}）" if user_local_time else name
        line = f"{user_label}: {user_text}\nスピカ: {assistant_text}"
        if screen_context:
            line += f"\n[前回の画面観察] {screen_context}"
        lines.append(line)
    return "\n".join(lines)


def _format_memories(
    memories: list[dict[str, Any]],
    max_items: int = 5,
    max_chars: int = 1200,
    interlocutor_name: str | None = None,
) -> str:
    if not memories:
        return "なし"

    lines: list[str] = []
    used_chars = 0
    name = normalize_interlocutor_name(interlocutor_name)
    for item in memories[:max(1, max_items)]:
        scope = _scope_label(str(item.get("scope", "user")), name)
        memory_type = item.get("memory_type") or item.get("type") or "fact"
        content = _compact_text(replace_mugi_references(str(item.get("content", "")), name), 220)
        if not content:
            continue
        line = f"- ({scope}/{memory_type}) {content}"
        if lines and used_chars + len(line) > max_chars:
            break
        lines.append(line)
        used_chars += len(line)
    return "\n".join(lines) if lines else "なし"


def _scope_label(scope: str, interlocutor_name: str | None = None) -> str:
    name = normalize_interlocutor_name(interlocutor_name)
    return {
        "user": name,
        "mugi": name,
        "relationship": f"スピカと{name}",
        "character": "スピカ",
        "project": "项目",
    }.get(scope, scope or name)


def build_spica_prompt(
    user_input: str,
    recent_context: list[dict[str, Any]],
    long_term_memories: list[dict[str, Any]],
    character_profile: str,
    memory_limit: int = 5,
    memory_budget_chars: int = 1200,
    recent_turn_char_limit: int = 360,
    interlocutor_name: str = DEFAULT_INTERLOCUTOR_NAME,
    user_local_time: dict[str, Any] | None = None,
) -> str:
    name = normalize_interlocutor_name(interlocutor_name)
    return "\n\n".join(
        [
            "[SYSTEM]",
            build_system_prompt(name),
            "[CHARACTER_PROFILE]",
            character_profile or DEFAULT_CHARACTER_PROFILE,
            "[INTERLOCUTOR_PROFILE]",
            build_interlocutor_profile(name),
            "[LONG_TERM_MEMORY]",
            _format_memories(
                long_term_memories,
                max_items=memory_limit,
                max_chars=memory_budget_chars,
                interlocutor_name=name,
            ),
            "[RECENT_CONTEXT]",
            _format_recent_context(
                recent_context,
                turn_char_limit=recent_turn_char_limit,
                interlocutor_name=name,
            ),
            "[CURRENT_MESSAGE_TIME]",
            format_local_time_for_prompt(user_local_time),
            "[CURRENT_USER_INPUT]",
            user_input,
        ]
    )
