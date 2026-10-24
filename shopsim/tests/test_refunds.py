from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shopsim.app.models import OrderStatus, PaymentStatus, Refund
from shopsim.tests.conftest import MakePayment


def post_refund(
    client: TestClient,
    payment_id: int,
    amount_cents: int,
    key: str | None,
    reason: str = "Article défectueux",
) -> httpx.Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    body: dict[str, Any] = {
        "payment_id": payment_id,
        "amount_cents": amount_cents,
        "reason": reason,
    }
    return client.post("/refunds", json=body, headers=headers)


def count_refunds(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Refund)) or 0


def test_full_refund_marks_payment_and_order_refunded(
    client: TestClient, session: Session, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=5000)

    response = post_refund(client, payment.id, 5000, key="full-1")

    assert response.status_code == 201
    body = response.json()
    assert body["payment_id"] == payment.id
    assert body["amount_cents"] == 5000
    assert body["idempotency_key"] == "full-1"
    session.refresh(payment)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.order.status == OrderStatus.REFUNDED


def test_partial_refunds_then_completion(
    client: TestClient, session: Session, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=5000)

    first = post_refund(client, payment.id, 2000, key="partial-1")

    assert first.status_code == 201
    session.refresh(payment)
    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.order.status == OrderStatus.PAID
    assert client.get(f"/payments/{payment.order_id}").json()["refunded_cents"] == 2000

    second = post_refund(client, payment.id, 3000, key="partial-2")

    assert second.status_code == 201
    session.refresh(payment)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.order.status == OrderStatus.REFUNDED


def test_replay_same_key_returns_existing_refund(
    client: TestClient, session: Session, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=5000)

    first = post_refund(client, payment.id, 1000, key="replay-1")
    second = post_refund(client, payment.id, 1000, key="replay-1")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    assert count_refunds(session) == 1


def test_same_key_with_different_body_is_rejected(
    client: TestClient, session: Session, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=5000)
    post_refund(client, payment.id, 1000, key="conflict-1")

    response = post_refund(client, payment.id, 1500, key="conflict-1")

    assert response.status_code == 409
    assert count_refunds(session) == 1


def test_refund_exceeding_remaining_amount_is_rejected(
    client: TestClient, session: Session, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=1000)

    too_much = post_refund(client, payment.id, 1001, key="excess-1")
    assert too_much.status_code == 422
    assert too_much.json()["detail"]["remaining_cents"] == 1000

    assert post_refund(client, payment.id, 600, key="excess-2").status_code == 201
    cumulative = post_refund(client, payment.id, 500, key="excess-3")

    assert cumulative.status_code == 422
    assert cumulative.json()["detail"]["remaining_cents"] == 400
    assert count_refunds(session) == 1


def test_missing_idempotency_key_is_rejected(
    client: TestClient, session: Session, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=1000)

    response = post_refund(client, payment.id, 500, key=None)

    assert response.status_code == 400
    assert response.json() == {"detail": "Idempotency-Key header is required"}
    assert count_refunds(session) == 0


def test_refund_on_non_succeeded_payment_is_rejected(
    client: TestClient, make_payment: MakePayment
) -> None:
    payment = make_payment(amount_cents=1000, status=PaymentStatus.PENDING)

    response = post_refund(client, payment.id, 500, key="pending-1")

    assert response.status_code == 409
    assert response.json() == {"detail": "Payment is not refundable"}


def test_refund_on_missing_payment_returns_404(client: TestClient) -> None:
    response = post_refund(client, 999999, 500, key="missing-1")

    assert response.status_code == 404
