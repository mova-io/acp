"""Cross-run Release history is owner-scoped and carries actionable evidence."""
from types import SimpleNamespace

from routes import scans


class _Store:
    def get_file_records(self, scan_id, files=None, owner=None):
        # History projects publication currency against the owner's current corrected copies.
        return {}

    def list_release_history(self, owner, limit=50):
        if owner != "owner@example.com":
            return []
        return [{
            "id": "release-1", "scan_id": "scan-1", "owner_email": owner,
            "source": "sharepoint", "folder_name": "Finance Release", "status": "completed",
            "created_at": "2026-09-06T12:00:00Z", "updated_at": "2026-09-06T12:05:00Z",
            "documents_total": 1, "published": 1, "failed": 0, "remaining": 0,
            "roots": [{"provider": "sharepoint", "provider_location": "graph:finance",
                       "folder_name": "Finance Release", "folder_url": "https://sp/folder"}],
            "documents": [{"file": "report.pdf", "status": "published",
                           "destination_relative_path": "Remediated/Finance Release/report.pdf",
                           "released_document_url": "https://sp/report", "created_result": 0,
                           "corrected_checksum": "a" * 64, "verification": "sha256",
                           "failure_category": None, "explanation": None,
                           "published_at": "2026-09-06T12:05:00Z"}],
        }]


def _request(owner):
    return SimpleNamespace(state=SimpleNamespace(user_email=owner))


def test_history_exposes_execution_destination_result_and_manifest(monkeypatch):
    monkeypatch.setattr(scans.core, "store", _Store())

    response = scans.get_release_history(_request("owner@example.com"), limit=25)

    release = response["releases"][0]
    assert release["actor"] == "owner@example.com"
    assert release["release_id"] == "release-1"
    assert release["destinations"][0]["folder_url"] == "https://sp/folder"
    assert release["documents"][0]["created"] is False
    assert release["documents"][0]["checksum"] == "a" * 64
    assert release["documents"][0]["verification"] == "sha256"
    assert release["manifest_url"] == "/api/scans/scan-1/release/manifest"
    # A receipt with no exact artifact digest never reads as current.
    assert release["publication_state"] == "identity_unknown"
    assert release["documents"][0]["publication_state"] == "identity_unknown"


def test_history_does_not_expose_another_owner(monkeypatch):
    monkeypatch.setattr(scans.core, "store", _Store())
    assert scans.get_release_history(_request("someone@example.com"))["releases"] == []
