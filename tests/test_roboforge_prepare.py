import json
import socket

import pytest

from portable.roboforge_prepare import retire_endpoint


@pytest.fixture
def output(tmp_path):
    (tmp_path / 'qualification.json').write_text(json.dumps({'status': 'passed', 'process_exit_code': 0}))
    return tmp_path


def test_finished_native_qualifier_retires_its_dead_socket(output):
    path = output / 'native.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
    marker = output / 'preserve'
    marker.write_text('native recording')
    assert retire_endpoint(output)['removed'] is True
    assert not path.exists()
    assert marker.read_text() == 'native recording'


def test_live_endpoint_is_preserved(output):
    path = output / 'native.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen()
        with pytest.raises(ValueError, match='still live'):
            retire_endpoint(output)
        assert path.exists()


def test_failed_qualifier_does_not_trigger_cleanup(output):
    (output / 'qualification.json').write_text(json.dumps({'status': 'failed', 'process_exit_code': 1}))
    with pytest.raises(ValueError, match='clean native exit'):
        retire_endpoint(output)


def test_regular_file_is_preserved(output):
    path = output / 'native.sock'
    path.write_text('keep')
    with pytest.raises(ValueError, match='image-owned socket'):
        retire_endpoint(output)
    assert path.read_text() == 'keep'
