"""Offline integration tests: Firestore is represented by an in-memory fake."""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The test runner can validate app behavior without installed Google SDKs or cloud credentials.
# In a normal run, requirements.txt installs the real SDKs and these fakes are not used by the app.

def _install_google_stubs() -> None:
    try:
        import google.genai  # noqa: F401
        import google.cloud.firestore  # noqa: F401
        import google.api_core.exceptions  # noqa: F401
        return
    except Exception:
        pass

    import google
    genai_mod = types.ModuleType("google.genai")
    genai_mod.Client = lambda **kwargs: None
    types_mod = types.ModuleType("google.genai.types")
    types_mod.GenerateContentConfig = lambda **kwargs: kwargs
    genai_mod.types = types_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod
    google.genai = genai_mod

    api_core = types.ModuleType("google.api_core")
    exceptions = types.ModuleType("google.api_core.exceptions")
    class GoogleAPICallError(Exception):
        pass
    class PermissionDenied(GoogleAPICallError):
        pass
    exceptions.GoogleAPICallError = GoogleAPICallError
    exceptions.PermissionDenied = PermissionDenied
    api_core.exceptions = exceptions
    sys.modules["google.api_core"] = api_core
    sys.modules["google.api_core.exceptions"] = exceptions

    cloud = types.ModuleType("google.cloud")
    firestore_mod = types.ModuleType("google.cloud.firestore")
    firestore_mod.Client = lambda **kwargs: None
    cloud.firestore = firestore_mod
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.firestore"] = firestore_mod
    google.cloud = cloud


_install_google_stubs()
main = importlib.import_module("app.main")
from fastapi.testclient import TestClient


class Snapshot:
    def __init__(self, ref, data=None):
        self.id = ref.path[-1]
        self._data = data
        self.exists = data is not None
    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocument:
    def __init__(self, client, path):
        self.client, self.path = client, tuple(path)
    @property
    def id(self):
        return self.path[-1]
    def set(self, data, merge=False):
        old = self.client.data.get(self.path, {}) if merge else {}
        self.client.data[self.path] = {**old, **data}
    def get(self):
        return Snapshot(self, self.client.data.get(self.path))
    def delete(self):
        self.client.data.pop(self.path, None)
    def collection(self, name):
        return FakeCollection(self.client, self.path + (name,))


class FakeQuery:
    def __init__(self, collection, limit=None):
        self.collection_ref, self.max_items = collection, limit
    def get(self):
        return list(self.stream())
    def stream(self):
        prefix = self.collection_ref.path
        docs = []
        for path, data in self.collection_ref.client.data.items():
            if len(path) == len(prefix) + 1 and path[:-1] == prefix:
                docs.append(Snapshot(FakeDocument(self.collection_ref.client, path), data))
        docs.sort(key=lambda d: d.id)
        return iter(docs[:self.max_items] if self.max_items is not None else docs)


class FakeCollection(FakeQuery):
    def __init__(self, client, path):
        self.client, self.path = client, tuple(path)
        self.collection_ref, self.max_items = self, None
    def document(self, doc_id):
        return FakeDocument(self.client, self.path + (str(doc_id),))
    def stream(self):
        return super().stream()
    def limit(self, amount):
        return FakeQuery(self, amount)


class FakeBatch:
    def __init__(self, client):
        self.client, self.operations = client, []
    def set(self, ref, data, merge=False):
        self.operations.append(("set", ref, data, merge))
        return self
    def delete(self, ref):
        self.operations.append(("delete", ref, None, False))
        return self
    def commit(self):
        if len(self.operations) > 500:
            raise AssertionError("Firestore batch limit exceeded")
        for operation, ref, data, merge in self.operations:
            if operation == "set":
                ref.set(data, merge=merge)
            else:
                ref.delete()
        return []


class FakeClient:
    def __init__(self):
        self.data = {}
    def collection(self, name):
        return FakeCollection(self, (name,))
    def batch(self):
        return FakeBatch(self)


