"""Offline tests for the blog platform backend.

All AWS access is replaced by in-memory fakes or monkeypatched boto3 stubs, so
the suite never touches the network or LocalStack.
"""

import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import storage as storage_module
from app import app, get_storage
from storage import (
    DynamoCommentRepository,
    DynamoPostRepository,
    S3ImageStore,
    SqsModerationQueue,
    Storage,
    StorageError,
    decode_token,
    encode_token,
)
from uploads import parse_upload, sanitize_filename


class FakePostRepository:
    def __init__(self):
        self.items = {}

    def ping(self):
        return True

    def create(self, item):
        self.items[item["post_id"]] = dict(item)
        return dict(item)

    def get(self, post_id):
        item = self.items.get(post_id)
        return dict(item) if item else None

    def list_posts(self, limit=20, next_token=None, status=None):
        rows = sorted(self.items.values(), key=lambda row: row["created_at"])
        if status:
            rows = [row for row in rows if row.get("status") == status]
        start = int(next_token) if next_token else 0
        page = rows[start:start + limit]
        token = str(start + limit) if start + limit < len(rows) else None
        return [dict(row) for row in page], token

    def update(self, post_id, changes):
        if post_id not in self.items:
            return None
        self.items[post_id].update(changes)
        return dict(self.items[post_id])

    def delete(self, post_id):
        return self.items.pop(post_id, None) is not None

    def add_image_key(self, post_id, image_key):
        if post_id not in self.items:
            return False
        self.items[post_id].setdefault("image_keys", []).append(image_key)
        return True


class FakeCommentRepository:
    def __init__(self):
        self.items = {}

    def ping(self):
        return True

    def put_comment(self, item):
        self.items[(item["post_id"], item["comment_id"])] = dict(item)
        return dict(item)

    def list_for_post(self, post_id, limit=50):
        rows = [dict(v) for k, v in self.items.items() if k[0] == post_id]
        rows.sort(key=lambda row: row["comment_id"])
        return rows[:limit]


class FakeImageStore:
    def __init__(self):
        self.objects = {}

    def ping(self):
        return True

    def put_image(self, key, data, content_type):
        self.objects[key] = (data, content_type)
        return key

    def list_keys(self, prefix):
        return sorted(key for key in self.objects if key.startswith(prefix))

    def presigned_url(self, key, expires_in=None):
        return "https://images.example.invalid/{0}?signed=1".format(key)


class FakeModerationQueue:
    def __init__(self):
        self.messages = []
        self.deleted = []
        self._counter = 0

    def ping(self):
        return True

    def send_comment(self, payload):
        self._counter += 1
        message_id = "msg-{0}".format(self._counter)
        self.messages.append({"message_id": message_id, "receipt_handle": "rh-{0}".format(self._counter),
                              "payload": dict(payload)})
        return message_id

    def receive_comments(self, max_messages=10, wait_seconds=0):
        results = []
        for message in self.messages[:max_messages]:
            entry = dict(message["payload"])
            entry["receipt_handle"] = message["receipt_handle"]
            entry["message_id"] = message["message_id"]
            results.append(entry)
        return results

    def delete_comment(self, receipt_handle):
        self.deleted.append(receipt_handle)
        self.messages = [m for m in self.messages if m["receipt_handle"] != receipt_handle]


@pytest.fixture()
def fake_storage():
    return Storage(
        posts=FakePostRepository(),
        comments=FakeCommentRepository(),
        images=FakeImageStore(),
        moderation=FakeModerationQueue(),
    )


