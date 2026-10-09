"""Owner-scoped Home tools; no device addresses, credentials or shell inputs."""
import re


def parse_home_control(text):
    message = text.strip().strip('。！!？?').strip()
    return {'查看叫醒': 'query', '查询叫醒': 'query', '查看闹钟': 'query',
            '取消关机': 'cancel_bedtime', '取消挂起': 'cancel_bedtime', '取消睡前操作': 'cancel_bedtime',
            '我起床了': 'restore_daily', '恢复日常': 'restore_daily',
            '继续叫醒': 'resume', '恢复叫醒': 'resume'}.get(message)


def explicit_shutdown_request(text):
    # The model cannot manufacture authority by calling the power tool. Match
    # the admitted user's request, including the agreed natural bedtime phrase.
    return bool(re.fullmatch(
        r'(?:(?:晚安|我要睡觉了|我先睡了|我去睡觉了|睡觉了|Spica|Sana|请|帮我|麻烦你|现在|'
        r'可以帮我|能不能帮我|请帮我)[\s，,。！!]*)*'
        r'(?:关闭(?:这台)?电脑|把电脑关掉|电脑关掉|关掉电脑|关机)'
        r'(?:吧|一下|好吗|吗)?[\s。！!？?]*', text.strip(), re.IGNORECASE))


def explicit_daily_restore_request(text):
    # The whole admitted request must authorize this write, not a quotation,
    # hypothetical or negated mention of the command inside ordinary dialogue.
    clause = (r'(?:(?:Sana|Spica|请|帮我|麻烦你|现在)[\s，,。！!]*)*'
              r'(?:我(?:已经)?起床了|恢复日常|(?:本次|这次)不用再叫)'
              r'(?:吧|一下|好吗)?[\s，,。！!；;]*')
    return bool(re.fullmatch(rf'(?:{clause})+', text.strip(), re.IGNORECASE))


def register_home_tools(registry, alarms):
    definitions = (
        ('set_wake_alarm', '设置未来24小时内单次叫醒。按本人消息日期和Asia/Shanghai时间传HH:MM及YYYY-MM-DD；相对时长只传after_minutes。普通“明早九点叫我”由核心优先修改同日固定的那次，“另外加一个”传additional=true，相对时长独立新增。有上午/晚上或目标歧义先问。永久时间/每周星期只在UI修改。成功后确认具体日期时间，并说明返回next中仍生效的更早临时闹钟；失败不能说已设置。设置不代表进入晚安。', alarms.set_from_text,
         dict(local_time={'type': 'string'}, on_date={'type': 'string'},
              after_minutes={'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1440},
              additional={'type': 'boolean'}, label={'type': 'string', 'maxLength': 80}), [], 'write'),
        ('list_wake_alarms', '查询本人闹钟。plan包含固定作息、本次调整、独立临时闹钟、实际next和正在叫醒的active；取消时先查看完整候选，不能因24小时限制偷偷遗漏其他目标。实例preflight/output_status只是准备状态，不能说已经出声。', alarms.query, {}, [], 'read'),
        ('adjust_wake_alarm', '只修改未来24小时内明确的那次：move改点，skip跳过，restore恢复当前固定作息。优先使用查询返回的instance_id；指定on_date有多个候选时先明确目标。恢复已过时间不补响；取消叫醒和恢复固定时间不同。当前已起播不允许用这些操作结束或延后。', alarms.adjust_from_text,
         dict(action={'type': 'string', 'enum': ['move', 'skip', 'restore']},
              instance_id={'type': 'string'}, on_date={'type': 'string'}, local_time={'type': 'string'}), ['action'], 'write'),
        ('control_wake_alarm', '取消未来24小时内尚未开始的明确那次叫醒，或明确恢复故障暂停的叫醒。已开始的叫醒仅在持续确认离床或满十分钟时结束；没有延后功能。再睡一会、拥抱等表达应依当前角色自然互动，不改变计时。', alarms.control_from_text,
         dict(action={'type': 'string', 'enum': ['cancel', 'resume']}, instance_id={'type': 'string', 'description': '查询得到的日期实例 ID；仅有一个进行中实例时可省略'}), ['action'], 'write'),
        ('prepare_home_bedtime', '本人明确说准备睡觉/晚安时使用：关灯并准备挂起到内存。自动关联未来24小时内最近闹钟，回复必须说明返回的具体日期时间，并说明 wake_readiness.warnings 中的具体异常；设备检查与到点实际执行结果分开，灯或相机故障时仍尝试语音叫醒。本人指定时间则传完整日期时间。没有闹钟时保持运行，只有 ask_wake_time=true 才问一次；明确不用叫醒时 no_wake=true。等待本轮回复和资源释放后复核 RTC；不足五分钟则保持运行准备，不声称已经睡眠或保证唤醒。', alarms.prepare_bedtime_from_text,
         dict(wake_date={'type': 'string'}, wake_time={'type': 'string'}, timezone={'type': 'string'},
              no_wake={'type': 'boolean'}), [], 'act'),
        ('cancel_home_bedtime', '撤销本次睡前意图并核对本次 RTC 清理；已提交的挂起无法假装撤回，结果未知时如实说明，保留原闹钟。', alarms.cancel_bedtime, {}, [], 'act'),
        ('restore_home_daily', '本人明确起床或恢复日常时结束晚安，并跳过尚未实际出声的本次叫醒，保留后续周期；已起播仍只能按持续离床或十分钟结束。', alarms.restore_daily_from_text, {}, [], 'write'),
    )
    for name, description, handler, properties, required, effect in definitions:
        registry.register_tool({'type': 'function', 'function': dict(name=name, description=description,
            parameters=dict(type='object', properties=properties, required=required, additionalProperties=False))},
            handler, intent_gated=False, effect=effect, chainable=effect == 'read')


def register_room_tools(registry, home):
    definitions = (
        ('get_home_status', '查询房间传感器、日常识别、相机与灯组操作结果。卧室门开关不代表回家/离家；按压请求不证明灯已亮或熄灭。',
         home.room_status, {}, [], 'read'),
        ('set_room_light', '本人明确要求时开/关家中这一组房间灯，准备与复位由设备层负责。返回请求结果不证明灯已亮/灭；未确认先查询，不盲目重试。不支持逐路灯控或设备地址。',
         home.set_room_light, {'action': {'type': 'string', 'enum': ['on', 'off']}}, ['action'], 'act'),
        ('set_home_detection', '本人要求时开启/停止日常房间相机识别并保存设置；不关闭存在/门磁传感器，不取消叫醒所需相机。',
         home.set_daily_detection, {'enabled': {'type': 'boolean'}}, ['enabled'], 'write'),
    )
    for name, description, handler, properties, required, effect in definitions:
        registry.register_tool({'type': 'function', 'function': dict(name=name, description=description,
            parameters=dict(type='object', properties=properties, required=required, additionalProperties=False))},
            handler, intent_gated=False, effect=effect, chainable=effect == 'read')
