from typing import List
from openai import OpenAI

from dotenv import load_dotenv
import os

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

def merge_group_preferences(
    favorites: List[str],
    preferences: List[str],
    exclude: List[str],
    notes: str
) -> List[str]:
    """
    使用 GPT 統整主揪偏好、避開條件、備註、團員收藏，回傳語意偏好描述（list 格式）
    """

    # 若無任何資訊，回傳預設句
    if not preferences and not favorites and not exclude and not notes:
        return ["本團體尚未提供偏好資訊，請提供更多成員喜好。"]

    prompt = "你是一位旅遊行程設計師，請根據以下資訊，統整出一句適合推薦系統理解的旅遊偏好描述：\n\n"

    if preferences:
        prompt += f"主揪勾選的旅遊偏好類型為：{', '.join(preferences)}。\n"
    if favorites:
        prompt += f"團體成員過去收藏的地點標籤包括：{', '.join(favorites)}。\n"
    if exclude:
        prompt += f"他們希望避開的地點類型有：{', '.join(exclude)}。\n"
    if notes:
        prompt += f"主揪備註說明：「{notes}」。\n"

    prompt += "\n請綜合上述資訊，生成一句簡潔摘要，例如：「本團體偏好自然景點與文化體驗，適合安排放鬆與深度探索的行程。」請用繁體中文。"

    print("🧠 發送給 GPT 的 prompt：\n", prompt)

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        summary = response.choices[0].message.content.strip()
        print("✅ GPT 回傳偏好摘要：", summary)
        return [summary]
    except Exception as e:
        print("❌ GPT 回傳失敗：", e)
        return ["本團體偏好整體資料錯誤，無法生成摘要。"]
