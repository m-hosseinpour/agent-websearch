from starlette.testclient import TestClient

from websearch.config import Config
from websearch.models import BackendReport, Hit, SearchResponse
from websearch.server.app import build_app


class FakeService:
    def __init__(self, failed: bool = False) -> None:
        self.failed = failed
        self.closed = False

    async def search(self, query, count=None, lang=None):
        if self.failed:
            return SearchResponse(
                query=query,
                lang="en",
                count=count or 10,
                backends=[BackendReport(name="ddg", status="error", error_code="captcha")],
                partial=True,
                error_code="all_backends_failed",
            )
        return SearchResponse(
            query=query,
            lang="en",
            count=count or 10,
            results=[Hit(title="t", url="https://e.com", snippet="s", engine="ddg")],
            backends=[BackendReport(name="ddg", status="ok", count=1)],
        )

    def health(self):
        return {"ok": True}

    async def close(self):
        self.closed = True


def client(tmp_path, service):
    return TestClient(build_app(Config(home=tmp_path), service=service))


def test_healthz_is_public_but_search_needs_the_token(tmp_path):
    with client(tmp_path, FakeService()) as http:
        assert http.get("/healthz").status_code == 200
        assert http.get("/search?q=deep").status_code == 401
        assert http.get("/mcp").status_code in (401, 405)


def test_search_with_token_get_and_post(tmp_path):
    with client(tmp_path, FakeService()) as http:
        token = (tmp_path / "token").read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}

        via_get = http.get("/search?q=deep&count=3", headers=headers)
        assert via_get.status_code == 200
        assert via_get.json()["results"][0]["url"] == "https://e.com"
        assert via_get.json()["count"] == 3

        via_post = http.post("/search", json={"q": "deep"}, headers=headers)
        assert via_post.status_code == 200
        assert via_post.json()["query"] == "deep"


def test_missing_query_is_a_400(tmp_path):
    with client(tmp_path, FakeService()) as http:
        token = (tmp_path / "token").read_text().strip()
        response = http.get("/search", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 400
        assert response.json()["error_code"] == "bad_request"


def test_non_string_lang_is_a_400_not_a_500(tmp_path):
    with client(tmp_path, FakeService()) as http:
        token = (tmp_path / "token").read_text().strip()
        response = http.post(
            "/search",
            json={"q": "deep", "lang": ["fa"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400


def test_all_backends_failing_is_a_502_with_reasons(tmp_path):
    with client(tmp_path, FakeService(failed=True)) as http:
        token = (tmp_path / "token").read_text().strip()
        response = http.get("/search?q=deep", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 502
        assert response.json()["backends"][0]["error_code"] == "captcha"
        assert response.json()["error_code"] == "all_backends_failed"
