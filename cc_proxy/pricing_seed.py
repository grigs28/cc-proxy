"""默认定价种子数据 —— 启动时自动填充 model_pricing 表缺失的模型"""
import logging

logger = logging.getLogger("cc-proxy")

# 默认定价（美元/百万 tokens）
DEFAULT_PRICING = {
    "deepseek-v4-pro": {"display_name": "DeepSeek V4 Pro",
        "input": 0.435, "output": 0.870, "cache_read": 0.0036, "cache_create": 0},
    "deepseek-v4-flash": {"display_name": "DeepSeek V4 Flash",
        "input": 0.14, "output": 0.28, "cache_read": 0.0028, "cache_create": 0},
    "claude-sonnet-4-20250514": {"display_name": "Claude Sonnet 4",
        "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_create": 3.75},
    "claude-opus-4-20250514": {"display_name": "Claude Opus 4",
        "input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_create": 18.75},
    "claude-haiku-4-5-20251001": {"display_name": "Claude Haiku 4.5",
        "input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_create": 1.25},
    "gpt-4o": {"display_name": "GPT-4o",
        "input": 2.50, "output": 10.00, "cache_read": 1.25, "cache_create": 0},
    "gpt-4o-mini": {"display_name": "GPT-4o Mini",
        "input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_create": 0},
}


def seed_model_pricing():
    """为 model_pricing 表中不存在的模型插入默认价格"""
    from cc_proxy.db import get_conn, put_conn

    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("SELECT model_id FROM model_pricing")
        existing = {row[0] for row in cur.fetchall()}

        inserted = 0
        for model_id, info in DEFAULT_PRICING.items():
            if model_id in existing:
                continue
            cur.execute("""
                INSERT INTO model_pricing (model_id, display_name,
                    input_cost_per_million, output_cost_per_million,
                    cache_read_cost_per_million, cache_creation_cost_per_million)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                model_id, info["display_name"],
                info["input"], info["output"],
                info["cache_read"], info["cache_create"],
            ))
            inserted += 1

        conn.commit()
        cur.close()
        if inserted:
            logger.info(f"定价种子: 已插入 {inserted} 个模型默认定价")
    except Exception as e:
        conn.rollback()
        logger.warning(f"定价种子插入失败: {e}")
    finally:
        put_conn(conn)
