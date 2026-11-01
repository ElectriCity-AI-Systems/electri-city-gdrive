from types import SimpleNamespace
import pytest

from electridrive.sync_schedule import periodic_sync


class Stop:
    def __init__(self):
        self.stopped = False
        self.waits = []
    def is_set(self): return self.stopped
    def wait(self, interval):
        self.waits.append(interval)
        self.stopped = True


def test_schedule_stops_cooperatively_after_one_cycle():
    stop = Stop()
    calls = []
    def run():
        calls.append(1)
        return SimpleNamespace(failed=0)
    assert len(list(periodic_sync(run, 10, stop))) == 1
    assert calls == [1]
    assert stop.waits == [10]


def test_schedule_does_not_repeat_failed_sync():
    stop = Stop()
    assert len(list(periodic_sync(lambda: SimpleNamespace(failed=1), 10, stop))) == 1
    assert not stop.stopped


def test_schedule_requires_positive_interval():
    with pytest.raises(ValueError):
        list(periodic_sync(lambda: None, 0, Stop()))


@pytest.mark.parametrize('interval', [float('inf'), float('nan'), -1])
def test_schedule_rejects_nonfinite_and_negative_intervals_before_running(interval):
    with pytest.raises(ValueError):
        list(periodic_sync(lambda: pytest.fail('invalid schedule ran'), interval))


def test_schedule_stops_on_incomplete_report():
    calls = []
    def run():
        calls.append(1)
        return SimpleNamespace(failed=0, unexplained_omissions=1)
    assert len(list(periodic_sync(run, 10, Stop()))) == 1
    assert calls == [1]
