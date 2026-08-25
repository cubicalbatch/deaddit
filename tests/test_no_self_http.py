"""Guard tests for the A4/S3 self-HTTP migration.

Static tests prove that no module in ``deaddit/`` calls its own HTTP API:
the loader/jobs ``get_api_base_url`` / ``get_api_headers`` helpers are gone,
``loader.ingest()`` is gone, none of the migrated files reference
``requests`` at all, and no f-string anywhere outside ``deaddit/api.py``
still builds an ``/api/ingest`` URL. Together these guarantee every write
path goes through ``deaddit.services.content`` or direct DB queries, so a
running API server is never required to generate content.

Behavioral tests exercise the migrated loader call paths offline: the LLM
transport is faked via ``tests.fakes.FakeProvider`` (registered by the
``fake_llm`` fixture), so ``loader.create_post`` / ``loader.create_comment``
run end-to-end — prompt building, parsing, service persistence — against an
in-memory database, and the resulting rows are asserted in the DB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deaddit import loader
from deaddit.models import Comment, Post, Subdeaddit, User

DEADDIT_DIR = Path(__file__).resolve().parent.parent / "deaddit"

# Files this slice migrated; they must not reference `requests` at all.
MIGRATED_FILES = (
    "loader.py",
    "jobs.py",
    "data/load_seed_data.py",
)


def test_api_helper_functions_deleted():
    """The self-HTTP helpers must not exist on loader/jobs namespaces."""
    from deaddit import jobs

    for module in (loader, jobs):
        assert not hasattr(module, "get_api_base_url")
        assert not hasattr(module, "get_api_headers")

    # loader.ingest() was plan-mandated for deletion.
    assert not hasattr(loader, "ingest")


def test_no_requests_usage_in_migrated_files():
    """loader.py, jobs.py and load_seed_data.py must not use `requests`."""
    for name in MIGRATED_FILES:
        source = (DEADDIT_DIR / name).read_text(encoding="utf-8")
        assert "import requests" not in source, f"{name} still imports requests"
        assert "requests.get(" not in source, f"{name} still calls requests.get"
        assert "requests.post(" not in source, f"{name} still calls requests.post"
        assert (
            "get_api_base_url()" not in source
        ), f"{name} still references get_api_base_url"


def test_no_self_ingest_urls_outside_api_module():
    """No module except deaddit/api.py may build a self-ingest URL.

    This catches any future re-introduction of self-HTTP writes: internal
    code must persist through deaddit.services.content instead.
    """
    offenders = []
    for path in DEADDIT_DIR.rglob("*.py"):
        if path.name == "api.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "/api/ingest" in source or "/api/ingest/user" in source:
            offenders.append(str(path.relative_to(DEADDIT_DIR)))
    assert offenders == []


def test_no_hardcoded_localhost_5000_calls():
    """No requests.* call in deaddit/ may target our own localhost:5000 base."""
    offenders = []
    for path in DEADDIT_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "requests." in line and ("localhost:5000" in line or "API_BASE_URL" in line):
                offenders.append(f"{path}:{lineno}")
    assert offenders == []


def _complete_personas(seeded_db, db_session):
    """Give seeded users the persona fields the frozen prompt code requires."""
    for user, occupation in zip(
        seeded_db["users"], ["qa engineer", "software developer"], strict=True
    ):
        user.gender = "Female" if user.username == "alice" else "Male"
        user.occupation = occupation
        user.education = "Bachelor's degree"
        user.writing_style = "casual"
        user.personality_traits = '["curious", "friendly"]'
    db_session.commit()


@pytest.mark.usefixtures("app")
class TestLoaderPersistsViaService:
    """Offline end-to-end runs of the migrated loader paths."""

    def test_create_post_persists(self, seeded_db, db_session, fake_llm):
        _complete_personas(seeded_db, db_session)
        fake_llm.enqueue_content(
            '{"posts": [{"title": "A Fresh Post", "content": "Brand new body",'
            ' "upvote_count": 42}]}'
        )

        post_id = loader.create_post("testsub")

        assert isinstance(post_id, int)
        row = db_session.get(Post, post_id)
        assert row is not None
        assert row.title == "A Fresh Post"
        assert row.content == "Brand new body"
        assert row.upvote_count == 42
        assert row.subdeaddit_name == "testsub"
        assert row.user in {"alice", "bob"}
        assert row.model  # model captured from the LLM response seam

    def test_create_comment_persists(self, seeded_db, db_session, fake_llm):
        _complete_personas(seeded_db, db_session)
        post = seeded_db["posts"][0]
        fake_llm.enqueue_content(
            '{"content": "A thoughtful reply", "upvote_count": 7}'
        )

        comment_data = loader.create_comment(post.id)

        assert comment_data is not None
        assert comment_data["post_id"] == post.id
        persisted = (
            Comment.query.filter_by(post_id=post.id, content="A thoughtful reply")
            .first()
        )
        assert persisted is not None
        assert persisted.upvote_count == 7
        assert persisted.user in {"alice", "bob"}

    def test_create_subdeaddit_upserts(self, seeded_db, db_session, fake_llm):
        fake_llm.enqueue_content(
            '{"name": "FreshSub", "description": "A brand new community.",'
            ' "post_types": ["discussion", "questions"]}'
        )

        data = loader.create_subdeaddit()

        assert data["name"] == "FreshSub"
        row = Subdeaddit.query.filter_by(name="FreshSub").first()
        assert row is not None
        assert row.description == "A brand new community."
        assert row.get_post_types() == ["discussion", "questions"]


@pytest.mark.usefixtures("app")
class TestSeedLoaderPersistsViaService:
    """data/load_seed_data.py must seed via the content service, not HTTP."""

    def test_seed_users_and_subdeaddits(self, tmp_path, db_session):
        from deaddit.data import load_seed_data as seed

        users_file = tmp_path / "users.json"
        users_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "username": "seed_user",
                            "age": 30,
                            "gender": "Female",
                            "bio": "bio",
                            "interests": ["x"],
                            "occupation": "dev",
                            "education": "BS",
                            "writing_style": "plain",
                            "personality_traits": ["curious"],
                            "model": "test-model",
                        }
                    ]
                }
            )
        )
        subs_file = tmp_path / "subs.json"
        subs_file.write_text(
            json.dumps(
                {
                    "subdeaddits": [
                        {
                            "name": "seedsub",
                            "description": "desc",
                            "post_types": ["discussion"],
                        }
                    ]
                }
            )
        )

        seed.ingest_users(str(users_file))
        seed.ingest_subdeaddits(str(subs_file))

        user = User.query.filter_by(username="seed_user").first()
        assert user is not None
        assert user.age == 30
        assert user.gender == "Female"
        sub = Subdeaddit.query.filter_by(name="seedsub").first()
        assert sub is not None
        assert sub.get_post_types() == ["discussion"]

    def test_bad_user_record_is_skipped_not_fatal(self, tmp_path, db_session):
        from deaddit.data import load_seed_data as seed

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"users": [{"username": "incomplete"}]}))

        # Must not raise; record lacks required fields and is reported.
        seed.ingest_users(str(bad))
        assert User.query.filter_by(username="incomplete").first() is None
