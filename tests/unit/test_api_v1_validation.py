import pytest
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


def test_validate_base64_invalid_chars():
    with pytest.raises(val.ValidationError):
        val.validate_base64({'b64': 'ab$cd'}, 'b64')


def test_validate_json_string_invalid():
    with pytest.raises(val.ValidationError):
        val.validate_json_string({'js': 'not-json'}, 'js')


def test_validate_chat_messages_invalid_role():
    messages = [{'role': 'bad', 'content': 'hi'}]
    with pytest.raises(val.ValidationError):
        val.validate_chat_messages(messages)


def test_validate_chat_messages_allows_text_content_blocks():
    messages = [
        {
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': 'hello'},
                {'type': 'text', 'text': 'world'},
            ],
        }
    ]

    assert val.validate_chat_messages(messages) is None


@pytest.mark.parametrize('block', [
    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aaa'}},
    {'type': 'image_url', 'image_url': 'https://example.com/image.png'},
    {'type': 'input_image', 'image': {'b64_json': 'ZmFrZQ=='}},
    {'type': 'image'},
])
def test_validate_chat_messages_rejects_image_content_blocks(block):
    messages = [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}, block]}]

    with pytest.raises(val.ValidationError) as exc:
        val.validate_chat_messages(messages)

    assert 'text-only' in str(exc.value)


def test_validate_encrypted_request_missing_fields():
    with pytest.raises(val.ValidationError):
        val.validate_encrypted_request({'client_public_key': 'x'})


def test_validate_model_name_not_found():
    with pytest.raises(val.ValidationError):
        val.validate_model_name('nope', ['model'])
