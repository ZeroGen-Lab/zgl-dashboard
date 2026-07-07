import json
import uuid
from datetime import datetime
from openai import OpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from db import get_db_connection

MODEL = 'deepseek-v4-flash'
ADVANCED_MODEL = 'deepseek-v4-pro'


def _extract_usage(usage):
    """从 OpenAI SDK 的 response.usage 提取 token 用量，usage 为空返回 None。
    如有 completion_tokens_details.reasoning_tokens 则一并取出。
    """
    if not usage:
        return None
    out = {
        'prompt_tokens': getattr(usage, 'prompt_tokens', None),
        'completion_tokens': getattr(usage, 'completion_tokens', None),
        'total_tokens': getattr(usage, 'total_tokens', None),
    }
    details = getattr(usage, 'completion_tokens_details', None)
    reasoning = getattr(details, 'reasoning_tokens', None) if details is not None else None
    if reasoning is not None:
        out['reasoning_tokens'] = reasoning
    return out


def _log_llm_call(session_id, request_id, model, messages, content,
                  token_usage, status, error, uid, requested_at):
    """把一次 LLM 调用落库到 llm_calls。best-effort：任何异常都 pass 吞掉，不影响调用本身。"""
    try:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO llm_calls "
                "(request_id, session_id, uid, model, prompt, response, status, token_usage, error, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, session_id, uid, model,
                 json.dumps(messages, ensure_ascii=False),
                 content,
                 status,
                 json.dumps(token_usage, ensure_ascii=False) if token_usage else None,
                 error,
                 requested_at))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def call_deepseek(system_prompt, user_prompt, model=MODEL, session_id=None, request_id=None, uid=None):
    """调用 DeepSeek chat completions API，返回文本响应。

    每次调用都会落库到 llm_calls：
        - request_id 唯一标识本次请求
        - session_id 用于把同一会话(多轮)的请求归到一组（调用方在入口处生成并透传，未传则回退为新 UUID）。
    落库是 best-effort，失败不影响调用本身；出错时静默返回 None（保持原行为）。
    """
    session_id = session_id or uuid.uuid4().hex
    request_id = request_id or uuid.uuid4().hex
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    requested_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if not DEEPSEEK_API_KEY:
        _log_llm_call(session_id, request_id, model, messages, None, None,
                      'error', 'DEEPSEEK_API_KEY not configured', uid, requested_at)
        return None

    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        content = response.choices[0].message.content
        _log_llm_call(session_id, request_id, model, messages, content,
                      _extract_usage(getattr(response, 'usage', None)),
                      'success', None, uid, requested_at)
        return content
    except Exception as e:
        _log_llm_call(session_id, request_id, model, messages, None, None,
                      'error', str(e), uid, requested_at)
        return None


def generate_daily_summary(uid, name, completions_text, session_id=None):
    """为某成员生成 daily completion 摘要并存入 monthly_summaries"""
    from db import get_db_connection
    from helpers import compute_month_range
    _, _, month_key = compute_month_range(0)

    system_prompt = (
        "你是一个团队工作摘要助手。请根据以下成员的每日工作完成记录，"
        "生成一段简洁的月度工作摘要（200-300字），概括主要工作内容，突出成果。"
    )
    user_prompt = f"成员：{name}\n\n每日工作完成记录：\n{completions_text}"

    result = call_deepseek(system_prompt, user_prompt, session_id=session_id, uid=uid)
    if result:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO monthly_summaries (uid, month_key, summary) VALUES (?, ?, ?) "
            "ON CONFLICT(uid, month_key) DO UPDATE SET summary=?, generated_at=datetime('now','localtime')",
            (uid, month_key, result, result)
        )
        conn.commit()
        conn.close()
    return result


def generate_work_suggestion(uid, name, completions_text, plans_text, session_id=None):
    """为某成员结合 weekly plan 和 daily completion 生成工作建议并存入 monthly_summaries"""
    from db import get_db_connection
    from helpers import compute_month_range
    _, _, month_key = compute_month_range(0)

    system_prompt = (
        "你是一个团队工作建议助手。根据成员的每周计划和每日完成记录，"
        "生成一段简洁的工作建议（100-200字），指出做得好的地方和可以改进的方向。"
    )
    user_prompt = f"成员：{name}\n\n每周计划：\n{plans_text}\n\n每日工作完成记录：\n{completions_text}"

    result = call_deepseek(system_prompt, user_prompt, session_id=session_id, uid=uid)
    if result:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO monthly_summaries (uid, month_key, suggestion) VALUES (?, ?, ?) "
            "ON CONFLICT(uid, month_key) DO UPDATE SET suggestion=?, generated_at=datetime('now','localtime')",
            (uid, month_key, result, result)
        )
        conn.commit()
        conn.close()
    return result


def judge_completed_plan_items(review, items, session_id=None, uid=None):
    """根据 review 判断 items(本周开放项) 中哪些已被完成，返回判定完成的 id 列表。

    - items: [{'id': int, 'text': str}, ...]，仅含 status='open' 的项。
    - 保守判定：仅当回顾清楚地表明该计划项已完成时才返回其 id。
    - 调用 deepseek-v4-pro，强制只输出 JSON {"done_ids":[id,...]}；解析失败/无 key/报错一律返回 []。
    """
    if not review or not items:
        return []
    import json
    import re
    system = (
        "你是一个工作完成情况判定助手。给定今天的工作回顾(review)和若干本周计划项(每项含 id 和 text)，"
        "判断哪些计划项已被该回顾明显完成。判定要保守：仅当回顾清楚地表明该计划项已完成时才标记为完成。"
        "只输出 JSON，不要任何额外文字或解释，格式：{\"done_ids\":[id,...]}；若没有则输出 {\"done_ids\":[]}。"
    )
    user = (f"今日工作回顾：\n{review}\n\n本周待判定的计划项：\n"
            f"{json.dumps(items, ensure_ascii=False)}")
    text = call_deepseek(system, user, model=ADVANCED_MODEL, session_id=session_id, uid=uid)
    if not text:
        return []
    try:
        m = re.search(r'\{.*\}', text, re.S)
        obj = json.loads(m.group(0)) if m else {}
        valid = {it['id'] for it in items}
        return [int(i) for i in obj.get('done_ids', []) if int(i) in valid]
    except Exception:
        return []