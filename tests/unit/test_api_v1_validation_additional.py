import base64
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
