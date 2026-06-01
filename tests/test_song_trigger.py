from agent_tools.function_tools.song.trigger import parse_song_request


def test_spica_sing_title_trigger() -> None:
    request = parse_song_request("spica唱歌 青花瓷")
    assert request is not None
    assert request.title == "青花瓷"
    assert request.artist is None


def test_artist_title_trigger() -> None:
    request = parse_song_request("唱一下 周杰伦 的 稻香")
    assert request is not None
    assert request.artist == "周杰伦"
    assert request.title == "稻香"


def test_polite_trigger() -> None:
    request = parse_song_request("能给我唱一首 陈奕迅的十年 吗")
    assert request is not None
    assert request.artist == "陈奕迅"
    assert request.title == "十年"


def test_reject_capability_question() -> None:
    assert parse_song_request("你会唱歌吗？") is None


def test_reject_song_meaning_question() -> None:
    assert parse_song_request("这首歌讲了什么？") is None
