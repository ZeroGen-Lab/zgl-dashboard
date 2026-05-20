from openai import OpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

MODEL = 'deepseek-v4-flash'
ADVANCED_MODEL = 'deepseek-v4-pro'


def call_deepseek(system_prompt, user_prompt, model=MODEL):
    """调用 DeepSeek chat completions API，返回文本响应"""
    if not DEEPSEEK_API_KEY:
        return None
    
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY, 
        base_url=DEEPSEEK_BASE_URL
    )

    try:    
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        return(response.choices[0].message.content)
    except Exception as e:
        return None
    

def generate_daily_summary(uid, name, completions_text):
    """为某成员生成 daily completion 摘要并存入 monthly_summaries"""
    from db import get_db_connection
    from helpers import compute_month_range
    _, _, month_key = compute_month_range(0)

    system_prompt = (
        "你是一个团队工作摘要助手。请根据以下成员的每日工作完成记录，"
        "生成一段简洁的月度工作摘要（200-300字），概括主要工作内容，突出成果。"
    )
    user_prompt = f"成员：{name}\n\n每日工作完成记录：\n{completions_text}"

    result = call_deepseek(system_prompt, user_prompt)
    if result:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO monthly_summaries (uid, month_key, summary) VALUES (?, ?, ?) "
            "ON CONFLICT(uid, month_key) DO UPDATE SET summary=?, generated_at=CURRENT_TIMESTAMP",
            (uid, month_key, result, result)
        )
        conn.commit()
        conn.close()
    return result


def generate_work_suggestion(uid, name, completions_text, plans_text):
    """为某成员结合 weekly plan 和 daily completion 生成工作建议并存入 monthly_summaries"""
    from db import get_db_connection
    from helpers import compute_month_range
    _, _, month_key = compute_month_range(0)

    system_prompt = (
        "你是一个团队工作建议助手。根据成员的每周计划和每日完成记录，"
        "生成一段简洁的工作建议（100-200字），指出做得好的地方和可以改进的方向。"
    )
    user_prompt = f"成员：{name}\n\n每周计划：\n{plans_text}\n\n每日工作完成记录：\n{completions_text}"

    result = call_deepseek(system_prompt, user_prompt)
    if result:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO monthly_summaries (uid, month_key, suggestion) VALUES (?, ?, ?) "
            "ON CONFLICT(uid, month_key) DO UPDATE SET suggestion=?, generated_at=CURRENT_TIMESTAMP",
            (uid, month_key, result, result)
        )
        conn.commit()
        conn.close()
    return result