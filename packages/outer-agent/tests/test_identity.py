import jwt
import pytest
from coa_outer_agent.identity import MissingIdentityError, from_runtime_context

pytestmark = pytest.mark.unit


class Context:
    def __init__(self, headers):
        self.request_headers = headers


def test_derives_user_context_from_validated_token_email():
    token = jwt.encode(
        {"sub": "subject-1", "email": "person@example.com"},
        "test-only-key",
    )
    identity = from_runtime_context(Context({"authorization": f"Bearer {token}"}))
    assert identity.subject == "subject-1"
    assert identity.user_id == "person@example.com"
    assert identity.knowledge_user_context == {"userId": "person@example.com"}


def test_falls_back_to_subject_when_email_is_absent():
    token = jwt.encode({"sub": "subject-1"}, "test-only-key")
    identity = from_runtime_context(Context({"Authorization": f"Bearer {token}"}))
    assert identity.user_id == "subject-1"


def test_uses_preferred_username_when_email_is_absent():
    token = jwt.encode(
        {"sub": "subject-1", "preferred_username": "person@example.com"},
        "test-only-key",
    )
    identity = from_runtime_context(Context({"Authorization": f"Bearer {token}"}))
    assert identity.user_id == "person@example.com"


@pytest.mark.parametrize(
    "context",
    [
        None,
        Context({}),
        Context({"authorization": "Basic value"}),
        Context({"authorization": "Bearer malformed"}),
    ],
)
def test_rejects_missing_or_malformed_identity(context):
    with pytest.raises(MissingIdentityError):
        from_runtime_context(context)
