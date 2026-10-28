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


@pytest.mark.parametrize(
    ("callback", "expected_status", "accepted"),
    [
        ("/?state=expected-state&code=authorization-code&picked_file_ids=qa-1", 200, True),
        ("/?code=authorization-code", 400, False),
        ("/?state=&code=authorization-code", 400, False),
        ("/?state=wrong-state&code=authorization-code", 400, False),
        ("/?state=expected-state&state=wrong-state&code=authorization-code", 400, False),
        ("/?state=expected-state&code=one&code=two", 400, False),
        ("/favicon.ico?state=expected-state&code=authorization-code", 404, False),
    ],
)
def test_picker_callback_requires_exact_oauth_state_and_unique_query(
    callback, expected_status, accepted,
):
    """Real local HTTP handler, synthetic code; never contacts Google."""
    import threading
    from http.server import HTTPServer
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from electridrive.google_api.picker import _Handler

    with HTTPServer(("127.0.0.1", 0), _Handler) as server:
        server.expected_state = "expected-state"
        server.captured = None
        server.timeout = 3
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}{callback}", timeout=3,
            ) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
            exc.close()
        finally:
            worker.join(timeout=4)
        assert not worker.is_alive()
        assert status == expected_status
        assert (server.captured is not None) is accepted
        if accepted:
            assert server.captured["state"] == "expected-state"
            assert server.captured["code"] == "authorization-code"
