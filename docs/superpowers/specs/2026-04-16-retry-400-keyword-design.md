# 重试逻辑增强设计

## 背景

上游提供商有时返回 HTTP 400（如 `"网络错误...请稍后重试"`），属于瞬态错误应重试。
当前 `RETRY_STATUSES = {404, 429, 500, 502, 503, 529}` 不包含 400，且重试日志不够详细。

## 改动

仅修改 `cc_proxy/client.py`。

### 1. 扩大重试状态码

```python
# 之前
RETRY_STATUSES = {404, 429, 500, 502, 503, 529}

# 之后
RETRY_STATUSES = {400, 404, 429, 500, 502, 503, 529}
```

新增 400，其余不变。

### 2. 增强重试日志

6 个重试循环中，将现有的 warning 日志改为输出**重试内容 + 重试次数**：

```python
# 之前
logger.warning(f"<- anthropic stream {resp.status_code} (attempt {attempt+1}): {err[:300]}")

# 之后
if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
    logger.warning(f"<- {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {err[:300]}")
    await asyncio.sleep(attempt + 1)
    continue
logger.warning(f"<- {resp.status_code} 重试耗尽: {err[:300]}")
```

### 不改动

- `MAX_RETRIES = 3` 不变
- 重试间隔 `attempt + 1` 秒不变
- 其他模块不变
- 不做关键词匹配，400 直接重试

## 测试

1. 400 → 重试最多 3 次后返回
2. 500/502/503/529 → 原有行为不变
3. 日志包含重试内容和次数
