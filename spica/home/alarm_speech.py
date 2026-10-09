"""Home's confirmed first-sound policy, expressed through shared speech delivery."""
from spica.core.proactive import ScheduledProactiveTurnRequest
from spica.ports.conversation import MaterialHint, BusinessEventBinding
import json


def wake_event(value, phase=None, *, facts=()):
    return BusinessEventBinding('home.wake', value['id'], value['revision'], phase or value['state'],
        tuple(facts) + tuple(f'{key}={value[key]}' for key in ('reason', 'light_status', 'first_sound_at', 'evidence_seconds', 'evidence_frames') if value.get(key) not in (None, '')))


def wake_outcome(value):
    return json.dumps({'event': '闹钟叫醒结果', **{key: value[key] for key in
        ('id', 'state', 'reason', 'first_sound_at', 'evidence_seconds', 'evidence_frames') if key in value}}, ensure_ascii=False)


def wake_finished_directive(elapsed):
    return (
        f'本次叫醒已实际出声，持续约 {int(elapsed)} 秒。'
        '最新连续有效画面确认床区无人、床区外有人，按约定已结束本轮叫醒；'
        '这是区域判断，不证明身份，也不证明对方已完全清醒。'
        '请结合当前角色卡与最近互动，用一两句简短口语自然接住起床这件事。'
        '熟悉程度沿用当前关系，平常的肯定、亲近或轻微调侃均可，不必套用固定的情绪转折；'
        '叫得久可以表达等了好一会，刚开始就起身则轻快回应，不编造等待时长。'
        '不要继续催起床或套用固定台词，变化措辞，不重复刚刚已经回应的话。'
        '只有明确的日程或对话依据才提上班、迟到或具体安排，不从星期或闹钟时间推断。'
        '不编造触碰、外貌、地点细节或没有确认的动作。若最近对话已自然收尾，无须再说，只输出 NO_COMMENT。'
    )


def wake_speech(directive, *, instance_id, due_at, volume=.45, response_window_seconds=0,
                response_window_deadline=None, response_scope_id='', is_current=None, event_binding=None,
                fallback=False, reserve_fallback=False):
    """One utterance; the Home task continues to own its durable instance.

    A deadline/result is never evidence that the owner woke up. The domain must
    inspect audio_outcome and apply its regional evidence or duration limit.
    Volume is per playback and does not change the chat preference.
    """
    return ScheduledProactiveTurnRequest(
        directive=directive, source=f'home.wake:{instance_id}',
        policy='queue_latest', ttl_seconds=15, want_audio=True,
        # Light/vision may consume eight seconds. Give the role five more,
        # reserving the final two seconds for a local cue within due+15.
        due_at=due_at, yield_at=due_at+5, first_sound_deadline=due_at+(13 if reserve_fallback and not fallback else 15),
        # The packaged tone measures ~13 dB above role speech in average level.
        # Attenuate only this cue, never the role or the system mixer.
        volume=volume * .22 if fallback else volume,
        response_window_seconds=response_window_seconds, is_current=is_current,
        response_window_deadline=response_window_deadline,
        response_scope_id=response_scope_id,
        material_hint=MaterialHint('home.wake', ''), event_binding=event_binding,
        local_audio_cue='home_wake' if fallback else None,
    )
