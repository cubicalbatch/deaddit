"""UX-5 slice WAVE-B scoped checks for the admin DenseTable work.

Covers:
- single-item GET endpoints return exactly the requested row, 404 on missing
- no per_page>=1000 client-side fetch remains under admin static/templates
- rebuilt admin templates contain no ORM ``.query`` usage
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_TEMPLATES = REPO_ROOT / "deaddit" / "templates" / "admin"
ADMIN_STATIC = REPO_ROOT / "deaddit" / "static" / "admin"


# ---------------------------------------------------------------- single-item GETs


def test_single_item_get_endpoints_return_row(client, seeded_db):
    post = seeded_db["posts"][0]
    body = client.get(f"/admin/api/posts/{post.id}").get_json()
    assert body["id"] == post.id
    assert body["title"] == post.title

    comment = seeded_db["comments"][0]
    body = client.get(f"/admin/api/comments/{comment.id}").get_json()
    assert body["id"] == comment.id
    assert body["post_id"] == comment.post_id

    body = client.get("/admin/api/users/alice").get_json()
    assert body["username"] == "alice"
    assert "bio" in body

    body = client.get("/admin/api/subdeaddits/testsub").get_json()
    assert body["name"] == "testsub"
    assert body["posts_count"] == 2


def test_single_item_get_endpoints_404_on_missing(client, seeded_db):
    assert client.get("/admin/api/posts/999999").status_code == 404
    assert client.get("/admin/api/comments/999999").status_code == 404
    assert client.get("/admin/api/users/nobody-here").status_code == 404
    assert client.get("/admin/api/subdeaddits/no-such-sub").status_code == 404


def test_content_filter_options_rendered_server_side(client, seeded_db):
    html = client.get("/admin/content").data.decode()
    select_block = html.split('id="postsSubdeadditFilter"', 1)[1].split("</select>", 1)[
        0
    ]
    assert ">testsub</option>" in select_block
    assert ">askdeaddit</option>" in select_block


# ---------------------------------------------------------------- string-level regressions


def test_no_per_page_1000_anywhere_under_admin_assets():
    offenders = []
    for root in (ADMIN_STATIC, ADMIN_TEMPLATES):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".js", ".html"}:
                text = path.read_text(encoding="utf-8", errors="replace")
                if "per_page=1000" in text:
                    offenders.append(str(path))
    assert not offenders, f"per_page=1000 client-side fetch found in: {offenders}"


@pytest.mark.parametrize(
    "name",
    [
        "content.html",
        "dashboard.html",
        "capabilities.html",
        "_macros.html",
    ],
)
def test_rebuilt_templates_have_no_orm_queries(name):
    source = (ADMIN_TEMPLATES / name).read_text(encoding="utf-8")
    # Inline JS uses querySelector/querySelectorAll for DOM lookups;
    # only SQLAlchemy `.query` access is gated here.
    without_dom_lookups = re.sub(
        r"querySelector(?:All)?\s*\(", "(", source, flags=re.IGNORECASE
    )
    assert ".query" not in without_dom_lookups.lower(), (
        f"{name} must receive data from routes"
    )
