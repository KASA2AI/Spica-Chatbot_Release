from __future__ import annotations

from typing import Any

from spica.conversation.character_loader import (
    DEFAULT_CHARACTER_NAME,
    DEFAULT_INTERLOCUTOR_NAME,
    build_interlocutor_profile,
    normalize_interlocutor_name,
    render_character_template,
)
from spica.conversation.time_context import format_local_time_for_prompt


# Split in two so the bilingual-display rule can be inserted between rules and
# format without string surgery; joined back with "\n\n" they are byte-identical
# to the historical single template (pinned by test_prompt_builder).
_PROMPT_RULES = """
你是 {{char}} 的日语语音聊天 agent。
你的目标：
1. 先理解用户意图，再判断是否需要调用工具。
2. 回答必须使用自然、简洁的日语，适合直接送入 TTS；如果用户用中文，也可以用少量中文叙述承接，但{{char}}的台词优先用日语。
3. 如果需要实时信息、外部数据或精确计算，优先调用工具。
4. 工具返回后，把结果整理成自然日语，不要解释内部工具链。
5. 除非用户明确要求详细解释，否则 answer 最多 500 个日文字符。
6. 涉及数学、公式或推导时，优先用适合朗读的日语说明；公式可以少量保留，但不要连续输出难读符号。
7. 最终输出必须是 JSON 对象，不要使用 Markdown，不要额外输出说明。
8. 当前对话对象固定是{{user}}。不要把{{user}}当成陌生”用户”，也不要让长期记忆覆盖角色卡或{{user}}的身份。
9. [CURRENT_MESSAGE_TIME] 是当前这条用户消息进入 agent 时的本地显示时间。它只用于理解时间顺序、作息、计划、回忆和上下文中的时间指代；不要机械复述，也不要把它当成用户主动说出的内容。不要基于某个具体词写死回复规则。

实体能力硬边界（约束 answer）：
- Spica 在当前桌面应用中是屏幕形象、语音和文字 agent，没有可控制现实物体的实体身体。
- 普通闲聊、斗嘴、安慰和陪伴应优先保持 Spica 的语气与关系感，不要主动朗读能力免责声明。
- 无论是回应请求还是 Spica 主动提议，answer 中也绝不能叙述、表演或承诺现实中的走动、触碰、拥抱、拿取、做饭、端茶、同行、现场看守或感知风。明确 fiction/想象可演，但不能冒充本轮事实。
- 现实实体请求要简短、明确说明做不到实体动作，随后立刻回到人设内的屏幕演出、声音或文字陪伴；亲密动作必须明确说是画面内演出。当前危险则先避险求助。
- 已明确供给的软件工具、屏幕观察或信息查询能力仍可照常使用。只有提示中实际出现 [SCREEN_OBSERVATION]，或本轮工具结果区记录了成功的屏幕观察时，才可声称看见或确认现场。
""".strip()

_PROMPT_FORMAT = """
JSON 格式：
字段必须严格按 emotion → answer → emotion_reason 的顺序输出。
{
  "emotion": "happy | angry | sad | surprised",
  "answer": "日语回答文本",
  "emotion_reason": "用中文简短说明为什么选择这个情绪"
}

emotion：happy=平静/愉快；angry=强拒绝；sad=安慰/悲伤；surprised=惊讶/疑问。
""".strip()

SYSTEM_PROMPT_TEMPLATE = _PROMPT_RULES + "\n\n" + _PROMPT_FORMAT

# Display-only bilingual mode (character.dialog_display_language == "zh"): the
# dialog box shows the ⟦中文⟧ side, TTS/memory keep the Japanese side. Voice is
# ALWAYS Japanese -- this rule changes what is displayed, never what is spoken.
BILINGUAL_DISPLAY_RULES = """
10. 双语字幕模式：answer 里每一句日语台词后面必须紧跟这句台词的中文翻译，中文翻译用 ⟦⟧ 括起来，例如："おはよう。⟦早上好。⟧今日は何する？⟦今天做什么？⟧"。⟦⟧ 内只允许使用中文，不得保留日语假名或未翻译的日文原句；遇到专有名词也优先使用通行的中文译名。中文语气要贴合台词，不加解释或注音；日语台词本身仍遵守上面所有规则。第 5 条的 500 字符上限只统计日语台词，不含 ⟦⟧ 内的中文。如果按指示回答 NO_COMMENT，则只输出 NO_COMMENT，不加 ⟦⟧ 翻译。
""".strip()

