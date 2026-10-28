"""Pages d'approbation, rendues côté serveur (Jinja2, échappement automatique).

- `GET /approvals` : actions en attente d'abord, puis les dernières décisions ;
- `GET /approvals/{id}` : dossier complet et formulaires ;
- `POST /approvals/{id}/approve | reject | retry | follow-up` : décision, puis redirection
  303 vers le dossier (un rechargement ne renvoie pas le formulaire) ;
- `GET /control` : pilotage de la boucle et de l'arrêt d'urgence ;
  `POST /control/loop/pause | resume` et `POST /control/emergency` (champ `name` obligatoire,
  utilisables aussi par curl : `curl -d name=… localhost:8001/control/loop/pause`).

Toutes les pages affichent un bandeau quand l'arrêt d'urgence est actif.

Pas d'authentification : le nom saisi est déclaratif (limite signalée dans CLAUDE.md).
Le contenu des tickets n'est pas fiable : il n'est jamais marqué sûr (`|safe`) dans les
gabarits.
"""

import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, select
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import alerts, control, decisions, worker
from agentpilot.api_schemas import AlertRead, ControlStatus
from agentpilot.approval import REASON_LABELS, REPLY, Reason
from agentpilot.decisions import InvalidInput, Notice
from agentpilot.models import CallOutcome, PauseReason, PendingAction, PendingStatus
from mcp_server.shopsim_client import ShopSimError, get_json

router = APIRouter(prefix="/approvals", tags=["approbations"])
control_router = APIRouter(prefix="/control", tags=["pilotage"])


def _emergency_banner(request: Request) -> dict[str, Any]:
    """Bandeau de toutes les pages ; base illisible : on l'affiche, par prudence."""
    try:
        stopped = control.emergency_stop(request.app.state.session_factory)
    except Exception:  # noqa: BLE001
        stopped = True
    return {"emergency_stop": stopped}


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_emergency_banner]
)
templates.env.filters["eur"] = decisions.eur

LIST_LIMIT = 100

NOTICES = {
    Notice.EXECUTED: ("ok", "Action approuvée et exécutée dans ShopSim."),
    Notice.IMPOSSIBLE: ("error", "Action approuvée mais devenue impossible : rien n'a été fait."),
    Notice.UNAVAILABLE: (
        "warn",
        "ShopSim n'a pas répondu : on ne sait pas si l'action a eu lieu. Relancez "
        "l'exécution (même clé d'idempotence, sans risque de doublon).",
    ),
    Notice.REJECTED: ("ok", "Action refusée."),
    Notice.ALREADY_DECIDED: (
        "warn",
        "Cette action avait déjà été décidée : votre demande n'a rien changé.",
    ),
    Notice.NOT_RETRYABLE: ("warn", "Rien à relancer pour cette action."),
    Notice.IN_PROGRESS: ("warn", "L'exécution de cette action est déjà en cours."),
    Notice.MESSAGE_SENT: ("ok", "Message envoyé au client."),
    Notice.MESSAGE_SKIPPED: (
        "warn",
        "Message et résolution non faits : l'action n'a pas été exécutée.",
    ),
    Notice.MESSAGE_ALREADY_SENT: ("warn", "Un message a déjà été envoyé pour cette décision."),
    Notice.RESOLVED: ("ok", "Ticket résolu."),
    Notice.RESOLVE_BLOCKED: (
        "warn",
        "Ticket non résolu : une action de ce ticket n'est pas terminée.",
    ),
    Notice.FOLLOW_UP_FAILED: ("error", "ShopSim a refusé le message ou la résolution."),
}
STATUS_LABELS = {
    PendingStatus.PENDING: "en attente",
    PendingStatus.APPROVED: "approuvée, à relancer",
    PendingStatus.EXECUTED: "exécutée",
    PendingStatus.FAILED: "impossible",
    PendingStatus.REJECTED: "refusée",
}
TOOL_LABELS = {"refund_order": "Remboursement", "resend_order": "Renvoi"}
templates.env.globals |= {
    "STATUS_LABELS": STATUS_LABELS,
    "TOOL_LABELS": TOOL_LABELS,
    "REASON_LABELS": {str(r): label for r, label in REASON_LABELS.items()},
}


def _factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.session_factory


def _redirect(action_id: uuid.UUID, notices: list[Notice], detail: str | None = None) -> Response:
    query = [("notice", str(n)) for n in notices]
    if detail:
        query.append(("detail", detail))
    return RedirectResponse(f"/approvals/{action_id}?{urlencode(query)}", status_code=303)


@router.get("", response_class=HTMLResponse)
def list_actions(
    request: Request, status: Annotated[PendingStatus | None, Query()] = None
) -> Response:
    query = select(PendingAction).limit(LIST_LIMIT)
    if status is not None:
        query = query.where(PendingAction.status == status)
    # En attente d'abord, les plus anciennes en tête ; puis les dernières décisions
    query = query.order_by(
        case((PendingAction.status == PendingStatus.PENDING, 0), else_=1),
        case(
            (PendingAction.status == PendingStatus.PENDING, PendingAction.created_at),
        ).asc(),
        PendingAction.decided_at.desc(),
    )
    with _factory(request)() as session:
        actions = list(session.scalars(query))
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {"actions": actions, "status": status, "statuses": list(PendingStatus)},
    )


