"""URL 构建和工具函数"""
import re


def dedupe_base_url_path(base_url: str, target_url: str) -> str:
    """去除 URL 路径中与 base_url 尾部重复的段

    例: base="http://host/v1", target="http://host/v1/v1/messages"
        → "http://host/v1/messages"
    """
    if not base_url or not target_url:
        return target_url

    base_path = base_url.rstrip("/").split("//")[-1]
    if "/" in base_path:
        last_segment = "/" + base_path.rsplit("/", 1)[-1]
    else:
        return target_url

    doubled = last_segment + last_segment
    if doubled in target_url:
        return target_url.replace(doubled, last_segment, 1)

    return target_url


def build_openai_url(base_url: str, path: str) -> str:
    """构建 OpenAI 上游 URL。如果 base_url 已包含版本路径 (/v2, /v4 等)，不再拼 /v1。

    例: base="https://host/api/paas/v4", path="/v1/chat/completions"
        → "https://host/api/paas/v4/chat/completions"
    """
    stripped = base_url.rstrip("/")
    if re.search(r"/v\d+$", stripped):
        actual_path = re.sub(r"^/v\d+/", "/", path)
        raw_url = stripped + actual_path
    else:
        raw_url = stripped + path
    return dedupe_base_url_path(stripped, raw_url)


def mask_api_key(d: dict) -> dict:
    """遮盖 API Key 用于前端展示"""
    d = d.copy()
    k = d.get("api_key", "")
    d["api_key"] = (k[:4] + "****" + k[-4:]) if len(k) > 8 else ("****" if k else "")
    return d