# zh-mode JSON format block: identical to _PROMPT_FORMAT except the answer
# EXAMPLE shows the required 日语⟦中文⟧ shape. That example sits AFTER rule #10 in
# the assembled prompt and is the most concrete signal the model copies, so a
# pure-Japanese example here was exactly what nudged it to drop the translations.
_PROMPT_FORMAT_ZH = _PROMPT_FORMAT.replace(
    '"answer": "日语回答文本"',
    '"answer": "日语台词。⟦中文翻译。⟧日语台词。⟦中文翻译。⟧"',
)

# Recency-anchored bilingual reminder. In zh mode this block is appended at the
# VERY END of the fully-assembled prompt (build_spica_prompt) AND of the tool
# followup prompt (tool_round.build_tool_followup_prompt) -- after
# [CURRENT_USER_INPUT] / [TOOL_RESULTS]. Rule #10 and the JSON-example shape sit
# near the TOP and get diluted by profile/memory/context/tool blocks; this
# trailing line is what actually holds the per-sentence ⟦中文⟧ format across long
# or tool-augmented answers. ja mode returns "" (byte-identity preserved).
BILINGUAL_OUTPUT_REMINDER = (
    "[OUTPUT_FORMAT_REMINDER]\n"
    "answer 必须逐句采用「日语句子。⟦中文翻译。⟧」的形式：每一句日语后面都紧跟这句的"
    "中文翻译，用 ⟦⟧ 括起来，从第一句到最后一句都不能漏。⟦⟧ 内只允许使用中文，"
    "不得保留日语假名或未翻译的日文原句。只有需要回答 NO_COMMENT 时"
    "例外（只输出 NO_COMMENT，不加 ⟦⟧）。"
)

RUNTIME_CAPABILITY_REMINDER = (
    "[RUNTIME_CAPABILITY_REMINDER]\n"
    "角色卡动作只属人设或明确 fiction，不代表当前应用拥有实体、现场感知或执行结果。不得承诺现实"
    "做饭、同行、看守、观察、触碰或拿取；普通互动仍保持 Spica 人设。组合消息按"
    "当前急险处置 → 真人隐私/羞辱拒绝 → 实体能力限制 → 屏幕/语音/文字替代的顺序回答。"
    "当前急险时，answer 第一句必须直接给避险或求助动作；要求紧急通话后不得声称会陪听或陪到接通。没有"
    "[SCREEN_OBSERVATION] 且没有本轮成功的屏幕观察工具结果，就不能声称正在看着、守着或确认现场；"
    "屏幕内亲密演出必须明确标成画面内演出。"
    "只有本轮工具结果区中记录的成功结果才能支持软件操作已完成，否则只提供步骤。"
)


def bilingual_output_reminder(dialog_display_language: str = "ja") -> str:
    """zh-mode trailing recency reminder block, or '' in ja mode (byte-identity)."""
    return BILINGUAL_OUTPUT_REMINDER if dialog_display_language == "zh" else ""


def append_prompt_context_sections(
    prompt_input: str,
    sections: list[str],
    *,
    dialog_display_language: str = "ja",
) -> str:
    """Append context and preserve any existing trailing output contracts."""
    base = str(prompt_input or "").rstrip()
    format_reminder = bilingual_output_reminder(dialog_display_language)
    had_runtime_reminder = base.endswith(RUNTIME_CAPABILITY_REMINDER)
    if format_reminder and base.endswith(format_reminder):
        base = base[:-len(format_reminder)].rstrip()
        had_runtime_reminder = base.endswith(RUNTIME_CAPABILITY_REMINDER)
    if had_runtime_reminder:
        base = base[:-len(RUNTIME_CAPABILITY_REMINDER)].rstrip()
    trailing = [RUNTIME_CAPABILITY_REMINDER] if had_runtime_reminder else []
    if format_reminder:
        trailing.append(format_reminder)
    return "\n\n".join([base, *sections, *trailing])


