import pytest

from shopsim.app import latency


def test_payment_latency_defaults_to_100_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYMENT_LATENCY_MS", raising=False)

    assert latency.payment_latency_ms() == 100


def test_payment_latency_is_read_on_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_LATENCY_MS", "250")
    assert latency.payment_latency_ms() == 250

    monkeypatch.setenv("PAYMENT_LATENCY_MS", "40")
    assert latency.payment_latency_ms() == 40


def test_simulate_payment_latency_sleeps_configured_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(latency.time, "sleep", slept.append)
    monkeypatch.setenv("PAYMENT_LATENCY_MS", "250")

    latency.simulate_payment_latency()

    assert slept == [0.25]
