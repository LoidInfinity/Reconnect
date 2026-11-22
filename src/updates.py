"""Read GitHub Releases without downloading or executing update code."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app_info import VERSION, REPOSITORY, RELEASES_URL


def version_key(value: str):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise ValueError(f"无效版本号：{value}")
    major, minor, patch, pre = match.groups()
    identifiers = pre.split(".") if pre else []
    if any(not item or (item.isdigit() and len(item) > 1 and item.startswith("0")) for item in identifiers):
        raise ValueError(f"无效版本号：{value}")
    prerelease = tuple((0, int(item)) if item.isdigit() else (1, item) for item in identifiers)
    return (int(major), int(minor), int(patch), pre is None, prerelease)


def select_release(releases: list, current: str = VERSION):
    current_key = version_key(current)
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name", "")
        if not isinstance(tag, str):
            continue
        try:
            key = version_key(tag)
        except ValueError:
            continue
        # Alpha versions also receive prereleases; stable versions stay stable.
        if current_key[3] and (release.get("prerelease") or not key[3]):
            continue
        candidates.append((key, tag))
    if not candidates:
        return dict(available=False, latest=None, url=RELEASES_URL)
    key, tag = max(candidates)
    return dict(available=key > current_key, latest=tag,
                url=RELEASES_URL + "/tag/" + quote(tag, safe=""))


def check_updates(current: str = VERSION):
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": f"NetworkGuard/{current}",
                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(request, timeout=15) as response:
            releases = json.load(response)
    except HTTPError as exc:
        if exc.code in (403, 429):
            raise RuntimeError("GitHub 暂时限制请求，请稍后重试。") from exc
        raise RuntimeError(f"GitHub 请求失败（HTTP {exc.code}）。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("无法连接 GitHub，请检查网络后重试。") from exc
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("GitHub 返回的更新信息无法解析。") from exc
    if not isinstance(releases, list):
        raise RuntimeError("GitHub 返回的更新信息格式不正确。")
    return select_release(releases, current)
