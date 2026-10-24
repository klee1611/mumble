"""Weekly review, with a fake Ultramarine client (no network, no spend)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row

from mumble import config, core, db, review


def test_grounding_drops_claims_without_real_citations():
    raw = {
        "headline": "A gentle week.",
        "keep_saying": [
            {"text": "Dentist, three times", "item_ids": [1, 2, 99]},  # 99 is made up
            {"text": "Pure invention", "item_ids": [42]},
            {"text": "No citation at all", "item_ids": []},
        ],
        "slipping": [{"text": "", "item_ids": [1]}],
        "connections": [],
        "one_thing": {"text": "Call the dentist at 9", "item_ids": [2]},
    }
    out = review.grounded(raw, {1, 2, 3})
    assert out["keep_saying"] == [{"text": "Dentist, three times", "item_ids": [1, 2]}]
    assert out["slipping"] == []
    assert out["one_thing"] == {"text": "Call the dentist at 9", "item_ids": [2]}


def test_grounding_drops_made_up_one_thing():
    out = review.grounded({"headline": "x", "one_thing": {"text": "Do X", "item_ids": [7]}}, {1})
    assert out["one_thing"] is None


def test_cost_matches_the_price_card():
    # The docs' example: 20,000 uncached input + 1,000 output tokens = $0.0035
    usage = SimpleNamespace(prompt_tokens=20_000, completion_tokens=1_000,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=0))
    assert review.cost_usd(usage) == pytest.approx(0.0035)


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.sent = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.sent = kwargs
        usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=300,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=200))
        msg = SimpleNamespace(content=json.dumps(self.answer(kwargs)))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


@pytest.fixture
def week():
    try:
        psycopg.connect(config.DATABASE_URL, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("local Postgres isn't running (docker compose up -d)")
    db.init()
    db.close()
    with psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        memo = conn.execute("INSERT INTO memos (mime, status, transcript) VALUES "
                            "('audio/webm', 'done', 'test') RETURNING id").fetchone()["id"]
        ids = [conn.execute("INSERT INTO items (memo_id, kind, title) VALUES (%s, 'todo', %s) RETURNING id",
                            (memo, t)).fetchone()["id"] for t in ("Call the dentist", "Call dentist about tooth")]
        yield ids
        conn.execute("DELETE FROM reviews WHERE body->>'headline' = 'Test week.'")
        conn.execute("DELETE FROM memos WHERE id = %s", (memo,))


def test_run_stores_review_and_cost(week):
    a, b = week
    client = FakeClient(lambda kw: {
        "headline": "Test week.",
        "keep_saying": [{"text": "The dentist keeps coming up", "item_ids": [a, b]}],
        "slipping": [], "connections": [],
        "one_thing": {"text": "Call at 9 tomorrow", "item_ids": [a]},
    })
    result = review.run(client=client, now=datetime.now(timezone.utc))

    sent = client.sent
    assert sent["model"] == "ultramarine-1-flash"
    assert sent["response_format"]["type"] == "json_schema"
    assert f"[{a}]" in sent["messages"][1]["content"]  # ids are shown to the model
    # 1000 uncached * 150 + 200 cached * 30 + 300 out * 500 nanodollars
    assert result["cost_usd"] == pytest.approx(0.000306)

    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
        latest = core.latest_review(conn)
    assert latest["body"]["headline"] == "Test week."
    assert [i["title"] for i in latest["body"]["keep_saying"][0]["items"]] == ["Call the dentist", "Call dentist about tooth"]