@pytest.fixture(autouse=True)
def isolated_firestore(tmp_path, monkeypatch):
    main._firestore_client = FakeClient()
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent-legacy-cases.json")
    monkeypatch.setenv("MIGRATE_LOCAL_CASES", "false")
    with TestClient(main.app) as client:
        yield client
    main._firestore_client = None



def create_case(client):
    response = client.post("/api/cases", json={"title": "Synthetic test", "incident_type": "Suspected manipulated media", "notes": "Synthetic-only test case"})
    assert response.status_code == 200, response.text
    return response.json()


def test_health_reports_firestore_connected(isolated_firestore):
    response = isolated_firestore.get("/api/health")
    assert response.status_code == 200
    assert response.json()["firestore"] == "connected"
    assert response.json()["firestore_database_id"] == "mr-db"


def test_case_create_persists_and_reloads(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    listed = client.get("/api/cases").json()
    assert any(item["id"] == case["id"] and item["title"] == "Synthetic test" for item in listed)
    assert client.get(f"/api/export/{case['id']}").json()["id"] == case["id"]


def test_url_validation_and_status_validation(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    bad_url = client.post(f"/api/cases/{case['id']}/links", json={"url": "javascript:alert(1)"})
    assert bad_url.status_code == 400
    added = client.post(f"/api/cases/{case['id']}/links", json={"url": "https://example.com/post", "platform": "Example"})
    assert added.status_code == 200
    bad_status = client.patch(f"/api/cases/{case['id']}/links/{added.json()['id']}", json={"status": "Definitely removed"})
    assert bad_status.status_code == 400
    good_status = client.patch(f"/api/cases/{case['id']}/links/{added.json()['id']}", json={"status": "Reported"})
    assert good_status.status_code == 200
    assert good_status.json()["status"] == "Reported"


def test_unsupported_and_empty_upload_rejected(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    unsupported = client.post(f"/api/cases/{case['id']}/analyze", files={"file": ("payload.exe", b"nope", "application/octet-stream")})
    assert unsupported.status_code == 400
    empty = client.post(f"/api/cases/{case['id']}/analyze", files={"file": ("empty.mp4", b"", "video/mp4")})
    assert empty.status_code == 400


def test_video_chunks_are_stored_and_download_round_trips(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    project_root = Path(main.ROOT)
    video_path = project_root / "sample_videos" / "synthetic_reference.mp4"
    original = video_path.read_bytes()
    response = client.post(f"/api/cases/{case['id']}/analyze", files={"file": ("synthetic_reference.mp4", original, "video/mp4")})
    assert response.status_code == 200, response.text
    analysis = response.json()
    assert analysis["authenticity_assessment"].startswith("UNVERIFIED")
    assert analysis["storage_note"].startswith("Original media bytes stored")
    download = client.get(f"/api/cases/{case['id']}/media/{analysis['upload_id']}")
    assert download.status_code == 200
    assert download.content == original
    assert len(download.content) == analysis["size_bytes"]
    case_after = client.get(f"/api/export/{case['id']}").json()
    assert case_after["evidence"][0]["storage"] == "firestore-chunks"
    assert not list((Path(main.ROOT) / "storage" / "uploads").glob(f"{case['id']}_evidence_*.mp4"))


def test_missing_case_and_missing_media_are_404(isolated_firestore):
    client = isolated_firestore
    assert client.get("/api/export/MR-NOTFOUND").status_code == 404
    case = create_case(client)
    assert client.get(f"/api/cases/{case['id']}/media/not-a-real-upload").status_code == 404


def test_response_plan_uses_safe_fallback_when_gemini_key_absent(isolated_firestore, monkeypatch):
    client = isolated_firestore
    case = create_case(client)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    response = client.post(f"/api/cases/{case['id']}/response-plan", json={})
    assert response.status_code == 200
    plan = response.json()
    assert "UNVERIFIED" in plan["summary"] or "unverified" in plan["summary"].lower()
    assert "complaint_draft" in plan
    assert "does not submit reports" in plan["caveat"]


def test_upload_size_limit_is_enforced_and_temp_file_removed(isolated_firestore, monkeypatch):
    client = isolated_firestore
    case = create_case(client)
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 5)
    response = client.post(f"/api/cases/{case['id']}/analyze", files={"file": ("oversized.mp4", b"123456", "video/mp4")})
    assert response.status_code == 413
    assert client.get(f"/api/export/{case['id']}").json()["evidence"] == []


def test_reference_video_is_also_persisted_and_downloadable(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    base = Path(main.ROOT) / "sample_videos"
    primary = (base / "synthetic_variant.mp4").read_bytes()
    reference = (base / "synthetic_reference.mp4").read_bytes()
    response = client.post(
        f"/api/cases/{case['id']}/analyze",
        files={
            "file": ("variant.mp4", primary, "video/mp4"),
            "reference": ("reference.mp4", reference, "video/mp4"),
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reference_video"]["upload_id"]
    primary_download = client.get(f"/api/cases/{case['id']}/media/{data['upload_id']}")
    reference_download = client.get(f"/api/cases/{case['id']}/media/{data['reference_video']['upload_id']}")
    assert primary_download.content == primary
    assert reference_download.content == reference


def test_firestore_failure_is_not_reported_as_success(isolated_firestore):
    client = isolated_firestore
    class BrokenClient:
        def collection(self, name):
            raise RuntimeError("synthetic connection failure")
    main._firestore_client = BrokenClient()
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["firestore"] == "error"
    cases = client.get("/api/cases")
    assert cases.status_code == 503
    assert "Firestore is unavailable" in cases.json()["detail"]


def test_legacy_case_migration_is_idempotent(isolated_firestore, tmp_path, monkeypatch):
    legacy_path = tmp_path / "cases.json"
    legacy_path.write_text('[{"id":"MR-LEGACY1","title":"Legacy","links":[],"analysis":{"upload_id":"local-only"},"evidence":[{"id":"ev1","upload_id":"local-only"}]}]', encoding="utf-8")
    monkeypatch.setattr(main, "DB_PATH", legacy_path)
    monkeypatch.setenv("MIGRATE_LOCAL_CASES", "true")
    first = main.migrate_local_cases_if_enabled()
    second = main.migrate_local_cases_if_enabled()
    assert first == {"imported": 1, "skipped": 0}
    assert second == {"imported": 0, "skipped": 1}
    migrated = next(c for c in main.read_db() if c["id"] == "MR-LEGACY1")
    assert "upload_id" not in migrated["analysis"]
    assert migrated["evidence"][0]["storage"] == "legacy-local-not-migrated"


def test_download_refuses_media_with_corrupted_chunks(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    store = main.get_firestore()
    media = store.collection("cases").document(case["id"]).collection("media").document("broken-upload")
    media.set({"filename": "broken.mp4", "mime_type": "video/mp4", "sha256": "0" * 64, "chunk_count": 1})
    media.collection("chunks").document("000000").set({"index": 0, "data_b64": "dGVzdA==", "byte_count": 4})
    response = client.get(f"/api/cases/{case['id']}/media/broken-upload")
    assert response.status_code == 500
    assert "SHA-256 integrity check" in response.json()["detail"]


def test_invalid_video_content_returns_useful_error_without_evidence(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    response = client.post(f"/api/cases/{case['id']}/analyze", files={"file": ("not-a-video.mp4", b"this is not a video", "video/mp4")})
    assert response.status_code == 422
    assert "could not be recognized as a readable video" in response.json()["detail"]
    exported = client.get(f"/api/export/{case['id']}").json()
    assert exported["evidence"] == []
    assert exported["analysis"] is None


def test_incomplete_case_gemini_prompt_forbids_inventing_facts(isolated_firestore, monkeypatch):
    client = isolated_firestore
    case = create_case(client)
    # Deliberately incomplete: no URL, account, date/time, location, or confirmed impact.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    captured = {}
    payload = {"title":"Cautious plan", "summary":"Authenticity remains unverified.", "findings":[],
               "known_facts":["The case note says this is a synthetic-only test case."],
               "unknowns":["Post URL not provided", "Date/time not provided"], "assumptions":[],
               "next_steps":["Record the URL if one becomes known."],
               "complaint_draft":"Case [case reference]. URL: [post URL not provided]. Date: [date/time not provided].",
               "caveat":"Draft only."}
    class Response:
        text = json.dumps(payload)
    class Models:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return Response()
    class Client:
        models = Models()
    monkeypatch.setattr(main.genai, "Client", lambda **kwargs: Client())
    response = client.post(f"/api/cases/{case['id']}/response-plan", json={})
    assert response.status_code == 200, response.text
    plan = response.json()
    prompt = captured["contents"].lower()
    assert "never invent" in prompt
    assert "bracketed placeholder" in prompt
    assert plan["unknowns"] == ["Post URL not provided", "Date/time not provided"]
    assert "[post URL not provided]" in plan["complaint_draft"]
    assert plan["assumptions"] == []


def test_delete_case_removes_nested_media_and_case(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    # Create nested Firestore docs as the media persistence path would.
    case_ref = main.get_firestore().collection(main.COLLECTION_NAME).document(case["id"])
    media_ref = case_ref.collection("media").document("upload123")
    media_ref.set({"filename":"test.mp4", "chunk_count":1})
    media_ref.collection("chunks").document("000000").set({"index":0, "data_b64":"eA==", "byte_count":1})
    response = client.delete(f"/api/cases/{case['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert client.get(f"/api/export/{case['id']}").status_code == 404
    assert media_ref.get().exists is False
    assert media_ref.collection("chunks").document("000000").get().exists is False


def test_secure_deployment_requires_auth_configuration(isolated_firestore, monkeypatch):
    monkeypatch.setenv("SECURE_DEPLOYMENT", "true")
    monkeypatch.delenv("APP_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("APP_AUTH_PASSWORD", raising=False)
    response = isolated_firestore.get("/")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_case_survives_new_test_client_instance(isolated_firestore):
    first_client = isolated_firestore
    case = create_case(first_client)
    fake_db = main._firestore_client
    # Simulate a FastAPI process restart while the backing Firestore remains durable.
    main._firestore_client = fake_db
    with TestClient(main.app) as restarted_client:
        response = restarted_client.get("/api/cases")
        assert response.status_code == 200
        assert any(item["id"] == case["id"] for item in response.json())


def test_export_includes_links_hash_metadata_and_response_plan(isolated_firestore):
    client = isolated_firestore
    case = create_case(client)
    link = client.post(f"/api/cases/{case['id']}/links", json={"url":"https://example.com/demo", "platform":"Other"})
    assert link.status_code == 200
    # Seed the same shapes produced by video analysis and Gemini response generation.
    stored = client.get(f"/api/export/{case['id']}").json()
    stored["analysis"] = {"filename":"synthetic.mp4", "sha256":"a" * 64, "size_bytes":123, "authenticity_assessment":"UNVERIFIED"}
    stored["evidence"] = [{"id":"ev1", "filename":"synthetic.mp4", "sha256":"a" * 64, "storage":"firestore-chunks"}]
    stored["response_plan"] = {"mode":"Gemini", "known_facts":["synthetic test"], "complaint_draft":"Draft only"}
    # Persist updated shape through the same case document, as normal endpoint updates do.
    payload = dict(stored); payload.pop("id", None)
    main.get_firestore().collection(main.COLLECTION_NAME).document(case["id"]).set(payload, merge=True)
    exported = client.get(f"/api/export/{case['id']}").json()
    assert exported["links"][0]["url"] == "https://example.com/demo"
    assert exported["analysis"]["sha256"] == "a" * 64
    assert exported["evidence"][0]["storage"] == "firestore-chunks"
    assert exported["response_plan"]["complaint_draft"] == "Draft only"
    assert "data_b64" not in json.dumps(exported)


def test_basic_auth_blocks_unauthenticated_requests(isolated_firestore, monkeypatch):
    import base64
    monkeypatch.setenv("SECURE_DEPLOYMENT", "true")
    monkeypatch.setenv("APP_AUTH_USERNAME", "demo-user")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "demo-password-long")
    denied = isolated_firestore.get("/")
    assert denied.status_code == 401
    allowed = isolated_firestore.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"demo-user:demo-password-long").decode()})
    assert allowed.status_code == 200
