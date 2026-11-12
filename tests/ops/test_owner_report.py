"""The local-only owner report: who runs which agents."""
from agentpit.api.main import app
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.ops import owner_report


def _rows() -> list[dict]:
    with DbSession(Settings().database_url).read() as conn:
        return TableRead.owner_agent_rows(conn)


def _agent(owner: str, app_name: str, deleted: bool = False) -> None:
    with DbSession(Settings().database_url).write() as conn:
        TableWrite.ensure_owner(conn, owner)
        user_id, _acct, _key = TableWrite.create_user(
            conn, email=None, password_hash=None, owner_workos_id=owner, agent_app=app_name
        )
        if deleted:
            TableWrite.delete_agent(conn, user_id, 1)


def test_no_http_route_serves_owner_emails():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any("owner" in p and p.startswith("/admin") for p in paths)


def test_web_email_is_used_and_workos_only_fills_the_rest():
    with DbSession(Settings().database_url).write() as conn:
        user_id, _acct, _key = TableWrite.create_user(conn, email="web@example.com", password_hash=None)
        TableWrite.set_workos_user_id(conn, user_id, "user_web")
    _agent("user_web", "Claude Code")
    _agent("user_mcp", "Cursor")
    _agent("user_mcp", "Old", deleted=True)
    asked: list[str] = []

    def lookup(workos_id: str) -> str | None:
        asked.append(workos_id)
        return {"user_mcp": "mcp@example.com"}.get(workos_id)

    lines = owner_report.build_rows(_rows(), lookup)
    by_app = {line[4]: line for line in lines}
    assert by_app["Claude Code"][0] == "web@example.com"
    assert by_app["Cursor"][0] == "mcp@example.com"
    assert "Old" not in by_app  # deleted agents are not listed
    assert asked == ["user_mcp"]  # only owners with no local email


def test_without_workos_the_email_is_blank():
    _agent("user_alone", "Cursor")
    lines = owner_report.build_rows(_rows(), None)
    assert [line[0] for line in lines if line[2] == "user_alone"] == [""]
