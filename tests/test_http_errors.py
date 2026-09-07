"""Regression coverage for Flask/Werkzeug HTTP exception handling."""

from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import Unauthorized


def test_wrong_method_preserves_flask_405_response(client):
    response = client.post("/")

    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]


def test_http_exception_preserves_status_and_challenge_header(app, client):
    @app.get("/_test/unauthorized")
    def unauthorized():
        raise Unauthorized(www_authenticate=WWWAuthenticate("basic", {"realm": "test"}))

    response = client.get("/_test/unauthorized")

    assert response.status_code == 401
    assert response.www_authenticate.type == "basic"
    assert response.www_authenticate.parameters["realm"] == "test"


def test_unexpected_exception_remains_sanitized_500(app, client):
    @app.get("/_test/runtime-error")
    def runtime_error():
        raise RuntimeError("secret implementation detail")

    response = client.get("/_test/runtime-error")

    assert response.status_code == 500
    assert response.get_json() == {"error": "An unexpected error occurred"}
    assert "secret implementation detail" not in response.get_data(as_text=True)


def test_existing_custom_not_found_response_remains(app, client):
    response = client.get("/_test/missing")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Resource not found"}
