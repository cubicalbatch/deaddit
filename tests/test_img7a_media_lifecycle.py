"""Media lifecycle across destructive paths (plan 7A).

Every hard-delete route in deaddit/admin.py that can remove a post -
single or bulk post deletion, user deletion, and subdeaddit deletion -
must also remove that post's stored image files. Soft removal must do the
opposite: it never touches files (they stay for tombstone/audit purposes;
6A already denies serving them), and the reconciliation CLI must default
to a dry run that reports without mutating anything.

Path strings are captured immediately after a fixture creates a
``PostImage`` row, and existence checks below always compare against those
plain strings rather than re-reading attributes off the ORM object later -
once a hard-delete route removes the row, the same session's identity map
holds an expired instance and re-touching its attributes raises
``ObjectDeletedError`` instead of the answer the test wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from deaddit import create_app
from deaddit import db as _db
from deaddit.images.storage import store_variants
from deaddit.models import Post, PostImage, Subdeaddit, User

_PRIVATE_PROMPT = "a private generation prompt"


@dataclass(frozen=True)
class _ImagePaths:
    original_path: str
    thumbnail_path: str


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "TESTING": True,
            "GENERATED_IMAGES_ROOT": str(tmp_path / "media"),
        }
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


@pytest.fixture()
def db_session(app):
    return _db.session


def _solid_png(color=(10, 20, 30), size=(16, 16)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _seed(db_session):
    db_session.add_all(
        [
            User(username="alice", bio="", interests="[]"),
            User(username="bob", bio="", interests="[]"),
            Subdeaddit(name="testsub", description="A test subdeaddit"),
            Subdeaddit(name="othersub", description="Another test subdeaddit"),
        ]
    )
    db_session.commit()


def _make_image_post(
    app,
    db_session,
    *,
    user="alice",
    subdeaddit="testsub",
    title="A photo",
    removed=False,
) -> tuple[Post, _ImagePaths]:
    root = app.config["GENERATED_IMAGES_ROOT"]
    stored = store_variants(_solid_png(color=(title.encode()[0], 20, 30)), Path(root))
    post = Post(
        title=title,
        content="body text",
        subdeaddit_name=subdeaddit,
        user=user,
        removed=removed,
    )
    db_session.add(post)
    db_session.flush()
    image = PostImage(
        post_id=post.id,
        original_path=stored.original_path,
        thumbnail_path=stored.thumbnail_path,
        mime_type=stored.mime_type,
        byte_size=stored.original_size,
        width=stored.width,
        height=stored.height,
        alt_text="A solid test square",
        source_prompt=_PRIVATE_PROMPT,
        provider_snapshot="Fal",
        model_snapshot="fal-ai/flux-1/schnell",
        request_snapshot="req-secret",
    )
    db_session.add(image)
    db_session.commit()
    # Capture plain strings now - post.id and these path attributes will not
    # be safely re-readable off `image` once a hard-delete route removes the
    # row from under this same session's identity map.
    post_id = post.id
    paths = _ImagePaths(stored.original_path, stored.thumbnail_path)
    return post_id, paths


def _files_exist(app, paths: _ImagePaths) -> bool:
    root = Path(app.config["GENERATED_IMAGES_ROOT"])
    return (root / paths.original_path).is_file() and (
        root / paths.thumbnail_path
    ).is_file()


class TestSinglePostHardDelete:
    def test_deleting_a_post_removes_its_files(self, app, admin_client, db_session):
        _seed(db_session)
        post_id, paths = _make_image_post(app, db_session)
        assert _files_exist(app, paths)

        resp = admin_client.delete(f"/admin/api/posts/{post_id}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not _files_exist(app, paths)
        assert PostImage.query.filter_by(post_id=post_id).first() is None

    def test_deleting_a_text_post_does_not_error(self, app, admin_client, db_session):
        _seed(db_session)
        post = Post(
            title="just text", content="hi", subdeaddit_name="testsub", user="alice"
        )
        db_session.add(post)
        db_session.commit()

        resp = admin_client.delete(f"/admin/api/posts/{post.id}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestBulkPostHardDelete:
    def test_bulk_deleting_posts_removes_every_files_owning_post(
        self, app, admin_client, db_session
    ):
        _seed(db_session)
        post1_id, paths1 = _make_image_post(app, db_session, title="First")
        post2_id, paths2 = _make_image_post(app, db_session, title="Second")
        text_post = Post(
            title="text only", content="hi", subdeaddit_name="testsub", user="alice"
        )
        db_session.add(text_post)
        db_session.commit()
        text_post_id = text_post.id

        resp = admin_client.post(
            "/admin/api/posts/bulk-delete",
            json={"post_ids": [post1_id, post2_id, text_post_id]},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["deleted"]["posts"] == 3
        assert not _files_exist(app, paths1)
        assert not _files_exist(app, paths2)


class TestUserHardDelete:
    def test_deleting_a_user_removes_their_image_post_files(
        self, app, admin_client, db_session
    ):
        _seed(db_session)
        post_id, paths = _make_image_post(app, db_session, user="alice")
        assert _files_exist(app, paths)

        resp = admin_client.delete("/admin/api/users/alice")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not _files_exist(app, paths)
        assert PostImage.query.filter_by(post_id=post_id).first() is None
        assert db_session.get(Post, post_id) is None

    def test_bulk_deleting_users_removes_image_post_files(
        self, app, admin_client, db_session
    ):
        _seed(db_session)
        _post_a, paths_a = _make_image_post(app, db_session, user="alice")
        _post_b, paths_b = _make_image_post(app, db_session, user="bob")

        resp = admin_client.post(
            "/admin/api/users/bulk-delete", json={"usernames": ["alice", "bob"]}
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not _files_exist(app, paths_a)
        assert not _files_exist(app, paths_b)


class TestSubdeadditHardDelete:
    def test_deleting_a_subdeaddit_removes_its_posts_image_files(
        self, app, admin_client, db_session
    ):
        _seed(db_session)
        post_id, paths = _make_image_post(app, db_session, subdeaddit="testsub")
        other_post_id, other_paths = _make_image_post(
            app, db_session, subdeaddit="othersub", title="Untouched"
        )

        resp = admin_client.delete("/admin/api/subdeaddits/testsub")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not _files_exist(app, paths)
        assert PostImage.query.filter_by(post_id=post_id).first() is None
        # A post in a different subdeaddit is untouched.
        assert _files_exist(app, other_paths)
        assert db_session.get(Post, other_post_id) is not None

    def test_bulk_deleting_subdeaddits_removes_image_files(
        self, app, admin_client, db_session
    ):
        _seed(db_session)
        _post, paths = _make_image_post(app, db_session, subdeaddit="testsub")
        _other_post, other_paths = _make_image_post(
            app, db_session, subdeaddit="othersub", title="Second"
        )

        resp = admin_client.post(
            "/admin/api/subdeaddits/bulk-delete",
            json={"names": ["testsub", "othersub"]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not _files_exist(app, paths)
        assert not _files_exist(app, other_paths)


class TestSoftRemovalKeepsFiles:
    def test_soft_removed_post_keeps_files_but_denies_serving(
        self, app, admin_client, client, db_session
    ):
        _seed(db_session)
        post_id, paths = _make_image_post(app, db_session)

        # Soft-remove directly, matching the moderation service's tombstone
        # behavior (row and files both kept; only public serving is denied).
        post = db_session.get(Post, post_id)
        post.removed = True
        db_session.commit()

        assert _files_exist(app, paths)
        original_resp = client.get(
            f"/media/images/original/{Path(paths.original_path).name}"
        )
        assert original_resp.status_code == 404


class TestReconcileMediaCLI:
    @pytest.fixture()
    def runner(self):
        return CliRunner()

    @pytest.fixture()
    def images_cli(self):
        from deaddit.images import cli as images_cli_module

        return images_cli_module

    @pytest.fixture()
    def patch_cli_app(self, monkeypatch, app, images_cli):
        monkeypatch.setattr(images_cli, "create_app", lambda *a, **k: app)

    def _orphan_file(self, app) -> Path:
        root = Path(app.config["GENERATED_IMAGES_ROOT"])
        stored = store_variants(_solid_png(), root)
        return root / stored.original_path

    def test_dry_run_reports_but_does_not_delete(
        self, app, db_session, runner, images_cli, patch_cli_app
    ):
        _seed(db_session)
        orphan_path = self._orphan_file(app)
        assert orphan_path.is_file()

        from deaddit.cli import cli

        result = runner.invoke(cli, ["images", "reconcile-media"])

        assert result.exit_code == 0
        assert "dry-run" in result.output
        assert orphan_path.name in result.output
        assert orphan_path.is_file(), "dry run must never delete files"

    def test_apply_deletes_orphaned_files(
        self, app, db_session, runner, images_cli, patch_cli_app
    ):
        _seed(db_session)
        orphan_path = self._orphan_file(app)
        assert orphan_path.is_file()

        from deaddit.cli import cli

        result = runner.invoke(cli, ["images", "reconcile-media", "--apply"])

        assert result.exit_code == 0
        assert not orphan_path.is_file()

    def test_apply_keeps_files_still_referenced_by_a_post_image_row(
        self, app, db_session, runner, images_cli, patch_cli_app
    ):
        _seed(db_session)
        _post_id, paths = _make_image_post(app, db_session)

        from deaddit.cli import cli

        result = runner.invoke(cli, ["images", "reconcile-media", "--apply"])

        assert result.exit_code == 0
        assert _files_exist(app, paths)

    def test_apply_keeps_files_for_a_soft_removed_post(
        self, app, db_session, runner, images_cli, patch_cli_app
    ):
        _seed(db_session)
        post_id, paths = _make_image_post(app, db_session)
        post = db_session.get(Post, post_id)
        post.removed = True
        db_session.commit()

        from deaddit.cli import cli

        result = runner.invoke(cli, ["images", "reconcile-media", "--apply"])

        assert result.exit_code == 0
        assert _files_exist(app, paths), "soft removal must keep evidence files"

    def test_reports_rows_with_missing_files(
        self, app, db_session, runner, images_cli, patch_cli_app
    ):
        _seed(db_session)
        _post_id, paths = _make_image_post(app, db_session)
        root = Path(app.config["GENERATED_IMAGES_ROOT"])
        (root / paths.original_path).unlink()

        from deaddit.cli import cli

        result = runner.invoke(cli, ["images", "reconcile-media"])

        assert result.exit_code == 0
        assert "missing files" in result.output

    def test_apply_against_production_db_requires_explicit_flag(
        self, app, db_session, runner, images_cli, monkeypatch, patch_cli_app
    ):
        monkeypatch.setattr(
            images_cli.seeding, "_resolves_to_production", lambda *a, **k: True
        )
        _seed(db_session)
        orphan_path = self._orphan_file(app)

        from deaddit.cli import cli

        result = runner.invoke(cli, ["images", "reconcile-media", "--apply"])

        assert result.exit_code != 0
        assert "production" in result.output.lower()
        assert orphan_path.is_file()

    def test_apply_against_production_db_proceeds_with_override_flag(
        self, app, db_session, runner, images_cli, monkeypatch, patch_cli_app
    ):
        monkeypatch.setattr(
            images_cli.seeding, "_resolves_to_production", lambda *a, **k: True
        )
        _seed(db_session)
        orphan_path = self._orphan_file(app)

        from deaddit.cli import cli

        result = runner.invoke(
            cli, ["images", "reconcile-media", "--apply", "--i-know-this-is-prod"]
        )

        assert result.exit_code == 0
        assert not orphan_path.is_file()
