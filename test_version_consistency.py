"""版本号必须在代码、文档与发布说明之间保持一致。

发版时最容易漏改的就是 README 与 CHANGELOG，这里把它们和
``app_info.VERSION`` 绑在一起，不一致就直接测试失败。
"""
import re
from pathlib import Path
import unittest

import app_info
import updates

ROOT = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _changelog_versions() -> list[str]:
    return re.findall(r"^## (\S+)", _read("CHANGELOG.md"), flags=re.MULTILINE)


class VersionConsistencyTests(unittest.TestCase):
    def test_version_format_is_semver_with_optional_prerelease(self):
        self.assertRegex(
            app_info.VERSION,
            r"^\d+\.\d+\.\d+(-(alpha|beta|rc)\.\d+)?$",
            "VERSION 必须是 x.y.z 或 x.y.z-beta.N 形式",
        )

    def test_version_is_parseable_by_update_checker(self):
        core = app_info.VERSION.split("-", 1)[0]
        expected = tuple(int(part) for part in core.split("."))
        self.assertEqual(updates.version_key(app_info.VERSION)[:3], expected)

    def test_changelog_latest_section_matches_version(self):
        versions = _changelog_versions()
        self.assertTrue(versions, "CHANGELOG 中没有任何版本章节")
        self.assertEqual(
            versions[0],
            app_info.VERSION,
            "CHANGELOG 最新章节与 app_info.VERSION 不一致",
        )

    def test_changelog_sections_are_strictly_descending(self):
        versions = _changelog_versions()
        keys = [updates.version_key(version) for version in versions]
        for newer, older in zip(keys, keys[1:]):
            self.assertGreater(
                newer,
                older,
                f"CHANGELOG 版本顺序不是从新到旧：{versions}",
            )

    def test_readme_current_version_matches(self):
        match = re.search(r"当前版本 \*\*([^*]+)\*\*", _read("README.md"))
        self.assertIsNotNone(match, "README 缺少“当前版本 **x.y.z**”声明")
        self.assertEqual(
            match.group(1),
            app_info.VERSION,
            "README 的当前版本与 app_info.VERSION 不一致",
        )

    def test_readme_release_example_uses_current_tag(self):
        tags = set(re.findall(r"git (?:tag -a|push origin) (v\S+)", _read("README.md")))
        self.assertTrue(tags, "README 的发布示例里没有出现标签")
        self.assertEqual(
            tags,
            {f"v{app_info.VERSION}"},
            "README 发布示例中的标签与当前版本不一致",
        )

    def test_windows_file_version_is_derivable(self):
        """build.py 用 主.次.修订.beta序号 生成 exe 的文件版本号。"""
        core, _, prerelease = app_info.VERSION.partition("-")
        revision = int(prerelease.rsplit(".", 1)[-1]) if prerelease else 0
        version = tuple(int(part) for part in core.split(".")) + (revision,)
        self.assertEqual(len(version), 4, f"Windows 文件版本必须是 4 段：{version}")
        for part in version:
            self.assertGreaterEqual(part, 0)
            self.assertLessEqual(part, 65535, f"Windows 文件版本段越界：{version}")


if __name__ == "__main__":
    unittest.main()
