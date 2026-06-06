__all__ = ["SimpleAgent", "EMOTION_LABELS"]


def __getattr__(name: str):
    if name == "SimpleAgent":
        from agent.simple_agent import SimpleAgent

        return SimpleAgent
    if name == "EMOTION_LABELS":
        from agent.reply_parser import EMOTION_LABELS

        return EMOTION_LABELS
    raise AttributeError(name)