async def _shopsim(
    ticket_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Ticket et client lus en direct ; message d'erreur si ShopSim ne répond pas."""
    try:
        ticket = await get_json(f"/tickets/{ticket_id}", f"Ticket {ticket_id} introuvable.")
        customer = await get_json(
            f"/customers/{ticket['customer_id']}", f"Client {ticket['customer_id']} introuvable."
        )
    except ShopSimError as exc:
        return None, None, str(exc)
    return ticket, customer, None


def _agent_reply(action: PendingAction) -> str | None:
    """Réponse que l'agent a réellement envoyée au client (journal)."""
    bodies = [
        call.input.get("body")
        for call in action.run.tool_calls
        if call.tool.endswith(f"__{REPLY}") and call.outcome is CallOutcome.SUCCEEDED
    ]
    return bodies[-1] if bodies else None


async def _detail(
    request: Request,
    action_id: uuid.UUID,
    *,
    notices: list[str] | None = None,
    detail: str | None = None,
    form_error: str | None = None,
    form: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    action = decisions.load(_factory(request), action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    ticket, customer, shopsim_error = await _shopsim(action.ticket_id)
    first_name = customer["first_name"] if customer else None
    decision = action.run.output
    if action.status in (PendingStatus.EXECUTED, PendingStatus.REJECTED):
        follow_up = decisions.default_message(
            action, action.status is PendingStatus.EXECUTED, first_name
        )
    else:
        follow_up = decisions.failure_message(first_name)
    shown = [NOTICES[Notice(n)] for n in notices or [] if n in Notice.__members__.values()]
    return templates.TemplateResponse(
        request,
        "approval.html",
        {
            "action": action,
            "mismatch": decisions.amount_mismatch(action, decision),
            "ticket": ticket,
            "customer": customer,
            "shopsim_error": shopsim_error,
            "decision": decision,
            "agent_reply": _agent_reply(action),
            "approve_message": decisions.default_message(action, True, first_name),
            "reject_message": decisions.default_message(action, False, first_name),
            "follow_up_message": follow_up,
            "notices": shown,
            "detail": detail,
            "form_error": form_error,
            "form": form or {},
            "reasons": [Reason(r) for r in action.reasons if r in Reason.__members__.values()],
        },
        status_code=status_code,
    )


@router.get("/{action_id}", response_class=HTMLResponse)
async def show_action(
    request: Request,
    action_id: uuid.UUID,
    notice: Annotated[list[str] | None, Query()] = None,
    detail: str | None = None,
) -> Response:
    return await _detail(request, action_id, notices=notice, detail=detail)


Text = Annotated[str, Form()]
Flag = Annotated[bool, Form()]


async def _then_follow_up(
    request: Request,
    action_id: uuid.UUID,
    notice: Notice,
    message: str,
    send_message: bool,
    resolve: bool,
) -> Response:
    """Après une décision : message et résolution si l'action est bien close."""
    notices = [notice]
    if not (send_message or resolve):
        return _redirect(action_id, notices)
    if notice in (Notice.EXECUTED, Notice.REJECTED):
        done, detail = await decisions.follow_up(
            _factory(request), action_id, message if send_message else None, resolve
        )
        return _redirect(action_id, notices + done, detail)
    if notice is not Notice.ALREADY_DECIDED:
        notices.append(Notice.MESSAGE_SKIPPED)
    return _redirect(action_id, notices)


@router.post("/{action_id}/approve")
async def approve_action(
    request: Request,
    action_id: uuid.UUID,
    name: Text = "",
    note: Text = "",
    message: Text = "",
    send_message: Flag = False,
    resolve: Flag = False,
) -> Response:
    form = {"decision": "approve", "name": name, "note": note, "message": message}
    try:
        notice = await decisions.approve(_factory(request), action_id, name, note)
        return await _then_follow_up(request, action_id, notice, message, send_message, resolve)
    except InvalidInput as exc:
        return await _detail(request, action_id, form_error=str(exc), form=form, status_code=422)


@router.post("/{action_id}/reject")
async def reject_action(
    request: Request,
    action_id: uuid.UUID,
    name: Text = "",
    note: Text = "",
    message: Text = "",
    send_message: Flag = False,
    resolve: Flag = False,
) -> Response:
    form = {"decision": "reject", "name": name, "note": note, "message": message}
    try:
        notice = await decisions.reject(_factory(request), action_id, name, note)
        return await _then_follow_up(request, action_id, notice, message, send_message, resolve)
    except InvalidInput as exc:
        return await _detail(request, action_id, form_error=str(exc), form=form, status_code=422)


@router.post("/{action_id}/retry")
async def retry_action(request: Request, action_id: uuid.UUID) -> Response:
    return _redirect(action_id, [await decisions.retry(_factory(request), action_id)])


@router.post("/{action_id}/follow-up")
async def follow_up_action(
    request: Request,
    action_id: uuid.UUID,
    message: Text = "",
    send_message: Flag = False,
    resolve: Flag = False,
) -> Response:
    try:
        notices, detail = await decisions.follow_up(
            _factory(request), action_id, message if send_message else None, resolve
        )
    except InvalidInput as exc:
        form = {"decision": "follow_up", "message": message}
        return await _detail(request, action_id, form_error=str(exc), form=form, status_code=422)
    return _redirect(action_id, notices, detail)


# --- Pilotage ------------------------------------------------------------------------------

SKIP_LABELS = {
    worker.Skip.HANDLED: "déjà traités",
    worker.Skip.HELD: "avec une action confiée à un humain",
    worker.Skip.RUNNING: "en cours",
    worker.Skip.PROPOSAL: "propositions, relancées à la levée de l'arrêt d'urgence",
    worker.Skip.COOLDOWN: f"échec récent, réessai après {worker.RETRY_DELAY.seconds // 60} min",
    worker.Skip.ABANDONED: "abandonnés après échecs répétés",
}
templates.env.globals["SKIP_LABELS"] = {str(k): v for k, v in SKIP_LABELS.items()}

CONTROL_NOTICES = {
    "paused": ("ok", "Boucle mise en pause : les exécutions en cours vont jusqu'au bout."),
    "resumed": ("ok", "Boucle relancée."),
    "budget": ("error", "Budget du jour atteint : la boucle reste en pause jusqu'à demain."),
    "emergency_on": ("error", "Arrêt d'urgence activé : toute action de l'agent est refusée."),
    "emergency_off": ("ok", "Arrêt d'urgence levé."),
}


def control_status(work: worker.Worker, factory: sessionmaker[Session]) -> ControlStatus:
    settings = control.load(factory)
    queue = work.queue
    with factory() as session:
        spent = control.spent_today(session)
        firing = [AlertRead.model_validate(a) for a in alerts.active(session)]
    return ControlStatus(
        loop_paused=settings.loop_paused,
        pause_reason=settings.pause_reason,
        paused_by=settings.paused_by,
        paused_at=settings.paused_at,
        emergency_stop=settings.emergency_stop,
        emergency_by=settings.emergency_by,
        emergency_at=settings.emergency_at,
        spent_today_usd=spent,
        daily_budget_usd=control.daily_budget(),
        running_tickets=work.running_tickets(),
        max_concurrent_runs=worker.MAX_CONCURRENT_RUNS,
        waiting_until=work.waiting_until,
        waiting_reason=work.waiting_reason,
        abandoned_tickets=worker.abandoned(factory),
        queue_checked_at=queue.at if queue else None,
        open_tickets=queue.open if queue else None,
        eligible_tickets=queue.eligible if queue else None,
        skipped_tickets={str(k): v for k, v in queue.skipped.items()} if queue else {},
        active_alerts=firing,
    )


def _control_page(
    request: Request,
    notice: str | None = None,
    form_error: str | None = None,
    form: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "control.html",
        {
            "status": control_status(request.app.state.worker, _factory(request)),
            "notices": [CONTROL_NOTICES[notice]] if notice in CONTROL_NOTICES else [],
            "form_error": form_error,
            "form": form or {},
        },
        status_code=status_code,
    )


def _control_done(request: Request, notice: str) -> Response:
    # Tour anticipé de la boucle : la relance ou la levée de l'arrêt agit tout de suite
    request.app.state.worker.wake()
    return RedirectResponse(f"/control?notice={notice}", status_code=303)


@control_router.get("", response_class=HTMLResponse)
def show_control(request: Request, notice: str | None = None) -> Response:
    return _control_page(request, notice)


@control_router.post("/loop/pause")
def pause_loop(request: Request, name: Text = "") -> Response:
    try:
        control.pause(_factory(request), name, PauseReason.MANUAL)
    except InvalidInput as exc:
        return _control_page(request, form_error=str(exc), form={"name": name}, status_code=422)
    return _control_done(request, "paused")


@control_router.post("/loop/resume")
def resume_loop(request: Request, name: Text = "") -> Response:
    try:
        resumed = control.resume(_factory(request), name)
    except InvalidInput as exc:
        return _control_page(request, form_error=str(exc), form={"name": name}, status_code=422)
    return _control_done(request, "resumed" if resumed else "budget")


@control_router.post("/emergency")
def switch_emergency(request: Request, on: Flag, name: Text = "") -> Response:
    try:
        control.set_emergency(_factory(request), on, name)
    except InvalidInput as exc:
        return _control_page(request, form_error=str(exc), form={"name": name}, status_code=422)
    return _control_done(request, "emergency_on" if on else "emergency_off")
