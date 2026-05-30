from __future__ import annotations

from agent.nodes import (
    build_prompt_node,
    build_response_node,
    build_visual_node,
    call_llm_node,
    extract_memory_node,
    load_recent_context_node,
    parse_reply_node,
    retrieve_long_term_memory_node,
    save_recent_context_node,
    synthesize_tts_node,
    validate_input_node,
)
from agent.state import AgentServices, AgentState


def run_voice_pipeline(state: AgentState, services: AgentServices) -> AgentState:
    state = validate_input_node(state, services)
    state = load_recent_context_node(state, services)
    state = retrieve_long_term_memory_node(state, services)
    state = build_prompt_node(state, services)
    state = call_llm_node(state, services)
    state = parse_reply_node(state, services)
    state = save_recent_context_node(state, services)
    state = extract_memory_node(state, services)
    state = build_visual_node(state, services)
    state = synthesize_tts_node(state, services)
    state = build_response_node(state, services)
    return state
