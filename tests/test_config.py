"""AgentConfig şeması ve YAML yükleyici testleri."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from server import config
from server.config import AgentConfig, AgentConfigError, load_agents, public_view

from tests.conftest import make_agent

ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """
id: {id}
name: Deneme
instructions: Merhaba de.
"""


def test_defaults():
    a = make_agent()
    assert a.language == "tr-TR"
    assert a.voice == "Kore"
    assert a.model is None
    assert a.limits.max_session_seconds == 600
    assert a.limits.max_daily_sessions == 200
    assert a.limits.max_daily_minutes == 300
    assert a.theme.primary_color == "#A855F7"
    assert a.theme.position == "bottom-right"


@pytest.mark.parametrize("bad_id", ["A", "a", "Buyuk-Harf", "alt_cizgi", "x" * 41, "türkçe"])
def test_invalid_id(bad_id):
    with pytest.raises(ValidationError):
        make_agent(id=bad_id)


def test_invalid_color_and_position():
    with pytest.raises(ValidationError):
        make_agent(theme={"primary_color": "purple"})
    with pytest.raises(ValidationError):
        make_agent(theme={"position": "top-left"})


def test_knowledge_limit():
    make_agent(knowledge="a" * 20_000)
    with pytest.raises(ValidationError):
        make_agent(knowledge="a" * 20_001)


def _tool(**kw):
    t = {"name": "book_demo", "description": "Demo ayarla", "url": "https://example.invalid/hook"}
    t.update(kw)
    return t


def test_tool_validation():
    a = make_agent(tools=[_tool(parameters={"name": {"type": "string"}})])
    assert a.tools[0].method == "POST"
    assert a.tools[0].timeout_s == 8.0
    assert a.tools[0].parameters["name"].required is True
    for bad in (
        _tool(name="BookDemo"),
        _tool(name="1abc"),
        _tool(timeout_s=0.5),
        _tool(timeout_s=31),
        _tool(url="not-a-url"),
        _tool(method="PUT"),
        _tool(parameters={"x": {"type": "object"}}),
        _tool(secret_env="gizli anahtar"),
    ):
        with pytest.raises(ValidationError):
            make_agent(tools=[bad])


def test_duplicate_tool_names():
    with pytest.raises(ValidationError):
        make_agent(tools=[_tool(), _tool()])


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        make_agent(instructons="yazım hatası")


def test_origins_normalized_and_validated():
    a = make_agent(allowed_origins=["HTTPS://Www.Example.com/", "http://localhost:8080", "https://a.com:443"])
    assert a.allowed_origins == ["https://www.example.com", "http://localhost:8080", "https://a.com"]
    with pytest.raises(ValidationError):
        make_agent(allowed_origins=["example.com"])
    with pytest.raises(ValidationError):
        make_agent(allowed_origins=["https://example.com/yol"])


def test_load_agents_and_filename_match(tmp_path):
    (tmp_path / "birinci.yaml").write_text(MINIMAL.format(id="birinci"), encoding="utf-8")
    (tmp_path / "ikinci.yml").write_text(MINIMAL.format(id="ikinci"), encoding="utf-8")
    agents = load_agents(tmp_path)
    assert set(agents) == {"birinci", "ikinci"}

    (tmp_path / "ucuncu.yaml").write_text(MINIMAL.format(id="baska-id"), encoding="utf-8")
    with pytest.raises(AgentConfigError):
        load_agents(tmp_path)


def test_load_agents_invalid_yaml(tmp_path):
    (tmp_path / "bozuk.yaml").write_text("id: [bozuk", encoding="utf-8")
    with pytest.raises(AgentConfigError):
        load_agents(tmp_path)
    (tmp_path / "bozuk.yaml").write_text("- liste", encoding="utf-8")
    with pytest.raises(AgentConfigError):
        load_agents(tmp_path)


def test_missing_dir_returns_empty(tmp_path):
    assert load_agents(tmp_path / "yok") == {}


def test_registry_get_agent():
    a = make_agent()
    config.set_agents({a.id: a})
    assert config.get_agent("test-agent") is a
    with pytest.raises(KeyError):
        config.get_agent("olmayan")


def test_public_view_hides_secrets():
    a = make_agent(knowledge="gizli bilgi", tools=[_tool(secret_env="BOOK_DEMO_SECRET")])
    view = public_view(a)
    assert set(view) == {"id", "name", "greeting", "theme", "language"}
    assert view["theme"]["primary_color"] == "#A855F7"
    dumped = str(view)
    assert "gizli" not in dumped and "example.invalid" not in dumped and "Kısa yanıt" not in dumped


def test_repo_agent_yaml_is_valid():
    agents = load_agents(ROOT / "agents")
    a = agents["botfusions-satis"]
    assert isinstance(a, AgentConfig)
    assert a.greeting
    assert "https://botfusions.com" in a.allowed_origins
    assert "https://www.botfusions.com" in a.allowed_origins
    tool = a.tools[0]
    assert tool.name == "book_demo"
    # Örnek asistan randevuyu doğrudan BOTCRm'e (Supabase) yazar ve sonucu bekler
    assert tool.type == "supabase_crm"
    assert tool.speak_while_running is False
    assert {"name", "email", "phone", "preferred_time"} <= set(tool.parameters)
