"""Persistence behavior for the Phase 1 image domain models."""

import pytest
from sqlalchemy.exc import IntegrityError

from deaddit.models import ImageModel, ImageProvider, Post, PostImage, Subdeaddit, User


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


def test_text_post_has_no_image(app, db_session):
    post = _post(db_session)

    assert post.image is None


def test_provider_owns_cached_models_and_rejects_duplicate_identifier(app, db_session):
    provider = ImageProvider(
        name="Example Provider",
        provider_type="fal",
        credential_env="FAL_KEY",
    )
    other_provider = ImageProvider(
        name="Other Provider",
        provider_type="runware",
        credential_env="RUNWARE_KEY",
    )
    db_session.add_all(
        [
            provider,
            other_provider,
            ImageModel(
                provider=provider,
                model_identifier="example-model",
                display_name="Example Model",
                category="text-to-image",
            ),
            ImageModel(provider=other_provider, model_identifier="example-model"),
        ]
    )
    db_session.commit()

    assert {model.model_identifier for model in provider.models} == {"example-model"}
    assert provider.to_dict()["credential_env"] == "FAL_KEY"

    duplicate = ImageModel(provider_id=provider.id, model_identifier="example-model")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_post_has_one_image_and_duplicate_post_id_is_rejected(app, db_session):
    post = _post(db_session)
    first = _image(post.id)
    db_session.add(first)
    db_session.commit()

    assert post.image is first

    second = _image(post.id)
    db_session.add(second)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_deleting_provider_nulls_link_but_keeps_image_provenance(app, db_session):
    post = _post(db_session)
    provider = ImageProvider(
        name="Example Provider",
        provider_type="fal",
        credential_env="FAL_KEY",
    )
    db_session.add(provider)
    db_session.commit()

    image = _image(post.id, provider.id)
    db_session.add(image)
    db_session.commit()

    db_session.delete(provider)
    db_session.commit()

    remaining = db_session.get(PostImage, post.id)
    assert remaining is not None
    assert remaining.provider_id is None
    assert remaining.source_prompt == "A private generation prompt"
    assert remaining.provider_snapshot == "Example Provider"
    assert remaining.model_snapshot == "example-model"
    assert remaining.request_snapshot == "request-123"


def test_post_image_serializer_exposes_only_public_metadata(app, db_session):
    post = _post(db_session)
    image = _image(post.id)

    assert image.to_dict() == {
        "original_url": "opaque-original.png",
        "thumbnail_url": "opaque-thumbnail.png",
        "mime_type": "image/png",
        "width": 800,
        "height": 600,
        "alt_text": "A useful description",
    }
    assert (
        not {
            "source_prompt",
            "request_snapshot",
            "provider_snapshot",
            "provider_id",
            "byte_size",
        }
        & image.to_dict().keys()
    )


def test_deleting_post_cascades_to_post_image(app, db_session):
    post = _post(db_session)
    image = _image(post.id)
    db_session.add(image)
    db_session.commit()

    db_session.delete(post)
    db_session.commit()

    assert db_session.get(PostImage, post.id) is None
