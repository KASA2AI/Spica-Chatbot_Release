import datetime
import math


def get_time():
    """Return current local time."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def calculate(expression: str):
    """Safely evaluate a small math expression for learning purposes."""
    allowed_names = {
        "sqrt": math.sqrt,
        "pow": pow,
        "abs": abs,
        "round": round,
    }

    try:
        return eval(expression, {"__builtins__": {}}, allowed_names)
    except Exception as exc:
        return f"计算失败: {exc}"


def todo_list():
    """Return a tiny fixed todo list, so you can learn how tools are added."""
    return ["学习 Flask 路由", "理解 Agent 选择工具", "自己新增一个工具"]


TOOLS = {
    "get_time": get_time,
    "calculate": calculate,
    "todo_list": todo_list,
}