@pytest.fixture()
def client(fake_storage):
    app.dependency_overrides[get_storage] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create_post(client, title="Hello", status="published"):
    response = client.post(
        "/posts",
        json={
            "title": title,
            "body_markdown": "# {0}".format(title),
            "tags": ["intro"],
            "status": status,
        },
        headers={"X-Author": "ada"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _multipart(data, filename="cover.png", content_type="image/png"):
    boundary = "testboundary12345"
    head = (
        "--{0}\r\n"
        'Content-Disposition: form-data; name="file"; filename="{1}"\r\n'
        "Content-Type: {2}\r\n\r\n"
    ).format(boundary, filename, content_type).encode("utf-8")
    tail = "\r\n--{0}--\r\n".format(boundary).encode("utf-8")
    return head + data + tail, "multipart/form-data; boundary={0}".format(boundary)


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["posts"] == "ok"
    assert body["region"]


def test_health_degraded(client, fake_storage):
    def boom():
        raise RuntimeError("unreachable")

    fake_storage.images.ping = boom
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["images"] == "unavailable"


def test_create_and_read_post(client):
    post = _create_post(client)
    assert post["author"] == "ada"
    assert post["status"] == "published"
    fetched = client.get("/posts/{0}".format(post["post_id"]))
    assert fetched.status_code == 200
    assert fetched.json()["body_markdown"] == "# Hello"


def test_create_post_rejects_bad_status(client):
    response = client.post("/posts", json={"title": "x", "status": "archived"})
    assert response.status_code == 400


def test_create_post_validation_error(client):
    assert client.post("/posts", json={"body_markdown": "no title"}).status_code == 422


def test_read_missing_post(client):
    assert client.get("/posts/missing").status_code == 404


def test_list_posts_pagination_and_filter(client):
    _create_post(client, title="one", status="published")
    _create_post(client, title="two", status="draft")
    _create_post(client, title="three", status="published")

    first = client.get("/posts", params={"limit": 2}).json()
    assert first["count"] == 2
    assert first["next_token"] == "2"

    second = client.get("/posts", params={"limit": 2, "next_token": first["next_token"]}).json()
    assert second["count"] == 1
    assert second["next_token"] is None

    drafts = client.get("/posts", params={"status": "draft"}).json()
    assert drafts["count"] == 1
    assert drafts["items"][0]["title"] == "two"


def test_list_posts_bad_token(client):
    response = client.get("/posts", params={"next_token": "!!not-base64!!"})
    assert response.status_code == 400


def test_list_posts_bad_status(client):
    assert client.get("/posts", params={"status": "nope"}).status_code == 400


def test_update_post(client):
    post = _create_post(client)
    response = client.put(
        "/posts/{0}".format(post["post_id"]),
        json={"title": "Updated", "status": "draft"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Updated"
    assert body["status"] == "draft"
    assert body["updated_at"] >= post["updated_at"]


def test_update_post_requires_fields(client):
    post = _create_post(client)
    assert client.put("/posts/{0}".format(post["post_id"]), json={}).status_code == 400


def test_update_missing_post(client):
    assert client.put("/posts/none", json={"title": "x"}).status_code == 404


def test_delete_post(client):
    post = _create_post(client)
    assert client.delete("/posts/{0}".format(post["post_id"])).status_code == 204
    assert client.delete("/posts/{0}".format(post["post_id"])).status_code == 404


def test_upload_image_multipart_and_list(client, fake_storage):
    post = _create_post(client)
    body, content_type = _multipart(b"fake-image-bytes")
    response = client.post(
        "/posts/{0}/images".format(post["post_id"]),
        content=body,
        headers={"content-type": content_type},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["image_key"].startswith("{0}/".format(post["post_id"]))
    assert payload["image_key"].endswith("cover.png")
    assert payload["size_bytes"] == len(b"fake-image-bytes")
    assert payload["content_type"] == "image/png"
    assert fake_storage.images.objects[payload["image_key"]][0] == b"fake-image-bytes"

    listed = client.get("/posts/{0}/images".format(post["post_id"])).json()
    assert listed["count"] == 1
    assert listed["images"][0]["presigned_url"].startswith("https://")


def test_upload_image_raw_body(client):
    post = _create_post(client)
    response = client.post(
        "/posts/{0}/images".format(post["post_id"]),
        params={"filename": "../../evil name.jpg"},
        content=b"rawbytes",
        headers={"content-type": "image/jpeg"},
    )
    assert response.status_code == 201
    assert response.json()["image_key"].endswith("evil_name.jpg")


def test_upload_image_empty_body(client):
    post = _create_post(client)
    response = client.post(
        "/posts/{0}/images".format(post["post_id"]),
        content=b"",
        headers={"content-type": "image/png"},
    )
    assert response.status_code == 400


def test_upload_image_missing_post(client):
    response = client.post("/posts/nope/images", content=b"data", headers={"content-type": "image/png"})
    assert response.status_code == 404


def test_comment_moderation_approve_flow(client, fake_storage):
    post = _create_post(client)
    post_id = post["post_id"]

    submitted = client.post(
        "/posts/{0}/comments".format(post_id),
        json={"author_name": "reader", "author_email": "r@example.com", "body": "nice post"},
    )
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "pending_moderation"
    comment_id = submitted.json()["comment_id"]

    # Not published yet.
    assert client.get("/posts/{0}/comments".format(post_id)).json()["count"] == 0

    pending = client.get("/moderation/comments").json()
    assert pending["count"] == 1
    entry = pending["comments"][0]
    assert entry["comment_id"] == comment_id
    assert entry["receipt_handle"]

    approved = client.post(
        "/moderation/comments/approve",
        json={
            "receipt_handle": entry["receipt_handle"],
            "approved_by": "moderator",
            "comment": {
                "comment_id": entry["comment_id"],
                "post_id": entry["post_id"],
                "author_name": entry["author_name"],
                "body": entry["body"],
                "submitted_at": entry["submitted_at"],
            },
        },
    )
    assert approved.status_code == 201
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "moderator"
    assert fake_storage.moderation.messages == []

    published = client.get("/posts/{0}/comments".format(post_id)).json()
    assert published["count"] == 1
    assert published["comments"][0]["comment_id"] == comment_id


def test_comment_moderation_reject_flow(client, fake_storage):
    post = _create_post(client)
    post_id = post["post_id"]
    client.post("/posts/{0}/comments".format(post_id), json={"author_name": "spam", "body": "buy now"})
    entry = client.get("/moderation/comments").json()["comments"][0]

    rejected = client.post(
        "/moderation/comments/reject",
        json={"receipt_handle": entry["receipt_handle"], "comment_id": entry["comment_id"], "reason": "spam"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert fake_storage.moderation.messages == []
    assert fake_storage.comments.items == {}
    assert client.get("/posts/{0}/comments".format(post_id)).json()["count"] == 0


def test_comment_submission_validation(client):
    post = _create_post(client)
    response = client.post("/posts/{0}/comments".format(post["post_id"]), json={"body": "missing author"})
    assert response.status_code == 422


def test_comment_on_missing_post(client):
    response = client.post("/posts/nope/comments", json={"author_name": "a", "body": "b"})
    assert response.status_code == 404


def test_storage_error_becomes_502(client, fake_storage):
    def boom(post_id):
        raise StorageError("get post failed (ResourceNotFoundException)")

    fake_storage.posts.get = boom
    response = client.get("/posts/anything")
    assert response.status_code == 502
    assert "failed" in response.json()["detail"]


# --- storage layer unit tests (boto3 fully stubbed) -------------------------


class FakeTable:
    table_status = "ACTIVE"

    def __init__(self):
        self.items = {}
        self.last_update = None

    def put_item(self, Item):
        self.items[Item["post_id"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["post_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        rows = list(self.items.values())
        response = {"Items": rows}
        if kwargs.get("Limit") and len(rows) >= kwargs["Limit"]:
            response["LastEvaluatedKey"] = {"post_id": rows[-1]["post_id"]}
        return response

    def query(self, **kwargs):
        return {"Items": [{"post_id": "p1", "comment_id": "c1", "body": "hi"}]}

    def update_item(self, **kwargs):
        self.last_update = kwargs
        post_id = kwargs["Key"]["post_id"]
        if post_id not in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        return {"Attributes": dict(self.items[post_id], title="updated")}

    def delete_item(self, **kwargs):
        post_id = kwargs["Key"]["post_id"]
        if post_id not in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")
        del self.items[post_id]
        return {}


def test_dynamo_post_repository_crud():
    table = FakeTable()
    repo = DynamoPostRepository(table=table)
    assert repo.ping() is True
    repo.create({"post_id": "p1", "title": "t", "created_at": "now"})
    assert repo.get("p1")["title"] == "t"
    assert repo.get("absent") is None

    items, token = repo.list_posts(limit=1, status="published")
    assert len(items) == 1
    assert token is not None

    updated = repo.update("p1", {"title": "updated", "status": "draft"})
    assert updated["title"] == "updated"
    assert "#a0" in table.last_update["ExpressionAttributeNames"]
    assert repo.update("absent", {"title": "x"}) is None
    with pytest.raises(ValueError):
        repo.update("p1", {})

    assert repo.add_image_key("p1", "p1/img.png") is True
    assert repo.add_image_key("absent", "k") is False
    assert repo.delete("p1") is True
    assert repo.delete("p1") is False


def test_dynamo_comment_repository():
    table = FakeTable()
    repo = DynamoCommentRepository(table=table)
    assert repo.ping() is True
    stored = repo.put_comment({"post_id": "p1", "comment_id": "c1", "body": "hi"})
    assert stored["comment_id"] == "c1"
    rows = repo.list_for_post("p1")
    assert rows[0]["comment_id"] == "c1"


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def head_bucket(self, Bucket):
        return {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = (Body, ContentType)

    def list_objects_v2(self, **kwargs):
        keys = [k for k in sorted(self.objects) if k.startswith(kwargs["Prefix"])]
        if "ContinuationToken" not in kwargs and len(keys) > 1:
            return {"Contents": [{"Key": keys[0]}], "IsTruncated": True, "NextContinuationToken": "more"}
        contents = keys[1:] if "ContinuationToken" in kwargs else keys
        return {"Contents": [{"Key": key} for key in contents], "IsTruncated": False}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return "https://s3.example.invalid/{0}?exp={1}".format(Params["Key"], ExpiresIn)


def test_s3_image_store():
    client = FakeS3Client()
    store = S3ImageStore(client=client, bucket="bucket", presign_ttl=60)
    assert store.ping() is True
    store.put_image("p1/a.png", b"a", "image/png")
    store.put_image("p1/b.png", b"b", "image/png")
    assert store.list_keys("p1/") == ["p1/a.png", "p1/b.png"]
    assert store.presigned_url("p1/a.png").endswith("exp=60")


class FakeSqsClient:
    def __init__(self):
        self.sent = []
        self.deleted = []

    def get_queue_url(self, QueueName):
        return {"QueueUrl": "https://sqs.example.invalid/{0}".format(QueueName)}

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": "m-1"}

    def receive_message(self, **kwargs):
        return {
            "Messages": [
                {"ReceiptHandle": "rh-1", "MessageId": "m-1", "Body": json.dumps({"comment_id": "c1"})},
                {"ReceiptHandle": "rh-2", "MessageId": "m-2", "Body": "not-json"},
            ]
        }

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


def test_sqs_moderation_queue():
    client = FakeSqsClient()
    queue = SqsModerationQueue(client=client, queue_name="q")
    assert queue.queue_url.endswith("/q")
    assert queue.ping() is True
    assert queue.send_comment({"comment_id": "c1"}) == "m-1"
    assert json.loads(client.sent[0][1])["comment_id"] == "c1"

    received = queue.receive_comments(max_messages=5, wait_seconds=1)
    assert received[0]["comment_id"] == "c1"
    assert received[0]["receipt_handle"] == "rh-1"
    assert received[1]["body"] == "not-json"

    queue.delete_comment("rh-1")
    assert client.deleted == ["rh-1"]


def test_build_storage_uses_environment(monkeypatch):
    calls = {"resources": [], "clients": []}

    class FakeBoto3:
        def resource(self, name, **kwargs):
            calls["resources"].append((name, kwargs))
            return SimpleNamespace(Table=lambda table_name: "table:{0}".format(table_name))

        def client(self, name, **kwargs):
            calls["clients"].append((name, kwargs))
            return FakeS3Client() if name == "s3" else FakeSqsClient()

    monkeypatch.setattr(storage_module, "boto3", FakeBoto3())
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("POSTS_TABLE", "custom-posts")
    monkeypatch.setenv("COMMENTS_TABLE", "custom-comments")
    monkeypatch.setenv("IMAGES_BUCKET", "custom-bucket")
    monkeypatch.setenv("MODERATION_QUEUE", "custom-queue")

    built = storage_module.build_storage()
    assert built.posts.table == "table:custom-posts"
    assert built.comments.table == "table:custom-comments"
    assert built.images.bucket == "custom-bucket"
    assert built.moderation.queue_name == "custom-queue"
    assert built.images.client is not None
    name, kwargs = calls["resources"][0]
    assert name == "dynamodb"
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["endpoint_url"] == "http://localhost:4566"
    assert calls["clients"][0][1]["endpoint_url"] == "http://localhost:4566"


def test_region_defaults(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    assert storage_module.aws_region() == "us-east-1"
    assert storage_module.aws_endpoint_url() is None


def test_token_round_trip():
    token = encode_token({"post_id": "abc"})
    assert decode_token(token) == {"post_id": "abc"}
    with pytest.raises(ValueError):
        decode_token("###")


def test_parse_upload_helpers():
    body, content_type = _multipart(b"data", filename="pic.png")
    upload = parse_upload(content_type, body)
    assert upload.filename == "pic.png"
    assert upload.data == b"data"

    raw = parse_upload("image/png", b"bytes", fallback_filename=None)
    assert raw.filename == "upload.bin"
    assert sanitize_filename("  a b/c!.png ") == "c_.png"

    with pytest.raises(ValueError):
        parse_upload("multipart/form-data; boundary=zzz", b"garbage")