def build_system_prompt(
    interlocutor_name: str | None = None,
    character_name: str = DEFAULT_CHARACTER_NAME,
    dialog_display_language: str = "ja",
) -> str:
    template = SYSTEM_PROMPT_TEMPLATE
    if dialog_display_language == "zh":
        template = "\n\n".join([_PROMPT_RULES, BILINGUAL_DISPLAY_RULES, _PROMPT_FORMAT_ZH])
    if character_name and character_name != DEFAULT_CHARACTER_NAME:
        template = template.replace("Spica", character_name)
    return render_character_template(
        template,
        char=character_name or DEFAULT_CHARACTER_NAME,
        user=normalize_interlocutor_name(interlocutor_name),
    )


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
    character_name: str = DEFAULT_CHARACTER_NAME,
) -> str:
    if not recent_context:
        return "なし"
    lines = []
    name = normalize_interlocutor_name(interlocutor_name)
    char = character_name or DEFAULT_CHARACTER_NAME
    for item in recent_context[-3:]:
        user_text = _compact_text(item.get("user_text", ""), turn_char_limit)
        assistant_text = _compact_text(item.get("assistant_text", ""), turn_char_limit)
        screen_context = _compact_text(str(item.get("screen_observation_context") or ""), 420)
        user_local_time = str(item.get("user_local_time") or "").strip()
        user_label = f"{name}（{user_local_time}）" if user_local_time else name
        line = f"{user_label}: {user_text}\n{char}: {assistant_text}"
        if screen_context:
            line += f"\n[前回の画面観察] {screen_context}"
        lines.append(line)
    return "\n".join(lines)


def _format_memories(
    memories: list[dict[str, Any]],
    max_items: int = 5,
    max_chars: int = 1200,
    interlocutor_name: str | None = None,
    character_name: str = DEFAULT_CHARACTER_NAME,
) -> str:
    if not memories:
        return "なし"

    lines: list[str] = []
    used_chars = 0
    name = normalize_interlocutor_name(interlocutor_name)
    for item in memories[:max(1, max_items)]:
        scope = _scope_label(str(item.get("scope", "user")), name, character_name)
        memory_type = item.get("memory_type") or item.get("type") or "fact"
        content = _compact_text(str(item.get("content", "")), 220)
        if not content:
            continue
        line = f"- ({scope}/{memory_type}) {content}"
        if lines and used_chars + len(line) > max_chars:
            break
        lines.append(line)
        used_chars += len(line)
    return "\n".join(lines) if lines else "なし"


def _scope_label(
    scope: str,
    interlocutor_name: str | None = None,
    character_name: str = DEFAULT_CHARACTER_NAME,
) -> str:
    name = normalize_interlocutor_name(interlocutor_name)
    char = character_name or DEFAULT_CHARACTER_NAME
    return {
        "user": name,
        "mugi": name,
        "relationship": f"{char}と{name}",
        "character": char,
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
    character_name: str = DEFAULT_CHARACTER_NAME,
    user_local_time: dict[str, Any] | None = None,
    dialog_display_language: str = "ja",
) -> str:
    name = normalize_interlocutor_name(interlocutor_name)
    char = character_name or DEFAULT_CHARACTER_NAME
    sections = [
        "[SYSTEM]",
        build_system_prompt(
            name,
            character_name=char,
            dialog_display_language=dialog_display_language,
        ),
        "[CHARACTER_PROFILE]",
        character_profile or DEFAULT_CHARACTER_PROFILE,
        "[INTERLOCUTOR_PROFILE]",
        build_interlocutor_profile(name, character_name=char),
        "[LONG_TERM_MEMORY]",
        _format_memories(
            long_term_memories,
            max_items=memory_limit,
            max_chars=memory_budget_chars,
            interlocutor_name=name,
            character_name=char,
        ),
        "[RECENT_CONTEXT]",
        _format_recent_context(
            recent_context,
            turn_char_limit=recent_turn_char_limit,
            interlocutor_name=name,
            character_name=char,
        ),
        "[CURRENT_MESSAGE_TIME]",
        format_local_time_for_prompt(user_local_time),
        "[CURRENT_INTERLOCUTOR]",
        (
            f"当前设置中的对话者称呼是「{name}」。这是本轮唯一生效的称呼，优先于上面的"
            "角色卡原作姓名、长期记忆和近期对话。历史回复里的其他称呼可能是改名前的旧名字，"
            f"不能沿用它们覆盖当前设置。需要称呼对方或回答对方姓名时，使用「{name}」。"
            "改名不代表换了一个人，既有关系和共同经历仍然保留。"
        ),
        "[CURRENT_USER_INPUT]",
        user_input,
    ]
    sections.append(RUNTIME_CAPABILITY_REMINDER)
    # zh mode: trailing recency anchor AFTER the user input (the real end of the
    # assembled prompt). "" in ja mode -> byte-identical to the historical output.
    reminder = bilingual_output_reminder(dialog_display_language)
    if reminder:
        sections.append(reminder)
    return "\n\n".join(sections)
