"""Offline tests for the blog platform backend.

The real repository implementation is exercised with fake boto3 clients, so the
full code path (expression building, S3 keys, SQS payloads) is covered without
any network access.
"""
import json
import os
import re

from fastapi.testclient import TestClient

import storage
from app import app, get_repository
from storage import AwsBlogRepository, PendingCommentNotFound, PostNotFound
from uploads import parse_upload

BOUNDARY = "----blogtestboundary"
LIST_APPEND_RE = re.compile(
    r"^list_append\(\s*(?:if_not_exists\(\s*([#\w]+)\s*,\s*(:\w+)\s*\)|([#\w]+))\s*,\s*(:\w+)\s*\)$"
)


def split_clauses(expression):
    """Split a DynamoDB SET expression on top-level commas."""
    clauses = []
    current = ""
    depth = 0
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            clauses.append(current)
            current = ""
        else:
            current += char
    clauses.append(current)
    return [clause.strip() for clause in clauses if clause.strip()]


class FakeTable(object):
    """Tiny in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, key_names):
        self.key_names = list(key_names)
        self.items = {}
        self.scan_kwargs = None
        self.last_evaluated_key = None
        self.fail = False

    def _key(self, source):
        return tuple(source[name] for name in self.key_names)

    def put_item(self, Item=None, **kwargs):
        self.items[self._key(Item)] = dict(Item)
        return {}

    def get_item(self, Key=None, **kwargs):
        if self.fail:
            raise RuntimeError("dynamodb unavailable")
        item = self.items.get(self._key(Key))
        if item is None:
            return {}
        return {"Item": dict(item)}

    def delete_item(self, Key=None, **kwargs):
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key=None, UpdateExpression="", ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **kwargs):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        key = self._key(Key)
        item = self.items.get(key)
        if item is None:
            item = dict(Key)
        expression = UpdateExpression.strip()
        if expression.upper().startswith("SET "):
            expression = expression[4:]
        for clause in split_clauses(expression):
            left, right = clause.split("=", 1)
            attribute = names.get(left.strip(), left.strip())
            right = right.strip()
            match = LIST_APPEND_RE.match(right)
            if match:
                base = match.group(1) or match.group(3)
                base_attribute = names.get(base, base)
                current = list(item.get(base_attribute) or [])
                current.extend(values[match.group(4)])
                item[attribute] = current
            else:
                item[attribute] = values[right]
        self.items[key] = item
        return {"Attributes": dict(item)}

    def scan(self, **kwargs):
        self.scan_kwargs = kwargs
        response = {"Items": [dict(item) for item in self.items.values()]}
        if self.last_evaluated_key:
            response["LastEvaluatedKey"] = dict(self.last_evaluated_key)
        return response

    def query(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}


class FakeS3(object):
    """In-memory stand-in for the S3 client operations used by the service."""

    def __init__(self, head_fails=False):
        self.objects = {}
        self.deleted = []
        self.head_fails = head_fails

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None, **kwargs):
        self.objects[(Bucket, Key)] = {"Body": Body, "ContentType": ContentType}
        return {"ETag": "fake-etag"}

    def delete_objects(self, Bucket=None, Delete=None, **kwargs):
        keys = [entry["Key"] for entry in (Delete or {}).get("Objects", [])]
        for key in keys:
            self.objects.pop((Bucket, key), None)
            self.deleted.append(key)
        return {"Deleted": [{"Key": key} for key in keys]}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None, **kwargs):
        params = Params or {}
        return "https://example.test/{}?op={}&exp={}".format(params.get("Key"), operation, ExpiresIn)

    def head_bucket(self, Bucket=None, **kwargs):
        if self.head_fails:
            raise RuntimeError("bucket unavailable")
        return {}


class FakeSQS(object):
    """In-memory stand-in for the SQS client operations used by the service."""

    def __init__(self):
        self.queue = []
        self.in_flight = {}
        self.deleted = []
        self.sent = []

    def send_message(self, QueueUrl=None, MessageBody=None, **kwargs):
        message_id = "msg-{}".format(len(self.sent) + 1)
        message = {
            "MessageId": message_id,
            "Body": MessageBody,
            "ReceiptHandle": "rh-{}".format(message_id),
        }
        self.queue.append(message)
        self.sent.append(message)
        return {"MessageId": message_id}

    def receive_message(self, QueueUrl=None, MaxNumberOfMessages=1, **kwargs):
        taken = self.queue[:MaxNumberOfMessages]
        self.queue = self.queue[MaxNumberOfMessages:]
        for message in taken:
            self.in_flight[message["ReceiptHandle"]] = message
        if not taken:
            return {}
        return {"Messages": [dict(message) for message in taken]}

    def delete_message(self, QueueUrl=None, ReceiptHandle=None, **kwargs):
        self.deleted.append(ReceiptHandle)
        self.in_flight.pop(ReceiptHandle, None)
        return {}

    def get_queue_attributes(self, QueueUrl=None, AttributeNames=None, **kwargs):
        return {"Attributes": {"ApproximateNumberOfMessages": str(len(self.queue))}}

    def get_queue_url(self, QueueName=None, **kwargs):
        return {"QueueUrl": "http://queue.test/{}".format(QueueName)}


def build_repo(head_fails=False):
    """Create a repository wired to fake AWS clients."""
    posts = FakeTable(["post_id"])
    comments = FakeTable(["post_id", "comment_id"])
    s3 = FakeS3(head_fails=head_fails)
    sqs = FakeSQS()
    repo = AwsBlogRepository(
        posts_table=posts,
        comments_table=comments,
        s3=s3,
        sqs=sqs,
        bucket="test-bucket",
        queue_url="http://queue.test/moderation",
        presign_expiry=900,
    )
    return {"repo": repo, "posts": posts, "comments": comments, "s3": s3, "sqs": sqs}


def build_client(head_fails=False):
    """Create a TestClient with the fake-backed repository injected."""
    parts = build_repo(head_fails=head_fails)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_repository] = lambda: parts["repo"]
    parts["client"] = TestClient(app)
    return parts


def multipart_body(filename, content_type, data):
    boundary = BOUNDARY.encode("ascii")
    disposition = 'Content-Disposition: form-data; name="file"; filename="{}"'.format(filename)
    return b"\r\n".join([
        b"--" + boundary,
        disposition.encode("ascii"),
        ("Content-Type: " + content_type).encode("ascii"),
        b"",
        data,
        b"--" + boundary + b"--",
        b"",
    ])


def post_raw(client, url, body, content_type, extra_headers=None):
    headers = {"Content-Type": content_type}
    if extra_headers:
        headers.update(extra_headers)
    try:
        return client.post(url, content=body, headers=headers)
    except TypeError:  # pragma: no cover - requests based TestClient
        return client.post(url, data=body, headers=headers)


def create_sample_post(client, title="Hello", status="published"):
    response = client.post("/posts", json={
        "title": title,
        "body_markdown": "# {}\n\nsome *markdown*".format(title),
        "tags": ["aws", "python"],
        "status": status,
    }, headers={"X-Author": "ada"})
    assert response.status_code == 201
    return response.json()


def expect_raises(func, exc_type):
    try:
        func()
    except exc_type:
        return True
    raise AssertionError("expected {} to be raised".format(exc_type.__name__))


def test_health_reports_all_services_ok():
    parts = build_client()
    response = parts["client"].get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["services"] == {"dynamodb": "ok", "s3": "ok", "sqs": "ok"}
    assert payload["resources"]["images_bucket"] == "test-bucket"


def test_health_degraded_when_bucket_unreachable():
    parts = build_client(head_fails=True)
    payload = parts["client"].get("/health").json()
    assert payload["status"] == "degraded"
    assert payload["services"]["s3"] == "unavailable"


def test_create_get_and_list_posts():
    parts = build_client()
    client = parts["client"]
    created = create_sample_post(client, title="First post")
    assert created["author"] == "ada"
    assert created["status"] == "published"
    assert created["image_keys"] == []

    fetched = client.get("/posts/{}".format(created["post_id"]))
    assert fetched.status_code == 200
    assert fetched.json()["body_markdown"].startswith("# First post")

    create_sample_post(client, title="Draft post", status="draft")
    listing = client.get("/posts").json()
    assert listing["count"] == 2
    assert "body_markdown" not in listing["items"][0]

    filtered = client.get("/posts", params={"status": "draft"}).json()
    assert filtered["count"] == 1
    assert filtered["items"][0]["title"] == "Draft post"


def test_pagination_token_roundtrip_through_api():
    parts = build_client()
    client = parts["client"]
    create_sample_post(client)
    parts["posts"].last_evaluated_key = {"post_id": "cursor-1"}
    first = client.get("/posts", params={"limit": 1}).json()
    assert first["next_token"]

    parts["posts"].last_evaluated_key = None
    second = client.get("/posts", params={"next_token": first["next_token"]})
    assert second.status_code == 200
    assert parts["posts"].scan_kwargs["ExclusiveStartKey"] == {"post_id": "cursor-1"}
    assert second.json()["next_token"] is None


def test_invalid_next_token_is_rejected():
    parts = build_client()
    response = parts["client"].get("/posts", params={"next_token": "!!!not-base64!!!"})
    assert response.status_code == 400
    assert "next_token" in response.json()["detail"]


def test_update_and_delete_post():
    parts = build_client()
    client = parts["client"]
    post = create_sample_post(client)
    post_id = post["post_id"]

    updated = client.put("/posts/{}".format(post_id), json={"title": "New title", "status": "draft"})
    assert updated.status_code == 200
    body = updated.json()
    assert body["title"] == "New title"
    assert body["status"] == "draft"

    upload = post_raw(client, "/posts/{}/images".format(post_id), b"binary-bytes", "image/png")
    assert upload.status_code == 201
    image_key = upload.json()["image_key"]

    deleted = client.delete("/posts/{}".format(post_id))
    assert deleted.status_code == 200
    assert deleted.json()["deleted_image_keys"] == [image_key]
    assert image_key in parts["s3"].deleted
    assert client.get("/posts/{}".format(post_id)).status_code == 404


def test_missing_post_paths_return_404():
    parts = build_client()
    client = parts["client"]
    assert client.get("/posts/nope").status_code == 404
    assert client.put("/posts/nope", json={"title": "x"}).status_code == 404
    assert client.delete("/posts/nope").status_code == 404
    assert client.get("/posts/nope/images").status_code == 404
    submit = client.post("/posts/nope/comments", json={"author_name": "bob", "body": "hi"})
    assert submit.status_code == 404
    upload = post_raw(client, "/posts/nope/images", b"data", "image/png")
    assert upload.status_code == 404


def test_invalid_payloads_are_rejected():
    parts = build_client()
    client = parts["client"]
    bad_status = client.post("/posts", json={"title": "t", "status": "archived"})
    assert bad_status.status_code == 422

    post = create_sample_post(client)
    empty_update = client.put("/posts/{}".format(post["post_id"]), json={})
    assert empty_update.status_code == 400

    missing_title = client.post("/posts", json={"body_markdown": "x"})
    assert missing_title.status_code == 422


def test_multipart_image_upload_and_listing():
    parts = build_client()
    client = parts["client"]
    post = create_sample_post(client)
    post_id = post["post_id"]

    body = multipart_body("cover.png", "image/png", b"\x89PNG\r\n\x1a\nfake")
    response = post_raw(
        client,
        "/posts/{}/images".format(post_id),
        body,
        "multipart/form-data; boundary={}".format(BOUNDARY),
    )
    assert response.status_code == 201
    image = response.json()
    assert image["image_key"].startswith("posts/{}/".format(post_id))
    assert image["image_key"].endswith(".png")
    assert image["content_type"] == "image/png"
    assert image["size_bytes"] == len(b"\x89PNG\r\n\x1a\nfake")
    assert ("test-bucket", image["image_key"]) in parts["s3"].objects

    listing = client.get("/posts/{}/images".format(post_id)).json()
    assert listing["count"] == 1
    entry = listing["items"][0]
    assert entry["image_key"] == image["image_key"]
    assert entry["download_url"].startswith("https://example.test/")
    assert "exp=900" in entry["download_url"]

    full_post = client.get("/posts/{}".format(post_id)).json()
    assert full_post["image_keys"] == [image["image_key"]]


def test_empty_upload_is_rejected():
    parts = build_client()
    client = parts["client"]
    post = create_sample_post(client)
    response = post_raw(client, "/posts/{}/images".format(post["post_id"]), b"", "image/png")
    assert response.status_code == 400


def test_comment_moderation_approval_flow():
    parts = build_client()
    client = parts["client"]
    sqs = parts["sqs"]
    post = create_sample_post(client)
    post_id = post["post_id"]

    submitted = client.post("/posts/{}/comments".format(post_id), json={
        "author_name": "reader",
        "author_email": "reader@example.test",
        "body": "great write-up",
    })
    assert submitted.status_code == 202
    payload = submitted.json()
    assert payload["status"] == "pending_moderation"
    assert payload["message_id"]
    assert len(sqs.queue) == 1
    queued = json.loads(sqs.queue[0]["Body"])
    assert queued["body"] == "great write-up"

    # Not published yet.
    assert client.get("/posts/{}/comments".format(post_id)).json()["count"] == 0

    pending = client.get("/moderation/comments").json()
    assert pending["count"] == 1
    item = pending["items"][0]
    assert item["receipt_handle"]

    approved = client.post("/moderation/comments/approve", json={
        "receipt_handle": item["receipt_handle"],
        "comment_id": item["comment_id"],
        "comment": item,
    }, headers={"X-Moderator": "editor"})
    assert approved.status_code == 200
    comment = approved.json()["comment"]
    assert comment["moderator"] == "editor"
    assert comment["author_email"] == "reader@example.test"
    assert item["receipt_handle"] in sqs.deleted

    published = client.get("/posts/{}/comments".format(post_id)).json()
    assert published["count"] == 1
    assert published["items"][0]["body"] == "great write-up"


def test_comment_rejection_discards_message():
    parts = build_client()
    client = parts["client"]
    post = create_sample_post(client)
    post_id = post["post_id"]
    client.post("/posts/{}/comments".format(post_id), json={"author_name": "spam", "body": "buy things"})
    item = client.get("/moderation/comments").json()["items"][0]

    rejected = client.post("/moderation/comments/reject", json={
        "receipt_handle": item["receipt_handle"],
        "comment_id": item["comment_id"],
        "reason": "spam",
    })
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "spam"
    assert item["receipt_handle"] in parts["sqs"].deleted
    assert client.get("/posts/{}/comments".format(post_id)).json()["count"] == 0


def test_approve_unknown_comment_returns_404():
    parts = build_client()
    client = parts["client"]
    response = client.post("/moderation/comments/approve", json={
        "receipt_handle": "rh-unknown",
        "comment_id": "does-not-exist",
    })
    assert response.status_code == 404
    assert "pending comment not found" in response.json()["detail"]


def test_approve_can_recover_comment_from_queue():
    parts = build_repo()
    repo = parts["repo"]
    post = repo.create_post({"title": "queue recovery", "body_markdown": "body"})
    pending = repo.submit_comment(post["post_id"], {"author_name": "reader", "body": "nice"})
    published = repo.approve_comment(
        receipt_handle="rh-msg-1",
        comment_id=pending["comment_id"],
        moderator="editor",
    )
    assert published["body"] == "nice"
    assert published["post_id"] == post["post_id"]
    assert "rh-msg-1" in parts["sqs"].deleted


def test_moderation_listing_empty_queue():
    parts = build_client()
    response = parts["client"].get("/moderation/comments", params={"max_messages": 5})
    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_repository_raises_domain_errors():
    parts = build_repo()
    repo = parts["repo"]
    expect_raises(lambda: repo.delete_post("missing"), PostNotFound)
    expect_raises(lambda: repo.update_post("missing", {"title": "x"}), PostNotFound)
    expect_raises(lambda: repo.list_images("missing"), PostNotFound)
    expect_raises(lambda: repo.add_image("missing", "a.png", "image/png", b"x"), PostNotFound)
    expect_raises(lambda: repo.approve_comment("rh", "cid"), PendingCommentNotFound)
    assert repo.get_post("missing") is None


def test_token_helpers_roundtrip():
    token = storage.encode_token({"post_id": "abc"})
    assert storage.decode_token(token) == {"post_id": "abc"}
    expect_raises(lambda: storage.decode_token("@@@"), ValueError)


def test_client_kwargs_honours_endpoint_env():
    previous = os.environ.get("AWS_ENDPOINT_URL")
    os.environ["AWS_ENDPOINT_URL"] = "http://localhost:4566"
    try:
        kwargs = storage.client_kwargs()
        assert kwargs["endpoint_url"] == "http://localhost:4566"
        assert kwargs["region_name"] == os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    finally:
        if previous is None:
            os.environ.pop("AWS_ENDPOINT_URL", None)
        else:
            os.environ["AWS_ENDPOINT_URL"] = previous
    os.environ.pop("AWS_ENDPOINT_URL", None)
    assert storage.client_kwargs()["endpoint_url"] is None
    if previous is not None:
        os.environ["AWS_ENDPOINT_URL"] = previous


def test_parse_upload_variants():
    body = multipart_body("photo.jpeg", "image/jpeg", b"jpegdata")
    parsed = parse_upload("multipart/form-data; boundary={}".format(BOUNDARY), body)
    assert parsed is not None
    filename, content_type, data = parsed
    assert filename == "photo.jpeg"
    assert content_type == "image/jpeg"
    assert data == b"jpegdata"

    raw = parse_upload("image/png", b"rawbytes")
    assert raw == ("upload.bin", "image/png", b"rawbytes")

    assert parse_upload("multipart/form-data; boundary=x", b"not-a-multipart-body") is None


def test_safe_extension_sanitises_filenames():
    assert storage.safe_extension("photo.PNG") == ".png"
    assert storage.safe_extension("../../etc/passwd") == ""
    assert storage.safe_extension(None) == ""
