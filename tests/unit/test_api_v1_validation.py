import pytest
import base64
from api.v1 import validation as val


def test_validate_required_fields_missing():
    with pytest.raises(val.ValidationError) as exc:
        val.validate_required_fields({'a': 1}, ['a', 'b'])
    assert 'Missing required parameter: b' in str(exc.value)


def test_validate_field_type_invalid():
    data = {'num': 'not-int'}
    with pytest.raises(val.ValidationError):
        val.validate_field_type(data, 'num', int)


def test_validate_string_length_bounds():
    data = {'s': 'abc'}
    with pytest.raises(val.ValidationError):
        val.validate_string_length(data, 's', min_length=5)
    with pytest.raises(val.ValidationError):
        val.validate_string_length(data, 's', max_length=2)


def test_validate_base64_invalid():
    with pytest.raises(val.ValidationError):
        val.validate_base64({'b64': 'abc!'}, 'b64')


def test_validate_json_string_invalid():
    with pytest.raises(val.ValidationError):
        val.validate_json_string({'js': 'not-json'}, 'js')


def test_validate_chat_messages_invalid_role():
    messages = [{'role': 'bad', 'content': 'hi'}]
    with pytest.raises(val.ValidationError):
        val.validate_chat_messages(messages)


def test_validate_encrypted_request_missing_fields():
    with pytest.raises(val.ValidationError):
        val.validate_encrypted_request({'client_public_key': 'x'})


def test_validate_model_name_not_found():
    with pytest.raises(val.ValidationError):
        val.validate_model_name('nope', ['model'])
