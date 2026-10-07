"""Tests for named Atlassian instances (utils/instances.py)."""

import json
import os

import pytest

from mcp_atlassian.utils import instances
from mcp_atlassian.utils.instances import (
    InstanceError,
    get_instance,
    instances_configured,
    load_instances,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    instances._cache = None
    yield
    instances._cache = None


@pytest.fixture
def declared(tmp_path, monkeypatch):
    """Write an instances file with one Jira and two Confluence instances."""
    cookies = {}
    for name in ("jira", "main", "labs"):
        cookie = tmp_path / f"{name}-cookie"
        cookie.write_text(f"JSESSIONID={name}-1\n", encoding="utf-8")
        cookies[name] = cookie
    path = tmp_path / "instances.json"
    path.write_text(
        json.dumps(
            {
                "jira": {
                    "main": {
                        "url": "https://jira.example.com/",
                        "cookie_file": str(cookies["jira"]),
                        "projects_filter": "PROJ",
                    }
                },
                "confluence": {
                    "main": {
                        "url": "https://confluence.example.com",
                        "cookie_file": str(cookies["main"]),
                    },
                    "labs": {
                        "url": "https://labs.example.com/wiki",
                        "cookie_file": str(cookies["labs"]),
                        "spaces_filter": "LAB",
                        "ssl_verify": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(instances.INSTANCES_FILE_ENV, str(path))
    return path, cookies


def test_no_file_configured_means_no_instances(monkeypatch):
    monkeypatch.delenv(instances.INSTANCES_FILE_ENV, raising=False)
    assert not instances_configured()
    assert load_instances() == {"jira": {}, "confluence": {}}
    with pytest.raises(InstanceError, match="Declared: none"):
        get_instance("jira", "main")


def test_instances_are_loaded_by_service_and_name(declared):
    loaded = load_instances()
    assert instances_configured()
    assert sorted(loaded["jira"]) == ["main"]
    assert sorted(loaded["confluence"]) == ["labs", "main"]
    assert get_instance("jira", "main").url == "https://jira.example.com"


def test_config_kwargs_carry_the_cookie_and_the_filter(declared):
    jira = get_instance("jira", "main").config_kwargs()
    assert jira == {
        "url": "https://jira.example.com",
        "auth_type": "external",
        "custom_headers": {"Cookie": "JSESSIONID=jira-1"},
        "ssl_verify": True,
        "projects_filter": "PROJ",
    }
    labs = get_instance("confluence", "labs").config_kwargs()
    assert labs["spaces_filter"] == "LAB"
    assert labs["ssl_verify"] is False
    assert "projects_filter" not in labs


def test_cookie_is_read_again_on_every_use(declared):
    _, cookies = declared
    instance = get_instance("confluence", "main")
    assert instance.read_cookie() == "JSESSIONID=main-1"
    cookies["main"].write_text("JSESSIONID=main-2", encoding="utf-8")
    assert instance.read_cookie() == "JSESSIONID=main-2"


def test_missing_or_empty_cookie_names_the_file_not_a_secret(declared):
    _, cookies = declared
    instance = get_instance("confluence", "labs")
    cookies["labs"].write_text("   \n", encoding="utf-8")
    with pytest.raises(InstanceError, match="is empty"):
        instance.read_cookie()
    cookies["labs"].unlink()
    with pytest.raises(InstanceError, match="cannot read") as error:
        instance.read_cookie()
    assert "labs-cookie" in str(error.value)


def test_unknown_instance_lists_the_declared_ones(declared):
    with pytest.raises(InstanceError, match="Declared: labs, main"):
        get_instance("confluence", "nope")


def test_file_is_reloaded_when_it_changes(declared):
    path, cookies = declared
    assert "extra" not in load_instances()["jira"]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["jira"]["extra"] = {
        "url": "https://jira2.example.com",
        "cookie_file": str(cookies["jira"]),
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert "extra" in load_instances()["jira"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must hold a JSON object"),
        ('{"bitbucket": {}}', "unknown service"),
        ('{"jira": {"a": {"cookie_file": "/x"}}}', "needs an http"),
        ('{"jira": {"a": {"url": "ftp://x", "cookie_file": "/x"}}}', "needs an http"),
        ('{"jira": {"a": {"url": "https://x"}}}', "needs a 'cookie_file'"),
        (
            '{"jira": {"a": {"url": "https://x", "cookie_file": "/x",'
            ' "projects_filter": ["A"]}}}',
            "must be a string",
        ),
    ],
)
def test_malformed_files_are_refused(tmp_path, monkeypatch, content, message):
    path = tmp_path / "instances.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv(instances.INSTANCES_FILE_ENV, str(path))
    with pytest.raises(InstanceError, match=message):
        load_instances()


def test_missing_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv(instances.INSTANCES_FILE_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(InstanceError, match="Cannot read"):
        load_instances()
