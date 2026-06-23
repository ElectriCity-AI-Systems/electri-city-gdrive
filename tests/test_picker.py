import pytest

from electridrive.google_api.picker import (
    PickerError,
    extract_pick_result,
    picker_auth_params,
)


def test_picker_auth_params_folders_on():
    p = picker_auth_params(allow_folders=True)
    assert p["trigger_onepick"] == "true"
    assert p["allow_folder_selection"] == "true"
    assert p["prompt"] == "consent"


def test_picker_auth_params_folders_off():
    assert picker_auth_params(allow_folders=False)["allow_folder_selection"] == "false"


def test_extract_pick_result_string_values():
    code, ids = extract_pick_result({"code": "AUTHCODE", "picked_file_ids": "a,b,c"})
    assert code == "AUTHCODE"
    assert ids == ["a", "b", "c"]


def test_extract_pick_result_list_values_parse_qs_style():
    code, ids = extract_pick_result({"code": ["X"], "picked_file_ids": ["id1,id2"]})
    assert code == "X"
    assert ids == ["id1", "id2"]


def test_extract_pick_result_no_picks():
    code, ids = extract_pick_result({"code": "Z"})
    assert code == "Z" and ids == []


def test_extract_pick_result_missing_code_raises():
    with pytest.raises(PickerError):
        extract_pick_result({"picked_file_ids": "a"})


def test_extract_pick_result_error_raises():
    with pytest.raises(PickerError):
        extract_pick_result({"error": "access_denied"})
