import base64
import pytest
from api.v1 import validation as val


def test_validate_field_type_allows_missing_and_none():
    data = {}
    assert val.validate_field_type(data, 'x', int) is None
    data['x'] = None
    assert val.validate_field_type(data, 'x', int, allow_none=True) is None


def test_validate_string_length_and_base64_json():
    data = {}
    # field missing should simply return
    assert val.validate_string_length(data, 's') is None
    assert val.validate_base64(data, 'b') is None
    assert val.validate_json_string(data, 'j') is None
    # valid cases
    data = {'s': 'abc', 'b': base64.b64encode(b'ok').decode(), 'j': '{"a":1}'}
    assert val.validate_string_length(data, 's') is None
    assert val.validate_base64(data, 'b') is None
    assert val.validate_json_string(data, 'j') is None


def test_validate_chat_messages_and_encrypted_request():
    with pytest.raises(val.ValidationError):
        val.validate_chat_messages('oops')
    with pytest.raises(val.ValidationError):
        val.validate_chat_messages(['hi'])
    msgs = [{'role': 'user', 'content': 'hi'}]
    assert val.validate_chat_messages(msgs) is None

    with pytest.raises(val.ValidationError):
        val.validate_encrypted_request({'client_public_key': 'pk', 'messages': []})
    payload = {
        'client_public_key': 'pk',
        'messages': {
            'ciphertext': base64.b64encode(b'd').decode(),
            'cipherkey': base64.b64encode(b'k').decode(),
            'iv': base64.b64encode(b'i').decode(),
        },
    }
    assert val.validate_encrypted_request(payload) is None
