import os, sys
from dotenv import load_dotenv
from openai import OpenAI

# 讀 .env（若沒有也沒關係，會直接讀系統環境變數）
load_dotenv()

client = OpenAI()  # 預設會從 OPENAI_API_KEY 讀金鑰

# 初始系統行為；你可改成你的專屬助理風格
SYSTEM_PROMPT = "You are a helpful assistant."

history = [{"role": "system", "content": SYSTEM_PROMPT}]
print("開始聊天，輸入 exit 結束。")

while True:
    try:
        user = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nBye")
        break

    if user.lower() == "exit":
        break
    if not user:
        continue

    history.append({"role": "user", "content": user})

    # 串流回覆（邊產生邊顯示）
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=history,
        stream=True,
    )

    print("AI:", end=" ", flush=True)
    buf = []
    for chunk in resp:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            text = delta.content
            buf.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()
    print()

    history.append({"role": "assistant", "content": "".join(buf)})
