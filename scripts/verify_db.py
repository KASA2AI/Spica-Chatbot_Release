# -*- coding: utf-8 -*-
"""验证 LimeLight 剧情进了独立的 galgame 库（不是Spica角色记忆库）"""
from spica.host.app_host import AppHost
h = AppHost(); h.initialize()
gm = h.services.game_memory_adapter

# 你刚才 stream/summary demo 用的 game_id 是 anemoi 还是 LimeLight？
# stream demo 默认 game_id 可能是固定值，先两个都查
for gid in ["anemoi", "LimeLight", "limelight", "testgame_clean2"]:
    print(f"\n===== game_id = {gid} =====")
    try:
        lines = gm.committed_story_lines(gid, "default")
        print(f"  committed 行: {len(lines)} 句")
        for l in lines[-3:]:
            print(f"    {getattr(l,'speaker','?')} | {getattr(l,'text','')[:40]}")
    except Exception as e:
        print(f"  committed err: {e}")
    try:
        sums = gm.recent_summaries(gid, "default", 5)
        print(f"  摘要: {len(sums)} 条")
        for s in sums:
            print(f"    {getattr(s,'summary_zh','')[:60]}")
    except Exception as e:
        print(f"  summary err: {e}")
