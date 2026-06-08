"""配置导出/导入 —— JSON 格式，含脱敏处理"""
import json
import logging

logger = logging.getLogger("cc-proxy")


def export_config() -> dict:
    """导出全部配置为字典"""
    from cc_proxy.db import db_export_all
    return db_export_all()


def import_config(data: dict) -> dict:
    """导入配置。预计调用方已有备份逻辑。

    Args:
        data: 与 export_config 返回格式一致的字典

    Returns:
        {"ok": True, "message": "..."} 或 {"ok": False, "error": "..."}
    """
    from cc_proxy.db import db_import_all
    from cc_proxy.providers import get_registry

    try:
        result = db_import_all(data)
        get_registry().reload()
        logger.info("配置已导入并重载")
        return result
    except Exception as e:
        logger.error(f"配置导入失败: {e}")
        return {"ok": False, "error": str(e)}
