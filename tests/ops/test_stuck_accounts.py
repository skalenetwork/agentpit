"""The local-only list of accounts whose WorkOS user is gone."""
import pytest

from agentpit.auth.workos_client import (
    FakeWorkOsClient,
    WorkOsNotFoundError,
    WorkOsRateLimitedError,
    WorkOsUnavailableError,
)
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.ops import stuck_accounts


def _db() -> DbSession:
    return DbSession(Settings().database_url)


def _person(email: str, workos_id: str, *, deleted: bool = False, bot: bool = False) -> str:
    """A person's row the way sign-in leaves one: an owner row, the address,
    the identity."""
    with _db().write() as conn:
        TableWrite.ensure_owner(conn, workos_id)
        user_id, _acct, api_key = TableWrite.create_user(conn, email=email, password_hash=None)
        TableWrite.set_workos_user_id(conn, user_id, workos_id)
        if bot:
            TableWrite.mark_user_as_bot(conn, api_key)
        if deleted:
            TableWrite.delete_account(conn, workos_id, email, 1)
    return user_id


def _agent(owner: str, app_name: str) -> None:
    with _db().write() as conn:
        TableWrite.create_user(
            conn, email=None, password_hash=None, owner_workos_id=owner, agent_app=app_name
        )


def _lines(workos: FakeWorkOsClient) -> list[list[str]]:
    with _db().read() as conn:
        people, unheld = TableRead.workos_people(conn), TableRead.unheld_owners(conn)
    return stuck_accounts.build_rows(
        people, unheld, lambda workos_id: stuck_accounts.lookup(workos, workos_id)
    )


class _Answers:
    """A WorkOS client whose `get_user` gives these answers, in order."""

    def __init__(self, *answers):
        self._answers = list(answers)

    def get_user(self, workos_user_id):
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_only_live_people_whose_workos_user_is_gone_are_listed():
    workos = FakeWorkOsClient()
    alive = workos.create_user(email="alive@example.com", password_hash=None)
    _person("alive@example.com", alive.workos_user_id)
    stuck = _person("stuck@example.com", "user_gone")
    _agent("user_gone", "Claude")
    _agent("user_gone", "Codex")
    _person("done@example.com", "user_done", deleted=True)
    _person("house@agentpit.local", "user_house", bot=True)

    lines = _lines(workos)

    assert [line[:3] for line in lines] == [["stuck@example.com", stuck, "user_gone"]]
    assert lines[0][5] == "2"  # its live agents
    assert lines[0][6] == ""  # nobody has tried that address since


def test_a_refused_sign_in_for_the_same_address_shows_as_came_back():
    workos = FakeWorkOsClient()
    _person("Back@Example.com", "user_gone")
    returning = workos.create_user(email="back@example.com", password_hash=None)
    with _db().write() as conn:
        # What a refused sign-in leaves: an owner row no account holds.
        TableWrite.ensure_owner(conn, returning.workos_user_id)
        TableWrite.ensure_owner(conn, "user_refused_then_gone")

    lines = _lines(workos)

    assert [line[6] for line in lines] == [returning.workos_user_id]


def test_a_rate_limit_is_waited_out():
    waits: list[float] = []
    client = _Answers(
        WorkOsRateLimitedError("429"), WorkOsRateLimitedError("429"), WorkOsNotFoundError("404")
    )

    assert stuck_accounts.lookup(client, "user_x", sleep=waits.append) is None
    assert waits == [stuck_accounts.WAIT_SECONDS] * 2


@pytest.mark.parametrize(
    "answers",
    [
        [WorkOsUnavailableError("down")],
        [WorkOsRateLimitedError("429")] * stuck_accounts.ATTEMPTS,
    ],
    ids=["unreachable", "rate-limited-throughout"],
)
def test_an_answer_short_of_a_404_is_never_taken_as_gone(answers):
    with pytest.raises(type(answers[0])):
        stuck_accounts.lookup(_Answers(*answers), "user_x", sleep=lambda _s: None)


def test_main_lists_without_migrating_and_counts_what_it_found(monkeypatch, capsys):
    from agentpit.db.table_create import TableCreate

    workos = FakeWorkOsClient()
    alive = workos.create_user(email="ok@example.com", password_hash=None)
    _person("ok@example.com", alive.workos_user_id)
    stuck = _person("cli@example.com", "user_gone")  # first: this helper's session migrates

    def boom(_conn):
        raise AssertionError("stuck_accounts ran the schema DDL")

    monkeypatch.setattr(TableCreate, "create_all_tables", boom)
    monkeypatch.setattr(stuck_accounts, "build_workos_client", lambda _settings: workos)
    stuck_accounts.main([])

    captured = capsys.readouterr()
    out = captured.out.splitlines()
    assert out[0].split("\t") == stuck_accounts.COLUMNS
    assert [line.split("\t")[1] for line in out[1:]] == [stuck]
    assert "1 of 2 accounts" in captured.err
    assert "WorkOS has none of them" not in captured.err


def test_a_key_for_another_workos_environment_is_called_out(monkeypatch, capsys):
    _person("a@example.com", "user_a")
    _person("b@example.com", "user_b")
    monkeypatch.setattr(stuck_accounts, "build_workos_client", lambda _settings: FakeWorkOsClient())

    stuck_accounts.main([])

    assert "WorkOS has none of them" in capsys.readouterr().err


def test_without_workos_nothing_is_listed(monkeypatch, capsys):
    monkeypatch.setattr(stuck_accounts, "build_workos_client", lambda _settings: None)

    with pytest.raises(SystemExit):
        stuck_accounts.main([])

    assert capsys.readouterr().out == ""
