"""Persistence and schema guarantees for the image domain models."""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from deaddit import create_app
from deaddit.models import ImageModel, ImageProvider, Post, PostImage, Subdeaddit, User

_PRE_IMAGE_HEAD = "323c82c6f88c"
_IMAGE_TABLES = {"image_provider", "image_model", "post_image"}


def _post(db_session):
    user = User(username="image-author")
    subdeaddit = Subdeaddit(name="image-subdeaddit")
    post = Post(
        title="An existing text post",
        content="Text content",
        user=user.username,
        subdeaddit_name=subdeaddit.name,
    )
    db_session.add_all([user, subdeaddit, post])
    db_session.commit()
    return post


def _image(post_id, provider_id=None):
    return PostImage(
        post_id=post_id,
        original_path="originals/opaque-original.png",
        thumbnail_path="thumbnails/opaque-thumbnail.png",
        mime_type="image/png",
        byte_size=1234,
        width=800,
        height=600,
        alt_text="A useful description",
        source_prompt="A private generation prompt",
        provider_id=provider_id,
        provider_snapshot="Example Provider",
        model_snapshot="example-model",
        request_snapshot="request-123",
    )


def test_image_rows_are_unique_per_post_and_outlive_their_provider(app, db_session):
    post = _post(db_session)
    assert post.image is None, "a text post carries no image"

    provider = ImageProvider(
        name="Example Provider", provider_type="fal", credential_env="FAL_KEY"
    )
    other = ImageProvider(
        name="Other Provider", provider_type="runware", credential_env="RUNWARE_KEY"
    )
    # The stored API key is masked in serialization - never the value itself.
    provider.api_key = "stored-secret-abcd"
    masked = provider.to_dict()
    assert masked["has_key"] is True and masked["key_last4"] == "abcd"
    assert "api_key" not in masked and "stored-secret-abcd" not in str(masked)
    assert (other.to_dict()["has_key"], other.to_dict()["key_last4"]) == (False, None)

    db_session.add_all(
        [
            provider,
            other,
            ImageModel(provider=provider, model_identifier="example-model"),
            ImageModel(provider=other, model_identifier="example-model"),
        ]
    )
    db_session.commit()

    # A model identifier is unique per provider, not globally.
    db_session.add(
        ImageModel(provider_id=provider.id, model_identifier="example-model")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(_image(post.id, provider.id))
    db_session.commit()
    assert post.image is not None

    # At most one image per post.
    db_session.add(_image(post.id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Deleting the provider unlinks but keeps the provenance snapshot.
    db_session.delete(provider)
    db_session.commit()
    remaining = db_session.get(PostImage, post.id)
    assert remaining.provider_id is None
    assert remaining.source_prompt == "A private generation prompt"
    assert remaining.provider_snapshot == "Example Provider"
    assert remaining.model_snapshot == "example-model"
    assert remaining.request_snapshot == "request-123"

    # The serializer is the public contract: no prompt, no provenance, no paths.
    assert remaining.to_dict() == {
        "original_url": "opaque-original.png",
        "thumbnail_url": "opaque-thumbnail.png",
        "mime_type": "image/png",
        "width": 800,
        "height": 600,
        "alt_text": "A useful description",
    }

    # Deleting the post cascades to its image row.
    db_session.delete(post)
    db_session.commit()
    assert db_session.get(PostImage, post.id) is None


def test_image_tables_migration_round_trip(tmp_path):
    db_path = tmp_path / "mig.db"
    app = create_app(
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "TESTING": True}
    )
    runner = app.test_cli_runner()

    def query(sql):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def tables():
        return {
            row[0]
            for row in query(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    assert _IMAGE_TABLES <= tables()
    assert {
        "post_id",
        "original_path",
        "thumbnail_path",
        "provider_id",
        "source_prompt",
    } <= {row[1] for row in query("PRAGMA table_info(post_image)")}
    assert "api_key" in {row[1] for row in query("PRAGMA table_info(image_provider)")}

    post_image_fks = query("PRAGMA foreign_key_list(post_image)")
    assert any(
        row[3] == "provider_id" and row[2] == "image_provider" and row[6] == "SET NULL"
        for row in post_image_fks
    )
    assert any(row[3] == "post_id" and row[2] == "post" for row in post_image_fks)
    assert any(
        row[3] == "provider_id" and row[2] == "image_provider" and row[6] == "CASCADE"
        for row in query("PRAGMA foreign_key_list(image_model)")
    )

    down = runner.invoke(args=["db", "downgrade", _PRE_IMAGE_HEAD])
    assert down.exit_code == 0, down.output
    assert not (_IMAGE_TABLES & tables())
    assert "post" in tables()

    again = runner.invoke(args=["db", "upgrade"])
    assert again.exit_code == 0, again.output
    assert _IMAGE_TABLES <= tables()
