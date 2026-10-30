"""The extension is JavaScript, but its safety properties are checked from here.

The manifest is what actually limits which sites the extension can read, so it is
tested as a security boundary, not as configuration.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from projectmind.sources.browser import DEFAULT_ALLOW, BrowserConfig

EXTENSION = Path(__file__).resolve().parents[1] / "extension"


def manifest() -> dict[str, object]:
    return json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def pattern(domain: str) -> str:
    return f"https://*.{domain.removeprefix('*.').removeprefix('www.')}/*"


class TestManifest:
    def test_it_is_manifest_v3(self) -> None:
        assert manifest()["manifest_version"] == 3

    def test_it_cannot_read_every_site(self) -> None:
        text = json.dumps(manifest())
        assert "<all_urls>" not in text
        assert '"*://*/*"' not in text
        assert '"https://*/*"' not in text
        assert '"http://*/*"' not in text

    def test_the_sites_it_can_read_are_exactly_the_default_allow_list(self) -> None:
        expected = sorted({pattern(domain) for domain in DEFAULT_ALLOW})
        hosts = manifest()["host_permissions"]
        assert isinstance(hosts, list)
        assert sorted(h for h in hosts if h.startswith("https://")) == expected

    def test_content_scripts_run_only_on_those_sites(self) -> None:
        [script] = manifest()["content_scripts"]  # type: ignore[misc]
        expected = sorted({pattern(domain) for domain in DEFAULT_ALLOW})
        assert sorted(script["matches"]) == expected
        assert script["all_frames"] is False

    def test_it_talks_only_to_this_computer(self) -> None:
        hosts = manifest()["host_permissions"]
        assert "http://127.0.0.1/*" in hosts  # type: ignore[operator]
        assert not any(h.startswith("http://") and "127.0.0.1" not in h for h in hosts)  # type: ignore[union-attr]

    def test_it_asks_for_no_more_than_it_uses(self) -> None:
        assert set(manifest()["permissions"]) == {  # type: ignore[call-overload]
            "storage",
            "alarms",
            "tabs",
            "scripting",
            "activeTab",
        }

    def test_every_file_it_names_exists(self) -> None:
        data = manifest()
        names = [data["background"]["service_worker"], data["action"]["default_popup"]]  # type: ignore[index]
        names += data["content_scripts"][0]["js"]  # type: ignore[index]
        names.append(data["options_ui"]["page"])  # type: ignore[index]
        for name in names:
            assert (EXTENSION / name).is_file(), name

    def test_the_version_matches_the_package(self) -> None:
        from projectmind import __version__

        assert manifest()["version"] == __version__


class TestSources:
    def test_the_extension_sends_nothing_to_any_remote_host(self) -> None:
        for path in EXTENSION.glob("*.js"):
            text = path.read_text(encoding="utf-8")
            for url in re.findall(r"https?://[^\s'\"`)]+", text):
                assert url.startswith(("http://127.0.0.1", "https://*", "https://docs")), (
                    path.name,
                    url,
                )

    def test_there_is_no_analytics_or_eval(self) -> None:
        for path in EXTENSION.glob("*.js"):
            text = path.read_text(encoding="utf-8")
            assert "eval(" not in text
            assert "new Function" not in text
            assert "XMLHttpRequest" not in text

    def test_the_default_allow_list_has_no_personal_or_commerce_sites(self) -> None:
        config = BrowserConfig(enabled=True)
        for host in ("mail.google.com", "www.facebook.com", "www.amazon.in", "web.whatsapp.com"):
            assert not config.permits(host)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_javascript_unit_tests_pass() -> None:
    files = sorted(str(path) for path in (EXTENSION / "test").glob("*.test.js"))
    assert files, "the extension has no unit tests"
    result = subprocess.run(
        ["node", "--test", *files],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
