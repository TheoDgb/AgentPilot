from fastapi.testclient import TestClient

from shopsim.tests.conftest import MakePayment


def test_get_payment(client: TestClient, make_payment: MakePayment) -> None:
    payment = make_payment(amount_cents=4990)

    response = client.get(f"/payments/{payment.order_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == payment.id
    assert body["order_id"] == payment.order_id
    assert body["status"] == "succeeded"
    assert body["amount_cents"] == 4990
    assert body["refunded_cents"] == 0


def test_get_payment_missing_returns_404(client: TestClient) -> None:
    response = client.get("/payments/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Payment not found"}
