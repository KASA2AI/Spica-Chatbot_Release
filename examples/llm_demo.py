from agent import SimpleAgent


def main():
    agent = SimpleAgent()
    conversation_id = "cli-demo"

    first_question = "我叫小三，请记住我的名字。"
    first_answer = agent.run(first_question, conversation_id=conversation_id)
    print(f"用户：{first_question}")
    print(f"模型：{first_answer}")

    second_question = "我叫什么名字？"
    second_answer = agent.run(second_question, conversation_id=conversation_id)
    print(f"\n用户：{second_question}")
    print(f"模型：{second_answer}")


if __name__ == "__main__":
    main()
