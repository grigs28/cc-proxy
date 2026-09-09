"""版本号与 CHANGELOG.md 一致性校验"""
import re
from pathlib import Path


def test_version_matches_changelog():
    """proxy.VERSION 必须等于 CHANGELOG.md 首个版本标题（版本号唯一来源）"""
    from cc_proxy.proxy import VERSION

    changelog = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    m = re.search(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?", text, re.MULTILINE)
    assert m, "CHANGELOG.md 中未找到 '## [x.y.z]' 版本标题"
    assert VERSION == m.group(1), f"VERSION={VERSION} 与 CHANGELOG 最新版本 {m.group(1)} 不一致"
