"""UX-5 slice WAVE-B scoped checks for the admin DenseTable work.

Covers:
- single-item GET endpoints return exactly the requested row, 404 on missing
- jobs() honors the ?sort= whitelist and falls back to the default on unknown keys
- status/type/per_page filters persist through generated pagination/sort links
- no per_page>=1000 client-side fetch remains under admin static/templates
- rebuilt admin templates contain no ORM ``.query`` usage
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deaddit.models import Job, JobStatus, JobType

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_TEMPLATES = REPO_ROOT / "deaddit" / "templates" / "admin"
ADMIN_STATIC = REPO_ROOT / "deaddit" / "static" / "admin"

ROW_LINK_RE = re.compile(r'href="/admin/jobs/(\d+)"')


def _make_jobs(db_session, count=25):
    """Create `count` failed create_post jobs with distinct priorities."""
    jobs = [
        Job(
            type=JobType.CREATE_POST,
            status=JobStatus.FAILED,
            priority=i,
            total_items=1,
        )
        for i in range(count)
    ]
    db_session.add_all(jobs)
    db_session.commit()
    return jobs


def _row_order(html: str) -> list[int]:
    """Job ids in table row order (first of the two detail links per row)."""
    links = [int(m) for m in ROW_LINK_RE.findall(html)]
    return links[::2]


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


# ---------------------------------------------------------------- jobs sort whitelist


def test_jobs_sort_whitelist_honored(client, db_session, seeded_db):
    _make_jobs(db_session)

    rv = client.get("/admin/jobs?sort=priority&dir=asc&per_page=100")
    assert rv.status_code == 200
    ids = _row_order(rv.data.decode())
    assert ids == sorted(ids)  # ascending by priority; ids were created in order

    rv = client.get("/admin/jobs?sort=priority&dir=desc&per_page=100")
    ids = _row_order(rv.data.decode())
    assert ids == sorted(ids, reverse=True)


@pytest.mark.parametrize("bogus", ["priority; DROP TABLE jobs", "../../etc/passwd", "nonsense"])
def test_jobs_unknown_sort_key_falls_back_to_default(client, db_session, seeded_db, bogus):
    _make_jobs(db_session)

    default = client.get("/admin/jobs?per_page=100")
    fallback = client.get(f"/admin/jobs?sort={bogus}&dir=lateral&per_page=100")
    assert fallback.status_code == 200
    # Unknown key/dir fall back to created_at desc (+ id tiebreaker): newest first.
    assert _row_order(fallback.data.decode()) == _row_order(default.data.decode())


# ---------------------------------------------------------------- filter persistence in links


def test_jobs_filters_and_per_page_persist_through_links(client, db_session, seeded_db):
    _make_jobs(db_session)

    html = client.get(
        "/admin/jobs?status=failed&type=create_post&per_page=20"
    ).data.decode()

    page_links = re.findall(r'class="page-link"[^>]*href="([^"]+)"', html)
    pager_links = [href for href in page_links if "page=" in href]
    for href in pager_links:
        assert "status=failed" in href
        assert "type=create_post" in href
        assert "per_page=20" in href

    # Sort headers keep every filter arg but reset to page 1.
    sort_links = re.findall(r'class="dt-sort"\s+href="([^"]+)"', html)
    assert sort_links, "expected sortable column headers"
    for href in sort_links:
        assert "status=failed" in href
        assert "type=create_post" in href
        assert "per_page=20" in href
        assert not re.search(r"[?&]page=", href), href


def test_content_filter_options_rendered_server_side(client, seeded_db):
    html = client.get("/admin/content").data.decode()
    select_block = html.split('id="postsSubdeadditFilter"', 1)[1].split("</select>", 1)[0]
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
        "jobs.html",
        "content.html",
        "dashboard.html",
        "capabilities.html",
        "_macros.html",
        "job_detail.html",
        "generate.html",
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
