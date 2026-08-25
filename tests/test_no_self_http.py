"""Guard tests for the A4/S3 self-HTTP migration.

Static tests prove that no module in ``deaddit/`` calls its own HTTP API or
references the deleted ``deaddit.loader`` module: the loader/jobs
``get_api_base_url`` / ``get_api_headers`` helpers are gone, ``loader.py``
itself is deleted (importing it must raise ``ModuleNotFoundError``), no
migrated file references ``requests`` at all, and no file anywhere in
``deaddit/`` — including ``api.py`` — still builds an ``/api/ingest`` URL.
Together these guarantee every write path goes through
``deaddit.services.content`` or direct DB queries, so a running API server
is never required to generate content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deaddit.models import Subdeaddit, User

DEADDIT_DIR = Path(__file__).resolve().parent.parent / "deaddit"

# Files that persist via services/direct DB access; no `requests` allowed.
MIGRATED_FILES = (
    "jobs.py",
    "data/load_seed_data.py",
)


def test_self_http_helpers_and_loader_module_gone():
    """The self-HTTP helpers are gone and loader.py is deleted wholesale."""
    from deaddit import jobs

    assert not hasattr(jobs, "get_api_base_url")
    assert not hasattr(jobs, "get_api_headers")

    # loader.py was deleted wholesale (AC-P4); importing it must raise.
    with pytest.raises(ModuleNotFoundError):
        import deaddit.loader  # noqa: F401


def test_no_requests_usage_in_migrated_files():
    """jobs.py and load_seed_data.py must not use `requests`."""
    for name in MIGRATED_FILES:
        source = (DEADDIT_DIR / name).read_text(encoding="utf-8")
        assert "import requests" not in source, f"{name} still imports requests"
        assert "requests.get(" not in source, f"{name} still calls requests.get"
        assert "requests.post(" not in source, f"{name} still calls requests.post"
        assert (
            "get_api_base_url()" not in source
        ), f"{name} still references get_api_base_url"


def test_no_self_ingest_urls_anywhere():
    """No module in deaddit/ may build a self-ingest URL.

    The /api/ingest routes themselves were deleted (AC-P4), so even
    deaddit/api.py must not mention them; internal code persists through
    deaddit.services.content instead.
    """
    offenders = []
    for path in DEADDIT_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "/api/ingest" in source:
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
