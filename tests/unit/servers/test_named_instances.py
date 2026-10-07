"""Named instances in the request path: middleware, tool filter, fetcher."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.confluence import ConfluenceFetcher
from mcp_atlassian.servers.dependencies import (
    _validation_cache,
    get_confluence_fetcher,
)
from mcp_atlassian.servers.main import AtlassianMCP, UserTokenMiddleware
from mcp_atlassian.utils import instances


@pytest.fixture(autouse=True)
def _isolate():
    instances._cache = None
    if _validation_cache is not None:
        _validation_cache.clear()
    yield
    instances._cache = None
    if _validation_cache is not None:
        _validation_cache.clear()


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    cookie = tmp_path / "labs-cookie"
    cookie.write_text("JSESSIONID=first", encoding="utf-8")
    path = tmp_path / "instances.json"
    path.write_text(
        json.dumps(
            {
                "confluence": {
                    "labs": {
                        "url": "https://labs.example.com/wiki",
                        "cookie_file": str(cookie),
                        "spaces_filter": "LAB",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(instances.INSTANCES_FILE_ENV, str(path))
    return cookie


def _scope(headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
        ],
        "state": {},
    }


def _middleware() -> UserTokenMiddleware:
    return UserTokenMiddleware(app=MagicMock(), mcp_server_ref=None)


def test_middleware_accepts_a_declared_instance(cookie_file):
    scope = _scope({"X-Atlassian-Confluence-Instance": "labs"})
    _middleware()._process_authentication_headers(scope)
    state = scope["state"]
    assert "auth_validation_error" not in state
    assert state["atlassian_service_headers"] == {
        "X-Atlassian-Confluence-Instance": "labs"
    }
    assert state["user_atlassian_auth_type"] == "instance"


def test_middleware_refuses_an_undeclared_instance(cookie_file):
    scope = _scope({"X-Atlassian-Confluence-Instance": "nope"})
    _middleware()._process_authentication_headers(scope)
    error = scope["state"]["auth_validation_error"]
    assert "Unknown confluence instance 'nope'" in error
    assert "user_atlassian_auth_type" not in scope["state"]


def test_middleware_refuses_any_instance_when_none_is_declared(monkeypatch):
    monkeypatch.delenv(instances.INSTANCES_FILE_ENV, raising=False)
    scope = _scope({"X-Atlassian-Jira-Instance": "main"})
    _middleware()._process_authentication_headers(scope)
    assert "Unknown jira instance" in scope["state"]["auth_validation_error"]


def test_a_user_token_keeps_its_own_auth_type(cookie_file):
    scope = _scope(
        {
            "Authorization": "Bearer user-token",
            "X-Atlassian-Confluence-Instance": "labs",
        }
    )
    _middleware()._process_authentication_headers(scope)
    assert scope["state"]["user_atlassian_auth_type"] != "instance"
    assert (
        scope["state"]["atlassian_service_headers"]["X-Atlassian-Confluence-Instance"]
        == "labs"
    )


def test_tool_filter_counts_the_named_service_as_available(cookie_file):
    server = AtlassianMCP(name="test")
    request = SimpleNamespace(
        state=SimpleNamespace(
            atlassian_service_headers={"X-Atlassian-Confluence-Instance": "labs"}
        )
    )
    request_context = SimpleNamespace(
        request=request,
        lifespan_context={"app_lifespan_context": None},
    )
    with patch.dict("os.environ", {}, clear=False) as env:
        for name in ("JIRA_URL", "CONFLUENCE_URL", "ATLASSIAN_EXTERNAL_AUTH_ENABLE"):
            env.pop(name, None)
        context = server._tool_filter_context(request_context)
    assert context is not None
    assert context["header_based_services"]["confluence"] is True
    assert not context["header_based_services"]["jira"]


class _State:
    """request.state for a request that named a Confluence instance."""

    def __init__(self) -> None:
        self.confluence_fetcher = None
        self.user_atlassian_auth_type = "instance"
        self.user_atlassian_email = None
        self.atlassian_service_headers = {"X-Atlassian-Confluence-Instance": "labs"}

    def __getattr__(self, name: str):
        if name == "user_atlassian_token":
            raise AttributeError(name)
        return None


@pytest.mark.anyio
@patch("mcp_atlassian.servers.dependencies._get_header_pat_network_config")
@patch("mcp_atlassian.servers.dependencies.get_http_request")
@patch("mcp_atlassian.servers.dependencies.ConfluenceFetcher")
async def test_fetcher_is_built_from_the_instance_and_its_cookie(
    fetcher_class, get_http_request, network_config, cookie_file
):
    network_config.return_value = {"ssl_verify": True}
    fetcher_class.return_value = MagicMock(spec=ConfluenceFetcher)
    request = MagicMock()
    request.state = _State()
    request.headers = {}
    get_http_request.return_value = request

    fetcher = await get_confluence_fetcher(MagicMock())

    assert fetcher is fetcher_class.return_value
    config = fetcher_class.call_args.kwargs["config"]
    assert config.url == "https://labs.example.com/wiki"
    assert config.auth_type == "external"
    assert config.custom_headers == {"Cookie": "JSESSIONID=first"}
    assert config.spaces_filter == "LAB"
    assert request.state.confluence_fetcher is fetcher


@pytest.mark.anyio
@patch("mcp_atlassian.servers.dependencies._get_header_pat_network_config")
@patch("mcp_atlassian.servers.dependencies.get_http_request")
@patch("mcp_atlassian.servers.dependencies.ConfluenceFetcher")
async def test_a_renewed_cookie_is_used_by_the_next_request(
    fetcher_class, get_http_request, network_config, cookie_file
):
    network_config.return_value = {"ssl_verify": True}
    fetcher_class.return_value = MagicMock(spec=ConfluenceFetcher)

    async def one_request() -> str:
        request = MagicMock()
        request.state = _State()
        request.headers = {}
        get_http_request.return_value = request
        await get_confluence_fetcher(MagicMock())
        return fetcher_class.call_args.kwargs["config"].custom_headers["Cookie"]

    assert await one_request() == "JSESSIONID=first"
    cookie_file.write_text("JSESSIONID=second", encoding="utf-8")
    assert await one_request() == "JSESSIONID=second"


@pytest.mark.anyio
@patch("mcp_atlassian.servers.dependencies._get_header_pat_network_config")
@patch("mcp_atlassian.servers.dependencies.get_http_request")
@patch("mcp_atlassian.servers.dependencies.ConfluenceFetcher")
async def test_a_refused_session_says_so(
    fetcher_class, get_http_request, network_config, cookie_file
):
    network_config.return_value = {"ssl_verify": True}
    fetcher = MagicMock(spec=ConfluenceFetcher)
    fetcher.get_current_user_info.side_effect = RuntimeError("401 from upstream")
    fetcher_class.return_value = fetcher
    request = MagicMock()
    request.state = _State()
    request.headers = {}
    get_http_request.return_value = request

    with pytest.raises(ValueError, match="did not accept the stored session"):
        await get_confluence_fetcher(MagicMock())
