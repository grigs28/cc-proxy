"""数据库核心模块 — openGauss 原生 SQL + psycopg2"""
import json
import logging
import threading
import time
from typing import Any, Optional

import psycopg2
from psycopg2 import pool

logger = logging.getLogger("cc-proxy")

# 连接池
_pool: Optional[pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def init_db(db_config: dict[str, Any]) -> None:
    """初始化数据库连接池并建表"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        _pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=db_config["host"],
            port=db_config.get("port", 5432),
            dbname=db_config.get("name", db_config.get("database", "cc_proxy")),
            user=db_config["user"],
            password=db_config["password"],
        )
    logger.info(f"数据库连接池已创建: {db_config['host']}:{db_config.get('port', 5432)}/{db_config.get('name', 'cc_proxy')}")
    _create_tables()


def _create_tables() -> None:
    """建表（IF NOT EXISTS）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS providers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                api_key TEXT NOT NULL,
                timeout INTEGER DEFAULT 300,
                provider_type VARCHAR(20) DEFAULT 'openai',
                supported_formats TEXT DEFAULT 'openai,anthropic',
                base_url_openai TEXT DEFAULT '',
                base_url_anthropic TEXT DEFAULT '',
                base_url TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id SERIAL PRIMARY KEY,
                model_id VARCHAR(200) NOT NULL,
                display_name VARCHAR(200),
                alias_name VARCHAR(200) DEFAULT '',
                supported_formats TEXT DEFAULT 'openai,anthropic',
                auth_style VARCHAR(20) DEFAULT 'auto',
                strip_fields BOOLEAN DEFAULT FALSE,
                provider_id INTEGER REFERENCES providers(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(provider_id, model_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                sso_user_id VARCHAR(100) UNIQUE,
                username VARCHAR(100) UNIQUE NOT NULL,
                display_name VARCHAR(200),
                is_local_admin BOOLEAN DEFAULT FALSE,
                last_login TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS model_map (
                source VARCHAR(200) PRIMARY KEY,
                target VARCHAR(200) NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS request_stats (
                id SERIAL PRIMARY KEY,
                model_id VARCHAR(200) NOT NULL,
                provider_name VARCHAR(100),
                request_count INTEGER DEFAULT 0,
                last_request TIMESTAMP,
                UNIQUE(model_id, provider_name)
            )
        """)
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def get_conn():
    """从连接池获取连接"""
    if _pool is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _pool.getconn()


def put_conn(conn):
    """归还连接到连接池"""
    if _pool is not None:
        _pool.putconn(conn)


# ============================================================
# Provider CRUD
# ============================================================

def db_get_providers() -> list[dict[str, Any]]:
    """获取所有提供商（含模型）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, api_key, timeout, provider_type, supported_formats,
                   base_url_openai, base_url_anthropic, base_url
            FROM providers ORDER BY id
        """)
        providers = []
        for row in cur.fetchall():
            pid, name, api_key, timeout, ptype, fmts_str, url_o, url_a, url_b = row
            models = _get_models_by_provider(cur, pid)
            providers.append({
                "id": pid,
                "name": name,
                "api_key": api_key,
                "timeout": timeout,
                "provider_type": ptype,
                "supported_formats": _parse_formats(fmts_str),
                "base_url_openai": url_o or "",
                "base_url_anthropic": url_a or "",
                "base_url": url_b or "",
                "models": models,
            })
        cur.close()
        return providers
    finally:
        put_conn(conn)


def db_get_provider(name: str) -> Optional[dict[str, Any]]:
    """按名称获取单个提供商"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, api_key, timeout, provider_type, supported_formats,
                   base_url_openai, base_url_anthropic, base_url
            FROM providers WHERE name = %s
        """, (name,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        pid, name, api_key, timeout, ptype, fmts_str, url_o, url_a, url_b = row
        models = _get_models_by_provider(cur, pid)
        cur.close()
        return {
            "id": pid,
            "name": name,
            "api_key": api_key,
            "timeout": timeout,
            "provider_type": ptype,
            "supported_formats": _parse_formats(fmts_str),
            "base_url_openai": url_o or "",
            "base_url_anthropic": url_a or "",
            "base_url": url_b or "",
            "models": models,
        }
    finally:
        put_conn(conn)


def db_add_provider(data: dict[str, Any]) -> dict[str, Any]:
    """添加提供商"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        fmts = ",".join(data.get("supported_formats", ["openai", "anthropic"]))
        cur.execute("""
            INSERT INTO providers (name, api_key, timeout, provider_type, supported_formats,
                                    base_url_openai, base_url_anthropic, base_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data["name"], data["api_key"], data.get("timeout", 300),
            data.get("type", "openai"), fmts,
            data.get("base_url_openai", ""), data.get("base_url_anthropic", ""),
            data.get("base_url", ""),
        ))
        pid = cur.fetchone()[0]
        conn.commit()
        result = db_get_provider(data["name"])
        cur.close()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_update_provider(name: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """更新提供商"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        fields = []
        values = []
        for key, col in [("name", "name"), ("api_key", "api_key"), ("timeout", "timeout"),
                         ("type", "provider_type"), ("base_url_openai", "base_url_openai"),
                         ("base_url_anthropic", "base_url_anthropic"), ("base_url", "base_url")]:
            if key in data:
                fields.append(f"{col} = %s")
                values.append(data[key])
        if "supported_formats" in data:
            fields.append("supported_formats = %s")
            values.append(",".join(data["supported_formats"]))
        if not fields:
            cur.close()
            return db_get_provider(name)
        values.append(name)
        sql = f"UPDATE providers SET {', '.join(fields)} WHERE name = %s"
        cur.execute(sql, values)
        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            return None
        conn.commit()
        # 如果改名，用新名查
        new_name = data.get("name", name)
        result = db_get_provider(new_name)
        cur.close()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_delete_provider(name: str) -> bool:
    """删除提供商"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM providers WHERE name = %s", (name,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ============================================================
# Model CRUD
# ============================================================

def _get_models_by_provider(cur, provider_id: int) -> list[dict[str, Any]]:
    """获取指定提供商的所有模型（使用现有游标）"""
    cur.execute("""
        SELECT model_id, display_name, alias_name, supported_formats, auth_style, strip_fields
        FROM models WHERE provider_id = %s ORDER BY id
    """, (provider_id,))
    models = []
    for row in cur.fetchall():
        mid, dname, alias, fmts_str, auth, strip = row
        models.append({
            "id": mid,
            "display_name": dname or mid,
            "alias": alias or "",
            "supported_formats": _parse_formats(fmts_str),
            "auth_style": auth or "auto",
            "strip_fields": bool(strip),
        })
    return models


def db_get_all_models() -> list[dict[str, Any]]:
    """获取所有模型（含提供商名）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.model_id, m.display_name, m.alias_name, m.supported_formats,
                   m.auth_style, m.strip_fields, p.name AS provider_name
            FROM models m JOIN providers p ON m.provider_id = p.id
            ORDER BY p.id, m.id
        """)
        models = []
        for row in cur.fetchall():
            mid, dname, alias, fmts_str, auth, strip, pname = row
            models.append({
                "id": mid,
                "display_name": dname or mid,
                "alias": alias or "",
                "supported_formats": _parse_formats(fmts_str),
                "auth_style": auth or "auto",
                "strip_fields": bool(strip),
                "provider_name": pname,
            })
        cur.close()
        return models
    finally:
        put_conn(conn)


def db_add_model(provider_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """添加模型"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        # 获取 provider_id
        cur.execute("SELECT id FROM providers WHERE name = %s", (provider_name,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"提供商 '{provider_name}' 不存在")
        provider_id = row[0]
        fmts = ",".join(data.get("supported_formats", ["openai", "anthropic"]))
        cur.execute("""
            INSERT INTO models (model_id, display_name, alias_name, supported_formats, auth_style, strip_fields, provider_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data["id"],
            data.get("display_name", data["id"]),
            data.get("alias", ""),
            fmts,
            data.get("auth_style", "auto"),
            data.get("strip_fields", False),
            provider_id,
        ))
        conn.commit()
        result = {
            "id": data["id"],
            "display_name": data.get("display_name", data["id"]),
            "alias": data.get("alias", ""),
            "supported_formats": data.get("supported_formats", ["openai", "anthropic"]),
            "auth_style": data.get("auth_style", "auto"),
            "strip_fields": data.get("strip_fields", False),
            "provider_name": provider_name,
        }
        cur.close()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_update_model(provider_name: str, model_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """更新模型"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM providers WHERE name = %s", (provider_name,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        provider_id = row[0]
        fields = []
        values = []
        if "display_name" in data:
            fields.append("display_name = %s")
            values.append(data["display_name"])
        if "alias" in data:
            fields.append("alias_name = %s")
            values.append(data["alias"])
        if "supported_formats" in data:
            fields.append("supported_formats = %s")
            values.append(",".join(data["supported_formats"]))
        if "auth_style" in data:
            fields.append("auth_style = %s")
            values.append(data["auth_style"])
        if "strip_fields" in data:
            fields.append("strip_fields = %s")
            values.append(data["strip_fields"])
        if "id" in data and data["id"] != model_id:
            fields.append("model_id = %s")
            values.append(data["id"])
        if not fields:
            cur.close()
            return None
        values.extend([provider_id, model_id])
        sql = f"UPDATE models SET {', '.join(fields)} WHERE provider_id = %s AND model_id = %s"
        cur.execute(sql, values)
        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            return None
        conn.commit()
        new_id = data.get("id", model_id)
        cur.execute("""
            SELECT m.model_id, m.display_name, m.alias_name, m.supported_formats,
                   m.auth_style, m.strip_fields, p.name
            FROM models m JOIN providers p ON m.provider_id = p.id
            WHERE m.provider_id = %s AND m.model_id = %s
        """, (provider_id, new_id))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        mid, dname, alias, fmts_str, auth, strip, pname = row
        return {
            "id": mid, "display_name": dname or mid, "alias": alias or "",
            "supported_formats": _parse_formats(fmts_str), "auth_style": auth or "auto",
            "strip_fields": bool(strip), "provider_name": pname,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_delete_model(provider_name: str, model_id: str) -> bool:
    """删除模型"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM models USING providers
            WHERE models.provider_id = providers.id
              AND providers.name = %s AND models.model_id = %s
        """, (provider_name, model_id))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_find_model(model_id: str) -> Optional[tuple[dict, dict]]:
    """按模型 ID 或别名查找，返回 (provider_dict, model_dict) 或 None"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        # 先按 model_id 查
        cur.execute("""
            SELECT m.model_id, m.display_name, m.alias_name, m.supported_formats,
                   m.auth_style, m.strip_fields,
                   p.id AS pid, p.name, p.api_key, p.timeout, p.provider_type,
                   p.supported_formats AS p_fmts, p.base_url_openai, p.base_url_anthropic, p.base_url
            FROM models m JOIN providers p ON m.provider_id = p.id
            WHERE m.model_id = %s
        """, (model_id,))
        row = cur.fetchone()
        # 如果没找到，按别名查（跳过空别名）
        if not row and model_id:
            cur.execute("""
                SELECT m.model_id, m.display_name, m.alias_name, m.supported_formats,
                       m.auth_style, m.strip_fields,
                       p.id AS pid, p.name, p.api_key, p.timeout, p.provider_type,
                       p.supported_formats AS p_fmts, p.base_url_openai, p.base_url_anthropic, p.base_url
                FROM models m JOIN providers p ON m.provider_id = p.id
                WHERE m.alias_name = %s AND m.alias_name != ''
            """, (model_id,))
            row = cur.fetchone()
        cur.close()
        if not row:
            return None
        (mid, dname, alias, mfmts, auth, strip,
         pid, pname, pkey, ptimeout, ptype, pfmts, purl_o, purl_a, purl_b) = row
        provider = {
            "id": pid, "name": pname, "api_key": pkey, "timeout": ptimeout,
            "provider_type": ptype, "supported_formats": _parse_formats(pfmts),
            "base_url_openai": purl_o or "", "base_url_anthropic": purl_a or "",
            "base_url": purl_b or "",
        }
        model = {
            "id": mid, "display_name": dname or mid, "alias": alias or "",
            "supported_formats": _parse_formats(mfmts), "auth_style": auth or "auto",
            "strip_fields": bool(strip), "provider_name": pname,
        }
        return provider, model
    finally:
        put_conn(conn)


# ============================================================
# User CRUD
# ============================================================

def db_upsert_user(sso_user_id: str, username: str, display_name: str,
                   is_admin: bool = False) -> dict[str, Any]:
    """创建或更新用户（SSO 登录时调用）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (sso_user_id, username, display_name, is_local_admin, last_login)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                display_name = VALUES(display_name),
                last_login = CURRENT_TIMESTAMP
        """, (sso_user_id, username, display_name, is_admin))
        conn.commit()
        # 查询刚 upsert 的记录
        cur.execute("SELECT id, username, display_name, is_local_admin FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        return {"id": row[0], "username": row[1], "display_name": row[2], "is_local_admin": row[3]}
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_get_user(username: str) -> Optional[dict[str, Any]]:
    """获取用户"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, sso_user_id, username, display_name, is_local_admin, last_login, created_at
            FROM users WHERE username = %s
        """, (username,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {
            "id": row[0], "sso_user_id": row[1], "username": row[2],
            "display_name": row[3], "is_local_admin": row[4],
            "last_login": str(row[5]) if row[5] else None,
            "created_at": str(row[6]) if row[6] else None,
        }
    finally:
        put_conn(conn)


def db_list_users() -> list[dict[str, Any]]:
    """列出所有用户"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, sso_user_id, username, display_name, is_local_admin, last_login, created_at
            FROM users ORDER BY id
        """)
        users = []
        for row in cur.fetchall():
            users.append({
                "id": row[0], "sso_user_id": row[1], "username": row[2],
                "display_name": row[3], "is_local_admin": row[4],
                "last_login": str(row[5]) if row[5] else None,
                "created_at": str(row[6]) if row[6] else None,
            })
        cur.close()
        return users
    finally:
        put_conn(conn)


def db_set_admin(username: str, is_admin: bool) -> bool:
    """设置用户管理员状态"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_local_admin = %s WHERE username = %s", (is_admin, username))
        updated = cur.rowcount > 0
        conn.commit()
        cur.close()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_is_admin(username: str) -> bool:
    """检查用户是否为管理员"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_local_admin FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        return bool(row and row[0])
    finally:
        put_conn(conn)


# ============================================================
# Model Map
# ============================================================

def db_get_model_map() -> dict[str, str]:
    """获取所有模型映射"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT source, target FROM model_map")
        result = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()
        return result
    finally:
        put_conn(conn)


def db_set_model_map(source: str, target: str) -> None:
    """设置模型映射"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO model_map (source, target) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE target = VALUES(target)
        """, (source, target))
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_delete_model_map(source: str) -> bool:
    """删除模型映射"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM model_map WHERE source = %s", (source,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_set_model_map_all(mapping: dict[str, str]) -> None:
    """批量替换模型映射"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM model_map")
        if mapping:
            args = [(k, v) for k, v in mapping.items()]
            cur.executemany("INSERT INTO model_map (source, target) VALUES (%s, %s)", args)
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ============================================================
# Settings (key-value)
# ============================================================

def db_get_setting(key: str, default: Any = None) -> Any:
    """获取单个配置"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        if not row or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]
    finally:
        put_conn(conn)


def db_set_setting(key: str, value: Any) -> None:
    """设置单个配置"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        v = json.dumps(value) if not isinstance(value, str) else value
        cur.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = CURRENT_TIMESTAMP
        """, (key, v))
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def db_get_all_settings() -> dict[str, Any]:
    """获取所有配置"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        result = {}
        for key, value in cur.fetchall():
            try:
                result[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                result[key] = value
        cur.close()
        return result
    finally:
        put_conn(conn)


# ============================================================
# Stats
# ============================================================

def db_increment_stat(model_id: str, provider_name: str) -> None:
    """递增请求统计"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO request_stats (model_id, provider_name, request_count, last_request)
            VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                request_count = request_stats.request_count + 1,
                last_request = CURRENT_TIMESTAMP
        """, (model_id, provider_name))
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.warning(f"统计写入失败: {e}")
    finally:
        put_conn(conn)


def db_get_stats() -> dict[str, Any]:
    """获取统计数据"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(request_count), 0) FROM request_stats")
        total = cur.fetchone()[0]
        cur.execute("SELECT model_id, SUM(request_count) FROM request_stats GROUP BY model_id")
        by_model = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT provider_name, SUM(request_count) FROM request_stats GROUP BY provider_name")
        by_provider = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()
        return {"total_requests": total, "by_model": by_model, "by_provider": by_provider}
    finally:
        put_conn(conn)


# ============================================================
# 数据迁移
# ============================================================

def migrate_from_yaml(yaml_config: dict[str, Any]) -> None:
    """从 YAML 配置迁移数据到数据库（仅首次运行）"""
    # 检查是否已迁移
    if db_get_setting("migrated"):
        logger.info("数据已迁移过，跳过")
        return

    logger.info("开始从 .env 迁移数据到数据库...")

    # 迁移 providers 和 models
    for p in yaml_config.get("providers", []):
        try:
            db_add_provider(p)
            # 添加模型
            if p.get("models"):
                for m in p["models"]:
                    db_add_model(p["name"], m)
            logger.info(f"  迁移提供商: {p['name']} ({len(p.get('models', []))} 个模型)")
        except Exception as e:
            logger.warning(f"  迁移提供商 {p.get('name')} 失败: {e}")

    # 迁移 model_map
    mm = yaml_config.get("model_map", {})
    if mm:
        db_set_model_map_all(mm)
        logger.info(f"  迁移模型映射: {len(mm)} 条")

    # 迁移 settings
    settings_to_migrate = {
        "passthrough_paths": yaml_config.get("server", {}).get("passthrough_paths", []),
        "sso_public_paths": yaml_config.get("sso_public_paths", []),
        "sso_admin_users": yaml_config.get("sso_admin_users", []),
    }
    for key, value in settings_to_migrate.items():
        db_set_setting(key, value)
    logger.info(f"  迁移系统配置: {len(settings_to_migrate)} 项")

    # 标记已迁移
    db_set_setting("migrated", True)
    logger.info("数据迁移完成")


# ============================================================
# 工具函数
# ============================================================

def _parse_formats(fmts_str: Optional[str]) -> list[str]:
    """解析逗号分隔的格式字符串"""
    if not fmts_str:
        return ["openai", "anthropic"]
    return [f.strip() for f in fmts_str.split(",") if f.strip()]
